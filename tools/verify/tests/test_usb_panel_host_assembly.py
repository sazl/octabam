"""Real host consumers of assembled EP0 code, with synthetic complete LCD writes.

Unicorn executes the actual capture/publisher/control units. Only USB I/O and
publisher scheduling are supplied by this firmware-free test instrument.
"""
import contextlib
import io
import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

from tools.verify.tests import test_usb_panel_capture as capture
from tools.panel import hardware_backend, usb_mirror as mirror, usb_mirror_protocol as protocol
from tools.hw import usb_panel


class AssembledTransport:
    def __init__(self, control):
        self.control = control
        self.identity = mirror.DeviceIdentity('assembled-only', 1, (2,), 3)
        self.calls = []
        self.info_generations = []
        self.closed = False
        self.lock = threading.Lock()
        self.ticks = 0

    def exchange(self, request, *, timeout_ms=250):
        with self.lock:
            protocol.validate_request(request)
            self.calls.append(request.request)
            if request.request == protocol.REQUEST_INFO and len(self.info_generations) == 3:
                self.control.feed(b'\x10\x00' + b'\xff' * 8)
            raw = self.control.request(request.request, value=request.value, index=request.index,
                                       length=request.length, bm=request.bm_request_type)
            if request.request == protocol.REQUEST_INFO:
                self.info_generations.append(int.from_bytes(raw[12:16], 'big'))
            if request.request == protocol.REQUEST_BEGIN and raw[6] == protocol.Status.PENDING:
                # Model one permitted low-priority publisher visit per lease.
                self.ticks += 13200000
                self.control.uc.mem_write(0xfc07c00c, struct.pack('>I', self.ticks))
                self.control.call('pm_publish')
            return raw[:protocol.HEADER_SIZE + int.from_bytes(raw[22:24], 'big')]

    def close(self):
        self.closed = True


@unittest.skipUnless(capture.INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class AssembledHostTests(unittest.TestCase):
    def setUp(self):
        control = capture.Control('test_info_and_malformed_lengths')
        control.setUp()
        self.addCleanup(control.doCleanups)
        control.feed(b''.join(bytes([0x10 | page, col]) + bytes(8)
                              for page in range(8) for col in range(0, 128, 8)))
        self.transport = AssembledTransport(control)

    def test_worker_static_heartbeat_then_changed_info_acquires_validated_frame(self):
        backend = hardware_backend.HardwareBackend(
            transport_factory=lambda *_args, **_kwargs: self.transport, poll_hz=50)
        self.addCleanup(backend.close)
        deadline = time.monotonic() + 5
        first = None
        while time.monotonic() < deadline:
            view = backend.snapshot()
            if view is not None and first is None:
                first = view
            with self.transport.lock:
                heartbeat_count = len(self.transport.info_generations)
            if heartbeat_count >= 6:
                break
            time.sleep(.005)
        self.assertGreaterEqual(heartbeat_count, 6, backend.status())
        self.assertIsNotNone(first)
        second = backend.snapshot()
        self.assertEqual(backend.status()['generation'], 129)
        self.assertEqual(backend.status()['connection_state'], 'live')
        self.assertEqual(second.generation, first.generation + 1)
        self.assertNotEqual(second.png, first.png)
        self.assertEqual(self.transport.info_generations[:6], [128, 128, 128, 129, 129, 129])
        self.assertEqual(self.transport.calls.count(protocol.REQUEST_BEGIN), 4)  # PENDING + READY per body
        self.assertEqual(self.transport.calls.count(protocol.REQUEST_READ), 80)  # two validated 1280-byte bodies
        backend.close()
        self.assertTrue(self.transport.closed)

    def test_watch_static_heartbeat_then_changed_info_reports_new_snapshot(self):
        output = io.StringIO()
        with patch.object(mirror, 'open_transport', return_value=self.transport), contextlib.redirect_stdout(output):
            result = usb_panel.main(['watch', '--count', '4'])
        self.assertEqual(result, 0, output.getvalue())
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r['generation'] for r in reports], [128, 128, 128, 129])
        self.assertEqual([r['changed'] for r in reports], [True, False, False, True])
        self.assertEqual([r['snapshot']['generation'] for r in reports if 'snapshot' in r], [128, 129])
        self.assertEqual(self.transport.calls.count(protocol.REQUEST_BEGIN), 4)
        self.assertEqual(self.transport.calls.count(protocol.REQUEST_READ), 80)
        self.assertTrue(self.transport.closed)
