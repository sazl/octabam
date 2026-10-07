"""Strict, stdlib-only host implementation of the PROVISIONAL OTPM v1 wire contract.

The definition and generated include are design artifacts; they do not establish
firmware availability, model identity, DMA safety or audio coexistence.
"""
from dataclasses import dataclass, fields
from enum import IntEnum
import json
import re
from pathlib import Path
import struct
import zlib

_DEFINITION_PATH = Path(__file__).resolve().parents[2] / 'modules/usb-panel-mirror/protocol.json'
DEFINITION = json.loads(_DEFINITION_PATH.read_text())
globals().update(DEFINITION['constants'])
globals().update(DEFINITION['flags'])
MAGIC = DEFINITION['magic'].encode('ascii')
HEADER_STRUCT = DEFINITION['header_struct']
INFO_STRUCT = DEFINITION['info_struct']
SUPPORTED_RESPONSE_SIZES = tuple(DEFINITION['supported_response_sizes'])
Operation = IntEnum('Operation', DEFINITION['operations'])
Status = IntEnum('Status', DEFINITION['statuses'])
Model = IntEnum('Model', DEFINITION['models'])
assert struct.calcsize(HEADER_STRUCT) == HEADER_SIZE
assert struct.calcsize(INFO_STRUCT) == INFO_SIZE
_REQUESTS = {op: DEFINITION['constants']['REQUEST_'+op.name] for op in Operation}
_BODY = DEFINITION['body']

class ProtocolError(ValueError):
    """Malformed setup, response, identity or canonical body."""

class IncompatibleProtocol(ProtocolError):
    """Version, geometry or required capabilities are unsupported."""

@dataclass(frozen=True)
class SetupRequest:
    bm_request_type: int
    request: int
    value: int
    index: int
    length: int

@dataclass(frozen=True)
class Header:
    major: int
    minor: int
    status: Status
    kind: Operation
    epoch: int
    generation: int
    token: int
    total_length: int
    offset: int
    payload_length: int
    crc32: int
    flags: int

@dataclass(frozen=True)
class Response:
    header: Header
    payload: bytes

@dataclass(frozen=True)
class Info:
    schema: int
    model: int
    width: int
    height: int
    pages: int
    block_columns: int
    max_response: int
    max_snapshot: int
    min_poll_ms: int
    valid_lcd_blocks: int
    lease_ms: int
    max_publications_hz: int
    build_id: str
    capabilities: int

@dataclass(frozen=True)
class SnapshotIdentity:
    connection_id: int
    epoch: int
    token: int
    generation: int
    total_length: int
    crc32: int
    frozen_flags: int

@dataclass(frozen=True)
class BodySummary:
    lcd_blocks: int
    led_rows: tuple[int, ...]
    led_ids: tuple[int, ...]
    backlight_known: bool


def validate_definition(definition) -> None:
    """Check every assembly offset/width against the actual host struct layout.

    Names follow the public dataclass API, not JSON object insertion order. This
    detects equal-width field swaps even after protocol.inc has been regenerated.
    """
    header_names=('magic',)+tuple(field.name for field in fields(Header))
    info_names=tuple(field.name for field in fields(Info))[:-2]+('build_id_length','build_id')
    for section,names in (('header',header_names),('info',info_names)):
        fmt=definition[section+'_struct']
        if not fmt.startswith('>'):
            raise ProtocolError('wire layout must be big-endian')
        tokens=re.findall(r'[0-9]*[sBHI]',fmt[1:])
        if ''.join(tokens)!=fmt[1:] or len(tokens)!=len(names):
            raise ProtocolError('wire layout field count/type mismatch')
        offset=0; offsets={}; widths={}
        for name,token in zip(names,tokens):
            offsets[name]=offset; widths[name]=struct.calcsize('>'+token)
            offset+=widths[name]
        if offsets!=definition[section+'_offsets'] or widths!=definition[section+'_widths']:
            raise ProtocolError(f'{section} assembly offset/width differs from host struct')
        if offset!=definition['constants'][section.upper()+'_SIZE']:
            raise ProtocolError(f'{section} size differs from field layout')


validate_definition(DEFINITION)


def generate_assembly_include() -> str:
    """Deterministic GNU-as constants, always derived from protocol.json."""
    validate_definition(DEFINITION)
    lines = ['/* Generated from protocol.json; PROVISIONAL: not deployed firmware. */']
    for section, prefix in (('constants',''),('flags',''),('statuses','STATUS_'),
                            ('operations','KIND_'),('models','MODEL_'),
                            ('header_offsets','HEADER_OFFSET_'),('info_offsets','INFO_OFFSET_'),
                            ('header_widths','HEADER_WIDTH_'),('info_widths','INFO_WIDTH_')):
        for key, value in DEFINITION[section].items():
            lines.append(f'.equ OTPM_{prefix}{key.upper()}, 0x{value:x}')
    lines.append(f'.equ OTPM_MAGIC, 0x{int.from_bytes(MAGIC, "big"):x}')
    for key, value in _BODY.items():
        if isinstance(value,int): lines.append(f'.equ OTPM_{key.upper()}, 0x{value:x}')
    return '\n'.join(lines)+'\n'


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError(f'{name} outside {low}..{high}')


def validate_request(request: SetupRequest, *, max_response: int = 256) -> Operation:
    """Host allowlist: only valid mirror requests, never firmware fuzz inputs."""
    if max_response not in SUPPORTED_RESPONSE_SIZES:
        raise ProtocolError('invalid negotiated response ceiling')
    if request.bm_request_type != BM_REQUEST_TYPE:
        raise ProtocolError('mirror requires device vendor IN 0xc0')
    try: op = next(op for op, number in _REQUESTS.items() if number == request.request)
    except StopIteration: raise ProtocolError('unallocated mirror request') from None
    _integer(request.value,0,65535,'wValue'); _integer(request.index,0,65535,'wIndex')
    _integer(request.length,HEADER_SIZE,max_response,'wLength')
    if op == Operation.INFO and (request.value or request.index or request.length < BOOTSTRAP_RESPONSE_SIZE):
        raise ProtocolError('invalid INFO setup')
    if op == Operation.BEGIN and not (request.value or request.index):
        raise ProtocolError('zero acquisition cookie')
    if op in (Operation.READ,Operation.RELEASE) and not request.value:
        raise ProtocolError('zero token')
    if op == Operation.RELEASE and request.index:
        raise ProtocolError('nonzero RELEASE index')
    return op


def _request(op, value, index, length):
    result=SetupRequest(BM_REQUEST_TYPE,_REQUESTS[op],value,index,length)
    validate_request(result)
    return result


def info_request(length: int = 64) -> SetupRequest:
    return _request(Operation.INFO,0,0,length)


def begin_request(cookie: int, length: int = 32) -> SetupRequest:
    _integer(cookie,1,0xffffffff,'cookie')
    return _request(Operation.BEGIN,cookie & 65535,cookie >> 16,length)


def read_request(token: int, offset: int, length: int) -> SetupRequest:
    return _request(Operation.READ,token,offset,length)


def release_request(token: int, length: int = 32) -> SetupRequest:
    return _request(Operation.RELEASE,token,0,length)


def _flags(flags):
    if flags & ~KNOWN_FLAGS: raise ProtocolError('undefined flags')
    if flags & REQUIRED_CAPABILITIES != REQUIRED_CAPABILITIES:
        raise IncompatibleProtocol('required LCD/lease/CRC capability missing')
    for known, support in ((ROW_KNOWN,LED_ROWS_SUPPORTED),(LEVEL_KNOWN,LED_LEVELS_SUPPORTED),
                           (BACKLIGHT_KNOWN,BACKLIGHT_SUPPORTED)):
        if flags & known and not flags & support: raise ProtocolError('known state without support')


def parse_response(raw: bytes, *, request: SetupRequest, max_response: int = 64) -> Response:
    op=validate_request(request,max_response=max_response)
    raw=bytes(raw)
    if len(raw)<HEADER_SIZE: raise ProtocolError('truncated header')
    values=struct.unpack(HEADER_STRUCT,raw[:HEADER_SIZE])
    if values[0]!=MAGIC: raise IncompatibleProtocol('wrong mirror magic')
    if values[1:3]!=(PROTOCOL_MAJOR,PROTOCOL_MINOR):
        raise IncompatibleProtocol('unsupported protocol version')
    try: h=Header(*values[1:3],Status(values[3]),Operation(values[4]),*values[5:])
    except ValueError: raise ProtocolError('unknown status or response kind') from None
    if h.kind!=op: raise ProtocolError('request/response kind mismatch')
    if not h.epoch: raise ProtocolError('zero session epoch')
    _flags(h.flags)
    if len(raw)!=HEADER_SIZE+h.payload_length or len(raw)>request.length or len(raw)>max_response:
        raise ProtocolError('actual response length mismatch')
    descriptor=(h.total_length,h.offset,h.payload_length,h.crc32)
    if h.status in (Status.BUSY,Status.NOT_READY,Status.STALE,Status.BAD_REQUEST):
        if h.token or any(descriptor): raise ProtocolError('error response leaks descriptor')
    elif h.status==Status.PENDING:
        if op not in (Operation.BEGIN,Operation.READ) or not h.token or h.generation or any(descriptor):
            raise ProtocolError('invalid pending descriptor')
        if op==Operation.READ and h.token!=request.value: raise ProtocolError('wrong pending token')
    elif op==Operation.INFO:
        if h.token or h.total_length or h.offset or h.crc32 or h.payload_length!=INFO_SIZE:
            raise ProtocolError('invalid INFO descriptor')
    elif op==Operation.RELEASE:
        if h.token!=request.value or any(descriptor): raise ProtocolError('invalid RELEASE descriptor')
    else:
        if not h.token or not LCD_WIRE_BYTES<=h.total_length<=MAX_SCHEMA_BODY_SIZE or h.total_length%2:
            raise ProtocolError('invalid ready descriptor')
        if h.flags & (LCD_COMPLETE|OBSERVER_ACTIVE)!=(LCD_COMPLETE|OBSERVER_ACTIVE):
            raise ProtocolError('ready snapshot lacks complete/active state')
        if op==Operation.BEGIN and (h.offset or h.payload_length): raise ProtocolError('BEGIN contains body')
        if op==Operation.READ:
            if h.token!=request.value or h.offset!=request.index or h.offset+h.payload_length>h.total_length:
                raise ProtocolError('READ offset/token/length mismatch')
            expected=min(request.length-HEADER_SIZE,h.total_length-h.offset)
            if h.payload_length!=expected or (h.offset<h.total_length and not h.payload_length):
                raise ProtocolError('READ does not make expected progress')
    return Response(h,raw[HEADER_SIZE:])


def parse_info(response: Response) -> Info:
    h=response.header
    if h.kind!=Operation.INFO or h.status!=Status.OK or len(response.payload)!=INFO_SIZE:
        raise ProtocolError('INFO OK metadata required')
    values=struct.unpack(INFO_STRUCT,response.payload)
    schema,model,width,height,pages,columns,ceiling,maximum,poll,valid,lease,rate,n,build=values
    if (schema,width,height,pages,columns)!=(BODY_SCHEMA,LCD_WIDTH,LCD_HEIGHT,LCD_PAGES,LCD_BLOCK_COLUMNS):
        raise IncompatibleProtocol('unsupported body schema or geometry')
    if ceiling not in SUPPORTED_RESPONSE_SIZES or not LCD_WIRE_BYTES<=maximum<=MAX_SCHEMA_BODY_SIZE:
        raise IncompatibleProtocol('unsupported response/body bound')
    if not poll or not lease or not rate or valid>LCD_BLOCK_COUNT:
        raise ProtocolError('invalid INFO limits')
    if bool(h.flags & LCD_COMPLETE)!=(valid==LCD_BLOCK_COUNT): raise ProtocolError('LCD validity contradiction')
    if n>len(build) or any(build[n:]) or any(not 32<=b<=126 for b in build[:n]):
        raise ProtocolError('invalid ASCII build prefix or padding')
    _flags(h.flags)
    return Info(*values[:12],build[:n].decode('ascii'),h.flags & CAPABILITY_MASK)


def ready_identity(response: Response, *, connection_id: int, info: Info) -> SnapshotIdentity:
    h=response.header
    _integer(connection_id,1,0xffffffffffffffff,'connection incarnation')
    if h.kind not in (Operation.BEGIN,Operation.READ) or h.status!=Status.OK or not h.token:
        raise ProtocolError('ready lease required')
    if h.flags & CAPABILITY_MASK != info.capabilities: raise ProtocolError('capabilities changed within epoch')
    if h.flags & (LCD_COMPLETE|OBSERVER_ACTIVE)!=(LCD_COMPLETE|OBSERVER_ACTIVE):
        raise ProtocolError('snapshot not ready')
    if not LCD_WIRE_BYTES<=h.total_length<=min(info.max_snapshot,MAX_SCHEMA_BODY_SIZE) or h.total_length%2:
        raise ProtocolError('body length outside negotiated bound')
    return SnapshotIdentity(connection_id,h.epoch,h.token,h.generation,h.total_length,h.crc32,h.flags)


def crc32(body: bytes) -> int:
    return zlib.crc32(body) & 0xffffffff


def validate_snapshot(body: bytes, *, identity: SnapshotIdentity, info: Info) -> BodySummary:
    body=bytes(body)
    if len(body)!=identity.total_length or len(body)%2 or not LCD_WIRE_BYTES<=len(body)<=min(info.max_snapshot,MAX_SCHEMA_BODY_SIZE):
        raise ProtocolError('snapshot body length invalid')
    _flags(identity.frozen_flags)
    if identity.frozen_flags & CAPABILITY_MASK != info.capabilities:
        raise ProtocolError('snapshot capabilities changed')
    if identity.frozen_flags & (LCD_COMPLETE|OBSERVER_ACTIVE)!=(LCD_COMPLETE|OBSERVER_ACTIVE):
        raise ProtocolError('snapshot lacks ready state')
    if crc32(body)!=identity.crc32: raise ProtocolError('snapshot CRC mismatch')
    pos=0
    for page in range(LCD_PAGES):
        for col in range(0,LCD_WIDTH,LCD_BLOCK_COLUMNS):
            if body[pos:pos+2]!=bytes((_BODY['lcd_opcode_base']|page,col)):
                raise ProtocolError('noncanonical LCD block order/column')
            pos+=LCD_BLOCK_COLUMNS+2
    rows=[]; ids=[]; backlight=False
    while pos<len(body):
        opcode,value=body[pos:pos+2]
        if _BODY['row_low_base']<=opcode<_BODY['row_low_base']+16: row=opcode-_BODY['row_low_base']
        elif _BODY['row_high_base']<=opcode<_BODY['row_high_base']+16: row=opcode-_BODY['row_high_base']+16
        else: break
        if rows and row<=rows[-1]: raise ProtocolError('row order/duplicate')
        rows.append(row); pos+=2
    while pos<len(body) and _BODY['level_base']<=body[pos]<_BODY['level_base']+16:
        led_id=body[pos+1]
        if ids and led_id<=ids[-1]: raise ProtocolError('LED ID order/duplicate')
        ids.append(led_id); pos+=2
    if pos<len(body) and body[pos]==_BODY['backlight_opcode']:
        backlight=True; pos+=2
    if pos!=len(body): raise ProtocolError('noncanonical trailing command/history')
    for present,known,support in ((bool(rows),ROW_KNOWN,LED_ROWS_SUPPORTED),
                                 (bool(ids),LEVEL_KNOWN,LED_LEVELS_SUPPORTED),
                                 (backlight,BACKLIGHT_KNOWN,BACKLIGHT_SUPPORTED)):
        if present!=bool(identity.frozen_flags & known) or (present and not identity.frozen_flags & support):
            raise ProtocolError('optional state/flags mismatch')
    return BodySummary(LCD_BLOCK_COUNT,tuple(rows),tuple(ids),backlight)


class SnapshotAssembler:
    """Bounded exact coverage; completion is certified only by finish()."""
    def __init__(self, identity: SnapshotIdentity, info: Info):
        if not LCD_WIRE_BYTES<=identity.total_length<=min(info.max_snapshot,MAX_SCHEMA_BODY_SIZE) or identity.total_length%2:
            raise ProtocolError('snapshot bound invalid before allocation')
        self.identity=identity; self.info=info
        self._body=bytearray(identity.total_length); self._coverage=bytearray(identity.total_length)

    def add(self, response: Response, *, requested_offset: int) -> None:
        h=response.header; expected=self.identity
        if h.kind!=Operation.READ or h.status!=Status.OK:
            raise ProtocolError('only READ OK may enter assembly')
        actual=SnapshotIdentity(expected.connection_id,h.epoch,h.token,h.generation,h.total_length,h.crc32,h.flags)
        if actual!=expected or h.offset!=requested_offset or h.payload_length!=len(response.payload):
            raise ProtocolError('mixed snapshot identity/offset/length')
        if h.offset<0 or h.offset+h.payload_length>len(self._body): raise ProtocolError('chunk out of bounds')
        for pos,value in enumerate(response.payload,h.offset):
            if self._coverage[pos] and self._body[pos]!=value: raise ProtocolError('inconsistent overlap')
        self._body[h.offset:h.offset+h.payload_length]=response.payload
        self._coverage[h.offset:h.offset+h.payload_length]=bytes([1])*h.payload_length

    @property
    def complete(self) -> bool:
        return all(self._coverage)

    def finish(self) -> tuple[bytes,BodySummary]:
        if not self.complete: raise ProtocolError('snapshot coverage incomplete')
        body=bytes(self._body)
        return body,validate_snapshot(body,identity=self.identity,info=self.info)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-include',action='store_true')
    args=parser.parse_args()
    output=_DEFINITION_PATH.with_suffix('.inc')
    generated=generate_assembly_include()
    if args.write_include: output.write_text(generated)
    elif output.read_text()!=generated: raise SystemExit('protocol.inc differs from authoritative protocol.json')
