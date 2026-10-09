#!/usr/bin/env python3
"""Final-image panel mirror gate on the emulated USB bench only.

Uses the selected REMIX/image, actual EP0 SETUP/IN/OUT transfers, and raw
UART A as an independent oracle. Never connects to physical USB hardware.
Instruction/transfer correctness is not a hardware timing or audio proof.
"""
import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
import pathlib
import queue
import re
import runpy
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools')); import toolpath  # noqa: E402,F401
sys.path.insert(0,str(ROOT/'tools/panel'))
from remix import ledger, registry  # noqa: E402
import emu_card  # noqa: E402
import usb_host  # noqa: E402
import usb_mirror_protocol as wire  # noqa: E402
from panel_link import PanelLink  # noqa: E402
from verify_usb import LAYOUTS, RB_BASE, MAIN_CUE_BASE, TAP_RB, TAP_MC, tap_word  # noqa: E402


class ConcurrentBench(usb_host.Bench):
    """One bounded reader for concurrent commands on the emulated socket.

    One request per exact response prefix; no unsolicited reply queue. The
    port has only one borrowed poke/call slot, so serialize those together.
    This is never a physical USB transport.
    """
    def __init__(self,base):
        self.sock=base.sock;self.timeout=base.timeout;self.buf=base.buf
        self.pending={};self.lock=threading.Lock();self.send_lock=threading.Lock()
        self.bench_lock=threading.RLock();self.error=None;self.closed=False
        self.sock.settimeout(.2)
        self.reader=threading.Thread(target=self._read,name='panel-bench-reader',daemon=True)
        self.reader.start()
    def _fail(self,error):
        with self.lock:
            if self.error is None:self.error=error
            for reply in self.pending.values():
                try:reply.put_nowait(self.error)
                except queue.Full:pass
    def _read(self):
        try:
            while not self.closed:
                if b'\n' not in self.buf:
                    try:data=self.sock.recv(65536)
                    except socket.timeout:continue
                    if not data:raise RuntimeError('bench closed the socket')
                    self.buf+=data
                    if len(self.buf)>65536:raise RuntimeError('bench response exceeded bound')
                    continue
                line,self.buf=self.buf.split(b'\n',1);line=line.decode()
                parts=line.split()
                if not parts or parts[0]=='err':raise RuntimeError('bench: '+line)
                prefix=' '.join(parts[:2]) if parts[0] in ('in','out') else parts[0]
                with self.lock:
                    reply=self.pending.get(prefix)
                    if reply is None:raise RuntimeError('unsolicited bench reply: '+line)
                    reply.put_nowait(line)
        except BaseException as exc:self._fail(exc)
    def cmd(self,line,expect=None,timeout=None):
        return self.cmd_batch(((line,expect or line.split()[0]),),timeout)[0]
    def cmd_batch(self,commands,timeout=None):
        """Reserve one command or the ISO IN/OUT pair, then send once.

        Both replies share one deadline and remain reserved until the whole
        batch completes. Only the reader thread receives from the socket.
        """
        assert 1<=len(commands)<=2,'bench batch must contain one or two commands'
        prefixes=[prefix for _,prefix in commands]
        assert len(set(prefixes))==len(prefixes),('duplicate pending response prefix',prefixes)
        allowed={'ok','poke','call',*(f'{direction} {ep}' for direction in ('in','out') for ep in range(4))}
        assert all(prefix in allowed for prefix in prefixes),prefixes
        if len(commands)==2:assert set(prefixes)=={'in 3','out 3'},prefixes
        guard=self.bench_lock if any(line.split()[0] in ('poke','call') for line,_ in commands) else contextlib.nullcontext()
        with guard:
            replies={prefix:queue.Queue(maxsize=1) for prefix in prefixes}
            with self.lock:
                if self.error is not None:raise self.error
                assert not self.pending.keys() & replies.keys(),('duplicate pending response prefix',prefixes)
                self.pending.update(replies)
            try:
                deadline=time.monotonic()+(timeout or self.timeout)
                with self.send_lock:self.sock.sendall(''.join(line+'\n' for line,_ in commands).encode())
                results=[]
                for prefix,reply in replies.items():
                    try:result=reply.get(timeout=max(0,deadline-time.monotonic()))
                    except queue.Empty:raise TimeoutError('no concurrent bench reply for '+prefix)
                    if isinstance(result,BaseException):raise result
                    results.append(result)
                return results
            except BaseException as exc:
                self._fail(exc);raise
            finally:
                with self.lock:
                    for prefix in prefixes:self.pending.pop(prefix,None)
    def try_pokes(self,items):
        # A pending borrowed call needs ISO polls to reach main; never block
        # the ISO worker behind that call just to refresh synthetic markers.
        if not self.bench_lock.acquire(blocking=False):return
        try:
            for address,data in items:self.poke(address,data)
        finally:self.bench_lock.release()
    def close(self):
        if self.closed:return
        self.closed=True;self._fail(RuntimeError('concurrent bench closed'))
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close();self.reader.join(timeout=2)
        assert not self.reader.is_alive(),'bench reader did not stop'


class IsoCycle:
    """One modeled ISO tick; duplex OUT follows the preceding IN size.

    As in verify_usb_in, the first HS bInterval2 packet contains 11 frames.
    Submitting both tokens together lets the model service them on one tick.
    """
    def __init__(self,bench,output_channels,input_channels=0):
        self.bench=bench;self.output_channels=output_channels;self.input_channels=input_channels
        self.input_frame=0;self.last_n=11
    def prime(self,consume):
        """Wait for deferred OUT activation using at most 16 IN-only polls.

        SET_INTERFACE records in_alt; state7 later arms OUT. Two observed
        state7 visits ensure the first visit's in_up has finished. Priming
        packets still count toward the existing 80-poll startup allowance.
        """
        if not self.input_channels:return 0
        def frames():return struct.unpack('>15I',self.bench.ctrl_in(0xc0,0x56,0,0,60))[9]
        before=frames()
        for polls in range(1,17):
            packet=self.bench.ep_in(3,1024);consume(packet)
            assert len(packet)%(4*self.output_channels)==0,'partial ISO IN frame'
            self.last_n=len(packet)//(4*self.output_channels)
            if frames()-before>=2:return polls
        raise AssertionError('input state7 did not complete startup within16polls')
    def __call__(self):
        if not self.input_channels:return self.bench.ep_in(3,1024)
        payload=b''.join(struct.pack('<I',((ch<<20)|((self.input_frame+j)&0xfffff))<<8)
                         for j in range(self.last_n) for ch in range(self.input_channels))
        incoming,outgoing=self.bench.cmd_batch((('in 3 1024','in 3'),(f'out 3 {payload.hex()}'.rstrip(),'out 3')))
        parts_in=incoming.split();parts_out=outgoing.split()
        if len(parts_in)>2 and parts_in[2]=='stall':raise usb_host.Stall('EP3 IN stalled')
        if parts_out[2]=='stall':raise usb_host.Stall('EP3 OUT stalled')
        assert int(parts_out[2])==len(payload),'short ISO OUT packet'
        packet=bytes.fromhex(parts_in[2]) if len(parts_in)>2 else b''
        assert len(packet)%(4*self.output_channels)==0,'partial ISO IN frame'
        self.input_frame+=self.last_n
        self.last_n=len(packet)//(4*self.output_channels)
        return packet


class IsoWorker:
    """Bounded ISO work with exact stopped phase boundaries and joined exit."""
    def __init__(self,operation,timeout=30):
        self.operation=operation;self.timeout=timeout;self.cv=threading.Condition()
        self.paused=True;self.stop=False;self.pause_requested=False;self.error=None
        self.remaining=0;self.finite=True
        self.thread=threading.Thread(target=self._run,name='panel-iso',daemon=True);self.thread.start()
    def _run(self):
        try:
            while True:
                with self.cv:
                    self.cv.wait_for(lambda:not self.paused or self.stop)
                    if self.stop:return
                self.operation()
                with self.cv:
                    self.remaining-=1
                    if self.remaining==0 and not self.finite:raise RuntimeError('ISO phase exceeded4096poll budget')
                    if self.pause_requested or self.remaining==0:self.paused=True;self.cv.notify_all()
        except BaseException as exc:
            with self.cv:self.error=exc;self.paused=True;self.cv.notify_all()
    def check(self):
        with self.cv:
            if self.error is not None:raise self.error
    def resume(self,polls=None):
        with self.cv:
            if self.error is not None:raise self.error
            assert self.paused and not self.stop
            assert polls is None or 0<polls<=4096
            self.remaining=polls or 4096;self.finite=polls is not None
            self.pause_requested=False;self.paused=False;self.cv.notify_all()
    def wait_paused(self):
        with self.cv:
            if not self.cv.wait_for(lambda:self.paused or self.error is not None,timeout=self.timeout):raise TimeoutError('ISO phase barrier timed out')
            if self.error is not None:raise self.error
    def pause(self):
        with self.cv:self.pause_requested=True
        self.wait_paused()
    def run(self,polls):self.resume(polls);self.wait_paused()
    def close(self):
        with self.cv:self.stop=True;self.cv.notify_all()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive(),'ISO worker did not stop'


class AudioPhase:
    """Independent interval accounting; warmup cannot satisfy a later phase.

    The producer has a startup cushion, so zero words can occur. Bound that
    allowance to eight consecutive empty-of-tags
    packets; nonzero unrecognized words and empty USB packets fail immediately
    after a declared settling interval.
    """
    def __init__(self,name,expected,settle=0,fs=False):
        self.name=name;self.expected=expected;self.settle=settle;self.fs=fs
        self.polls=0;self.frames=0;self.hits=[0]*len(expected);self.blank=[0]*len(expected)
    def feed(self,packet):
        self.polls+=1
        assert len(packet)%(4*len(self.expected))==0,(self.name,'partial audio frame')
        if self.polls<=self.settle:return
        assert packet,(self.name,'empty audio packet')
        frames=len(packet)//(4*len(self.expected));self.frames+=frames
        if self.fs:
            # STEP_FS44100 +/- SERVO_MAX200, accumulator0..999. These
            # bounds come from the builder, independently of observed tags.
            assert 43<=frames<=45,(self.name,'FS packet frames',frames)
            packets=self.polls-self.settle
            assert 43900*packets//1000<=self.frames<=(44300*packets+999)//1000,(self.name,'FS cadence',packets,self.frames)
        hits=[0]*len(self.expected)
        for i in range(0,len(packet),4):
            word=int.from_bytes(packet[i:i+4],'little');channel=i//4%len(self.expected)
            assert word==0 or word in self.expected[channel],(self.name,'unexpected audio word',channel,hex(word))
            if word:self.hits[channel]+=1;hits[channel]+=1
        for channel,count in enumerate(hits):
            self.blank[channel]=0 if count else self.blank[channel]+1
            assert self.blank[channel]<=8,(self.name,'sustained unidentifiable/silent channel',channel)
    def finish(self):
        assert min(self.hits)>100,(self.name,'insufficient identifiable audio',self.hits)
        return dict(phase=self.name,polls=self.polls,measured_polls=self.polls-self.settle,frames=self.frames,hits=list(self.hits))


def uart_at_generation(raw_uart,final_generation,frozen_generation):
    """Replay a proved complete-message boundary with all 128 LCD blocks."""
    represented=lambda link:sum(link.stats['ops'].get(key,0) for key in ('lcd','led_row','led_level','backlight'))
    final=PanelLink();final.feed(raw_uart)
    count=represented(final)
    assert 0<frozen_generation<=final_generation,'snapshot generation is outside captured history'
    target=count-(final_generation-frozen_generation)
    assert 0<target<=count,'snapshot boundary is absent from UART capture'
    class CoveredPanel(PanelLink):
        def __init__(self):super().__init__();self.coverage=set()
        def _message(self,message):
            if 0x10<=message[0]<=0x17:self.coverage.add((message[0]&7,message[1]))
            super()._message(message)
    at=CoveredPanel();reached=False
    for value in raw_uart:
        at.feed(bytes([value]))
        if represented(at)==target:reached=True;break
    assert reached and represented(at)==target,'UART target message boundary was not reached'
    assert len(at.coverage)==128,'UART target lacks complete LCD initialization'
    return at


def check_audio_fixture(selectors,state):
    assert selectors==bytes([7])*16,'synthetic source selectors changed'
    for track in range(8):
        values=struct.unpack('>4I',state[68*track:68*track+16])
        assert values==(0,0x7fffffff,0,0),('CF delay did not reach bypass',track,values)


def check_counter_interval(before,after,hs,host_frames):
    keys=('produced','consumed','underruns','overruns','bankdup','reprimes')
    delta={key:after[key]-before[key] for key in keys}
    assert delta['produced']>0 and delta['consumed']>0,delta
    assert delta['produced']%16==0,delta
    assert before['anchor']==after['anchor'],('stream anchor changed',before,after)
    assert all(delta[key]==0 for key in ('overruns','bankdup','reprimes')),delta
    if hs:assert delta['underruns']==0,delta
    else:
        # usbaudio_kick stops at the first failed builder, so at most one
        # speculative short build occurs per16-frame producer visit. +1
        # covers a starting snapshot after produced advances but before that
        # visit's possible failure. ACTIVE-tail returns before this counter:
        # relative rates across different queue/servo states are not fixed.
        assert 0<=delta['underruns']<=delta['produced']//16+1,delta
        # Consumed counts frames BUILT, not frames delivered. All IN replies
        # between these stopped worker boundaries are in host_frames; with
        # no reset/anchor change, their difference is queued inventory. Four
        # slots of at most45 FS frames bound that difference, not U's rate.
        assert host_frames>0 and abs(delta['consumed']-host_frames)<=4*45,('FS queued-frame conservation',delta,host_frames)
    return delta


def audio_interval(phase,before,after,hs):
    """Freeze the sample/counter window while the ISO worker is paused."""
    result=phase.finish()
    result['output_delta']=check_counter_interval(before,after,hs,result['frames'])
    result['output_counter_window']=dict(before=dict(before),after=dict(after),
                                         host_frames=result['frames'],host_packets=result['measured_polls'])
    return result


def stream_service(b,consume):
    """Fence guest service while continuing ISO polls on the same bench.

    A synchronous borrowed call alone deadlocks the model's ISO starvation
    hold. Multiplex its response with actual IN polls; consume every response
    and leave no pending transfer before returning.
    """
    b.sock.sendall(b'call 0x40010b00\nin 3 1024\n')
    done=False;pending=True;polls=0;deadline=time.monotonic()+10
    while not done or pending:
        assert time.monotonic()<deadline,'streaming guest-service fence timed out'
        line=b.wait('',timeout=max(.01,deadline-time.monotonic()))
        if line.startswith('call '):
            assert not line.startswith('call err'),line
            done=True
        elif line.startswith('in 3'):
            fields=line.split();assert len(fields)<3 or fields[2]!='stall',line
            consume(bytes.fromhex(fields[2]) if len(fields)>2 else b'')
            polls+=1;pending=False
            assert polls<=512,'streaming guest-service fence exceeded poll budget'
            if not done:b.sock.sendall(b'in 3 1024\n');pending=True
        else:raise AssertionError(('unexpected fence response',line))


def uac2_during_lease(b,request,cookie,service=None):
    """Preserve one live lease across class requests and deferred OUT abort."""
    lease=request(0x58,cookie&65535,cookie>>16)
    assert lease.header.status in (wire.Status.PENDING,wire.Status.OK)
    token=lease.header.token
    assert b.ctrl_in(0xa1,1,0x100,0x1003,4)==struct.pack('<I',44100)
    assert b.ctrl_in(0xa1,2,0x100,0x1003,14)==b'\x01\x00'+struct.pack('<III',44100,44100,0)
    assert b.ctrl_in(0xa1,1,0x200,0x1003,1)==b'\x01'
    for rate in (44100,48000):
        b.setup(0x21,1,0x100,0x1003,4)
        assert b.ep_out(0,struct.pack('<I',rate))==4
        try:b.ep_in(0,64)
        except usb_host.Stall:assert rate==48000
        else:assert rate==44100,'unsupported rate did not STALL'
    # No OUT data arrives. This bounded guest-service barrier makes the
    # existing 100,000-poll clock wait retire into its deferred state before
    # the replacement SETUP; it is not a physical packet-timing claim.
    b.setup(0x21,1,0x100,0x1003,4)
    (service or (lambda:b.call(0x40010b00,timeout=10)))()
    request(0x57,length=64)
    b.setup(0x21,1,0x100,0x1003,4)
    assert b.ep_out(0,struct.pack('<I',44100))==4
    assert b.ep_in(0,64)==b''
    assert b.ctrl_in(0xa1,1,0x100,0x1003,4)==struct.pack('<I',44100)
    same=request(0x58,cookie&65535,cookie>>16)
    assert same.header.status in (wire.Status.PENDING,wire.Status.OK) and same.header.token==token
    return 1


def read_frozen_body(request,identity,info,interleave=None):
    """Read one lease with bounded progress and exact identity on every chunk."""
    body=bytearray()
    while len(body)<identity.total_length:
        if interleave:interleave()
        piece=request(0x59,identity.token,len(body),info.max_response)
        if wire.ready_identity(piece,connection_id=identity.connection_id,info=info)!=identity:
            raise wire.ProtocolError('frozen descriptor changed during body reread')
        if piece.header.offset!=len(body) or not piece.payload:
            raise wire.ProtocolError('frozen body reread made no progress at the requested offset')
        if len(body)+len(piece.payload)>identity.total_length:
            raise wire.ProtocolError('frozen body reread exceeds its descriptor')
        body.extend(piece.payload)
    return bytes(body)


def require_input_epoch(observation,frozen_identity):
    """Ordinary input activity and lease expiry cannot reset the USB session."""
    if observation.epoch!=frozen_identity.epoch:
        raise wire.ProtocolError(f'input sequence changed epoch {frozen_identity.epoch}->{observation.epoch}')


def mki_fader_registration(bench,scratch):
    """Read the real UART2 registration; do not substitute the UART1 parser."""
    factory=bench.call(0x4003232c)
    assert factory in (0,1),'unexpected stock factory predicate'
    # Emulator-only authored getter: move.l (460ba984).l,d0; rts. The
    # USB bench has no memory-read or UART2-input command. Module-owned
    # scratch is never patched into an image or used by a physical client.
    bench.poke(scratch,bytes.fromhex('2039460ba9844e75'))
    registered=bench.call(scratch)
    expected=0x40092f88 if factory else 0x40092fac
    assert registered==expected,f'MKI UART2 callback registration mismatch: {registered:#x} != {expected:#x}'
    return dict(registered_callback=registered,factory_inverted=bool(factory),
                method='invoke registered stock UART2 callback; no UART2 wire/ADC timing claim')


def require_midi_note(bench,midi,max_polls=8):
    """Require an exact USB-MIDI note event after bounded legitimate traffic."""
    assert len(midi)==3 and midi[0]&0xf0==0x90,'expected a complete note-on'
    expected=b'\x09'+midi
    packets=[]
    for poll in range(max_polls):
        packet=bench.ep_in(2,512)
        packets.append(packet.hex())
        print(f'  [INFO] MIDI EP2 IN poll {poll+1}: {packet.hex() or "empty"}',flush=True)
        assert len(packet)%4==0,'partial USB-MIDI event: '+packet.hex()
        events=[packet[i:i+4] for i in range(0,len(packet),4)]
        for event in events:
            cin=event[0]&15
            assert event[0]<16 and cin>=2,'invalid USB-MIDI cable/CIN: '+event.hex()
            if 8<=cin<=14:
                assert event[1]&0xf0==cin<<4 and event[2]<128 and event[3]<128,'invalid channel event: '+event.hex()
                if cin in (12,13):assert event[3]==0,'nonzero short-channel padding: '+event.hex()
        if expected in events:
            return dict(polls=poll+1,packets=packets)
    raise AssertionError(f'exact USB-MIDI note {expected.hex()} absent in {max_polls} polls: {packets}')


def symbols():
    nm=shutil.which('m68k-elf-nm') or shutil.which('m68k-linux-gnu-nm')
    if not nm: raise RuntimeError('missing ColdFire nm instrument')
    lines=subprocess.check_output([nm,str(ROOT/'out/platform/runtime/runtime.elf')],text=True).splitlines()
    return {p[2]:int(p[0],16) for p in (line.split() for line in lines) if len(p)==3}


def run(image, modules, model, hs, out):
    sym=symbols()
    for key in ('pm_ctrl','pm_reply','pm_publish','mirror_idle_park','mirror_idle_resume','pm_idle_entry','pm_inputs','pm_output_generation','pm_body_output_generation'):
        if key not in sym: raise AssertionError('selected runtime lacks '+key)
    raw=image.read_bytes()
    assert raw[0x1d658-0x400:0x1d65c-0x400] == bytes.fromhex('20400000'), 'EP0 IN still appends an automatic ZLP after full panel replies'
    # The port has no CPU instruction-cache model: validate the mandatory
    # OS-resident synchronization boundary structurally as well as booting.
    nm=shutil.which('m68k-elf-nm') or shutil.which('m68k-linux-gnu-nm')
    cache_elf=ROOT/'out/linked/usb-panel-mirror/panel_cache/u.elf'
    cache_lines=subprocess.check_output([nm,str(cache_elf)],text=True).splitlines()
    cache={p[2]:int(p[0],16) for p in (line.split() for line in cache_lines) if len(p)==3}
    assert raw[0x512-0x400:0x518-0x400] == b'\x4e\xf9'+cache['pm_cache_handoff'].to_bytes(4,'big'), 'early handoff bypasses instruction-cache synchronization'
    for slot,target in (('pm_cache_first','pm_accept'),('pm_cache_end','_edata'),('pm_cache_target','pm_after_loader')):
        offset=cache[slot]-0x40000400
        assert int.from_bytes(raw[offset:offset+4],'big')==sym[target], 'cache bridge mismatch: '+slot
    assert raw[0x1fc96-0x400:0x1fc9c-0x400] == b'\x4e\xf9'+sym['pm_idle_entry'].to_bytes(4,'big'), 'selected image lacks matching publisher detour'
    for address,target in ((0x400923c0,'pm_key_tap'),(0x400924e4,'pm_encoder_tap'),(0x40092fc2,'pm_fader_tap'),(0x40092fa2,'pm_fader_inverted_tap')):
        offset=address-0x40000400
        assert raw[offset:offset+6]==b'\x4e\xf9'+sym[target].to_bytes(4,'big'),'selected image lacks accepted-input detour: '+target
    assert raw[0x925fe-0x400:0x92604-0x400]==bytes.fromhex('4eb940092fac'),'UART1 fader callback call changed'
    assert sym['pm_reply']%4096==0,'private reply is not page aligned'
    audio=next((key for key in LAYOUTS if key in modules),None)
    ain=next((key for key in ('USB AUDIO IN AB','USB AUDIO IN CD','USB AUDIO IN ABCD') if key in modules),None)
    in_channels=4 if ain=='USB AUDIO IN ABCD' else 2
    tag=f'{model}-'+('hs' if hs else 'fs')
    sock=f'/tmp/ot-panel-{os.getpid()}.sock'
    log=out/(tag+'.log');uart=out/(tag+'-uart.bin')
    dump=out/(tag+'-shadow');dump.mkdir(exist_ok=True)
    fields={'pm_output_generation':4,'pm_body_output_generation':4,'pm_inputs':168,'pm_generation':4,'pm_lcd':1024,'pm_row_seen':32,'pm_row_values':32,'pm_level_seen':256,'pm_level_values':256,'pm_backlight':1,'pm_backlight_known':4}
    memory=';'.join(f'{sym[name]:#x},{size}={dump}/{name}.bin' for name,size in fields.items())
    if model=='mki':memory+=f';0x460ba984,4={dump}/fixture_uart2_callback.bin'
    if audio:memory+=f';0x80000eb4,16={dump}/fixture_selectors.bin;0x80005f60,544={dump}/fixture_delay.bin'
    shared=ROOT/'out/verify_usb_panel';shared.mkdir(exist_ok=True)
    tree=shared/'cardtree';(tree/'OCTABAM/AUDIO').mkdir(parents=True,exist_ok=True)
    card=shared/'card.img'
    if not card.exists():card.write_bytes(emu_card.build_image(str(tree),size_mb=64))
    script=out/'settle.txt'
    script.write_text('1500 key 0x32 down\n1540 key 0x32 up\n1900 key 0x26 down\n1940 key 0x26 up\n2200 key 0x32 down\n2240 key 0x32 up\n2800 quit\n')
    args=[str(ROOT/'out/emu/ot_emu'),'--image',str(image),'--usb-host',sock,'--usb-hold-ms','120000',
          '--main-park',f'{sym["mirror_idle_park"]:#x}:{sym["mirror_idle_resume"]:#x}',
          '--panel-tx',str(uart),'--rtc','off','--card',str(card),'--live-script',str(script),'--mem-dump',memory]
    if model=='mkii':args+=['--mkii']
    if audio:args+=['--frame']
    expected_stalls=0; b=None; pump=None; last_body=None; descriptions={}
    with log.open('w') as lf:
        emu=subprocess.Popen(args,stdout=lf,stderr=subprocess.STDOUT,cwd=ROOT)
    def request(op,value=0,index=0,length=32):
        raw=b.ctrl_in(0xc0,op,value,index,length)
        try:return wire.parse_response(raw,request=wire.SetupRequest(0xc0,op,value,index,length))
        except wire.ProtocolError as exc:raise wire.ProtocolError(f'{exc}: request {op:#x} received {raw.hex()}') from exc
    def snapshot(cookie, interleave=None, release=True):
        response=request(0x58,cookie & 65535,cookie>>16)
        for _ in range(2000):
            if response.header.status==wire.Status.OK:break
            assert response.header.status==wire.Status.PENDING,response.header
            if interleave:interleave()
            time.sleep(.002)
            response=request(0x58,cookie & 65535,cookie>>16)
        assert response.header.status==wire.Status.OK,f'unchanged-screen publisher did not complete: cookie {cookie:#x}'
        identity=wire.ready_identity(response,connection_id=1,info=info)
        body=read_frozen_body(request,identity,info,interleave)
        wire.validate_snapshot(bytes(body),identity=identity,info=info)
        assert request(0x59,identity.token,len(body),32).payload==b''
        # Duplicate reads are byte-identical; the client may retry after timeout.
        a=request(0x59,identity.token,0,64);c=request(0x59,identity.token,0,64)
        assert a==c
        if release:
            assert request(0x5a,identity.token).header.status==wire.Status.OK
            assert request(0x5a,identity.token).header.status==wire.Status.OK
            assert request(0x59,identity.token,0,64).header.status==wire.Status.STALE
        return bytes(body),identity
    try:
        b=usb_host.Bench(sock,timeout=30)
        _,cfg=usb_host.enumerate_device(b,hs=hs)
        descriptions['configuration']=cfg.hex()
        descriptions['other_speed']=b.ctrl_in(0x80,6,0x700,0,1024).hex()
        tables,_=runpy.run_path(str(ROOT/'modules/usb-midi/descriptors.py'))['configs'](audio,ain)
        expected=tables['cfg_hs' if hs else 'cfg_fs']
        assert cfg==expected[:int.from_bytes(expected[2:4],'little')],'configuration bytes changed'
        assert bytes.fromhex(descriptions['other_speed'])==tables['cfg_os_fs' if hs else 'cfg_os_hs'],'other-speed configuration bytes changed'
        initial_info=request(0x57,length=64)
        info=wire.parse_info(initial_info)
        assert initial_info.header.generation>0,'INFO omitted the initialized shadow generation'
        assert info.valid_lcd_blocks==128,'capture did not observe complete initialization'
        assert info.max_response==64
        assert usb_host.msc_test(b),'MSC fallback failed'
        first,identity=snapshot(0x12340001)
        last_body,again=snapshot(0x12340002)
        # The stock UI may finish a popup or blink while boot settles. Find
        # a bounded quiet LCD interval instead of asserting it has no timers.
        for retry in range(8):
            if last_body[:1280]==first[:1280]:break
            first=last_body;identity=again
            last_body,again=snapshot(0x22340000+retry)
        assert last_body[:1280]==first[:1280],'no static LCD interval in eight attempts'
        print(f'  [PASS] {tag}: fresh static-LCD lease, generations {identity.generation}/{again.generation} (LED refresh may advance generation)',flush=True)
        # Feed the real stock RX-ring producer, which raises parser INT37.
        # The accepted-input detours, calibration, serializer and EP0 lease
        # all execute in this selected image; no observer helper is called.
        def input_report(opcode,value):
            b.call(0x40092254,opcode)
            b.call(0x40092254,value)
        fader_route=mki_fader_registration(b,sym['pm_test_scratch']) if model=='mki' else dict(method='stock calibrated UART1 parser')
        def fader_position(stock):
            if model=='mki':
                received=127-stock if fader_route['factory_inverted'] else stock
                b.call(fader_route['registered_callback'],received)
            else:
                input_report(0x40,min(255,stock*2))
        baseline=wire.validate_snapshot(last_body,identity=again,info=info).inputs
        assert baseline is not None and baseline.known & 5 == 5,'input baseline was not seeded'
        frozen_body,frozen_identity=snapshot(0x12340006,release=False)
        # Borrowed RX calls can advance more than the one-second lease in
        # emulated time. Accept only the contract's two precise outcomes:
        # a still-live immutable body or a canonical expired lease.
        fader_position(64)
        after_input=request(0x59,frozen_identity.token,0,info.max_response)
        require_input_epoch(after_input.header,frozen_identity)
        if after_input.header.status==wire.Status.OK:
            assert wire.ready_identity(after_input,connection_id=1,info=info)==frozen_identity
            reread=read_frozen_body(request,frozen_identity,info)
            assert reread==frozen_body,'accepted input mutated the immutable READY body'
            assert request(0x5a,frozen_identity.token).header.status==wire.Status.OK
            lease_input_outcome='immutable body reread after accepted input'
        else:
            assert after_input.header.status==wire.Status.STALE,'after-report lease was neither READY nor expired'
            header=after_input.header
            assert not any((header.generation,header.token,header.total_length,header.offset,
                            header.payload_length,header.crc32)) and not after_input.payload,'expired lease leaked a descriptor'
            lease_input_outcome='STALE: lease expired during borrowed parser calls'
        input_report(0x27,0)
        input_report(0x27,1)
        input_report(0x27,1)  # duplicate held report is no new edge
        input_report(0x27,0)  # short tap retained between acquisitions
        input_report(0x30,3)
        input_report(0x30,254)  # -2
        input_report(0x36,128)  # -128, extend before negating
        fader_position(64)  # final stock position 64 -> browser 63
        input_body,input_identity=snapshot(0x12340007)
        require_input_epoch(input_identity,frozen_identity)
        observed=wire.validate_snapshot(input_body,identity=input_identity,info=info).inputs
        assert observed.known==7 and observed.fader==63,'calibrated fader input mismatch'
        assert observed.keys[7]==0,'released encoder push remained held'
        assert (observed.press_counts[56]-baseline.press_counts[56])%65536==1,'short push/duplicate edge mismatch'
        assert tuple((v-u)%65536 for v,u in zip(observed.encoder_counts[0],baseline.encoder_counts[0]))==(3,2),'opposite detents lost'
        assert tuple((v-u)%65536 for v,u in zip(observed.encoder_counts[6],baseline.encoder_counts[6]))==(0,128),'-128 LEVEL detents lost'
        for stock,position,cookie in ((0,127,0x12340008),(127,0,0x12340009)):
            fader_position(stock)
            fader_body,fader_identity=snapshot(cookie)
            require_input_epoch(fader_identity,frozen_identity)
            assert wire.validate_snapshot(fader_body,identity=fader_identity,info=info).inputs.fader==position,'fader endpoint orientation mismatch'
        if model=='mki':
            # Also execute the real alternate factory callback, without
            # changing the registered pointer or pretending to measure ADC.
            alternate=0x40092fac if fader_route['factory_inverted'] else 0x40092f88
            for received in (0,64,127):
                b.call(alternate,received)
                alternate_body,alternate_identity=snapshot(0x12340100+received)
                require_input_epoch(alternate_identity,frozen_identity)
                alternate_inputs=wire.validate_snapshot(alternate_body,identity=alternate_identity,info=info).inputs
                position=received if alternate==0x40092f88 else 127-received
                assert alternate_inputs.known==7 and alternate_inputs.fader==position,'alternate stock fader callback orientation mismatch'
                assert alternate_inputs.press_counts==observed.press_counts and alternate_inputs.encoder_counts==observed.encoder_counts,'fader callback manufactured button/encoder edges'
            fader_route['alternate_callback']=alternate
        print(f'  [PASS] {tag}: real stock-parser short push and signed/opposite detents; fader midpoint/endpoints via {fader_route}; {lease_input_outcome}',flush=True)
        # A delayed, primed reply is superseded by a new SETUP. Borrowed stock
        # getter forces the guest through the first SETUP before the second.
        b.setup(0xc0,0x57,0,0,64);b.call(0x40010b00)
        b.setup(0xc0,0x58,0x1234,0x2345,32);b.call(0x40010b00)
        aborted=b.ep_in(0,32);b.ep_out(0)
        after=wire.parse_response(aborted,request=wire.SetupRequest(0xc0,0x58,0x1234,0x2345,32))
        assert after.header.status==wire.Status.PENDING
        assert request(0x58,0x1235,0x2345).header.status==wire.Status.BUSY
        assert request(0x5a,after.header.token).header.status==wire.Status.OK
        for bm,req,value,index,length in ((0xc0,0x57,0,0,31),(0xc1,0x57,0,0,64),(0x40,0x57,0,0,64)):
            expected_stalls+=1
            b.setup(bm,req,value,index,length)
            try:b.ep_in(0,length)
            except usb_host.Stall:pass
            else:raise AssertionError('malformed request did not stall')
        # Invalid but replyable lengths/fields have no frozen identity.
        for req,val,idx,length in ((0x57,0,0,65),(0x57,1,0,64),(0x58,0,0,32),(0x5a,1,1,32)):
            bad=b.ctrl_in(0xc0,req,val,idx,length)
            assert len(bad)==32 and bad[6]==wire.Status.BAD_REQUEST and bad[16:28]==bytes(12)
        # Normal-runtime producer call changes the real emulated panel. Mirror
        # reads themselves never request a redraw. Scratch is module-owned.
        before_update=request(0x57,length=64).header.generation
        assert before_update>=again.generation,'INFO is older than the validated snapshot'
        synthetic=bytes([0x10,0])+bytes([0x81,0x01,0x80,0x55,0xaa,0,0xff,0x18])+b'\x21\xa5\x3a\x12'
        b.poke(sym['pm_test_scratch'],synthetic)
        b.call(0x40010b1c,len(synthetic),sym['pm_test_scratch'])
        updated_info=request(0x57,length=64).header.generation
        assert updated_info>=before_update+3,'INFO did not observe the three complete synthetic updates'
        last_body,changed=snapshot(0x12340003)
        assert changed.generation>=updated_info,'new snapshot predates the INFO change'
        assert request(0x57,length=64).header.generation>=changed.generation,'INFO regressed after snapshot'
        print(f'  [PASS] {tag}: INFO current generation advances {before_update}->{updated_info}, validated snapshot {changed.generation}',flush=True)
        assert changed.generation>again.generation and last_body!=first
        if audio:
            b=ConcurrentBench(b)
            nch,_,bint,taps=LAYOUTS[audio]
            # Controlled synthetic source fixture, not an operator project.
            # Stock 40003510/1c tests this two-bank selector against7; that
            # path sets CF delay wet/send/feedback0, dry7fffffff. Merely
            # editing the four-bank parameter cache is ineffective: stock
            # 4000d13c/146 refreshes it each frame. Assert the retained
            # selectors and independently derived cached coefficients below.
            b.poke(0x80000eb4,bytes([7])*16)
            midi=bytes.fromhex('904163')
            b.poke(sym['pm_test_scratch'],midi);b.call(0x40010bc8,len(midi),sym['pm_test_scratch'])
            mirror_phase=False;midi_during_mirror=False
            if not hs:nch=2;taps=[(7 if audio.endswith('MASTER') else 8,0),(7 if audio.endswith('MASTER') else 8,1)]
            b.iso_hz(8000//(1<<(bint-1)) if hs else 1000)
            b.ctrl_nodata(0x01,0x0b,1,4)
            if ain and hs:b.ctrl_nodata(0x01,0x0b,1,5)
            cycle=IsoCycle(b,nch,in_channels if ain and hs else 0);audio_evidence=[]
            summed=not hs and audio in ('USB AUDIO OUT TRACKS','USB AUDIO OUT TRACKS MAIN CUE')
            expected=[{sum((t+1)*0x100000+lr*0x10000+f*0x100 for t in range(8)) for f in range(16)} for lr in range(2)] if summed else [{tap_word(src,lr,f)&0xffffff00 for f in range(16)} for src,lr in taps]
            # The low-byte guard also exists in tap_word: Q31 unity truncates
            # positive source words by one, below the transmitted24bits.
            rb=TAP_RB if not summed else b''.join(((t+1)*0x100000+lr*0x10000+f*0x100+0x77).to_bytes(4,'big') for _ in range(2) for t in range(8) for f in range(16) for lr in range(2))
            phase=AudioPhase('warmup',expected,settle=80,fs=not hs)
            controls_done=False
            def input_counters():return struct.unpack('>15I',b.ctrl_in(0xc0,0x56,0,0,60))
            def poll_one():
                b.try_pokes(((RB_BASE,rb),(MAIN_CUE_BASE,TAP_MC)))
                phase.feed(cycle())
            pump=IsoWorker(poll_one)
            def concurrent():
                nonlocal midi_during_mirror,controls_done,expected_stalls
                pump.check()
                if mirror_phase and not midi_during_mirror:
                    usb_host.midi_send(b,bytes.fromhex('904163f8'))
                    require_midi_note(b,midi)
                    midi_during_mirror=True
                if mirror_phase and not controls_done:
                    expected_stalls+=uac2_during_lease(b,request,0x12340004)
                    controls_done=True
            pump.run(80-cycle.prime(phase.feed))
            warm_before=usb_host.counters(b)
            pump.run(160)
            before=usb_host.counters(b);before_in=input_counters() if ain and hs else None
            warm=audio_interval(phase,warm_before,before,hs)
            audio_evidence.append(warm)
            lease=request(0x58,0x0004,0x1234)
            assert lease.header.status in (wire.Status.PENDING,wire.Status.OK)
            phase=AudioPhase('active lease',expected,fs=not hs)
            mirror_phase=True;pump.resume()
            last_body,active_identity=snapshot(0x12340004,concurrent,release=False)
            pump.pause()
            assert midi_during_mirror and controls_done
            after=usb_host.counters(b)
            active=audio_interval(phase,before,after,hs)
            if before_in is not None:
                after_in=input_counters();delta=[v-u for u,v in zip(before_in,after_in)]
                assert delta[0]>0 and delta[1]>0 and delta[2]>0 and delta[8]==0 and delta[14]==0,delta
                active['input_delta']=delta
            audio_evidence.append(active)
            # The body is READY and streams are still alt1. Prime a real READ,
            # allow guest software to issue it, then reset before data/status.
            pump.resume()
            assert b.ctrl_in(0x81,0x0a,0,4,1)==b'\x01'
            b.setup(0xc0,0x59,active_identity.token,0,64);b.call(0x40010b00,timeout=10)
            pump.pause()
            active.update(phase.finish())
            active['output_counter_scope']='through snapshot completion, before READY READ fence'
            b.reset()
            assert b.ctrl_in(0x81,0x0a,0,4,1)==b'\x00'
            if ain and hs:assert b.ctrl_in(0x81,0x0a,0,5,1)==b'\x00'
            stopped=[b.ep_in(3,1024) for _ in range(8)]
            assert all(not packet for packet in stopped[2:]),'reset left audio stream running'
            usb_host.enumerate_device(b,hs=hs)
            info=wire.parse_info(request(0x57,length=64))
            assert request(0x57,length=64).header.epoch!=active_identity.epoch
            assert request(0x59,active_identity.token,0,64).header.status==wire.Status.STALE
            b.ctrl_nodata(0x01,0x0b,1,4)
            if ain and hs:b.ctrl_nodata(0x01,0x0b,1,5)
            mirror_phase=False
            phase=AudioPhase('after reset/restart',expected,settle=80,fs=not hs)
            pump.run(80-cycle.prime(phase.feed))
            restart_before=usb_host.counters(b)
            pump.run(160)
            restart=audio_interval(phase,restart_before,usb_host.counters(b),hs)
            audio_evidence.append(restart)
            if ain and hs:b.ctrl_nodata(0x01,0x0b,0,5)
            b.ctrl_nodata(0x01,0x0b,0,4)
            for _ in range(8):b.ep_in(3,1024)
            assert b.ctrl_in(0x81,0x0a,0,4,1)==b'\x00'
            pump.close();pump=None
            print(f'  [PASS] {tag}: separate warm/active/restart audio intervals {audio_evidence}; input={ain if hs else None}',flush=True)
        # MIDI runs on its ordinary endpoints alongside the frozen protocol.
        usb_host.midi_send(b,bytes.fromhex('903c64b03c40'))
        midi=bytes.fromhex('903c64');b.poke(sym['pm_test_scratch'],midi)
        b.call(0x40010bc8,len(midi),sym['pm_test_scratch'])
        midi_evidence=require_midi_note(b,midi)
        # Reset in the middle of a read retires the old epoch/token.
        pending=request(0x58,77)
        b.setup(0xc0,0x59,pending.header.token,0,64);b.call(0x40010b00)
        old_epoch=pending.header.epoch
        usb_host.enumerate_device(b,hs=hs)
        info=wire.parse_info(request(0x57,length=64))
        assert request(0x57,length=64).header.epoch!=old_epoch
        assert request(0x59,pending.header.token,0,64).header.status==wire.Status.STALE
        last_body,last_identity=snapshot(0x12340005)
        if isinstance(b,ConcurrentBench):b.close()
        else:b.sock.close()
        b=None
        assert emu.wait(timeout=30)==0,'port did not exit cleanly'
    finally:
        if isinstance(b,ConcurrentBench):b.close()
        elif b is not None:b.sock.close()
        if pump is not None:pump.close()
        if emu.poll() is None:emu.terminate();emu.wait(timeout=10)
    raw_uart=uart.read_bytes()
    oracle=PanelLink();oracle.feed(raw_uart)
    shadow=lambda name:(dump/(name+'.bin')).read_bytes()
    if model=='mki':assert int.from_bytes(shadow('fixture_uart2_callback'),'big')==fader_route['registered_callback'],'UART2 registration changed during the gate'
    if audio:check_audio_fixture(shadow('fixture_selectors'),shadow('fixture_delay'))
    assert shadow('pm_lcd')==bytes(oracle.frame),'live shadow LCD disagrees with independent UART'
    for family,reference in (('row',oracle.led_rows),('level',oracle.leds)):
        seen=shadow('pm_'+family+'_seen');values=shadow('pm_'+family+'_values')
        assert {i:v for i,v in enumerate(values) if seen[i]}==reference,'live shadow '+family+' disagrees with UART'
    # Boot-ring bytes before RTOS attachment precede this UART instrument.
    # Anchor the generation offset with the final separately-checked shadow,
    # then compare the frozen body at its own exact complete-message boundary.
    final_generation=int.from_bytes(shadow('pm_output_generation'),'big')
    frozen_output_generation=int.from_bytes(shadow('pm_body_output_generation'),'big')
    at_snapshot=uart_at_generation(raw_uart,final_generation,frozen_output_generation)
    frozen=PanelLink();frozen.feed(last_body[:-wire.INPUT_RECORD_SIZE])
    assert frozen.frame==at_snapshot.frame,'snapshot LCD disagrees with UART at its generation'
    assert frozen.led_rows==at_snapshot.led_rows,'snapshot LED rows disagree with UART'
    assert frozen.leds==at_snapshot.leds,'snapshot LED levels disagree with UART'
    assert frozen.backlight==at_snapshot.backlight,'snapshot backlight disagrees with UART'
    txt=log.read_text();matches=re.findall(r'(\d+) stall\(s\)',txt)
    assert matches and int(matches[-1])==expected_stalls,(matches,expected_stalls)
    assert 'hold ended on the client' in txt,'bench expired instead of completing'
    print(f'  [PASS] {tag}: protocol1.1, complete UART-equal LCD/LED snapshot and accepted input suffix; expected STALLs={expected_stalls}',flush=True)
    return dict(model=model,speed='hs' if hs else 'fs',uart_bytes=len(uart.read_bytes()),body_bytes=len(last_body),descriptors=descriptions,expected_stalls=expected_stalls,input_lease=lease_input_outcome,fader_route=fader_route,midi_note=midi_evidence,audio=audio_evidence if audio else [])


def matrix_selections():
    """Derive accepted cases from the current descriptor and module registry."""
    descriptor=runpy.run_path(str(ROOT/'modules/usb-midi/descriptors.py'))
    known=registry.modules();accepted=[];rejected=[]
    for output in descriptor['HS_LAYOUT']:
        for inputs in (None,*descriptor['IN_LAYOUT']):
            keys=['USB MIDI',output,'USB PANEL MIRROR']+([inputs] if inputs else [])
            for key in keys:
                for dependency in known[key].requires:
                    if dependency not in keys:keys.append(dependency)
            keys=tuple(keys)
            problems=ledger.check([known[key] for key in keys])
            row=dict(output=output,input=inputs,modules=keys)
            if problems:rejected.append(dict(row,problems=problems))
            else:accepted.append(row)
    return accepted,rejected


def matrix(args):
    """Explicit developer operation: generate, validate and fingerprint carriers.

    Ordinary image-gate execution never builds or substitutes another image.
    The temporary remix is removed and the requested carrier restored on exit.
    """
    accepted,rejected=matrix_selections()
    out=ROOT/'out/verify_usb_panel/matrix';out.mkdir(parents=True,exist_ok=True)
    name=f'_usb_panel_matrix_{os.getpid()}'
    path=ROOT/'remixes'/(name+'.py')
    assert not path.exists()
    restore_env=dict(os.environ,REMIX=args.remix or 'usb-panel-main')
    env=dict(restore_env,REMIX=name,BUILD='0',XBUS='1',SPEC='1')
    rows=[];report=dict(accepted=len(accepted),rejected=rejected,rows=rows)
    models=('mki','mkii') if args.model=='both' else (args.model,)
    speeds=(True,False) if args.speed=='both' else (args.speed=='hs',)
    try:
        for number,case in enumerate(accepted):
            if number<args.matrix_start:continue
            slug=case['output'].removeprefix('USB AUDIO OUT ').lower().replace(' ','-')+'-'+(case['input'] or 'none').removeprefix('USB AUDIO IN ').lower()
            folder=out/slug;folder.mkdir(exist_ok=True)
            path.write_text('from remix.schema import Remix,Proof\nREMIX=Remix(name='+repr(name)+', family="probes", proof=Proof.CHECK, doc="Generated USB panel matrix carrier", modules='+repr(case['modules'])+', fallback="NONE")\n')
            selection=registry.remix(name)
            assert selection.modules==case['modules']
            with (folder/'build.log').open('w') as log:
                subprocess.run([sys.executable,'tools/build/build_bus.py'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
            image=ROOT/'out/mainos_bus.bin'
            fingerprint=hashlib.sha256(image.read_bytes()).hexdigest()
            print(f'  [INFO] matrix {number+1}/{len(accepted)} {slug} image_sha256={fingerprint}',flush=True)
            shutil.copy2(image,folder/'image.bin')
            shutil.copy2(ROOT/'out/platform/runtime/runtime.elf',folder/'runtime.elf')
            row=dict(case,image_sha256=fingerprint,results=[])
            rows.append(row)
            (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
            # Four independent emulated units read this one immutable image.
            # Builds stay serial; each process has its own USB socket, state
            # and model/speed-named log and memory dumps.
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
                pending=[pool.submit(run,image,selection.modules,model,hs,folder) for model in models for hs in speeds]
                row['results']=[future.result() for future in pending]
            (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    finally:
        path.unlink(missing_ok=True)
        with (out/'restore.log').open('w') as log:
            subprocess.run([sys.executable,'tools/build/build_bus.py'],cwd=ROOT,env=restore_env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
    print(f'  [PASS] matrix: {len(rows)} accepted carriers verified; {len(rejected)} named rejections',flush=True)
    return 0


def falsify(args):
    """Firmware-bearing negative controls remain private under out/."""
    out=ROOT/'out/verify_usb_panel/falsification';out.mkdir(parents=True,exist_ok=True)
    original=args.image.read_bytes()
    cases={
        'capture':((0x40010aea,'23c0400b96c8'),(0x40010b6a,'23c0400b96c8')),
        'dispatch':((0x4001de64,'2039fc0b01c0'),),
        'publisher':((0x4001fc96,'4eb940098a2c'),),
    }
    for name,patches in cases.items():
        altered=bytearray(original)
        for address,expected in patches:
            at=address-0x40000400;altered[at:at+6]=bytes.fromhex(expected)
        image=out/(name+'.bin');image.write_bytes(altered)
        result=subprocess.run([sys.executable,__file__,args.remix,'--image',str(image),'--model','mki','--speed','hs'],cwd=ROOT,capture_output=True,text=True,timeout=90)
        (out/(name+'.log')).write_text(result.stdout+result.stderr)
        assert result.returncode!=0 and '[FAIL] verify_usb_panel:' in result.stdout,(name,result.returncode,result.stdout[-500:])
        print(f'  [PASS] verifier falsification: bypassed {name} is rejected',flush=True)
    return 0


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('remix',nargs='?',default=os.environ.get('REMIX'))
    ap.add_argument('--image',type=pathlib.Path,default=ROOT/'out/mainos_bus.bin')
    ap.add_argument('--model',choices=('mki','mkii','both'),default='both')
    ap.add_argument('--speed',choices=('hs','fs','both'),default='both')
    ap.add_argument('--falsify',action='store_true',help='prove private capture/dispatch/publisher mutations fail this image gate')
    ap.add_argument('--matrix',action='store_true',help='explicitly build and verify all registry-accepted audio/input carriers')
    ap.add_argument('--jobs',type=int,choices=range(1,5),default=4,help='independent emulated units per matrix image; builds remain serial')
    ap.add_argument('--matrix-start',type=int,default=0,help='resume matrix at this zero-based case; earlier output is not revalidated')
    args=ap.parse_args()
    if args.matrix:return matrix(args)
    selection=registry.remix(args.remix)
    if 'USB PANEL MIRROR' not in selection.modules:
        print('  [ -- ] verify_usb_panel: selected remix has no mirror');return 0
    if not (ROOT/'out/emu/ot_emu').is_file():
        print('  [SKIP] verify_usb_panel: build port with make emu-cf');return 0
    out=ROOT/'out/verify_usb_panel';out.mkdir(exist_ok=True)
    try:
        if args.falsify:return falsify(args)
        fingerprint=hashlib.sha256(args.image.read_bytes()).hexdigest()
        print(f'  [INFO] verify_usb_panel REMIX={selection.name} image_sha256={fingerprint}',flush=True)
        results=[run(args.image,selection.modules,model,hs,out)
                 for model in (('mki','mkii') if args.model=='both' else (args.model,))
                 for hs in ((True,False) if args.speed=='both' else (args.speed=='hs',))]
        (out/'result.json').write_text(json.dumps(dict(remix=selection.name,image_sha256=fingerprint,results=results),indent=2)+'\n')
        print('  [PASS] verify_usb_panel: selected-image digital checks; hardware timing/cache/audio quality UNMEASURED')
        return 0
    except (AssertionError,OSError,RuntimeError,TimeoutError,wire.ProtocolError,usb_host.Stall,subprocess.SubprocessError) as exc:
        print(f'  [FAIL] verify_usb_panel: {exc}',flush=True);return 1

if __name__=='__main__':raise SystemExit(main())
