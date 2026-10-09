"""HTTP source policy, exercised against a recording read-only backend."""
import http.client
import json
import pathlib
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'tools/panel'))
import panel_server as ps
from panel_backend import ViewSnapshot, LedSnapshot

DENIED = '/samples /samples/add /samples/upload /samples/remove /samples/commit /card /card/eject /card/insert /audio/status /audio/pcm /audio.wav /audio/devices /audio/output /audio/enable /peek /rtstatus /port /keys /key /knob /knob/reset /knob/press /xfader /midi /tap /transport /poke_trig /project /stack /run'.split()
ALLOWED = '/ /skin.js /screen.png /screen.txt /status /map /leds /leds/stream'.split()

class RecordingBackend:
    source = 'hardware'
    capabilities = frozenset(('screen', 'leds'))
    def __init__(self):
        self.closed = threading.Event()
        self.reads = 0
        self.view = ViewSnapshot(b'cached png', 'cached text', LedSnapshot(bytes(17), ((5, 15),)), 42, None, self.capabilities, 100, 90)
    def snapshot(self):
        self.reads += 1
        return self.view
    def supports(self, capability):
        return capability in self.capabilities
    def status(self):
        return dict(source=self.source, backend=self.source, read_only=True, capabilities=sorted(self.capabilities), connection_state='closed' if self.closed.is_set() else 'disconnected', has_frame=self.view is not None, model=None)
    def close(self):
        self.closed.set()

class HardwareRoutes(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.handler = type('TestHandler', (ps.Handler,), {'backend': self.backend, 'panel': None})
        self.server = ps.ThreadingHTTPServer(('127.0.0.1', 0), self.handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
    def tearDown(self):
        self.backend.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
    def request(self, path, method='GET', headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body
    def test_every_known_emulator_route_denied_before_malformed_parameters(self):
        for method in ('GET', 'POST'):
            for path in DENIED:
                with self.subTest(method=method, path=path):
                    code, body = self.request(path + '?row=bad&len=bad', method)
                    self.assertEqual(code, 409)
                    self.assertEqual(json.loads(body)['code'], 'unsupported_backend_operation')
        self.assertEqual(self.backend.reads, 0)
    def test_post_denied_without_waiting_for_advertised_upload(self):
        code, body = self.request('/samples/upload', 'POST', {'Content-Length': '10000000'})
        self.assertEqual(code, 409)
    def test_unknown_suffixes_remain_unknown(self):
        for path in ('/audio/nope', '/samples/nope', '/press', '/nope'):
            for method in ('GET', 'POST'):
                self.assertEqual(self.request(path, method)[0], 404)
    def test_shared_routes_serve_cache_after_disconnection(self):
        with patch.object(ps, 'skin_js', return_value=b'skin'):
            for path in ALLOWED[:-1]:
                self.assertEqual(self.request(path)[0], 200, path)
        self.assertEqual(self.request('/screen.png')[1], b'cached png')
        self.assertEqual(self.request('/screen.txt')[1], b'cached text')
        self.assertIsNone(json.loads(self.request('/map')[1])['model'])
        status = json.loads(self.request('/status')[1])
        self.assertTrue(status['read_only'])
        self.assertIn('pid', status)
        self.assertNotIn('image', status)
    def test_first_frame_waiting_and_optional_led_absence(self):
        self.backend.view = None
        for path in ('/screen.png', '/screen.txt'):
            code, body = self.request(path)
            self.assertEqual(code, 503)
            self.assertEqual(json.loads(body)['code'], 'frame_unavailable')
        self.backend.capabilities = frozenset(('screen',))
        for path in ('/leds', '/leds/stream'):
            code, body = self.request(path)
            self.assertEqual(code, 409)
            self.assertEqual(json.loads(body)['code'], 'capability_unavailable')
    def test_two_sse_viewers_share_cached_immutable_view_and_end_on_close(self):
        connections = []
        try:
            for _ in range(2):
                conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                conn.request('GET', '/leds/stream')
                r = conn.getresponse()
                self.assertEqual(r.status, 200)
                self.assertTrue(r.readline().startswith(b'data:'))
                connections.append((conn, r))
            self.backend.close()
            for conn, r in connections:
                r.read()
        finally:
            for conn, _ in connections:
                conn.close()
    def test_unsupported_methods_cannot_dispatch_controls(self):
        for method in ('PUT', 'DELETE', 'PATCH', 'HEAD'):
            self.assertIn(self.request('/key?row=bad', method)[0], (409, 501))
        self.assertEqual(self.backend.reads, 0)

class PortRoutes(unittest.TestCase):
    """The port route table still reaches its original controls and helpers."""
    request = HardwareRoutes.request
    tearDown = HardwareRoutes.tearDown
    def setUp(self):
        from test_panel_backend_modes import PortAdapter
        self.panel = PortAdapter().panel()
        self.panel.proc = None; self.panel.handlers = [0x1234]
        self.panel.pool = None; self.panel.card_file = None; self.panel.card_meta = {}; self.panel.card_flush_note = None; self.panel.card_files = {}; self.panel.card_removals = set(); self.panel.loaded = None
        self.calls = []
        def operation(name):
            def call(*a, **kw):
                self.calls.append((name,a,kw)); return True, {}
            return call
        for name in ('key','knob','knob_reset','knob_press','xfader','tap','transport','poke_trig','eject_card','insert_card'):
            setattr(self.panel, name, operation(name))
        self.panel.do = operation('do')
        self.backend = RecordingBackend(); self.backend.source = 'port'
        def helper(handler, path, args, *body):
            self.calls.append((path,args,body)); handler._json({'legacy_helper':path})
        self.handler = type('PortHandler', (ps.Handler,), {'backend':self.backend,'panel':self.panel,'_samples':helper,'_audio':helper})
        self.server = ps.ThreadingHTTPServer(('127.0.0.1',0),self.handler)
        self.worker = threading.Thread(target=self.server.serve_forever,daemon=True); self.worker.start()
    def test_every_emulator_route_keeps_port_dispatch(self):
        for path in DENIED:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0],200)
        self.assertEqual(self.request('/key?row=0x22&bit=3&down=0')[0],200)
        self.assertIn(('key',(0x22,3,False),{}),self.calls)
        for path in ('/samples/add','/samples/commit','/audio/output','/audio/enable'):
            self.assertTrue(any(c[0] == path for c in self.calls),path)
    def test_port_post_upload_reads_body_and_uses_legacy_helper(self):
        conn = http.client.HTTPConnection(*self.server.server_address,timeout=2)
        conn.request('POST','/samples/upload?name=test.wav',body=b'file')
        response = conn.getresponse(); self.assertEqual(response.status,200); response.read(); conn.close()
        self.assertIn(('/samples/upload',{'name':'test.wav'},(b'file',)),self.calls)
