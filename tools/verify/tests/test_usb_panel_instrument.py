"""Firmware-free falsification of the final-image verifier's own oracles."""
import os
import concurrent.futures
import pathlib
import struct
import socket
import threading
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
    def paired_bench(self,timeout=1):
        client,server=socket.socketpair();server.settimeout(1)
        # Wrap a real socket to observe write boundaries, not emulate replies.
        sock=mock.Mock(wraps=client)
        mux=gate.ConcurrentBench(types.SimpleNamespace(sock=sock,buf=b'',timeout=timeout))
        self.addCleanup(server.close);self.addCleanup(mux.close)
        return mux,server,sock

    def test_dispatcher_batch_reserves_both_before_one_send_and_routes_either_order(self):
        for responses in (b'in 3 aa\nout 3 4\n',b'out 3 4\nin 3 aa\n'):
            with self.subTest(responses=responses):
                mux,server,sock=self.paired_bench()
                send=sock.sendall._mock_wraps
                def checked_send(data):
                    self.assertEqual(set(mux.pending),{'in 3','out 3'})
                    send(data)
                sock.sendall.side_effect=checked_send
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    job=pool.submit(mux.cmd_batch,(('in 3 1024','in 3'),('out 3 00000000','out 3')))
                    self.assertEqual(server.recv(4096),b'in 3 1024\nout 3 00000000\n')
                    server.sendall(responses)
                    self.assertEqual(job.result(timeout=1),['in 3 aa','out 3 4'])
                sock.sendall.assert_called_once()
                self.assertEqual(mux.pending,{})

    def test_dispatcher_batch_rejects_overlap_without_partial_reservation(self):
        mux,server,sock=self.paired_bench()
        commands=(('in 3 1024','in 3'),('out 3 00000000','out 3'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            out=pool.submit(mux.ep_out,3,bytes(4))
            server.recv(4096)
            with self.assertRaisesRegex(AssertionError,'duplicate pending'):
                mux.cmd_batch(commands)
            self.assertEqual(set(mux.pending),{'out 3'})
            server.sendall(b'out 3 4\n');self.assertEqual(out.result(timeout=1),4)
            pair=pool.submit(mux.cmd_batch,commands)
            server.recv(4096)
            server.sendall(b'in 3 aa\n')
            for line,prefix in commands:
                with self.assertRaisesRegex(AssertionError,'duplicate pending'):
                    mux.cmd(line,prefix)
            server.sendall(b'out 3 4\n');pair.result(timeout=1)
        self.assertEqual(sock.sendall.call_count,2)
        self.assertEqual(mux.pending,{})
        for invalid in ((),commands*2,(commands[0],commands[0])):
            with self.subTest(invalid=invalid),self.assertRaises(AssertionError):mux.cmd_batch(invalid)
            self.assertEqual(mux.pending,{})
        self.assertEqual(sock.sendall.call_count,2)

    def test_dispatcher_batch_uses_one_deadline_for_both_replies(self):
        mux,server,_=self.paired_bench()
        # Advance the clock between the two real queue reads, avoiding a
        # timing-sensitive sleep. The second reply has no new timeout budget.
        with mock.patch.object(gate.time,'monotonic',side_effect=(100,100.1,101.1)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                job=pool.submit(mux.cmd_batch,(('in 3 1024','in 3'),('out 3 00000000','out 3')))
                server.recv(4096);server.sendall(b'in 3 aa\n')
                with self.assertRaisesRegex(TimeoutError,'out 3'):job.result(timeout=.5)
        self.assertEqual(mux.pending,{})

    def test_dispatcher_batch_cleans_both_on_failure_and_joins_reader(self):
        for failure in ('timeout','error','close','send'):
            for first in (b'',b'in 3 aa\n',b'out 3 4\n'):
                with self.subTest(failure=failure,first=first):
                    mux,server,sock=self.paired_bench(timeout=.08)
                    if failure=='send':sock.sendall.side_effect=OSError('deliberate send failure')
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        job=pool.submit(mux.cmd_batch,(('in 3 1024','in 3'),('out 3 00000000','out 3')))
                        if failure!='send':
                            server.recv(4096)
                            if first:server.sendall(first)
                            if failure=='error':server.sendall(b'err deliberate\n')
                            elif failure=='close':mux.close()
                        with self.assertRaises((RuntimeError,TimeoutError,OSError)):job.result(timeout=1)
                    self.assertEqual(mux.pending,{})
                    with self.assertRaises((RuntimeError,TimeoutError,OSError)):mux.ep_in(0,64)
                    mux.close();self.assertFalse(mux.reader.is_alive())

    def test_duplex_cycle_sends_previous_in_frame_count_and_advances_exactly(self):
        for in_channels in (2,4):
            with self.subTest(in_channels=in_channels):
                mux,server,_=self.paired_bench()
                cycle=gate.IsoCycle(mux,20,in_channels)
                frame=0;prior=11
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    for n in (12,0,10,11):
                        job=pool.submit(cycle)
                        lines=server.recv(4096).decode().splitlines()
                        self.assertEqual(len(lines),2)
                        self.assertEqual(lines[0],'in 3 1024')
                        parts=lines[1].split()
                        payload=bytes.fromhex(parts[2]) if len(parts)>2 else b''
                        words=struct.unpack('<'+'I'*(len(payload)//4),payload)
                        self.assertEqual(words,tuple(((ch<<20)|(f&0xfffff))<<8 for f in range(frame,frame+prior) for ch in range(in_channels)))
                        packet=bytes(n*20*4)
                        server.sendall(f'out 3 {len(payload)}\nin 3 {packet.hex()}\n'.encode())
                        self.assertEqual(job.result(timeout=1),packet)
                        frame+=prior;prior=n
                        self.assertEqual(cycle.input_frame,frame)

    def test_single_direction_cycle_only_polls_in(self):
        mux,server,sock=self.paired_bench()
        cycle=gate.IsoCycle(mux,2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            job=pool.submit(cycle)
            self.assertEqual(server.recv(4096),b'in 3 1024\n')
            server.sendall(b'in 3 0000000000000000\n')
            self.assertEqual(job.result(timeout=1),bytes(8))
        self.assertEqual(cycle.input_frame,0)
        sock.sendall.assert_called_once()

    def test_duplex_priming_waits_for_two_state7_visits_without_out(self):
        class Bench:
            frames=iter((100,100,101,102))
            packets=iter((b'',bytes(11*80),bytes(12*80)))
            def ctrl_in(inner,*args):
                self.assertEqual(args,(0xc0,0x56,0,0,60))
                values=[0]*15;values[9]=next(inner.frames)
                return struct.pack('>15I',*values)
            def ep_in(inner,*args):
                self.assertEqual(args,(3,1024));return next(inner.packets)
        cycle=gate.IsoCycle(Bench(),20,2);consumed=[]
        self.assertEqual(cycle.prime(consumed.append),3)
        self.assertEqual([len(packet) for packet in consumed],[0,880,960])
        self.assertEqual(cycle.last_n,12)
        self.assertEqual(cycle.input_frame,0)

    def test_duplex_priming_is_bounded_and_single_direction_skips_it(self):
        consumed=[]
        cycle=gate.IsoCycle(types.SimpleNamespace(ctrl_in=lambda *args:bytes(60),ep_in=lambda *args:bytes(80)),20,2)
        with self.assertRaisesRegex(AssertionError,'state7'):
            cycle.prime(consumed.append)
        self.assertEqual(len(consumed),16)
        self.assertEqual(cycle.input_frame,0)
        self.assertEqual(gate.IsoCycle(object(),2).prime(consumed.append),0)
        self.assertEqual(len(consumed),16)

    def test_duplex_cycle_rejects_stall_short_out_and_partial_in(self):
        for response,error in ((b'in 3 stall\nout 3 88\n',gate.usb_host.Stall),
                               (b'in 3\nout 3 stall\n',gate.usb_host.Stall),
                               (b'in 3\nout 3 80\n',AssertionError),
                               (b'in 3 aabb\nout 3 88\n',AssertionError)):
            with self.subTest(response=response):
                mux,server,_=self.paired_bench();cycle=gate.IsoCycle(mux,20,2)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    job=pool.submit(cycle);server.recv(4096);server.sendall(response)
                    with self.assertRaises(error):job.result(timeout=1)
                self.assertEqual(cycle.input_frame,0)
                self.assertEqual(mux.pending,{})

    def test_dispatcher_routes_interleaved_endpoint_and_call_replies(self):
        client,server=socket.socketpair()
        mux=gate.ConcurrentBench(types.SimpleNamespace(sock=client,buf=b'',timeout=1))
        self.addCleanup(mux.close);self.addCleanup(server.close)
        def respond():
            data=b''
            while data.count(b'\n')<4:data+=server.recv(4096)
            server.sendall(b'out 3 4\nin 0 aa\ncall 0x17\nin 3 bb\n')
        thread=threading.Thread(target=respond);thread.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs=[pool.submit(mux.ep_in,0,64),pool.submit(mux.ep_in,3,1024),pool.submit(mux.ep_out,3,bytes(4)),pool.submit(mux.call,0x40010b00)]
            self.assertEqual([job.result() for job in jobs],[b'\xaa',b'\xbb',4,23])
        thread.join(1);self.assertFalse(thread.is_alive())
        mux.close();self.assertFalse(mux.reader.is_alive())

    def test_borrowed_call_serializes_pokes_but_not_iso(self):
        client,server=socket.socketpair()
        mux=gate.ConcurrentBench(types.SimpleNamespace(sock=client,buf=b'',timeout=1))
        self.addCleanup(mux.close);self.addCleanup(server.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            call=pool.submit(mux.call,0x40010b00)
            self.assertEqual(server.recv(4096),b'call 0x40010b00\n')
            # Marker refresh must skip a held borrowed-call slot instead of
            # preventing the ISO token needed for that call to complete.
            mux.try_pokes(((0x80000000,b'xx'),))
            poll=pool.submit(mux.ep_in,3,64)
            self.assertEqual(server.recv(4096),b'in 3 64\n')
            with self.assertRaisesRegex(AssertionError,'duplicate pending'):
                mux.ep_in(3,64)
            server.sendall(b'in 3 aa\ncall 0\n')
            self.assertEqual(poll.result(),b'\xaa');self.assertEqual(call.result(),0)
            poke=pool.submit(mux.poke,0x80000000,b'xx')
            self.assertEqual(server.recv(4096),b'poke 0x80000000 7878\n')
            server.sendall(b'poke ok\n');poke.result()

    def test_iso_continuous_phase_is_bounded(self):
        calls=[]
        worker=gate.IsoWorker(lambda:calls.append(1))
        try:
            worker.resume()
            with self.assertRaisesRegex(RuntimeError,'4096'):
                worker.wait_paused()
            self.assertEqual(len(calls),4096)
        finally:worker.close()

    def test_dispatcher_timeout_error_and_close_wake_waiters(self):
        for failure in ('timeout','error','close'):
            with self.subTest(failure=failure):
                client,server=socket.socketpair()
                mux=gate.ConcurrentBench(types.SimpleNamespace(sock=client,buf=b'',timeout=.1))
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        job=pool.submit(mux.ep_in,0,64)
                        server.recv(4096)
                        if failure=='error':server.sendall(b'err deliberate\n')
                        elif failure=='close':mux.close()
                        with self.assertRaises((RuntimeError,TimeoutError)):job.result(timeout=1)
                finally:mux.close();server.close()
                self.assertFalse(mux.reader.is_alive())

    def test_iso_worker_has_exact_phase_barriers_and_propagates_failure(self):
        phase=gate.AudioPhase('warm',({0x18200000},{0x18210000}))
        worker=gate.IsoWorker(lambda:phase.feed(GOOD))
        try:
            worker.run(80);self.assertEqual(phase.polls,80)
            phase=gate.AudioPhase('active',({0x18200000},{0x18210000}))
            worker.run(160);self.assertEqual(phase.polls,160)
            self.assertEqual(phase.finish()['hits'],[160,160])
        finally:worker.close()
        self.assertFalse(worker.thread.is_alive())
        bad=gate.IsoWorker(lambda:(_ for _ in ()).throw(RuntimeError('deliberate ISO failure')))
        try:
            with self.assertRaisesRegex(RuntimeError,'deliberate'):bad.run(1)
        finally:bad.close()

    def test_counter_progress_and_fs_source_bounds(self):
        names=('produced','consumed','underruns','overruns','bankdup','reprimes','anchor')
        zero=dict.fromkeys(names,0)
        # Both queue-full (zero attempts) and free-tail/low-ring (one per
        # block) are legal. Their relative rates need not match after reset.
        active=dict(zero,produced=7056,consumed=7052,underruns=441)
        for short_builds in (0,147,369,441,442):
            gate.check_counter_interval(zero,dict(active,underruns=short_builds),False,7052)
        # Preserve the earlier actual reduced-attempt-rate case as well.
        lower=dict(zero,produced=18336,consumed=18335,underruns=1025)
        gate.check_counter_interval(zero,lower,False,18335)
        for key,value in (('produced',0),('produced',-16),('produced',7057),('consumed',0),
                          ('underruns',-1),('underruns',443),('overruns',1),('bankdup',1),('reprimes',1),('anchor',1)):
            with self.subTest(key=key,value=value),self.assertRaises(AssertionError):
                gate.check_counter_interval(zero,dict(active,**{key:value}),False,7052)
        with self.assertRaises(AssertionError):gate.check_counter_interval(zero,active,True,7052)
        gate.check_counter_interval(zero,dict(active,underruns=0),True,7052)

    def test_fs_counter_conservation_uses_only_four_packet_slots(self):
        zero=dict.fromkeys(('produced','consumed','underruns','overruns','bankdup','reprimes','anchor'),0)
        after=dict(zero,produced=7056,consumed=7052,underruns=441)
        for queued_change in (-180,180):
            gate.check_counter_interval(zero,dict(after,consumed=7052+queued_change),False,7052)
        for queued_change in (-181,181):
            with self.subTest(queued_change=queued_change),self.assertRaisesRegex(AssertionError,'queued'):
                gate.check_counter_interval(zero,dict(after,consumed=7052+queued_change),False,7052)

    def test_fs_packet_size_and_aggregate_cadence_are_independent_of_tags(self):
        expected=({0x18200000},{0x18210000})
        good=gate.AudioPhase('FS',expected,fs=True)
        for n in (43,45,44)*40:good.feed(GOOD*n)
        self.assertEqual(good.finish()['frames'],5280)
        for n in (1,42,46):
            with self.subTest(n=n):
                phase=gate.AudioPhase('FS wrong size',expected,fs=True)
                for _ in range(10):phase.feed(GOOD*44)
                with self.assertRaisesRegex(AssertionError,'packet frames'):phase.feed(GOOD*n)
        for n in (43,45):
            with self.subTest(n=n):
                phase=gate.AudioPhase('FS wrong aggregate',expected,fs=True)
                phase.feed(GOOD*n)
                with self.assertRaisesRegex(AssertionError,'cadence'):phase.feed(GOOD*n)

    def test_counter_window_samples_exclude_settling_and_later_read_fence(self):
        phase=gate.AudioPhase('active',({0x18200000},{0x18210000}),settle=2,fs=True)
        phase.feed(b'');phase.feed(b'')
        for _ in range(160):phase.feed(GOOD*44)
        zero=dict.fromkeys(('produced','consumed','underruns','overruns','bankdup','reprimes','anchor'),0)
        after=dict(zero,produced=7056,consumed=7040,underruns=441)
        evidence=gate.audio_interval(phase,zero,after,False)
        # The actual worker pauses before the counter snapshot. Later fence
        # polls extend displayed totals, but cannot extend its frozen window.
        for _ in range(2):phase.feed(GOOD*44)
        evidence.update(phase.finish())
        self.assertEqual(evidence['frames'],7128)
        self.assertEqual(evidence['output_counter_window']['host_frames'],7040)
        self.assertEqual(evidence['output_counter_window']['host_packets'],160)
        after['consumed']=999999
        self.assertEqual(evidence['output_counter_window']['after']['consumed'],7040)

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

    def test_each_channel_silent_tail_fails_after_good_prefix(self):
        for channels in (2,20):
            words=[0x18200000+channel*0x10000 for channel in range(channels)]
            good=b''.join(word.to_bytes(4,'little') for word in words)*11
            for silent in range(channels):
                with self.subTest(channels=channels,silent=silent):
                    phase=gate.AudioPhase('active',tuple({word} for word in words))
                    for _ in range(120):phase.feed(good)
                    bad=b''.join((0 if channel==silent else word).to_bytes(4,'little') for channel,word in enumerate(words))*11
                    with self.assertRaises(AssertionError):
                        for _ in range(1000):phase.feed(bad)
                        phase.finish()
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
