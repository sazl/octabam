"""Transport allowlist, exclusive cleanup and bounded snapshot failure behavior."""
import dataclasses
import contextlib
import io
import json
import tempfile
from pathlib import Path
import errno
import importlib.util
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import patch

from tools.panel import usb_mirror as m
from tools.panel import usb_mirror_protocol as p
from tools.verify.tests.test_usb_panel_protocol import body, wire, info_wire, FLAGS

class FakeDevice:
    bus=1; port_numbers=(2,3); address=4; iSerialNumber=1
    def __init__(self,serial='unit-A',failure=None):
        self.serial=serial; self.failure=failure; self.calls=[]
    def ctrl_transfer(self,bm,req,value,index,length,timeout):
        self.calls.append((bm,req,value,index,length,timeout))
        if self.failure: raise self.failure
        if bm==0x80:
            assert req==6 and value>>8==3 and length<=255 and timeout>0
            if value==0x300: return b'\x04\x03\x09\x04'
            payload=self.serial.encode('utf-16-le')
            return bytes((len(payload)+2,3))+payload
        assert bm==0xc0 and req in (0x57,0x58,0x59,0x5a)
        if req==0x57: return info_wire()
        return wire(kind=4,token=value)
    def __getattr__(self,name):
        if name in ('reset','set_configuration','set_interface_altsetting','detach_kernel_driver','write','read','clear_halt'):
            raise AssertionError('forbidden method '+name)
        raise AttributeError(name)

class FakeCore:
    def __init__(self,devices): self.devices=devices
    def find(self,**kw):
        assert kw['find_all'] and kw['idVendor']==0x1935 and kw['idProduct']==2
        return iter(self.devices)

class FakeUtil:
    def __init__(self): self.closed=[]
    def dispose_resources(self,dev): self.closed.append(dev)

class FakeTransport:
    identity=m.DeviceIdentity('unit-A',1,(2,3),4)
    def __init__(self,fail_at=None,pending=0,flags=FLAGS):
        self.requests=[]; self.fail_at=fail_at; self.pending=pending; self.flags=flags
    def exchange(self,req,*,timeout_ms=250):
        self.requests.append(req)
        if req.request==self.fail_at: raise m.TransportError('disconnected','unplugged')
        data=body()
        if req.request==0x57: return info_wire()
        if req.request==0x58:
            if self.pending:
                self.pending-=1
                return wire(kind=2,status=1,generation=0,token=5)
            return wire(kind=2,token=5,total=len(data),crc=p.crc32(data),flags=self.flags)
        if req.request==0x59:
            return wire(kind=3,token=5,total=len(data),offset=req.index,
                        payload=data[req.index:req.index+req.length-32],crc=p.crc32(data),flags=self.flags)
        if req.request==0x5a: return wire(kind=4,token=5)
        raise AssertionError('forbidden request')

class TransportTests(unittest.TestCase):
    def open(self,devices,selector=None,**kw):
        util=FakeUtil()
        with patch.object(m,'_load_usb',return_value=(FakeCore(devices),util,object())):
            transport=m.open_transport(m.DeviceSelector.parse(selector),**kw)
        return transport,util

    def test_transport_error_exposes_code_and_message(self):
        error=m.TransportError('timeout','finite transfer timed out')
        self.assertEqual(error.code,'timeout')
        self.assertEqual(error.message,'finite transfer timed out')
        self.assertEqual(str(error),error.message)

    def test_selector_and_initial_identity(self):
        self.assertEqual(m.DeviceSelector.parse('serial:unit-A').serial,'unit-A')
        self.assertEqual(m.DeviceSelector.parse('topology:1-2.3').port_numbers,(2,3))
        for selector in ('','serial:','topology:0-1','topology:1-0','topology:1-2.','unit-A'):
            with self.assertRaises(ValueError): m.DeviceSelector.parse(selector)
        dev=FakeDevice(); t,u=self.open([dev])
        self.assertEqual(t.identity.as_dict(),dict(serial='unit-A',bus=1,port_numbers=[2,3],address=4))
        raw=t.exchange(p.info_request()); self.assertEqual(raw,info_wire())
        t.close(); t.close(); self.assertEqual(u.closed,[dev])
        with self.assertRaises(m.TransportError) as cm: t.exchange(p.info_request())
        self.assertEqual(cm.exception.code,'closed')
        self.assertEqual([r[0] for r in dev.calls],[0x80,0x80,0xc0])

    def test_ambiguity_precedes_protocol_probe_and_selection(self):
        devices=[FakeDevice('A'),FakeDevice('B')]
        with self.assertRaises(m.TransportError) as cm: self.open(devices)
        self.assertEqual(cm.exception.code,'ambiguous_device')
        self.assertFalse(any(c[0]==0xc0 for d in devices for c in d.calls))
        t,u=self.open(devices,'serial:B'); self.assertEqual(t.identity.serial,'B'); t.close()
        with self.assertRaises(m.TransportError) as cm: self.open([FakeDevice('B')],expected_serial='A')
        self.assertEqual(cm.exception.code,'device_not_found')
        with self.assertRaises(m.TransportError) as cm: self.open([])
        self.assertEqual(cm.exception.code,'device_not_found')

    def test_missing_serial_and_discovery_failure(self):
        dev=FakeDevice(); dev.iSerialNumber=0
        t,u=self.open([dev]); self.assertIsNone(t.identity.serial); self.assertFalse(dev.calls); t.close()
        dev=FakeDevice(failure=PermissionError('permission'))
        with self.assertRaises(m.TransportError) as cm: self.open([dev])
        self.assertEqual(cm.exception.code,'permission_denied')

    def test_error_mapping_and_allowlist(self):
        t,u=self.open([FakeDevice()])
        for req in (p.SetupRequest(0x40,0x57,0,0,64),p.SetupRequest(0xc0,0x55,0,0,64)):
            with self.assertRaises(m.TransportError) as cm: t.exchange(req)
            self.assertEqual(cm.exception.code,'protocol_error')
        for error,code in ((PermissionError(), 'permission_denied'),
                           (OSError(errno.ETIMEDOUT,'timeout'),'timeout'),
                           (OSError(errno.ENODEV,'gone'),'disconnected'),
                           (OSError(errno.EPIPE,'stall'),'unsupported'),
                           (OSError(errno.EBUSY,'busy'),'busy'),
                           (RuntimeError('backend exploded'),'protocol_error')):
            t.close()
            t,u=self.open([FakeDevice()])
            t._device.failure=error
            with self.assertRaises(m.TransportError) as cm: t.exchange(p.info_request())
            self.assertEqual(cm.exception.code,code)
        t.close()

    def test_snapshot_pending_cookie_fragments_release(self):
        t=FakeTransport(pending=2); client=m.SnapshotClient(t,connection_id=3)
        r,i=client.info(); result=client.snapshot(r,i)
        self.assertEqual(result.body,body()); self.assertEqual(result.identity.connection_id,3)
        begins=[r for r in t.requests if r.request==0x58]
        self.assertEqual(len(begins),3); self.assertTrue(begins[0].value or begins[0].index)
        self.assertTrue(all(r==begins[0] for r in begins))
        reads=[r for r in t.requests if r.request==0x59]
        self.assertEqual([r.index for r in reads],list(range(0,1280,32)))
        self.assertEqual(t.requests[-1],p.release_request(5))
        client.snapshot(*client.info())
        newer=[r for r in t.requests if r.request==0x58][-1]
        self.assertNotEqual(newer,begins[0])

    def test_unplug_crc_and_stop_release_policy(self):
        for operation in (0x57,0x58,0x59,0x5a):
            t=FakeTransport(fail_at=operation); client=m.SnapshotClient(t,connection_id=3)
            with self.assertRaises(m.TransportError): client.snapshot(*client.info())
            if operation==0x59:
                self.assertNotEqual(t.requests[-1].request,0x5a)
        t=FakeTransport(); event=threading.Event(); event.set()
        client=m.SnapshotClient(t,connection_id=3)
        with self.assertRaises(m.TransportError): client.snapshot(*client.info(),stop_event=event)
        self.assertEqual([r.request for r in t.requests],[0x57])
        t=FakeTransport(pending=10000); client=m.SnapshotClient(t,connection_id=3)
        r,i=client.info(); i=dataclasses.replace(i,lease_ms=15)
        with self.assertRaises(m.TransportError) as cm: client.snapshot(r,i)
        self.assertEqual(cm.exception.code,'timeout'); self.assertLess(len(t.requests),20)
        self.assertEqual(t.requests[-1].request,0x5a)

    def test_cancel_during_final_read_releases_without_returning_candidate(self):
        event=threading.Event()
        class CancelTransport(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=super().exchange(req,**kwargs)
                if req.request==0x59 and req.index==1248: event.set()
                return raw
        transport=CancelTransport(); client=m.SnapshotClient(transport,connection_id=3)
        with self.assertRaises(m.TransportError) as cm:
            client.snapshot(*client.info(),stop_event=event)
        self.assertEqual(cm.exception.code,'closed')
        self.assertEqual(transport.requests[-1].request,0x5a)

    def test_cancel_during_release_does_not_return_candidate(self):
        event=threading.Event()
        class CancelTransport(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=super().exchange(req,**kwargs)
                if req.request==0x5a: event.set()
                return raw
        transport=CancelTransport(); client=m.SnapshotClient(transport,connection_id=3)
        with self.assertRaises(m.TransportError) as cm:
            client.snapshot(*client.info(),stop_event=event)
        self.assertEqual(cm.exception.code,'closed')
        self.assertEqual([req.request for req in transport.requests].count(0x5a),1)
        self.assertEqual(transport.requests[-1].request,0x5a)

    def test_late_final_response_is_not_published(self):
        clock=[10.0]
        class LateTransport(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=super().exchange(req,**kwargs)
                if req.request==0x59 and req.index==1248: clock[0]=12.0
                return raw
        transport=LateTransport(); client=m.SnapshotClient(transport,connection_id=3)
        with patch.object(m.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(m.TransportError) as cm: client.snapshot(*client.info())
        self.assertEqual(cm.exception.code,'timeout')
        self.assertEqual(transport.requests[-1].request,0x5a)

    def test_corrupt_candidate_is_released_and_busy_does_not_touch_other_lease(self):
        class Corrupt(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=bytearray(super().exchange(req,**kwargs))
                if req.request==0x59: raw[-1]^=1
                return raw
        transport=Corrupt(); client=m.SnapshotClient(transport,connection_id=3)
        with self.assertRaises(m.TransportError) as cm: client.snapshot(*client.info())
        self.assertEqual(cm.exception.code,'protocol_error')
        self.assertEqual(transport.requests[-1].request,0x5a)
        class Busy(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=super().exchange(req,**kwargs)
                if req.request==0x58: return wire(kind=2,status=2)
                return raw
        transport=Busy(); client=m.SnapshotClient(transport,connection_id=3)
        with self.assertRaises(m.TransportError) as cm: client.snapshot(*client.info())
        self.assertEqual(cm.exception.code,'busy')
        self.assertEqual([r.request for r in transport.requests],[0x57,0x58])

    def test_epoch_change_does_not_release_obsolete_token(self):
        class ChangedTransport(FakeTransport):
            def exchange(self,req,**kwargs):
                raw=bytearray(super().exchange(req,**kwargs))
                if req.request==0x59: raw[8:12]=(8).to_bytes(4,'big')
                return raw
        transport=ChangedTransport(); client=m.SnapshotClient(transport,connection_id=3)
        with self.assertRaises(m.TransportError): client.snapshot(*client.info())
        self.assertEqual(transport.requests[-1].request,0x59)

    def test_close_waits_for_inflight_transfer_and_prevents_reopen(self):
        entered=threading.Event(); finished=threading.Event()
        device=FakeDevice(); transport,util=self.open([device]); original=device.ctrl_transfer
        def blocked(*args,**kwargs):
            entered.set(); self.assertTrue(finished.wait(1))
            return original(*args,**kwargs)
        device.ctrl_transfer=blocked
        transfer=threading.Thread(target=lambda: transport.exchange(p.info_request()))
        transfer.start(); self.assertTrue(entered.wait(1))
        closer=threading.Thread(target=transport.close); closer.start()
        self.assertTrue(closer.is_alive()); self.assertEqual(util.closed,[])
        finished.set(); transfer.join(1); closer.join(1)
        self.assertFalse(transfer.is_alive()); self.assertFalse(closer.is_alive())
        self.assertEqual(util.closed,[device])

    @unittest.skipUnless(importlib.util.find_spec('usb'), 'needs optional PyUSB')
    def test_missing_libusb_and_unsupported_info(self):
        import usb.backend.libusb1
        with patch.object(usb.backend.libusb1,'get_backend',return_value=None):
            with self.assertRaises(m.TransportError) as cm: m._load_usb()
        self.assertEqual(cm.exception.code,'unavailable_libusb')
        with patch.object(usb.backend.libusb1,'get_backend',side_effect=OSError('wrong architecture')):
            with self.assertRaises(m.TransportError) as cm: m._load_usb()
        self.assertEqual(cm.exception.code,'unavailable_libusb')
        self.assertIn('architecture',str(cm.exception))
        class Unsupported(FakeTransport):
            def exchange(self,req,**kwargs): return b'NOPE'+bytes(60)
        with self.assertRaises(m.TransportError) as cm: m.SnapshotClient(Unsupported(),connection_id=1).info()
        self.assertEqual(cm.exception.code,'unsupported')

    def test_import_without_usb_and_missing_dependencies(self):
        program='import sys; sys.modules["usb"]=None; from tools.panel import usb_mirror; print("lazy")'
        result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        with patch.dict(sys.modules,{'usb':None}):
            with self.assertRaises(m.TransportError) as cm: m._load_usb()
        self.assertEqual(cm.exception.code,'unavailable_dependency')

@unittest.skipUnless(importlib.util.find_spec('usb'), 'needs optional PyUSB')
class RealPyUsbTests(unittest.TestCase):
    def test_real_library_claim_free_disposal_and_borrowed_context_regression(self):
        import usb.core
        import usb.util
        import usb.backend
        class Backend(usb.backend.IBackend):
            def __init__(self): self.events=[]; self.device=FakeDevice()
            def enumerate_devices(self): return [1]
            def get_device_descriptor(self,dev):
                return types.SimpleNamespace(bLength=18,bDescriptorType=1,bcdUSB=0x200,
                    bDeviceClass=0,bDeviceSubClass=0,bDeviceProtocol=0,bMaxPacketSize0=64,
                    idVendor=0x1935,idProduct=2,bcdDevice=1,iManufacturer=0,iProduct=0,
                    iSerialNumber=1,bNumConfigurations=1,address=4,bus=1,port_number=2,
                    port_numbers=(2,3),speed=2)
            def open_device(self,dev): self.events.append(('open',)); return dev
            def close_device(self,handle): self.events.append(('close',))
            def ctrl_transfer(self,handle,bm,req,value,index,data,timeout):
                self.events.append(('ctrl',bm,req,value,index,len(data),timeout))
                raw=self.device.ctrl_transfer(bm,req,value,index,len(data),timeout)
                data[:len(raw)]=__import__('array').array('B',raw)
                return len(raw)
            def claim_interface(self,handle,index): self.events.append(('claim',index))
            def release_interface(self,handle,index): self.events.append(('release',index))
            # Any configuration/reset/endpoint/alt/detach path uses IBackend's
            # NotImplementedError and fails this real-library integration test.
        backend=Backend()
        with patch.object(m,'_load_usb',return_value=(usb.core,usb.util,backend)):
            transport=m.open_transport(m.DeviceSelector.parse(None))
        device=transport._device
        self.assertEqual(device._ctx._claimed_intf,set())
        transport.exchange(p.info_request()); transport.close(); transport.close()
        self.assertEqual(backend.events,[('open',),('ctrl',0x80,6,0x300,0,255,250),
                    ('ctrl',0x80,6,0x301,0x409,255,250),('ctrl',0xc0,0x57,0,0,64,250),('close',)])
        with self.assertRaises(m.TransportError): transport.exchange(p.info_request())
        self.assertEqual(backend.events[-1],('close',))
        # PyUSB itself WOULD reopen after dispose, demonstrating CLOSED guard.
        device.ctrl_transfer(0xc0,0x57,0,0,64,timeout=250)
        self.assertEqual(backend.events[-2],('open',)); usb.util.dispose_resources(device)
        with self.assertRaises(m.TransportError): m.UsbTransport(device,usb.util,transport.identity)
        # Independent intentionally shared context demonstrates unsafe disposal:
        # it releases another client's claims, hence borrowing is prohibited.
        shared=usb.core.Device(1,backend); usb.util.claim_interface(shared,3)
        usb.util.dispose_resources(shared)
        self.assertEqual(backend.events[-4:],[('open',),('claim',3),('release',3),('close',)])

class ProbeTests(unittest.TestCase):
    def test_info_capture_watch_are_bounded_and_close(self):
        from tools.hw import usb_panel as probe
        for args in (['info'],['watch','--count','2']):
            t=FakeTransport(); t.close=lambda: None
            output=io.StringIO()
            with patch.object(probe.m,'open_transport',return_value=t), contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(args),0)
            lines=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(lines),1 if args[0]=='info' else 2)
            self.assertEqual(lines[0]['model_label'],'unknown (239)')
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'snapshot.dat'
            t=FakeTransport(); t.close=lambda: None
            with patch.object(probe.m,'open_transport',return_value=t), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(['capture','--output',str(destination)]),0)
            self.assertEqual(destination.read_bytes(),body())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): probe.main(['watch'])

if __name__=='__main__': unittest.main()
