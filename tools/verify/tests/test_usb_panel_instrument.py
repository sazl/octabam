"""Firmware-free falsification of the final-image verifier's own oracles."""
import os
import pathlib
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/verify'))
import verify_usb_panel as gate

LCD=b''.join(bytes([0x10|p,c])+bytes([p+1])*8 for p in range(8) for c in range(0,128,8))
GOOD=(0x18200000).to_bytes(4,'little')+(0x18210000).to_bytes(4,'little')

class Instrument(unittest.TestCase):
    def test_counter_progress_and_fs_short_build_baseline(self):
        names=('produced','consumed','underruns','overruns','bankdup','reprimes')
        zero=dict.fromkeys(names,0)
        warm=dict(zero,produced=1600,consumed=1550,underruns=100)
        active=dict(zero,produced=3200,consumed=3170,underruns=200)
        gate.check_counter_interval(zero,active,zero,warm,False)
        for changed in (dict(active,produced=0),dict(active,underruns=240),dict(active,overruns=1)):
            with self.assertRaises(AssertionError):gate.check_counter_interval(zero,changed,zero,warm,False)
        with self.assertRaises(AssertionError):gate.check_counter_interval(zero,active,zero,warm,True)

    def test_source_fixture_requires_proved_bypass_state(self):
        state=(struct.pack('>4I',0,0x7fffffff,0,0)+bytes(52))*8
        gate.check_audio_fixture(bytes([7])*16,state)
        for selectors,data in ((bytes(16),state),(bytes([7])*16,bytes(544))):
            with self.assertRaises(AssertionError):gate.check_audio_fixture(selectors,data)

    def test_streaming_service_fence_drains_both_response_orders(self):
        for replies in (['call 0','in 3 11223344'],['in 3 11223344','call 0','in 3 55667788']):
            bench=types.SimpleNamespace(sock=mock.Mock())
            bench.wait=mock.Mock(side_effect=replies)
            consumed=[]
            gate.stream_service(bench,consumed.append)
            self.assertEqual(len(consumed),len(replies)-1)
            self.assertEqual(bench.wait.call_count,len(replies))
        bad=types.SimpleNamespace(sock=mock.Mock(),wait=mock.Mock(return_value='call err never reached main'))
        with self.assertRaises(AssertionError):gate.stream_service(bad,lambda packet:None)

    def test_controls_hold_lease_and_supersede_deferred_out(self):
        events=[]
        class Bench:
            rate=None
            def setup(self,*args):events.append(('setup',args))
            def ep_out(self,ep,data):self.rate=int.from_bytes(data,'little');events.append(('out',self.rate));return len(data)
            def ep_in(self,*args):
                if self.rate==48000:raise gate.usb_host.Stall('intentional')
                return b''
            def call(self,*args,**kwargs):events.append(('barrier',args))
            def ctrl_in(self,bm,req,val,idx,n):
                events.append(('control',bm,req,val))
                return (44100).to_bytes(4,'little') if n==4 else (b'\x01' if n==1 else b'\x01\x00'+(44100).to_bytes(4,'little')*2+bytes(4))
        def request(op,*args,**kwargs):
            events.append(('mirror',op))
            return types.SimpleNamespace(header=types.SimpleNamespace(status=gate.wire.Status.PENDING,token=9))
        self.assertEqual(gate.uac2_during_lease(Bench(),request,17),1)
        self.assertEqual([x[1] for x in events if x[0]=='out'],[44100,48000,44100])
        barrier=next(i for i,x in enumerate(events) if x[0]=='barrier')
        self.assertEqual(events[barrier+1],('mirror',0x57))
        self.assertNotIn(('mirror',0x5a),events)

    def test_good_warmup_cannot_hide_bad_active_audio(self):
        for bad in (bytes(8),b'',bytes.fromhex('7856341278563412')):
            warm=gate.AudioPhase('warm',({0x18200000},{0x18210000}),settle=0)
            for _ in range(120):warm.feed(GOOD)
            warm.finish()
            active=gate.AudioPhase('active',({0x18200000},{0x18210000}),settle=0)
            with self.assertRaises(AssertionError):
                for _ in range(120):active.feed(bad)
                active.finish()
        # Even a long good prefix inside the active phase cannot hide later
        # sustained silence or an empty/wrong transfer.
        for bad in (bytes(8),b'',bytes.fromhex('7856341278563412')):
            active=gate.AudioPhase('active',({0x18200000},{0x18210000}),settle=0)
            for _ in range(120):active.feed(GOOD)
            with self.assertRaises(AssertionError):
                for _ in range(9):active.feed(bad)
    def test_valid_audio_is_counted_per_phase(self):
        phase=gate.AudioPhase('active',({0x18200000},{0x18210000}),settle=0)
        for _ in range(120):phase.feed(GOOD)
        self.assertEqual(phase.finish()['hits'],[120,120])
    def test_future_generation_and_incomplete_lcd_fail(self):
        with self.assertRaises(AssertionError):gate.uart_at_generation(LCD,128,129)
        with self.assertRaises(AssertionError):gate.uart_at_generation(LCD,128,0)
        with self.assertRaises(AssertionError):gate.uart_at_generation(b'\x21\x01'*128,128,128)
        with self.assertRaises(AssertionError):gate.uart_at_generation(LCD[:-1],128,128)
        self.assertEqual(gate.uart_at_generation(LCD,128,128).stats['lcd_blocks'],128)
    def test_matrix_restores_callers_build_on_success_and_error(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp);(root/'remixes').mkdir()
                selected=types.SimpleNamespace(modules=('USB MIDI',))
                cases=[dict(output='USB AUDIO OUT MAIN',input=None,modules=selected.modules)] if fail else []
                calls=[]
                def build(*args,**kwargs):
                    calls.append(kwargs['env'])
                    if fail and len(calls)==1:raise RuntimeError('deliberate build failure')
                args=types.SimpleNamespace(model='both',speed='both',matrix_start=0,remix='selected',jobs=1)
                with mock.patch.object(gate,'ROOT',root),mock.patch.object(gate,'matrix_selections',return_value=(cases,[])),mock.patch.object(gate.registry,'remix',return_value=selected),mock.patch.object(gate.subprocess,'run',side_effect=build),mock.patch.dict(os.environ,{'BUILD':'123','XBUS':'0','SPEC':'0'}):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'deliberate'):gate.matrix(args)
                    else:gate.matrix(args)
                self.assertEqual({k:calls[-1][k] for k in ('REMIX','BUILD','XBUS','SPEC')},dict(REMIX='selected',BUILD='123',XBUS='0',SPEC='0'))
                self.assertEqual(list((root/'remixes').iterdir()),[])

if __name__=='__main__':unittest.main()
