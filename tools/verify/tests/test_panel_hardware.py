"""Hardware worker behavior through the real synchronous mirror client."""
from collections import deque
import importlib
import pathlib
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/panel"))
from tools.panel import usb_mirror as m
from tools.panel import usb_mirror_protocol as p
from tools.verify.tests.test_usb_panel_protocol import body, wire, FLAGS


class RecordingTransport:
    """A recording EP0 peer, emitting actual wire bytes rather than views."""
    def __init__(self, *, serial="unit-A", ceiling=64, model=2, flags=FLAGS,
                 data=None, generation=9, epoch=7, token=5, poll=20):
        self.identity = m.DeviceIdentity(serial, 1, (2, 3), 4)
        self.lock = threading.Lock()
        self.ceiling, self.model, self.flags = ceiling, model, flags
        self.data = body() if data is None else data
        self.generation, self.epoch, self.token, self.poll = generation, epoch, token, poll
        self.mode = None
        self.calls, self.closed_by = [], []
        self.read_entered, self.unblock = threading.Event(), threading.Event()
        self.lease = None

    def change(self, **fields):
        with self.lock:
            for name, value in fields.items():
                setattr(self, name, value)

    def calls_for(self, operation):
        with self.lock:
            return [call for call in self.calls if call[0].request == operation]

    def exchange(self, request, *, timeout_ms=250):
        with self.lock:
            self.calls.append((request, time.monotonic(), threading.get_ident(), timeout_ms))
            mode = self.mode
            generation, epoch, flags = self.generation, self.epoch, self.flags
            if mode == "disconnected":
                raise m.TransportError("disconnected", "fake unplug")
            if mode == "exception":
                raise RuntimeError("worker transport exploded")
            if request.request == p.REQUEST_INFO:
                valid = 128 if flags & p.LCD_COMPLETE else 0
                payload = struct.pack(p.INFO_STRUCT, 1, self.model, 128, 64, 8, 8,
                                      self.ceiling, 1858, self.poll, valid, 1000, 100,
                                      3, b"abc" + bytes(8))
                raw = wire(epoch=epoch, generation=generation, flags=flags, payload=payload)
                if mode == "blocked_info":
                    self.read_entered.set()
                    self.unblock.wait(timeout_ms / 1000)
                return raw[:4] + b"\x02" + raw[5:] if mode == "wrong_version" else raw
            if request.request == p.REQUEST_BEGIN:
                if mode == "busy":
                    return wire(kind=2, status=2, epoch=epoch, generation=generation, flags=flags)
                if mode == "pending":
                    return wire(kind=2, status=1, epoch=epoch, generation=0, token=self.token, flags=flags)
                self.lease = (self.data, generation, epoch, flags, self.token)
                total = 2000 if mode == "bound" else len(self.data)
                return wire(kind=2, epoch=epoch, generation=generation, token=self.token,
                            total=total, flags=flags, crc=p.crc32(self.data))
            if request.request == p.REQUEST_RELEASE:
                return wire(kind=4, epoch=epoch, generation=generation, token=request.value, flags=flags)
            assert request.request == p.REQUEST_READ, "forbidden USB operation"
            data, generation, epoch, flags, token = self.lease
            payload = data[request.index:request.index + request.length - p.HEADER_SIZE]
            offset = request.index + (2 if mode == "offset" else 0)
            response_generation = generation + (1 if mode == "mixed_generation" else 0)
            response_token = token + (1 if mode == "mixed_token" else 0)
            response_epoch = epoch + (1 if mode == "mixed_epoch" else 0)
            raw = wire(kind=3, epoch=response_epoch, generation=response_generation,
                       token=response_token, total=len(data), offset=offset, payload=payload,
                       crc=p.crc32(data), flags=flags)
        if mode in ("blocked", "violates_timeout"):
            self.read_entered.set()
            self.unblock.wait(timeout_ms / 1000 if mode == "blocked" else 2)
            if mode == "blocked" and not self.unblock.is_set():
                raise m.TransportError("timeout", "bounded fake READ timeout")
        if mode == "crc":
            raw = raw[:-1] + bytes((raw[-1] ^ 1,))
        if mode == "truncation":
            raw = raw[:-1]
        return raw

    def close(self):
        with self.lock:
            self.closed_by.append(threading.get_ident())


class Factory:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, selector, **kwargs):
        with self.lock:
            self.calls.append((selector, kwargs, threading.get_ident()))
            if not self.results:
                raise m.TransportError("device_not_found", "fake device absent")
            result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "tools/panel/hardware_backend.py").exists(),
                        "missing HardwareBackend worker implementation")
        self.h = importlib.import_module("tools.panel.hardware_backend")
        self.addCleanup(patch.stopall)
        patch.object(self.h, "RETRY_INITIAL_S", 0.02).start()
        patch.object(self.h, "RETRY_MAX_S", 0.1).start()
        self.backends = []
        self.addCleanup(self.close_backends)

    def close_backends(self):
        for backend in self.backends:
            backend.close()

    def start(self, transport=None, *, factory=None, **kwargs):
        transport = RecordingTransport() if transport is None else transport
        factory = Factory(transport) if factory is None else factory
        backend = self.h.HardwareBackend(transport_factory=factory, poll_hz=50, **kwargs)
        self.backends.append(backend)
        return backend, transport, factory

    def wait_for(self, condition, *, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.005)
        self.fail("hardware worker condition did not become true")

    def live(self, backend):
        return self.wait_for(lambda: backend.snapshot() if backend.status()["connection_state"] == "live" else None)

    def test_constructor_rejects_invalid_poll_rates_and_selectors_before_open(self):
        factory = Factory()
        for rate in (0, -1, float("inf"), float("nan")):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.h.HardwareBackend(poll_hz=rate, transport_factory=factory)
        with self.assertRaises(ValueError):
            self.h.HardwareBackend("unit-A", transport_factory=factory)
        self.assertEqual(factory.calls, [])

    def test_constructor_rejects_unrepresentable_poll_periods_before_open(self):
        for rate in (1e-20, 5e-324):
            with self.subTest(rate=rate):
                factory = Factory(RecordingTransport())
                with self.assertRaises(ValueError):
                    backend = self.h.HardwareBackend(poll_hz=rate, transport_factory=factory)
                    self.backends.append(backend)
                self.assertEqual(factory.calls, [])

    def test_clean_package_and_script_imports_use_canonical_transport_types(self):
        import subprocess
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
from tools.panel import hardware_backend as package_worker
import hardware_backend as script_worker
from tools.panel import usb_mirror
import panel_backend
assert package_worker.mirror is usb_mirror
assert script_worker.mirror is usb_mirror
assert package_worker.ViewSnapshot is panel_backend.ViewSnapshot
assert script_worker.ViewSnapshot is panel_backend.ViewSnapshot
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT)],
                                cwd="/tmp", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_client_fragmentation_and_single_worker_ownership(self):
        for ceiling in (64, 128, 256):
            with self.subTest(ceiling=ceiling):
                backend, transport, factory = self.start(RecordingTransport(ceiling=ceiling))
                view = self.live(backend)
                self.assertTrue(view.png.startswith(b"\x89PNG"))
                self.assertEqual(view.model, "MKII")
                self.assertEqual(view.capabilities, frozenset(("screen",)))
                reads = transport.calls_for(p.REQUEST_READ)
                self.assertEqual([call[0].index for call in reads],
                                 list(range(0, 1280, ceiling - p.HEADER_SIZE)))
                self.assertTrue(all(call[0].bm_request_type == 0xc0 for call in transport.calls))
                self.assertTrue(all(0 < call[3] <= 250 for call in transport.calls))
                owners = {call[2] for call in transport.calls}
                self.assertEqual(owners, {factory.calls[0][2]})
                self.assertNotIn(threading.get_ident(), owners)
                backend.close()
                self.assertEqual(transport.closed_by, list(owners))

    def test_static_heartbeat_does_not_reacquire_or_change_view_timestamps(self):
        backend, transport, _ = self.start()
        view = self.live(backend)
        self.wait_for(lambda: len(transport.calls_for(p.REQUEST_INFO)) >= 4)
        status = backend.status()
        self.assertIs(backend.snapshot(), view)
        self.assertEqual(len(transport.calls_for(p.REQUEST_BEGIN)), 1)
        self.assertEqual(status["connection_state"], "live")
        self.assertGreater(status["last_contact_at"], status["last_snapshot_at"])
        self.assertEqual(status["last_snapshot_at"], view.snapshot_at)
        self.assertEqual(status["last_display_change_at"], view.display_changed_at)

    def test_instance_identity_is_stable_and_distinguishes_backend_lifetimes(self):
        # Publication wall time is not a lifecycle identity, even if clocks repeat.
        with patch.object(self.h.time, 'time', return_value=1234567890.0):
            first, transport, _ = self.start()
            view = self.live(first)
            identity = first.status()['instance_id']
            self.assertTrue(identity)
            self.wait_for(lambda: len(transport.calls_for(p.REQUEST_INFO)) >= 4)
            self.assertEqual(first.status()['instance_id'], identity)
            self.assertEqual(first.status()['seq'], view.generation)
            self.assertEqual(first.status()['last_snapshot_at'], view.snapshot_at)
            first.close()
            second, _, _ = self.start()
            restarted = self.live(second)
            self.assertEqual(restarted.generation, view.generation)
            self.assertEqual(restarted.snapshot_at, view.snapshot_at)
            self.assertNotEqual(second.status()['instance_id'], identity)

    def test_device_contact_timeout_is_visible_and_browser_reads_cannot_refresh_it(self):
        with patch.object(self.h, "CONTACT_TIMEOUT_S", 0.05):
            backend, transport, _ = self.start()
            view = self.live(backend)
            transport.change(mode="blocked_info")
            self.assertTrue(transport.read_entered.wait(1))
            before = backend.status()["last_contact_at"]
            status = self.wait_for(lambda: backend.status()
                                   if backend.status()["connection_state"] == "stale" else None)
            self.assertEqual(status["error"]["code"], "timeout")
            self.assertEqual(status["last_contact_at"], before)
            self.assertIs(backend.snapshot(), view)
            transport.unblock.set()
            self.wait_for(lambda: backend.status()["connection_state"] == "live")

    def test_cancel_after_completed_transfer_never_publishes_rendered_candidate(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.h.render_link
        def delayed_render(link):
            result = original(link)
            entered.set()
            finish.wait(1)
            return result
        with patch.object(self.h, "render_link", side_effect=delayed_render), \
                patch.object(self.h, "CLOSE_TIMEOUT_S", 0.05):
            backend, transport, _ = self.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(transport.calls_for(p.REQUEST_RELEASE)), 1)
            backend.close()
            self.assertIsNone(backend.snapshot())
            finish.set()
            self.wait_for(lambda: not backend._worker.is_alive())
            self.assertIsNone(backend.snapshot())
            self.assertEqual(backend.status()["connection_state"], "closed")

    def test_optional_state_all_rows_unknown_families_and_no_leak(self):
        flags = FLAGS | p.LED_ROWS_SUPPORTED | p.LED_LEVELS_SUPPORTED | p.BACKLIGHT_SUPPORTED
        known = flags | p.ROW_KNOWN | p.LEVEL_KNOWN | p.BACKLIGHT_KNOWN
        rows = b"".join(bytes((0x20 + row, row + 1)) if row < 16 else
                        bytes((0xa0 + row - 16, row + 1)) for row in range(32))
        data = body() + rows + b"\x30\x05\x3f\xff\xb7\x00"
        backend, transport, _ = self.start(RecordingTransport(flags=known, data=data))
        view = self.live(backend)
        self.assertEqual(view.leds.bits, bytes(range(1, 33)))
        self.assertEqual(view.leds.ids, ((5, 0), (255, 15)))
        self.assertEqual(view.leds.backlight, 0)
        self.assertEqual(view.capabilities, frozenset(("screen", "leds", "backlight")))
        transport.change(flags=flags, data=body(), generation=10)
        newer = self.wait_for(lambda: backend.snapshot() if backend.snapshot().generation > view.generation else None)
        self.assertIsNone(newer.leds)
        self.assertEqual(newer.capabilities, frozenset(("screen",)))
        self.assertEqual(newer.display_changed_at, view.display_changed_at)
        self.assertGreater(newer.snapshot_at, view.snapshot_at)
        status = backend.status()
        self.assertEqual(status["negotiated_capabilities"], ["backlight", "leds", "screen"])
        self.assertFalse(backend.supports("leds"))
        for capability in ("controls", "midi", "memory", "emulator", "samples", "card", "audio"):
            self.assertFalse(backend.supports(capability))

    def test_supported_entirely_unknown_optional_state_is_absent(self):
        flags = FLAGS | p.LED_ROWS_SUPPORTED | p.LED_LEVELS_SUPPORTED | p.BACKLIGHT_SUPPORTED
        backend, _, _ = self.start(RecordingTransport(flags=flags))
        view = self.live(backend)
        self.assertIsNone(view.leds)
        self.assertEqual(view.capabilities, frozenset(("screen",)))

    def test_invalid_candidates_preserve_last_good_view(self):
        for mode in ("crc", "truncation", "offset", "mixed_generation", "mixed_token", "mixed_epoch", "bound"):
            with self.subTest(mode=mode):
                backend, transport, _ = self.start()
                view = self.live(backend)
                transport.change(mode=mode, generation=10)
                status = self.wait_for(lambda: backend.status() if backend.status()["error"] else None)
                self.assertEqual(status["error"]["code"], "protocol_error")
                self.assertEqual(status["connection_state"], "stale")
                self.assertIs(backend.snapshot(), view)
                self.assertEqual(status["last_snapshot_at"], view.snapshot_at)
                self.assertEqual(status["seq"], view.generation)
                backend.close()

    def test_wrong_version_unknown_model_and_waiting_without_black_frame(self):
        transport = RecordingTransport()
        transport.mode = "wrong_version"
        backend, _, _ = self.start(transport)
        status = self.wait_for(lambda: backend.status() if backend.status()["error"] else None)
        self.assertEqual(status["connection_state"], "unsupported")
        self.assertEqual(status["error"]["code"], "unsupported")
        self.assertIsNone(backend.snapshot())
        self.assertFalse(status["has_frame"])
        backend, _, _ = self.start(RecordingTransport(model=239))
        self.assertIsNone(self.live(backend).model)
        self.assertEqual(backend.status()["model"], "unknown")
        self.assertEqual(backend.status()["model_raw"], 239)

    def test_incomplete_or_inactive_observer_never_acquires(self):
        for missing in (p.LCD_COMPLETE, p.OBSERVER_ACTIVE):
            with self.subTest(missing=missing):
                backend, transport, _ = self.start(RecordingTransport(flags=FLAGS & ~missing))
                self.wait_for(lambda: len(transport.calls_for(p.REQUEST_INFO)) >= 2)
                self.assertIsNone(backend.snapshot())
                self.assertEqual(backend.status()["connection_state"], "syncing")
                self.assertEqual(transport.calls_for(p.REQUEST_BEGIN), [])

    def test_stalled_observer_preserves_frame_visibly_stale(self):
        backend, transport, _ = self.start()
        view = self.live(backend)
        transport.change(flags=FLAGS & ~p.OBSERVER_ACTIVE)
        self.wait_for(lambda: backend.status()["connection_state"] == "stale")
        self.assertIs(backend.snapshot(), view)
        self.assertEqual(len(transport.calls_for(p.REQUEST_BEGIN)), 1)

    def test_generation_epoch_token_wrap_and_connection_incarnation(self):
        first = RecordingTransport(token=65535)
        second = RecordingTransport(token=1)
        backend, _, factory = self.start(first, factory=Factory(first, second))
        view = self.live(backend)
        first.change(token=1, generation=10)
        changed = self.wait_for(lambda: backend.snapshot() if backend.snapshot().generation > view.generation else None)
        first.change(epoch=8)
        new_epoch = self.wait_for(lambda: backend.snapshot() if backend.snapshot().generation > changed.generation else None)
        first.change(mode="disconnected")
        reconnect = self.wait_for(lambda: backend.snapshot() if backend.snapshot().generation > new_epoch.generation else None)
        self.assertEqual(factory.calls[1][1]["expected_serial"], "unit-A")
        self.assertGreater(reconnect.generation, new_epoch.generation)
        self.assertEqual(len(second.calls_for(p.REQUEST_BEGIN)), 1)
        self.assertEqual(first.closed_by, [factory.calls[0][2]])

    def test_reconnect_rejects_wrong_serial_before_info(self):
        first, wrong, right = RecordingTransport(), RecordingTransport(serial="unit-B"), RecordingTransport()
        backend, _, factory = self.start(first, factory=Factory(first, wrong, right))
        view = self.live(backend)
        first.change(mode="disconnected")
        self.wait_for(lambda: backend.snapshot().generation > view.generation)
        self.assertEqual(wrong.calls, [])
        self.assertEqual(len(wrong.closed_by), 1)
        self.assertEqual([call[1]["expected_serial"] for call in factory.calls[:3]], [None, "unit-A", "unit-A"])

    def test_serial_identity_remains_selected_after_unsupported_info(self):
        first, wrong, right = RecordingTransport(), RecordingTransport(serial="unit-B"), RecordingTransport()
        first.mode = "wrong_version"
        backend, _, factory = self.start(first, factory=Factory(first, wrong, right))
        self.live(backend)
        self.assertEqual(wrong.calls, [])
        self.assertEqual(factory.calls[1][1]["expected_serial"], "unit-A")
        self.assertEqual(backend.status()["device"]["serial"], "unit-A")

    def test_serialless_disconnect_requires_restart_even_for_topology(self):
        first, replacement = RecordingTransport(serial=None), RecordingTransport()
        backend, _, factory = self.start(first, factory=Factory(first, replacement), selector="topology:1-2.3")
        view = self.live(backend)
        first.change(mode="disconnected")
        status = self.wait_for(lambda: backend.status() if backend.status()["connection_state"] == "disconnected" else None)
        self.assertIn("restart", status["error"]["message"].lower())
        time.sleep(0.15)
        self.assertEqual(len(factory.calls), 1)
        self.assertIs(backend.snapshot(), view)

    def test_busy_permission_and_unavailable_errors_are_distinct_and_bounded(self):
        states = {"busy": "busy", "permission_denied": "permission_denied", "ambiguous_device": "fault",
                  "unavailable_dependency": "fault", "unavailable_libusb": "fault"}
        for code, state in states.items():
            with self.subTest(code=code):
                errors = [m.TransportError(code, "actionable " + code)] * 100
                backend, _, factory = self.start(factory=Factory(*errors))
                status = self.wait_for(lambda: backend.status() if backend.status()["error"] else None)
                self.assertEqual(status["error"]["code"], code)
                self.assertEqual(status["connection_state"], state)
                time.sleep(0.12)
                self.assertLessEqual(len(factory.calls), 5)
                backend.close()

    def test_busy_existing_lease_retries_without_disposing_or_reopening_device(self):
        transport = RecordingTransport(serial=None)
        transport.mode = "busy"
        with patch.object(self.h, "RETRY_MAX_S", 0.02):
            backend, _, factory = self.start(transport)
            self.wait_for(lambda: len(transport.calls_for(p.REQUEST_BEGIN)) >= 5)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(transport.closed_by, [])
        self.assertEqual(backend.status()["connection_state"], "busy")
        self.assertIsNone(backend.snapshot())

    def test_exception_is_reported_and_handle_closed(self):
        transport = RecordingTransport()
        transport.mode = "exception"
        backend, _, _ = self.start(transport)
        status = self.wait_for(lambda: backend.status() if backend.status()["error"] else None)
        self.assertEqual(status["error"]["code"], "worker_exception")
        self.assertEqual(status["connection_state"], "fault")
        self.wait_for(lambda: transport.closed_by)

    def test_diagnostics_remain_bounded_and_detached_during_persistent_errors(self):
        factory = Factory(*([m.TransportError("busy", "fake repeated busy")] * 100))
        with patch.object(self.h, "RETRY_MAX_S", 0.02):
            backend, _, _ = self.start(factory=factory)
            self.wait_for(lambda: len(factory.calls) >= 20)
        status = backend.status()
        self.assertEqual(len(status["diagnostics"]), 16)
        status["diagnostics"][0]["message"] = "caller mutation"
        self.assertNotEqual(backend.status()["diagnostics"][0]["message"], "caller mutation")

    def test_cancel_before_frame_and_repeated_close(self):
        transport = RecordingTransport()
        transport.mode = "pending"
        backend, _, _ = self.start(transport)
        self.wait_for(lambda: transport.calls_for(p.REQUEST_BEGIN))
        backend.close()
        backend.close()
        self.assertIsNone(backend.snapshot())
        self.assertEqual(backend.status()["connection_state"], "closed")
        self.assertEqual(len(transport.closed_by), 1)
        self.assertFalse(backend._worker.is_alive())

    def test_close_blocked_read_and_incomplete_shutdown_have_no_concurrent_disposal(self):
        for mode in ("blocked", "violates_timeout"):
            with self.subTest(mode=mode):
                transport = RecordingTransport()
                transport.mode = mode
                with patch.object(self.h, "CLOSE_TIMEOUT_S", 0.05 if mode == "violates_timeout" else 1):
                    backend, _, factory = self.start(transport)
                    self.assertTrue(transport.read_entered.wait(1))
                    before = time.monotonic()
                    backend.close()
                    self.assertLess(time.monotonic() - before, 1.1)
                    if mode == "violates_timeout":
                        self.assertEqual(backend.status()["error"]["code"], "shutdown_incomplete")
                        self.assertEqual(transport.closed_by, [])
                        transport.unblock.set()
                        self.wait_for(lambda: transport.closed_by)
                        backend.close()
                    self.assertEqual(backend.status()["connection_state"], "closed")
                    self.assertEqual(transport.closed_by, [factory.calls[0][2]])

    def test_worker_completion_before_close_status_lock_cannot_report_incomplete(self):
        backend, transport, factory = self.start()
        view = self.live(backend)
        allow_dispose = threading.Event()
        join_expired = threading.Event()
        real_close = transport.close
        real_join = backend._worker.join
        real_alive = backend._worker.is_alive
        real_lock = backend._lock
        caller = threading.get_ident()

        def held_dispose():
            allow_dispose.wait(1)
            real_close()

        def joined(timeout=None):
            real_join(timeout)
            self.assertTrue(real_alive(), "the controlled disposal must outlast the join")
            join_expired.set()

        case = self
        class CompleteBeforeStatusLock:
            triggered = False
            def __enter__(self):
                if threading.get_ident() == caller and join_expired.is_set() and not self.triggered:
                    self.triggered = True
                    # Force the legal interleaving: bounded join expired with
                    # a live worker; disposal/final status finish before the
                    # caller acquires the status-publication lock.
                    allow_dispose.set()
                    real_join(1)
                    case.assertFalse(real_alive())
                return real_lock.__enter__()
            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        backend._lock = CompleteBeforeStatusLock()
        self.addCleanup(allow_dispose.set)
        with patch.object(transport, "close", side_effect=held_dispose), \
                patch.object(backend._worker, "join", side_effect=joined), \
                patch.object(self.h, "CLOSE_TIMEOUT_S", 0.02):
            backend.close()
        self.assertTrue(join_expired.is_set())
        self.assertFalse(real_alive())
        self.assertEqual(transport.closed_by, [factory.calls[0][2]])
        self.assertEqual(backend.status()["connection_state"], "closed")
        self.assertIsNone(backend.status()["error"])
        self.assertIs(backend.snapshot(), view)

    def test_status_is_detached_and_viewers_never_perform_io(self):
        backend, transport, _ = self.start()
        view = self.live(backend)
        started = threading.Event()
        finished = threading.Event()
        viewer_results = []
        def slow_viewer():
            cached = backend.snapshot()
            started.set()
            finished.wait(1)
            viewer_results.append(cached)
        viewer = threading.Thread(target=slow_viewer)
        viewer.start()
        self.assertTrue(started.wait(1))
        transport.change(generation=10)
        self.wait_for(lambda: backend.snapshot().generation > view.generation)
        for _ in range(50):
            backend.snapshot()
            status = backend.status()
        status["device"]["port_numbers"].clear()
        status["protocol"]["epoch"] = 0
        status["limits"]["max_response"] = 0
        status["capabilities"].append("controls")
        status["diagnostics"].clear()
        current = backend.status()
        self.assertEqual(current["device"]["port_numbers"], [2, 3])
        self.assertEqual(current["protocol"]["epoch"], 7)
        self.assertEqual(current["limits"]["max_response"], 64)
        self.assertEqual(current["capabilities"], ["screen"])
        self.assertTrue(current["read_only"])
        self.assertEqual((current["source"], current["backend"]), ("hardware", "hardware"))
        for key in ("booted", "phase", "project", "image", "sound", "speed", "rt", "card"):
            self.assertNotIn(key, current)
        owner = {call[2] for call in transport.calls}
        self.assertEqual(len(owner), 1)
        self.assertNotIn(threading.get_ident(), owner)
        self.assertNotIn(viewer.ident, owner)
        finished.set()
        viewer.join(1)
        self.assertFalse(viewer.is_alive())
        self.assertEqual(len(viewer_results), 1)
        self.assertIs(viewer_results[0], view)

    def test_polling_honors_configured_and_firmware_minimum_intervals(self):
        for rate, firmware_ms, minimum in ((100, 50, 0.045), (10, 20, 0.09)):
            with self.subTest(rate=rate):
                transport = RecordingTransport(poll=firmware_ms)
                backend = self.h.HardwareBackend(poll_hz=rate, transport_factory=Factory(transport))
                self.backends.append(backend)
                self.live(backend)
                calls = self.wait_for(lambda: transport.calls_for(p.REQUEST_INFO)
                                      if len(transport.calls_for(p.REQUEST_INFO)) >= 3 else None)
                self.assertTrue(all(b[1] - a[1] >= minimum for a, b in zip(calls, calls[1:])))
                backend.close()


if __name__ == "__main__":
    unittest.main()
