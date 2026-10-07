#!/usr/bin/env python3
"""Final-image panel mirror gate on the emulated USB bench only.

Uses the selected REMIX/image, actual EP0 SETUP/IN/OUT transfers, and raw
UART A as an independent oracle. Never connects to physical USB hardware.
Instruction/transfer correctness is not a hardware timing or audio proof.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import runpy
import shutil
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools')); import toolpath  # noqa: E402,F401
sys.path.insert(0,str(ROOT/'tools/panel'))
from remix import ledger, registry  # noqa: E402
import emu_card  # noqa: E402
import usb_host  # noqa: E402
import usb_mirror_protocol as wire  # noqa: E402
from panel_link import PanelLink  # noqa: E402
from verify_usb import LAYOUTS, RB_BASE, MAIN_CUE_BASE, TAP_RB, TAP_MC  # noqa: E402


def symbols():
    nm=shutil.which('m68k-elf-nm') or shutil.which('m68k-linux-gnu-nm')
    if not nm: raise RuntimeError('missing ColdFire nm instrument')
    lines=subprocess.check_output([nm,str(ROOT/'out/platform/runtime/runtime.elf')],text=True).splitlines()
    return {p[2]:int(p[0],16) for p in (line.split() for line in lines) if len(p)==3}


def run(image, modules, model, hs, out):
    sym=symbols()
    for key in ('pm_ctrl','pm_reply','pm_publish','mirror_idle_park','mirror_idle_resume','pm_idle_entry'):
        if key not in sym: raise AssertionError('selected runtime lacks '+key)
    raw=image.read_bytes()
    assert raw[0x1fc96-0x400:0x1fc9c-0x400] == b'\x4e\xf9'+sym['pm_idle_entry'].to_bytes(4,'big'), 'selected image lacks matching publisher detour'
    assert sym['pm_reply']%4096==0,'private reply is not page aligned'
    audio=next((key for key in LAYOUTS if key in modules),None)
    ain=next((key for key in ('USB AUDIO IN AB','USB AUDIO IN CD','USB AUDIO IN ABCD') if key in modules),None)
    in_channels=4 if ain=='USB AUDIO IN ABCD' else 2
    tag=f'{model}-'+('hs' if hs else 'fs')
    sock=f'/tmp/ot-panel-{os.getpid()}.sock'
    log=out/(tag+'.log');uart=out/(tag+'-uart.bin')
    dump=out/(tag+'-shadow');dump.mkdir(exist_ok=True)
    fields={'pm_generation':4,'pm_lcd':1024,'pm_row_seen':32,'pm_row_values':32,'pm_level_seen':256,'pm_level_values':256,'pm_backlight':1,'pm_backlight_known':4}
    memory=';'.join(f'{sym[name]:#x},{size}={dump}/{name}.bin' for name,size in fields.items())
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
    expected_stalls=0; b=None; last_body=None; descriptions={}
    with log.open('w') as lf:
        emu=subprocess.Popen(args,stdout=lf,stderr=subprocess.STDOUT,cwd=ROOT)
    def request(op,value=0,index=0,length=32):
        raw=b.ctrl_in(0xc0,op,value,index,length)
        try:return wire.parse_response(raw,request=wire.SetupRequest(0xc0,op,value,index,length))
        except wire.ProtocolError as exc:raise wire.ProtocolError(f'{exc}: request {op:#x} received {raw.hex()}') from exc
    def snapshot(cookie, interleave=None):
        response=request(0x58,cookie & 65535,cookie>>16)
        for _ in range(2000):
            if response.header.status==wire.Status.OK:break
            assert response.header.status==wire.Status.PENDING,response.header
            if interleave:interleave()
            time.sleep(.002)
            response=request(0x58,cookie & 65535,cookie>>16)
        assert response.header.status==wire.Status.OK,f'unchanged-screen publisher did not complete: cookie {cookie:#x}'
        identity=wire.ready_identity(response,connection_id=1,info=info)
        body=bytearray()
        while len(body)<identity.total_length:
            if interleave:interleave()
            piece=request(0x59,identity.token,len(body),info.max_response)
            assert wire.ready_identity(piece,connection_id=1,info=info)==identity,'frozen descriptor changed'
            body.extend(piece.payload)
        wire.validate_snapshot(bytes(body),identity=identity,info=info)
        assert request(0x59,identity.token,len(body),32).payload==b''
        # Duplicate reads are byte-identical; the client may retry after timeout.
        a=request(0x59,identity.token,0,64);c=request(0x59,identity.token,0,64)
        assert a==c
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
        info=wire.parse_info(request(0x57,length=64))
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
        synthetic=bytes([0x10,0])+bytes([0x81,0x01,0x80,0x55,0xaa,0,0xff,0x18])+b'\x21\xa5\x3a\x12'
        b.poke(sym['pm_test_scratch'],synthetic)
        b.call(0x40010b1c,len(synthetic),sym['pm_test_scratch'])
        last_body,changed=snapshot(0x12340003)
        assert changed.generation>again.generation and last_body!=first
        if audio:
            nch,_,bint,taps=LAYOUTS[audio]
            midi=bytes.fromhex('904163')
            b.poke(sym['pm_test_scratch'],midi);b.call(0x40010bc8,len(midi),sym['pm_test_scratch'])
            mirror_phase=False;midi_during_mirror=False
            if not hs:nch=2;taps=[(7 if audio.endswith('MASTER') else 8,0),(7 if audio.endswith('MASTER') else 8,1)]
            b.iso_hz(8000//(1<<(bint-1)) if hs else 1000)
            b.ctrl_nodata(0x01,0x0b,1,4)
            if ain and hs:b.ctrl_nodata(0x01,0x0b,1,5)
            seen=[0]*nch;wrong=[];audio_polls=0;input_frame=0
            summed=not hs and audio in ('USB AUDIO OUT TRACKS','USB AUDIO OUT TRACKS MAIN CUE')
            sum_words=[{sum((t+1)*0x100000+lr*0x10000+f*0x100 for t in range(8)) for f in range(16)} for lr in range(2)]
            rb=TAP_RB if not summed else b''.join(((t+1)*0x100000+lr*0x10000+f*0x100).to_bytes(4,'big') for _ in range(2) for t in range(8) for f in range(16) for lr in range(2))
            def concurrent():
                nonlocal audio_polls,input_frame,midi_during_mirror
                if mirror_phase and not midi_during_mirror:
                    usb_host.midi_send(b,bytes.fromhex('904163f8'))
                    assert b'\x09'+midi in b.ep_in(2,512)
                    midi_during_mirror=True
                for _ in range(8):
                    b.poke(RB_BASE,rb);b.poke(MAIN_CUE_BASE,TAP_MC)
                    packet=b.ep_in(3,1024);audio_polls+=1
                    assert len(packet)%(4*nch)==0
                    for i in range(0,len(packet),4):
                        w=int.from_bytes(packet[i:i+4],'little');src=(w>>24)-0x10;lr=((w>>16)&255)-0x20
                        if summed:
                            channel=(i//4)%2
                            if w in sum_words[channel]:seen[channel]+=1
                            elif w in sum_words[1-channel]:wrong.append((channel,w))
                        elif 0<=src<10 and lr in (0,1):
                            channel=(i//4)%nch
                            if (src,lr)==taps[channel]:seen[channel]+=1
                            else:wrong.append((channel,src,lr))
                    if ain and hs:
                        n=len(packet)//(4*nch)
                        payload=b''.join(struct.pack('<I',(((ch<<20)|((input_frame+j)&0xfffff))<<8)) for j in range(n) for ch in range(in_channels))
                        assert b.ep_out(3,payload)==len(payload);input_frame+=n
            for _ in range(30):concurrent()
            mirror_phase=True
            last_body,_=snapshot(0x12340004,concurrent)
            assert midi_during_mirror,'MIDI was not exercised with an active mirror lease/audio stream'
            assert not wrong and min(seen)>100,(seen,wrong[:8])
            assert b.ctrl_in(0xa1,1,0x100,0x1003,4)==struct.pack('<I',44100)
            assert len(b.ctrl_in(0xa1,2,0x100,0x1003,14))==14
            assert b.ctrl_in(0xa1,1,0x200,0x1003,1)==b'\x01'
            assert len(b.ctrl_in(0xc0,0x55,0,0,60))==60
            if ain and hs:
                counters=struct.unpack('>15I',b.ctrl_in(0xc0,0x56,0,0,60))
                assert counters[0]>0 and counters[2]>0 and counters[8]==0 and counters[14]==0,counters
                b.ctrl_nodata(0x01,0x0b,0,5)
            b.ctrl_nodata(0x01,0x0b,0,4)
            for _ in range(8):b.ep_in(3,1024)
            assert b.ctrl_in(0x81,0x0a,0,4,1)==b'\x00'
            print(f'  [PASS] {tag}: concurrent snapshot/{audio_polls} identifiable audio polls; input={ain if hs else None}',flush=True)
        # MIDI runs on its ordinary endpoints alongside the frozen protocol.
        usb_host.midi_send(b,bytes.fromhex('903c64b03c40'))
        midi=bytes.fromhex('903c64');b.poke(sym['pm_test_scratch'],midi)
        b.call(0x40010bc8,len(midi),sym['pm_test_scratch'])
        assert b'\x09'+midi in b.ep_in(2,512)
        # Reset in the middle of a read retires the old epoch/token.
        pending=request(0x58,77)
        b.setup(0xc0,0x59,pending.header.token,0,64);b.call(0x40010b00)
        old_epoch=pending.header.epoch
        usb_host.enumerate_device(b,hs=hs)
        info=wire.parse_info(request(0x57,length=64))
        assert request(0x57,length=64).header.epoch!=old_epoch
        assert request(0x59,pending.header.token,0,64).header.status==wire.Status.STALE
        last_body,last_identity=snapshot(0x12340005)
        b.sock.close();b=None
        assert emu.wait(timeout=30)==0,'port did not exit cleanly'
    finally:
        if b is not None:b.sock.close()
        if emu.poll() is None:emu.terminate();emu.wait(timeout=10)
    raw_uart=uart.read_bytes()
    oracle=PanelLink();oracle.feed(raw_uart)
    shadow=lambda name:(dump/(name+'.bin')).read_bytes()
    assert shadow('pm_lcd')==bytes(oracle.frame),'live shadow LCD disagrees with independent UART'
    for family,reference in (('row',oracle.led_rows),('level',oracle.leds)):
        seen=shadow('pm_'+family+'_seen');values=shadow('pm_'+family+'_values')
        assert {i:v for i,v in enumerate(values) if seen[i]}==reference,'live shadow '+family+' disagrees with UART'
    represented=lambda link:sum(link.stats['ops'].get(key,0) for key in ('lcd','led_row','led_level','backlight'))
    # Boot-ring bytes before RTOS attachment precede this UART instrument.
    # Anchor the generation offset with the final separately-checked shadow,
    # then compare the frozen body at its own exact complete-message boundary.
    final_generation=int.from_bytes(shadow('pm_generation'),'big')
    target=represented(oracle)-(final_generation-last_identity.generation)
    assert target>=128,'snapshot predates complete UART initialization'
    at_snapshot=PanelLink()
    for value in raw_uart:
        at_snapshot.feed(bytes([value]))
        if represented(at_snapshot)==target:break
    frozen=PanelLink();frozen.feed(last_body)
    assert frozen.frame==at_snapshot.frame,'snapshot LCD disagrees with UART at its generation'
    assert frozen.led_rows==at_snapshot.led_rows,'snapshot LED rows disagree with UART'
    assert frozen.leds==at_snapshot.leds,'snapshot LED levels disagree with UART'
    assert frozen.backlight==at_snapshot.backlight,'snapshot backlight disagrees with UART'
    txt=log.read_text();matches=re.findall(r'(\d+) stall\(s\)',txt)
    assert matches and int(matches[-1])==expected_stalls,(matches,expected_stalls)
    assert 'hold ended on the client' in txt,'bench expired instead of completing'
    print(f'  [PASS] {tag}: protocol1.0, complete UART-equal LCD/LED snapshot; expected STALLs={expected_stalls}',flush=True)
    return dict(model=model,speed='hs' if hs else 'fs',uart_bytes=len(uart.read_bytes()),body_bytes=len(last_body),descriptors=descriptions,expected_stalls=expected_stalls)


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
    env=dict(os.environ,REMIX=name,BUILD='0',XBUS='1',SPEC='1')
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
        restore=args.remix or 'usb-panel-main'
        with (out/'restore.log').open('w') as log:
            subprocess.run([sys.executable,'tools/build/build_bus.py'],cwd=ROOT,env=dict(env,REMIX=restore),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
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
