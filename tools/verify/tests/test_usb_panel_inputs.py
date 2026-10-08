"""Independent schema-2 input vectors and cached HTTP delivery, without USB."""
import dataclasses
import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

from tools.panel import usb_mirror_protocol as p
from tools.panel import usb_mirror as mirror
from tools.verify.tests.test_usb_panel_protocol import body, wire, FLAGS
from tools.verify.tests.test_panel_hardware import Factory, RecordingTransport
from tools.verify.tests.test_panel_hardware_e2e import HardwareHttpIntegration


def record(known=7, fader=73):
    return (bytes((0xb8, 1, known, fader)) + bytes((1, 2, 4, 8, 16, 32, 64, 128))
            + struct.pack('>64H', *range(64))
            + struct.pack('>14H', 1, 65535, 128, 4, 0, 0, 7, 8, 9, 10, 11, 12, 13, 14))


def input_info(flags=FLAGS | 0x40, schema=2, minor=1, maximum=2026):
    payload = struct.pack('>BBHHBBHHHHHHB11s', schema, 1, 128, 64, 8, 8,
                          64, maximum, 200, 128, 1000, 10, 3, b'new' + bytes(8))
    raw = bytearray(wire(flags=flags, payload=payload)); raw[5] = minor
    return p.parse_info(p.parse_response(raw, request=p.info_request()))


def validate(data, info=None, crc=None, flags=FLAGS):
    info = input_info() if info is None else info
    identity = p.SnapshotIdentity(1, 7, 5, 9, len(data),
                                 p.crc32(data) if crc is None else crc,
                                 (flags & ~0x7f) | info.capabilities)
    return p.validate_snapshot(data, identity=identity, info=info)


class InputProtocolTests(unittest.TestCase):
    def test_schema_two_decodes_unsigned_big_endian_counters_and_full_prefix(self):
        prefix = body() + b'\x20\x03\x3f\xff\xb7\x80'
        flags = FLAGS | 0xe000e | 0x40
        info = input_info(flags=flags)
        summary = validate(prefix + record(), info, flags=flags)
        self.assertEqual((summary.led_rows, summary.led_ids, summary.backlight_known), ((0,), (255,), True))
        self.assertEqual(summary.output_length, len(prefix))
        self.assertEqual(summary.inputs.known, 7)
        self.assertEqual(summary.inputs.fader, 73)
        self.assertEqual(summary.inputs.keys, (1, 2, 4, 8, 16, 32, 64, 128))
        self.assertEqual(summary.inputs.press_counts, tuple(range(64)))
        self.assertEqual(summary.inputs.encoder_counts[:2], ((1, 65535), (128, 4)))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            summary.inputs.fader = 0

    def test_schema_one_remains_output_only_with_minor_zero(self):
        old = input_info(flags=FLAGS, schema=1, minor=0, maximum=1858)
        summary = validate(body(), old)
        self.assertIsNone(summary.inputs)
        self.assertEqual(summary.output_length, 1280)
        with self.assertRaises(p.ProtocolError):
            validate(body() + record(), old)

    def test_known_flags_distinguish_unknown_fader_from_zero(self):
        unknown = bytes((0xb8, 1, 0, 0)) + bytes(164)
        inputs = validate(body() + unknown).inputs
        self.assertEqual(inputs.known, 0)
        self.assertIsNone(inputs.fader)
        known_zero = bytes((0xb8, 1, 2, 0)) + bytes(164)
        self.assertEqual(validate(body() + known_zero).inputs.fader, 0)

    def test_rejects_malformed_suffix_and_nonzero_unknown_families(self):
        invalid = []
        for offset, value in ((0, 0xb9), (1, 2), (2, 8), (3, 128)):
            changed = bytearray(record()); changed[offset] = value; invalid.append(changed)
        for offset in (3, 4, 12, 140):
            changed = bytearray(bytes((0xb8, 1, 0, 0)) + bytes(164))
            changed[offset] = 1; invalid.append(changed)
        invalid.extend((record()[:-2], record() + bytes(2), record() + record(), b''))
        for suffix in invalid:
            with self.subTest(suffix=bytes(suffix)[:16], length=len(suffix)), self.assertRaises(p.ProtocolError):
                validate(body() + suffix)
        with self.assertRaisesRegex(p.ProtocolError, 'CRC'):
            validate(body() + record(), crc=p.crc32(body() + record()) ^ 1)

    def test_schema_capability_and_minor_must_agree(self):
        for kwargs in ({'flags': FLAGS}, {'schema': 1}, {'minor': 0},
                       {'schema': 3}, {'minor': 2}, {'maximum': 2028},
                       {'schema': 1, 'flags': FLAGS, 'maximum': 1860}):
            with self.subTest(kwargs=kwargs), self.assertRaises(p.ProtocolError):
                input_info(**kwargs)

    def test_maximum_body_accepts_2026_and_rejects_overflow_before_allocation(self):
        prefix = body() + b''.join(bytes((0x20+i if i<16 else 0xa0+i-16, i)) for i in range(32))
        prefix += b''.join(bytes((0x30+i%16, i)) for i in range(256)) + b'\xb7\x00'
        info = input_info(flags=FLAGS | 0xe000e | 0x40)
        self.assertEqual(len(prefix + record()), 2026)
        self.assertEqual(validate(prefix + record(), info, flags=FLAGS | 0xe000e).output_length, 1858)
        identity = p.SnapshotIdentity(1, 7, 5, 9, 2028, 0, FLAGS | 0x40)
        with self.assertRaises(p.ProtocolError):
            p.SnapshotAssembler(identity, info)


class InputTransport(RecordingTransport):
    def exchange(self, request, *, timeout_ms=250):
        raw = bytearray(super().exchange(request, timeout_ms=timeout_ms))
        raw[5] = 1
        if request.request == p.REQUEST_INFO:
            raw[32] = 2
            raw[42:44] = (2026).to_bytes(2, 'big')
        return bytes(raw)


class InputHttpTests(unittest.TestCase):
    wait_for = HardwareHttpIntegration.wait_for
    request = HardwareHttpIntegration.request
    def test_negotiated_poll_interval_includes_snapshot_transfer_time(self):
        from tools.panel import hardware_backend as hardware

        # Run the actual worker/client synchronously: USB exchange work and
        # event waits advance a virtual monotonic clock. Wall-clock scheduling
        # of dozens of tiny sleeps cannot change the intended 98 ms work cost.
        clock = [10.0]

        class WorkCostTransport(InputTransport):
            def exchange(self, request, *, timeout_ms=250):
                if request.request == p.REQUEST_INFO:
                    self.change(generation=self.generation + 1)
                clock[0] += .002
                return super().exchange(request, timeout_ms=timeout_ms)

        class ThreePolls:
            stopped = False
            cycles = 0

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                clock[0] += timeout
                self.cycles += 1
                self.stopped = self.cycles == 3
                return self.stopped

        peer = WorkCostTransport(flags=FLAGS | 0x40, data=body() + record(), poll=200)
        with patch.object(hardware.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(hardware.threading.Thread, 'start'):
            backend = hardware.HardwareBackend(poll_hz=50, transport_factory=lambda *a, **k: peer)
            backend._stop = ThreePolls()
            backend._run()

        calls = peer.calls_for(p.REQUEST_INFO)
        self.assertEqual(len(calls), 3)
        intervals = [b[1] - a[1] for a, b in zip(calls, calls[1:])]
        # 200 ms starts include INFO + BEGIN + 46 READs + RELEASE (49 * 2 ms),
        # rather than adding that work after every complete 200 ms wait.
        for interval in intervals:
            self.assertAlmostEqual(interval, .2, places=9)
        self.assertEqual(len(peer.calls_for(p.REQUEST_READ)), 138)
        self.assertEqual(len(peer.calls_for(p.REQUEST_RELEASE)), 3)
        self.assertEqual(backend.snapshot().generation, 3)
        self.assertEqual(backend.snapshot().inputs.fader, 73)

    def test_slow_transport_discovery_does_not_shorten_first_info_interval(self):
        from tools.panel.hardware_backend import HardwareBackend
        peer = InputTransport(flags=FLAGS | 0x40, data=body() + record(), poll=200)
        def discover(*args, **kwargs):
            time.sleep(.3)
            return peer
        backend = HardwareBackend(poll_hz=50, transport_factory=discover)
        self.addCleanup(backend.close)
        calls = self.wait_for(lambda: peer.calls_for(p.REQUEST_INFO)
                              if len(peer.calls_for(p.REQUEST_INFO)) >= 3 else None)
        intervals = [b[1] - a[1] for a, b in zip(calls, calls[1:])]
        self.assertTrue(all(interval >= .195 for interval in intervals), intervals)

    def test_slow_poll_does_not_create_catch_up_bursts(self):
        from tools.panel.hardware_backend import HardwareBackend

        class SlowFirstInfo(InputTransport):
            def __init__(self):
                super().__init__(flags=FLAGS | 0x40, data=body() + record(), poll=200)
                self.info_starts = []

            def exchange(self, request, *, timeout_ms=250):
                if request.request == p.REQUEST_INFO:
                    self.info_starts.append(time.monotonic())
                    if len(self.info_starts) == 1:
                        # Model a single scheduling delay longer than one
                        # polling period; later requests complete promptly.
                        time.sleep(.36)
                return super().exchange(request, timeout_ms=timeout_ms)

        peer = SlowFirstInfo()
        backend = HardwareBackend(poll_hz=50, transport_factory=lambda *a, **k: peer)
        self.addCleanup(backend.close)
        self.wait_for(lambda: len(peer.info_starts) >= 4)
        intervals = [b-a for a, b in zip(peer.info_starts, peer.info_starts[1:])]
        self.assertGreaterEqual(intervals[0], .35)
        self.assertTrue(all(interval >= .195 for interval in intervals[1:]), intervals)

    def test_inputs_reach_cached_http_without_control_capability_or_extra_acquisition(self):
        import panel_server as server
        from tools.panel.hardware_backend import HardwareBackend
        peer = InputTransport(flags=FLAGS | 0x40, data=body() + record())
        backend = HardwareBackend(poll_hz=1, transport_factory=lambda *a, **k: peer)
        self.addCleanup(backend.close)
        handler = type('InputHandler', (server.Handler,), {'backend': backend, 'panel': None})
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        worker = threading.Thread(target=self.http.serve_forever, daemon=True); worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        view = self.wait_for(lambda: backend.snapshot())
        status = json.loads(self.request('/status')[1])
        self.assertEqual(status['inputs'], {'known': 7, 'fader': 73,
                         'keys': [1, 2, 4, 8, 16, 32, 64, 128],
                         'press_counts': list(range(64)),
                         'encoder_counts': [[1, 65535], [128, 4], [0, 0], [7, 8], [9, 10], [11, 12], [13, 14]]})
        self.assertEqual(status['inputs_epoch'], 7)
        self.assertEqual(status['inputs_connection_id'], 1)
        self.assertIn('input_observation', status['capabilities'])
        self.assertIn('input_observation', status['negotiated_capabilities'])
        self.assertFalse(backend.supports('controls'))
        self.assertEqual(view.inputs.fader, 73)
        self.assertEqual(self.request('/key?row=0&bit=0')[0], 409)
        status['inputs']['keys'][0] = 255
        for _ in range(5):
            self.assertEqual(json.loads(self.request('/status')[1])['inputs']['keys'][0], 1)
        self.assertEqual(len(peer.calls_for(p.REQUEST_BEGIN)), 1)

        # A pending publication in the same session keeps the last verified
        # observation live, so tabs retain counter baselines during refresh.
        peer.change(generation=10, mode='pending')
        self.wait_for(lambda: len(peer.calls_for(p.REQUEST_BEGIN)) >= 2)
        self.assertEqual(backend.status()['connection_state'], 'live')
        self.assertEqual(backend.status()['inputs_epoch'], 7)
        peer.change(mode=None)
        self.wait_for(lambda: backend.snapshot().generation > view.generation)
        begin_count = len(peer.calls_for(p.REQUEST_BEGIN))
        peer.change(epoch=8, mode='pending')
        self.wait_for(lambda: len(peer.calls_for(p.REQUEST_BEGIN)) > begin_count)
        transitioning = json.loads(self.request('/status')[1])
        self.assertEqual(transitioning['connection_state'], 'syncing')
        self.assertEqual(transitioning['protocol']['epoch'], 8)
        self.assertEqual(transitioning['inputs_epoch'], 7)
        peer.change(mode=None)
        self.wait_for(lambda: backend.status().get('inputs_epoch') == 8)

    def test_input_snapshot_detaches_nested_arrays_and_unknown_capability_is_offered(self):
        from panel_backend import InputSnapshot, input_payload
        keys = list(range(8)); presses = list(range(64)); encoders = [[1, 2] for _ in range(7)]
        state = InputSnapshot(7, 0, keys, presses, encoders)
        keys.clear(); presses.clear(); encoders[0][0] = 99
        self.assertEqual(state.keys, tuple(range(8)))
        self.assertEqual(state.press_counts, tuple(range(64)))
        self.assertEqual(state.encoder_counts, ((1, 2),) * 7)
        payload = input_payload(state); payload['encoder_counts'][0][0] = 99
        self.assertEqual(input_payload(state)['encoder_counts'][0], [1, 2])
        from tools.panel.hardware_backend import HardwareBackend
        self.assertIn('input_observation', HardwareBackend._offered(input_info()))

    def test_stale_inputs_stay_stale_during_recovery_until_verified_publication(self):
        from tools.panel.hardware_backend import HardwareBackend

        class RecoveryTransport(InputTransport):
            def __init__(self):
                super().__init__(flags=FLAGS | 0x40, data=body() + record())
                self.recovering = False
                self.attempts = 0
                self.retry_entered = threading.Event()
                self.release_retry = threading.Event()
                self.final_entered = threading.Event()
                self.release_final = threading.Event()

            def exchange(self, request, *, timeout_ms=250):
                if self.recovering and request.request == p.REQUEST_READ:
                    self.attempts += 1
                    if self.attempts == 1:
                        raise mirror.TransportError('timeout', 'first acquisition failed')
                    if self.attempts == 2:
                        self.retry_entered.set()
                        self.release_retry.wait(timeout_ms / 1000)
                        raise mirror.TransportError('timeout', 'recovery acquisition failed')
                    if self.attempts == 3:
                        self.final_entered.set()
                        self.release_final.wait(timeout_ms / 1000)
                return super().exchange(request, timeout_ms=timeout_ms)

        peer = RecoveryTransport()
        backend = HardwareBackend(poll_hz=50, transport_factory=lambda *a, **k: peer)
        self.addCleanup(backend.close)
        self.addCleanup(peer.release_retry.set)
        self.addCleanup(peer.release_final.set)
        first = self.wait_for(lambda: backend.snapshot())
        self.assertEqual(first.inputs.keys[0], 1)
        updated = bytearray(record()); updated[4:12] = bytes(8)
        peer.change(generation=10, data=body() + updated, recovering=True)
        self.assertTrue(peer.retry_entered.wait(2), backend.status())
        status = backend.status()
        self.assertEqual(status['connection_state'], 'stale')
        self.assertEqual(status['error']['code'], 'timeout')
        self.assertEqual(status['seq'], first.generation)
        self.assertEqual(status['inputs']['keys'][0], 1)
        self.assertIs(backend.snapshot(), first)
        peer.release_retry.set()
        self.assertTrue(peer.final_entered.wait(2), backend.status())
        status = backend.status()
        self.assertEqual(status['connection_state'], 'stale')
        self.assertEqual(status['error']['code'], 'timeout')
        peer.release_final.set()
        fresh = self.wait_for(lambda: backend.snapshot()
                              if backend.snapshot().generation > first.generation else None)
        self.assertEqual(fresh.inputs.keys[0], 0)
        self.assertEqual(backend.status()['connection_state'], 'live')
        self.assertIsNone(backend.status()['error'])

    def test_cached_input_connection_identity_changes_only_after_reconnect_publication(self):
        from tools.panel.hardware_backend import HardwareBackend
        first = InputTransport(flags=FLAGS | 0x40, data=body() + record())
        second = InputTransport(flags=FLAGS | 0x40, data=body() + record(), epoch=7)
        second.change(mode='pending')
        backend = HardwareBackend(poll_hz=50, transport_factory=Factory(first, second))
        self.addCleanup(backend.close)
        view = self.wait_for(lambda: backend.snapshot())
        self.assertEqual(backend.status()['inputs_connection_id'], 1)
        first.change(mode='disconnected')
        self.wait_for(lambda: second.calls_for(p.REQUEST_BEGIN))
        status = backend.status()
        self.assertEqual(status['connection_state'], 'syncing')
        self.assertEqual(status['protocol']['connection_id'], 2)
        self.assertEqual(status['protocol']['epoch'], 7)
        self.assertEqual(status['inputs_epoch'], 7)
        self.assertEqual(status['inputs_connection_id'], 1)
        self.assertIs(backend.snapshot(), view)
        second.change(mode=None)
        self.wait_for(lambda: backend.snapshot().generation > view.generation)
        self.assertEqual(backend.status()['inputs_epoch'], 7)
        self.assertEqual(backend.status()['inputs_connection_id'], 2)


if __name__ == '__main__':
    unittest.main()
