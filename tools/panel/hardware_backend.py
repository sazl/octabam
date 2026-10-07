"""One read-only USB worker publishing immutable cached panel views.

INFO heartbeats count as contact even when a screen is static. Reads have a
250 ms transfer timeout and SnapshotClient's at-most-two-second lease budget.
Polling waits at least the requested period, firmware minimum, and advertised
publication period. Contact is stale after max(2 seconds, three poll periods).
Poll rates must also have a finite reciprocal no larger than threading.TIMEOUT_MAX;
unrepresentable worker waits are rejected before startup or device access.
Recovery backs off from 0.5 to 5 seconds; three corrupt/timed-out acquisitions
dispose the handle and renegotiate. Diagnostics retain only the last 16 entries.
close signals cancellation and waits at most three seconds. A transport which
violates its timeout is reported as shutdown_incomplete, never disposed from
the caller thread. Its worker disposes it when the in-flight operation ends.
"""
from collections import deque
import math
from pathlib import Path
import sys
import threading
import time
import uuid

# Existing panel tools import their shared renderer by its script-directory
# name. Keep one class identity for those consumers in package mode as well.
_PANEL_DIR = Path(__file__).resolve().parent
if str(_PANEL_DIR) not in sys.path:
    sys.path.insert(0, str(_PANEL_DIR))
from panel_backend import LedSnapshot, ViewSnapshot, render_link
from panel_link import PanelLink
_ROOT = _PANEL_DIR.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from tools.panel import usb_mirror as mirror
from tools.panel import usb_mirror_protocol as protocol

RETRY_INITIAL_S = 0.5
RETRY_MAX_S = 5.0
CLOSE_TIMEOUT_S = 3.0
CONTACT_TIMEOUT_S = 2.0
MAX_RESYNC_FAILURES = 3


class HardwareBackend:
    source = "hardware"

    def __init__(self, selector: str | None = None, *, poll_hz: float = 5,
                 transport_factory=None):
        try:
            rate = float(poll_hz)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("poll_hz must be finite and positive") from error
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("poll_hz must be finite and positive")
        configured_period = 1 / rate
        if not math.isfinite(configured_period) or configured_period > threading.TIMEOUT_MAX:
            raise ValueError("poll_hz is too small: polling interval must not exceed threading.TIMEOUT_MAX seconds")
        self._instance_id = uuid.uuid4().hex
        self._selector = mirror.DeviceSelector.parse(selector)
        self._selector_text = selector
        self._factory = mirror.open_transport if transport_factory is None else transport_factory
        self._configured_period = configured_period
        self._period = self._configured_period
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._view = None
        self._capabilities = frozenset()
        self._state = "connecting"
        self._error = None
        self._diagnostics = deque(maxlen=16)
        self._identity = None
        self._info = None
        self._header = None
        self._connection_id = 0
        self._device_generation = None
        self._accepted_key = None
        self._last_contact_at = None
        self._last_contact_mono = None
        self._last_snapshot_at = None
        self._last_display_change_at = None
        self._known_rows = ()
        self._known_ids = ()
        self._backlight_known = False
        self._worker_finished = False
        self._worker = threading.Thread(target=self._run, name="panel-usb-mirror", daemon=True)
        self._worker.start()

    @property
    def capabilities(self) -> frozenset[str]:
        with self._lock:
            return self._capabilities

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def snapshot(self) -> ViewSnapshot | None:
        with self._lock:
            return self._view

    def status(self) -> dict[str, object]:
        # Only copy references/scalars under the publication lock; nested JSON
        # construction and serialization belong outside it.
        with self._lock:
            view, identity, info, header = self._view, self._identity, self._info, self._header
            state, error = self._state, self._error
            capabilities = self._capabilities
            connection_id, generation = self._connection_id, self._device_generation
            contact_at, contact_mono = self._last_contact_at, self._last_contact_mono
            snapshot_at, display_at = self._last_snapshot_at, self._last_display_change_at
            rows, ids, backlight = self._known_rows, self._known_ids, self._backlight_known
            period, diagnostics = self._period, tuple(self._diagnostics)
        if state == "live" and contact_mono is not None:
            if time.monotonic() - contact_mono > max(CONTACT_TIMEOUT_S, 3 * period):
                state = "stale"
                error = ("timeout", "No successful device contact within the liveness threshold")
        return {
            "source": self.source, "backend": "hardware", "read_only": True,
            "instance_id": self._instance_id,
            "connection_state": state, "capabilities": sorted(capabilities),
            "negotiated_capabilities": sorted(self._offered(info)) if info else [],
            "generation": generation, "seq": view.generation if view else None,
            "has_frame": view is not None,
            "model": self._model(info) or "unknown", "model_raw": info.model if info else None,
            "device": identity.as_dict() if identity else None, "selector": self._selector_text,
            "protocol": {"major": header.major, "minor": header.minor, "schema": info.schema,
                         "epoch": header.epoch, "connection_id": connection_id} if info and header else None,
            "build": (info.build_id or None) if info else None,
            "limits": {"max_response": info.max_response, "max_snapshot": info.max_snapshot,
                       "min_poll_ms": info.min_poll_ms, "lease_ms": info.lease_ms,
                       "max_publications_hz": info.max_publications_hz} if info else None,
            "last_contact_at": contact_at, "last_snapshot_at": snapshot_at,
            "last_display_change_at": display_at,
            "known_state": {"led_rows": list(rows), "led_ids": list(ids), "backlight": backlight},
            "error": {"code": error[0], "message": error[1]} if error else None,
            "diagnostics": [{"at": at, "code": code, "message": message}
                            for at, code, message in diagnostics],
        }

    @staticmethod
    def _model(info):
        if info is None:
            return None
        return {int(protocol.Model.MKI): "MKI", int(protocol.Model.MKII): "MKII"}.get(info.model)

    @staticmethod
    def _offered(info):
        capabilities = {"screen"}
        if info.capabilities & (protocol.LED_ROWS_SUPPORTED | protocol.LED_LEVELS_SUPPORTED):
            capabilities.add("leds")
        if info.capabilities & protocol.BACKLIGHT_SUPPORTED:
            capabilities.add("backlight")
        return frozenset(capabilities)

    def _set_state(self, state, error=None):
        with self._lock:
            if self._stop.is_set():
                return
            self._state = state
            self._error = error
            if error is not None:
                self._diagnostics.append((time.time(), error[0], error[1][:512]))

    def _contact(self, response, info):
        with self._lock:
            if self._stop.is_set():
                return
            self._info, self._header = info, response.header
            self._device_generation = response.header.generation
            self._last_contact_at, self._last_contact_mono = time.time(), time.monotonic()
            self._period = max(self._configured_period, info.min_poll_ms / 1000, 1 / info.max_publications_hz)
            if self._view is None:
                self._capabilities = frozenset(("screen",))

    def _publish(self, completed, connection_id):
        # All transfer validation lives in SnapshotClient/protocol. Decode a
        # fresh candidate and independently check the decoder consumed only
        # the complete canonical view, never history or partial state.
        link = PanelLink()
        link.feed(completed.body)
        if (link.pending or link.stats["unknown"] or link.stats["lcd_badcol"] or
                link.commands or link.stats["lcd_blocks"] != protocol.LCD_BLOCK_COUNT):
            raise mirror.TransportError("protocol_error", "Canonical body did not decode to 128 complete LCD blocks")
        summary = completed.summary
        if (tuple(sorted(link.led_rows)) != summary.led_rows or
                tuple(sorted(link.leds)) != summary.led_ids or
                (link.backlight is not None) != summary.backlight_known):
            raise mirror.TransportError("protocol_error", "Decoded optional state differs from canonical summary")
        png, text = render_link(link)
        capabilities = {"screen"}
        if summary.led_rows or summary.led_ids:
            capabilities.add("leds")
        if summary.backlight_known:
            capabilities.add("backlight")
        leds = None
        if summary.led_rows or summary.led_ids or summary.backlight_known:
            bits = bytes(link.led_rows.get(row, 0) for row in range(protocol.MAX_LED_ROWS)) if summary.led_rows else b""
            leds = LedSnapshot(bits, tuple(sorted(link.leds.items())), link.backlight)
        now, monotonic_now = time.time(), time.monotonic()
        with self._lock:
            if self._stop.is_set():
                return
            old = self._view
            changed_at = now if old is None or old.png != png else old.display_changed_at
            sequence = 1 if old is None else old.generation + 1
            self._view = ViewSnapshot(png, text, leds, sequence, self._model(completed.info),
                                      frozenset(capabilities), now, changed_at)
            self._capabilities = self._view.capabilities
            self._accepted_key = (connection_id, completed.identity.epoch, completed.identity.generation,
                                  completed.info.capabilities, completed.info.model)
            self._device_generation = completed.identity.generation
            self._known_rows, self._known_ids = summary.led_rows, summary.led_ids
            self._backlight_known = summary.backlight_known
            self._last_contact_at, self._last_contact_mono = now, monotonic_now
            self._last_snapshot_at, self._last_display_change_at = now, changed_at
            self._state, self._error = "live", None

    def _dispose(self, transport):
        try:
            transport.close()
        except Exception as error:
            with self._lock:
                self._diagnostics.append((time.time(), "dispose_failed", str(error)[:512]))

    def _run(self):
        transport = None
        client = None
        expected_serial = None
        serialless_selected = False
        incarnation, failures = 0, 0
        retry = RETRY_INITIAL_S
        reconnect_allowed = True
        try:
            while not self._stop.is_set():
                if not reconnect_allowed:
                    self._stop.wait()
                    break
                try:
                    if transport is None:
                        self._set_state("connecting")
                        transport = self._factory(self._selector, expected_serial=expected_serial,
                                                  timeout_ms=protocol.USB_TIMEOUT_MS)
                        required_serial = expected_serial or self._selector.serial
                        if required_serial is not None and transport.identity.serial != required_serial:
                            raise mirror.TransportError("device_not_found", "Selected physical serial changed; refusing replacement")
                        # Transport discovery verified this physical serial;
                        # even unsupported firmware must not release selection.
                        if transport.identity.serial is not None:
                            expected_serial = transport.identity.serial
                        incarnation += 1
                        serialless_selected = transport.identity.serial is None
                        client = mirror.SnapshotClient(transport, connection_id=incarnation,
                                                       timeout_ms=protocol.USB_TIMEOUT_MS)
                        with self._lock:
                            self._identity = transport.identity
                            self._connection_id = incarnation
                            self._capabilities = frozenset(("screen",)) if self._view else frozenset()
                    if self._stop.is_set():
                        break
                    response, info = client.info()
                    self._contact(response, info)
                    ready = (info.valid_lcd_blocks == protocol.LCD_BLOCK_COUNT and
                             response.header.flags & (protocol.LCD_COMPLETE | protocol.OBSERVER_ACTIVE)
                             == (protocol.LCD_COMPLETE | protocol.OBSERVER_ACTIVE))
                    if not ready:
                        self._set_state("stale" if self.snapshot() else "syncing",
                                        ("not_ready", "Waiting for a complete, active physical panel observer"))
                    else:
                        key = (incarnation, response.header.epoch, response.header.generation,
                               info.capabilities, info.model)
                        with self._lock:
                            unchanged = self._accepted_key == key
                        if unchanged:
                            self._set_state("live")
                        else:
                            self._set_state("syncing")
                            completed = client.snapshot(response, info, stop_event=self._stop)
                            self._publish(completed, incarnation)
                        failures, retry = 0, RETRY_INITIAL_S
                    if self._stop.wait(self._period):
                        break
                except Exception as error:
                    if self._stop.is_set():
                        break
                    code = error.code if isinstance(error, mirror.TransportError) else "worker_exception"
                    message = str(error)[:512]
                    state = {"unsupported": "unsupported", "permission_denied": "permission_denied",
                             "busy": "busy", "disconnected": "disconnected",
                             "device_not_found": "disconnected",
                             "not_ready": "stale" if self.snapshot() else "syncing"}.get(code,
                                  "stale" if self.snapshot() and code in ("timeout", "protocol_error") else "fault")
                    failures = failures + 1 if code in ("timeout", "protocol_error") else 0
                    drop = (code not in ("busy", "not_ready", "timeout", "protocol_error") or
                            (code in ("timeout", "protocol_error") and failures >= MAX_RESYNC_FAILURES))
                    if code in ("disconnected", "device_not_found"):
                        drop = True
                    if drop and transport is not None:
                        self._dispose(transport)
                        transport, client = None, None
                        if serialless_selected:
                            reconnect_allowed = False
                            message += "; device has no verified serial: restart the server to adopt a physical unit"
                    self._set_state(state, (code, message))
                    if self._stop.wait(max(self._period, retry)):
                        break
                    retry = min(RETRY_MAX_S, retry * 2)
        finally:
            if transport is not None:
                self._dispose(transport)
            with self._lock:
                self._state = "closed"
                if self._error and self._error[0] == "shutdown_incomplete":
                    self._diagnostics.append((time.time(), "shutdown_complete", "Worker finished after delayed in-flight I/O"))
                    self._error = None
                self._worker_finished = True

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._state = "closed"
        if threading.current_thread() is not self._worker:
            self._worker.join(CLOSE_TIMEOUT_S)
        with self._lock:
            if not self._worker_finished:
                self._error = ("shutdown_incomplete", "Worker still has in-flight I/O; its handle will close when I/O ends")
                self._diagnostics.append((time.time(), *self._error))
