"""Independent wire vectors for the provisional mirror protocol (no firmware bytes)."""
import dataclasses
import copy
import pathlib
import struct
import unittest

from tools.panel import usb_mirror_protocol as p

ROOT = pathlib.Path(__file__).resolve().parents[3]
FLAGS = 0x110031

def body():
    return b''.join(bytes((0x10 | page, col)) + bytes((page, col, 1, 2, 3, 4, 5, 6))
                    for page in range(8) for col in range(0, 128, 8))

def wire(kind=1, status=0, epoch=7, generation=9, token=0, total=0,
         offset=0, payload=b'', crc=0, flags=FLAGS):
    return struct.pack('>4sBBBBIIHHHHII', b'OTPM', 1, 0, status, kind,
                       epoch, generation, token, total, offset, len(payload), crc, flags) + payload

def info_wire(flags=FLAGS, **changes):
    fields = dict(schema=1, model=239, width=128, height=64, pages=8, columns=8,
                  ceiling=64, maximum=1858, poll=200, valid=128, lease=1000, rate=10,
                  n=3, build=b'abc' + bytes(8))
    fields.update(changes)
    return wire(payload=struct.pack('>BBHHBBHHHHHHB11s', *fields.values()), flags=flags)

def info(flags=FLAGS):
    return p.parse_info(p.parse_response(info_wire(flags), request=p.info_request()))

def identity(data=None, flags=FLAGS):
    data = body() if data is None else data
    r = p.parse_response(wire(kind=2, token=5, total=len(data), crc=p.crc32(data), flags=flags),
                         request=p.begin_request(0x10203040))
    return p.ready_identity(r, connection_id=11, info=info(flags & ~0xe0000))

class ProtocolTests(unittest.TestCase):
    def test_layout_vectors_and_generated_definition(self):
        self.assertEqual(struct.calcsize(p.HEADER_STRUCT), 32)
        self.assertEqual(struct.calcsize(p.INFO_STRUCT), 32)
        r = p.parse_response(info_wire(), request=p.info_request())
        self.assertEqual((r.header.epoch, r.header.generation, r.header.payload_length), (7,9,32))
        self.assertEqual(p.parse_info(r).model, 239)
        self.assertEqual((ROOT/'modules/usb-panel-mirror/protocol.inc').read_text(), p.generate_assembly_include())
        self.assertEqual(p.info_request(), p.SetupRequest(0xc0,0x57,0,0,64))
        self.assertEqual(p.begin_request(0x10203040), p.SetupRequest(0xc0,0x58,0x3040,0x1020,32))
        with self.assertRaises(dataclasses.FrozenInstanceError): r.header.epoch = 2

    def test_definition_rejects_same_size_field_offset_swaps(self):
        for section, first, second in (('header_offsets','token','total_length'),
                                        ('header_offsets','epoch','generation'),
                                        ('info_offsets','width','height'),
                                        ('info_offsets','lease_ms','max_publications_hz')):
            definition=copy.deepcopy(p.DEFINITION)
            fields=definition[section]
            fields[first],fields[second]=fields[second],fields[first]
            with self.subTest(section=section,first=first),self.assertRaises(p.ProtocolError):
                p.validate_definition(definition)
        for section in ('header','info'):
            definition=copy.deepcopy(p.DEFINITION)
            first=next(iter(definition[section+'_widths']))
            definition[section+'_widths'][first]+=1
            with self.assertRaises(p.ProtocolError): p.validate_definition(definition)
        definition=copy.deepcopy(p.DEFINITION)
        definition['header_struct']=definition['header_struct'].replace('H','2B',1)
        with self.assertRaises(p.ProtocolError): p.validate_definition(definition)
        p.validate_definition(p.DEFINITION)

    def test_crc_vectors(self):
        for data, crc in ((b'',0),(b'123456789',0xcbf43926),(bytes(range(256)),0x29058c73)):
            self.assertEqual(p.crc32(data),crc)

    def test_setup_bounds(self):
        for length in (0,31,257,-1,65536):
            with self.assertRaises(p.ProtocolError): p.begin_request(1,length)
        for cookie in (0,-1,0x100000000):
            with self.assertRaises(p.ProtocolError): p.begin_request(cookie)
        for token in (0,-1,65536):
            with self.assertRaises(p.ProtocolError): p.read_request(token,0,64)
        for offset in (-1,65536):
            with self.assertRaises(p.ProtocolError): p.read_request(1,offset,64)
        for length in (32,33,63,64,65,128,256):
            self.assertEqual(p.begin_request(1,length).length,length)
        for length in (32,33,63):
            with self.assertRaises(p.ProtocolError): p.info_request(length)
        with self.assertRaises(p.ProtocolError):
            p.parse_response(info_wire(),request=p.SetupRequest(0x40,0x57,0,0,64))

    def test_every_truncation_trailing_and_bad_header(self):
        raw = info_wire()
        for n in range(64):
            with self.subTest(n=n), self.assertRaises(p.ProtocolError):
                p.parse_info(p.parse_response(raw[:n],request=p.info_request()))
        mutations = [(0,0),(4,2),(5,1),(6,6),(7,2),(8,0),(31,0x40)]
        for index, value in mutations:
            bad=bytearray(raw); bad[index]=value
            if index==8: bad[8:12]=bytes(4)
            with self.subTest(index=index), self.assertRaises(p.ProtocolError):
                p.parse_response(bad,request=p.info_request())
        with self.assertRaises(p.ProtocolError): p.parse_response(raw+b'x',request=p.info_request())

    def test_info_metadata_rejection(self):
        for changes in (dict(schema=2),dict(width=127),dict(height=63),dict(pages=7),dict(columns=4),
                        dict(ceiling=65),dict(maximum=1279),dict(maximum=1859),dict(poll=0),
                        dict(valid=127),dict(lease=0),dict(rate=0),dict(n=12),
                        dict(build=b'ab\x00'+bytes(8)),dict(build=b'abcx'+bytes(7))):
            with self.subTest(changes=changes), self.assertRaises(p.ProtocolError):
                p.parse_info(p.parse_response(info_wire(**changes),request=p.info_request()))
        for flags in (FLAGS|0x20000, FLAGS|0x40000, FLAGS|0x80000, FLAGS|0x40):
            with self.assertRaises(p.ProtocolError): p.parse_response(info_wire(flags),request=p.info_request())

    def test_operation_specific_status_fields(self):
        for kind, req in ((2,p.begin_request(1)),(3,p.read_request(5,0,64)),(4,p.release_request(5))):
            for status in (2,3,4,5):
                p.parse_response(wire(kind=kind,status=status),request=req)
                with self.assertRaises(p.ProtocolError):
                    p.parse_response(wire(kind=kind,status=status,token=5),request=req)
        pending = wire(kind=2,status=1,token=5,generation=0)
        p.parse_response(pending,request=p.begin_request(1))
        with self.assertRaises(p.ProtocolError):
            p.parse_response(wire(kind=2,status=1,token=5),request=p.begin_request(1))
        p.parse_response(wire(kind=4,token=5),request=p.release_request(5))
        with self.assertRaises(p.ProtocolError):
            p.parse_response(wire(kind=4,token=6),request=p.release_request(5))

    def test_canonical_full_and_optional_order(self):
        full=body()+b''.join(bytes((0x20+i if i<16 else 0xa0+i-16,i)) for i in range(32))
        full+=b''.join(bytes((0x30+(i%16),i)) for i in range(256))+b'\xb7\x00'
        self.assertEqual(len(full),1858)
        ident=identity(full,FLAGS|0xe000e)
        summary=p.validate_snapshot(full,identity=ident,info=info(FLAGS|0xe))
        self.assertEqual((summary.lcd_blocks,summary.led_rows,summary.led_ids,summary.backlight_known),
                         (128,tuple(range(32)),tuple(range(256)),True))
        self.assertEqual(p.validate_snapshot(body(),identity=identity(),info=info()).led_rows,())

    def test_canonical_rejects_bad_lcd_and_history(self):
        data=body()
        invalid=[data[:-10],data+data[:10],data[10:20]+data[:10]+data[20:],
                 bytes((0x10,1))+data[2:],data+b'\x43\x00',data+b'\xb5\x01',data+b'\xff',
                 data+b'\x30\x01\x20\x00',data+b'\xb7\x00\xb7\x01',
                 data+b'\x30\x01\x31\x01',data+b'\xa0\x01\x2f\x01']
        for bad in invalid:
            with self.subTest(length=len(bad)),self.assertRaises(p.ProtocolError):
                p.validate_snapshot(bad,identity=identity(bad,FLAGS|0xe000e),info=info(FLAGS|0xe))
        for block in range(128):
            bad=data[:block*10]+data[(block+1)*10:]+data[:10]
            with self.assertRaises(p.ProtocolError): p.validate_snapshot(bad,identity=identity(bad),info=info())

    def test_fragment_coverage_and_identity(self):
        data=body(); ident=identity(); a=p.SnapshotAssembler(ident,info())
        def chunk(offset,size=17,**changes):
            args=dict(kind=3,token=5,total=len(data),crc=p.crc32(data),offset=offset,payload=data[offset:offset+size])
            args.update(changes)
            return p.parse_response(wire(**args),request=p.read_request(5,offset,32+len(args["payload"])))
        with self.assertRaises(p.ProtocolError): a.finish()
        a.add(chunk(0),requested_offset=0); a.add(chunk(0),requested_offset=0)
        for offset in range(0,len(data),17): a.add(chunk(offset),requested_offset=offset)
        self.assertTrue(a.complete); self.assertEqual(a.finish()[0],data)
        for key,value in (('epoch',8),('generation',10),('token',6),('flags',FLAGS|2),('crc',1),('total',1282)):
            with self.assertRaises(p.ProtocolError): a.add(chunk(0,**{key:value}),requested_offset=0)
        with self.assertRaises(p.ProtocolError): a.add(chunk(0),requested_offset=1)
        with self.assertRaises(p.ProtocolError): a.add(chunk(0,payload=bytes(17)),requested_offset=0)
        bad=dataclasses.replace(ident,crc32=1); a=p.SnapshotAssembler(bad,info())
        r=p.Response(p.Header(1,0,p.Status.OK,p.Operation.READ,7,9,5,1280,0,1280,1,FLAGS),data)
        a.add(r,requested_offset=0)
        with self.assertRaises(p.ProtocolError): a.finish()

if __name__=='__main__': unittest.main()
