"""Real hardware worker/client and HTTP routes with an encoded recording EP0 peer."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import pathlib
import struct
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/panel'))
import panel_server as server
from tools.panel import hardware_backend as hardware
from tools.panel import usb_mirror_protocol as protocol
from tools.verify.tests.test_panel_hardware import Factory, RecordingTransport
from tools.verify.tests.test_usb_panel_protocol import body, FLAGS


class HardwareHttpIntegration(unittest.TestCase):
    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.005)
        self.fail('real worker/HTTP condition did not become true within bound')

    def request(self, path, method='GET'):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=2)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_encoded_ep0_to_http_cache_disconnect_recovery_and_close(self):
        flags = (FLAGS | protocol.LED_ROWS_SUPPORTED | protocol.ROW_KNOWN |
                 protocol.LED_LEVELS_SUPPORTED | protocol.LEVEL_KNOWN)
        data = body() + bytes((0x20, 1, 0x3f, 5))
        first = RecordingTransport(flags=flags, data=data)
        changed = bytearray(data)
        changed[2] ^= 0xff
        second = RecordingTransport(flags=flags, data=bytes(changed), epoch=8)
        second.change(mode='pending')
        factory = Factory(first, second)
        backend = hardware.HardwareBackend('serial:unit-A', poll_hz=50,
                                           transport_factory=factory)
        handler = type('HardwareHttpHandler', (server.Handler,),
                       {'backend': backend, 'panel': None})
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        http_worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        http_worker.start()
        streams = []
        try:
            view = self.wait_for(lambda: backend.snapshot()
                                 if backend.status()['connection_state'] == 'live' else None)
            code, png = self.request('/screen.png')
            self.assertEqual(code, 200)
            self.assertEqual(png, view.png)
            self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', png[16:24]), (128, 64))
            self.assertEqual(self.request('/screen.txt'), (200, view.text.encode()))
            leds = json.loads(self.request('/leds')[1])
            self.assertEqual(bytes.fromhex(leds['bits'])[0], 1)
            self.assertEqual(leds['ids'], {'0x05': 15})
            status = json.loads(self.request('/status')[1])
            self.assertEqual(status['source'], 'hardware')
            self.assertTrue(status['read_only'])
            self.assertEqual(status['protocol']['epoch'], 7)
            self.assertEqual(status['model'], 'MKII')
            self.assertEqual(status['known_state']['led_rows'], [0])
            self.assertEqual(json.loads(self.request('/map')[1])['model'], 'MKII')

            # Independent HTTP consumers read the same real worker's published cache.
            with ThreadPoolExecutor(max_workers=2) as consumers:
                results = list(consumers.map(self.request, ['/screen.png', '/screen.png']))
            self.assertEqual(results, [(200, png), (200, png)])
            for _ in range(2):
                connection = http.client.HTTPConnection(*self.http.server_address, timeout=2)
                connection.request('GET', '/leds/stream')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                event = response.readline()
                self.assertTrue(event.startswith(b'data:'))
                self.assertEqual(json.loads(event[5:]), leds)
                streams.append((connection, response))
            self.assertEqual(len(factory.calls), 1)
            self.assertEqual(len(first.calls_for(protocol.REQUEST_BEGIN)), 1)

            # Controls reach the actual policy but cannot send a mirror/control write.
            for method in ('GET', 'POST'):
                for path in ('/key?row=bad', '/transport?op=play', '/audio/output',
                             '/card/eject', '/samples/upload'):
                    code, payload = self.request(path, method)
                    self.assertEqual(code, 409, (method, path))
                    self.assertEqual(json.loads(payload)['code'], 'unsupported_backend_operation')
            self.assertEqual(len(first.calls_for(protocol.REQUEST_BEGIN)), 1)

            first.change(mode='disconnected')
            self.wait_for(lambda: second.calls_for(protocol.REQUEST_BEGIN))
            self.assertIs(backend.snapshot(), view)
            self.assertEqual(self.request('/screen.png'), (200, png))
            pending = json.loads(self.request('/status')[1])
            self.assertTrue(pending['has_frame'])
            self.assertNotEqual(pending['connection_state'], 'live')
            second.change(mode=None)
            recovered = self.wait_for(lambda: backend.snapshot()
                                      if backend.status()['connection_state'] == 'live'
                                      and backend.snapshot() is not view else None)
            status = json.loads(self.request('/status')[1])
            self.assertEqual(status['protocol']['epoch'], 8)
            self.assertEqual(status['protocol']['connection_id'], 2)
            self.assertEqual(status['generation'], first.generation)
            self.assertEqual(second.generation, first.generation)
            self.assertGreater(status['seq'], view.generation)
            self.assertGreater(recovered.generation, view.generation)
            self.assertNotEqual(recovered.png, png)
            self.assertEqual(self.request('/screen.png'), (200, recovered.png))

            started = time.monotonic()
            backend.close()
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(json.loads(self.request('/status')[1])['connection_state'], 'closed')
            for _connection, response in streams:
                response.read()  # SSE handlers finish on actual backend close.
            owners = {call[2] for peer in (first, second) for call in peer.calls}
            self.assertEqual(owners, {factory.calls[0][2]})
            self.assertNotIn(threading.get_ident(), owners)
            for peer in (first, second):
                self.assertEqual(peer.closed_by, list(owners))
                for request, _when, _owner, timeout in peer.calls:
                    protocol.validate_request(request, max_response=peer.ceiling)
                    self.assertEqual(request.bm_request_type, 0xc0)
                    self.assertLessEqual(timeout, 250)
        finally:
            backend.close()
            for connection, _response in streams:
                connection.close()
            self.http.shutdown()
            self.http.server_close()
            http_worker.join(2)
            self.assertFalse(http_worker.is_alive())
