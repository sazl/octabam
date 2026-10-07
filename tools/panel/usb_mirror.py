"""Exclusive synchronous, claim-free USB transport for the provisional panel mirror.

No worker lives here. The backend's single worker owns transfer and teardown;
the standalone probe owns its separate context synchronously. Topology identifies
a USB location, not a physical unit. A device without serial must not be silently
adopted after reconnect. Discovery emits only bounded standard device STRING
reads; mirror I/O emits the four 0xc0 requests. No recovery mutates device state.
"""
from dataclasses import dataclass
import errno
import logging
import platform
import re
import secrets
import threading
import time

from . import usb_mirror_protocol as p

_LOG=logging.getLogger(__name__)
# Existing tools/hw/usb_counters.py / usb_probe.py deployment identifier.
_VENDOR_ID=0x1935
_PRODUCT_ID=0x0002
_MAX_TIMEOUT_MS=60000
_MAX_ACQUISITION_MS=2000

@dataclass(frozen=True)
class DeviceSelector:
    serial: str | None = None
    bus: int | None = None
    port_numbers: tuple[int,...] = ()

    @classmethod
    def parse(cls, selector: str | None) -> 'DeviceSelector':
        if selector is None: return cls()
        if selector.startswith('serial:') and selector[7:]: return cls(serial=selector[7:])
        match=re.fullmatch(r'topology:([1-9][0-9]*)-([1-9][0-9]*(?:\.[1-9][0-9]*)*)',selector)
        if match: return cls(bus=int(match[1]),port_numbers=tuple(map(int,match[2].split('.'))))
        raise ValueError('selector must be serial:<nonempty> or topology:<bus>-<port>[.<port>]')

@dataclass(frozen=True)
class DeviceIdentity:
    serial: str | None
    bus: int | None
    port_numbers: tuple[int,...]
    address: int | None

    def as_dict(self):
        return dict(serial=self.serial,bus=self.bus,port_numbers=list(self.port_numbers),address=self.address)

class TransportError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code=code
        self.message=message
        super().__init__(message)


def _timeout(timeout_ms):
    if type(timeout_ms) is not int or not 1<=timeout_ms<=_MAX_TIMEOUT_MS:
        raise ValueError(f'timeout_ms must be an integer in 1..{_MAX_TIMEOUT_MS}')
    return timeout_ms


def _error(error: Exception) -> TransportError:
    if isinstance(error,TransportError): return error
    number=getattr(error,'errno',None)
    backend=getattr(error,'backend_error_code',None)
    if isinstance(error,PermissionError) or number in (errno.EACCES,errno.EPERM) or backend==-3:
        code='permission_denied'
    elif number==errno.ETIMEDOUT or backend==-7: code='timeout'
    elif number in (errno.ENODEV,errno.ESHUTDOWN) or backend==-4: code='disconnected'
    elif number==errno.EPIPE or backend==-9: code='unsupported'
    elif number==errno.EBUSY or backend==-6: code='busy'
    elif number in (errno.ENOSYS,errno.ENOTSUP) or backend==-12: code='unsupported'
    else: code='protocol_error'
    return TransportError(code,f'{type(error).__name__}: {error}')


def _load_usb():
    """Imports stay lazy so emulator users do not need PyUSB or libusb."""
    try:
        import usb.core
        import usb.util
        import usb.backend.libusb1
    except ImportError as error:
        raise TransportError('unavailable_dependency','Physical mirror needs pyusb>=1.3.1 (uv sync --extra emu)') from error
    try: backend=usb.backend.libusb1.get_backend()
    except (OSError,RuntimeError) as error:
        raise TransportError('unavailable_libusb',f'libusb load failed on {platform.system()} {platform.machine()}: {error}') from error
    if backend is None:
        raise TransportError('unavailable_libusb',f'Native libusb unavailable for {platform.system()} {platform.machine()}; check library/runtime architecture')
    return usb.core,usb.util,backend


def _serial(device, timeout_ms):
    if not device.iSerialNumber: return None
    def read(index,language):
        _LOG.debug('discovery STRING bm=0x80 request=0x06 value=0x%04x language=0x%04x length=255 timeout=%s',0x300|index,language,timeout_ms)
        raw=bytes(device.ctrl_transfer(0x80,6,0x300|index,language,255,timeout=timeout_ms))
        if len(raw)<2 or raw[1]!=3 or raw[0]!=len(raw) or len(raw)%2:
            raise TransportError('protocol_error','Malformed standard STRING descriptor')
        return raw[2:]
    languages=read(0,0)
    if not languages: raise TransportError('protocol_error','No USB string language advertised')
    language=int.from_bytes(languages[:2],'little')
    payload=read(device.iSerialNumber,language)
    try: serial=payload.decode('utf-16-le')
    except UnicodeDecodeError as error: raise TransportError('protocol_error','Invalid serial STRING encoding') from error
    if not serial: return None
    if '\x00' in serial: raise TransportError('protocol_error','Serial contains NUL')
    return serial


class UsbTransport:
    """An exclusively created Device, never an audio helper's borrowed context.

    Construction is internal to open_transport. One lock serializes exchange and
    close; caller must let its owning worker finish before calling close. CLOSED
    persists because PyUSB dispose alone would allow lazy reopen.
    """
    def __init__(self, device, util, identity: DeviceIdentity, *, _owner_key=None):
        if _owner_key is not _OWNER_KEY:
            raise TransportError('protocol_error','Device borrowing is forbidden; use open_transport')
        self._device=device; self._util=util; self.identity=identity
        self._lock=threading.Lock(); self._closed=False; self._unavailable=False

    def exchange(self, request: p.SetupRequest, *, timeout_ms: int = 250) -> bytes:
        timeout_ms=_timeout(timeout_ms)
        try: p.validate_request(request)
        except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error
        with self._lock:
            if self._closed: raise TransportError('closed','USB transport permanently closed')
            if self._unavailable: raise TransportError('disconnected','USB device unavailable')
            _LOG.debug('mirror %s bm=0xc0 request=0x%02x value=%s index=%s length=%s timeout=%s',
                       p.validate_request(request).name,request.request,request.value,request.index,request.length,timeout_ms)
            try:
                return bytes(self._device.ctrl_transfer(request.bm_request_type,request.request,
                                                       request.value,request.index,request.length,timeout=timeout_ms))
            except Exception as error:
                translated=_error(error)
                if translated.code=='disconnected': self._unavailable=True
                raise translated from error

    def close(self) -> None:
        with self._lock:
            if self._closed: return
            self._closed=True
            # Safe only because this exclusively owned context has never claimed
            # an interface. No audio/helper Device may reach this class.
            try: self._util.dispose_resources(self._device)
            except Exception as error: raise _error(error) from error

_OWNER_KEY=object()


def open_transport(selector: DeviceSelector, *, expected_serial: str | None = None,
                   timeout_ms: int = 250) -> UsbTransport:
    timeout_ms=_timeout(timeout_ms)
    if not isinstance(selector,DeviceSelector): raise TypeError('DeviceSelector required')
    core,util,backend=_load_usb()
    candidates=[]; selected=None
    try:
        candidates=list(core.find(find_all=True,idVendor=_VENDOR_ID,idProduct=_PRODUCT_ID,backend=backend))
        if selector.bus is not None:
            candidates=[dev for dev in candidates if dev.bus==selector.bus and tuple(dev.port_numbers or ())==selector.port_numbers]
        # Unique/topology selection refuses ambiguity before any mirror request
        # or arbitrary first candidate probe. Serial filtering uses only STRING.
        if selector.serial is None and expected_serial is None and len(candidates)>1:
            raise TransportError('ambiguous_device','Multiple Octatracks; select serial:<serial> or topology:<bus>-<ports>')
        matches=[]
        for dev in candidates:
            serial=_serial(dev,timeout_ms)
            if selector.serial is not None and serial!=selector.serial: continue
            if expected_serial is not None and serial!=expected_serial: continue
            matches.append((dev,serial))
        if not matches: raise TransportError('device_not_found','No matching Octatrack (1935:0002) or expected serial changed')
        if len(matches)>1: raise TransportError('ambiguous_device','Multiple devices match identity selector')
        selected,serial=matches[0]
        identity=DeviceIdentity(serial,selected.bus,tuple(selected.port_numbers or ()),selected.address)
        transport=UsbTransport(selected,util,identity,_owner_key=_OWNER_KEY)
        return transport
    except Exception as error:
        selected=None
        raise _error(error) from error
    finally:
        # Discovery may have opened serial descriptor handles of nonselected
        # devices. Each was created by find here and remains claim-free.
        for dev in candidates:
            if dev is not selected:
                try: util.dispose_resources(dev)
                except Exception: _LOG.exception('Failed to dispose exclusive discovery context')

@dataclass(frozen=True)
class CompletedSnapshot:
    identity: p.SnapshotIdentity
    info: p.Info
    body: bytes
    summary: p.BodySummary


class SnapshotClient:
    """Finite synchronous lease acquisition; caller owns polling/lifecycle policy."""
    def __init__(self, transport, *, connection_id: int, timeout_ms: int = 250):
        if type(connection_id) is not int or connection_id<=0: raise ValueError('positive connection_id required')
        self.transport=transport; self.connection_id=connection_id; self.timeout_ms=_timeout(timeout_ms)
        self._last_cookie=0

    def _exchange(self, request, *, ceiling=64, timeout_ms=None):
        raw=self.transport.exchange(request,timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms)
        try: return p.parse_response(raw,request=request,max_response=ceiling)
        except p.IncompatibleProtocol as error: raise TransportError('unsupported',str(error)) from error
        except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error

    def info(self) -> tuple[p.Response,p.Info]:
        response=self._exchange(p.info_request())
        if response.header.status!=p.Status.OK: self._status(response)
        try: return response,p.parse_info(response)
        except p.IncompatibleProtocol as error: raise TransportError('unsupported',str(error)) from error
        except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error

    @staticmethod
    def _status(response):
        status=response.header.status
        code={p.Status.BUSY:'busy',p.Status.NOT_READY:'not_ready',p.Status.STALE:'not_ready',
              p.Status.BAD_REQUEST:'protocol_error',p.Status.PENDING:'not_ready'}.get(status,'protocol_error')
        raise TransportError(code,f'Mirror {response.header.kind.name} returned {status.name}')

    def snapshot(self, response: p.Response, info: p.Info, *,
                 stop_event: threading.Event | None = None) -> CompletedSnapshot:
        if response.header.kind!=p.Operation.INFO or response.header.status!=p.Status.OK:
            raise TransportError('protocol_error','Valid INFO required before acquisition')
        if response.header.flags & p.CAPABILITY_MASK != info.capabilities:
            raise TransportError('protocol_error','INFO capability mismatch')
        deadline=time.monotonic()+min(info.lease_ms,_MAX_ACQUISITION_MS)/1000
        token=None; unavailable=False; primary=None; pending_token=None
        # Fresh nonzero cookie for each new lease; same cookie only while pending.
        cookie=secrets.randbits(32) or 1
        if cookie==self._last_cookie: cookie=cookie%0xffffffff+1
        self._last_cookie=cookie
        request=p.begin_request(cookie)
        def checked_exchange(req):
            if stop_event is not None and stop_event.is_set(): raise TransportError('closed','Snapshot cancelled')
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TransportError('timeout','Snapshot acquisition deadline exceeded')
            return self._exchange(req,ceiling=info.max_response,
                                  timeout_ms=max(1,min(self.timeout_ms,int(remaining*1000))))
        try:
            for attempt in range(64):
                candidate=checked_exchange(request)
                h=candidate.header
                if h.epoch!=response.header.epoch:
                    token=None  # Never release an old token on a new session.
                    raise TransportError('protocol_error','Epoch changed during acquisition')
                if h.flags & p.CAPABILITY_MASK != info.capabilities:
                    raise TransportError('protocol_error','Capabilities changed during acquisition')
                if h.status==p.Status.PENDING:
                    if pending_token is not None and pending_token!=h.token:
                        raise TransportError('protocol_error','Pending token changed')
                    pending_token=token=h.token
                    pause=min(0.01,max(0,deadline-time.monotonic()))
                    if stop_event is not None: stop_event.wait(pause)
                    else: time.sleep(pause)
                    continue
                if h.status!=p.Status.OK: self._status(candidate)
                if pending_token is not None and pending_token!=h.token:
                    raise TransportError('protocol_error','Ready token differs from pending token')
                token=h.token
                try:
                    identity=p.ready_identity(candidate,connection_id=self.connection_id,info=info)
                    assembler=p.SnapshotAssembler(identity,info)
                except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error
                offset=0
                while offset<identity.total_length:
                    chunk=checked_exchange(p.read_request(token,offset,info.max_response))
                    if chunk.header.epoch!=identity.epoch:
                        token=None  # Epoch invalidates previous lease and token.
                        raise TransportError('protocol_error','Epoch changed during READ')
                    if chunk.header.status!=p.Status.OK: self._status(chunk)
                    try: assembler.add(chunk,requested_offset=offset)
                    except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error
                    offset+=chunk.header.payload_length
                if time.monotonic()>=deadline:
                    raise TransportError('timeout','Snapshot completed after acquisition deadline')
                if stop_event is not None and stop_event.is_set():
                    raise TransportError('closed','Snapshot cancelled after final READ')
                try: body,summary=assembler.finish()
                except p.ProtocolError as error: raise TransportError('protocol_error',str(error)) from error
                return CompletedSnapshot(identity,info,body,summary)
            raise TransportError('timeout','Pending acquisition retry limit exceeded')
        except BaseException as error:
            primary=error
            unavailable=isinstance(error,TransportError) and error.code in ('disconnected','closed')
            # Cancellation retains a live handle for bounded release; an actual
            # CLOSED transport must itself refuse any late exchange.
            if isinstance(error,TransportError) and error.code=='closed' and stop_event is not None and stop_event.is_set():
                unavailable=False
            raise
        finally:
            if token is not None and not unavailable:
                try:
                    released=self._exchange(p.release_request(token),ceiling=info.max_response)
                    if released.header.epoch!=response.header.epoch:
                        raise TransportError('protocol_error','Epoch changed during release')
                    if released.header.status not in (p.Status.OK,p.Status.STALE): self._status(released)
                except Exception:
                    if primary is None: raise
                    _LOG.debug('Best-effort bounded RELEASE failed after acquisition error',exc_info=True)
