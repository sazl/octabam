"""Playing-DSP evidence for VOLUME MIXER; all artifacts are disposable."""
from __future__ import annotations
import hashlib
import json
import random
import re
import shutil
import struct
import subprocess
import wave

from volume_mixer_probe import ROOT, PINS, STOCK
import emu_card
import ot_project

# Predeclared proof interval, independent of observed capture lengths. The
# script drains beyond it because CF frame gates can stop between ESAI samples.
AUDIO_WINDOW_MS = 7200
AUDIO_WINDOW_SAMPLES = AUDIO_WINDOW_MS * 44100 // 1000
AUDIO_DRAIN_MS = 200


def audio_window(pcm):
    assert len(pcm) % 12 == 0, 'partial MAIN/CUE sample'
    assert len(pcm) >= AUDIO_WINDOW_SAMPLES * 12, 'capture shorter than declared audio window'
    return pcm[:AUDIO_WINDOW_SAMPLES * 12]


def assert_audio_equal(reference, actual):
    assert len(reference) == len(actual) == AUDIO_WINDOW_SAMPLES * 12, 'wrong declared audio window'
    assert reference == actual, f'MAIN/CUE differ at byte {next(i for i,(a,b) in enumerate(zip(reference,actual)) if a!=b)}'


def test_audio_window():
    reference = bytes(AUDIO_WINDOW_SAMPLES * 12)
    assert_audio_equal(audio_window(reference), audio_window(reference + bytes([255])*12))
    changed = bytearray(reference)
    changed[len(changed)//2] = 1
    for check in (lambda: audio_window(reference[:-12]),
                  lambda: assert_audio_equal(reference, audio_window(changed))):
        try:
            check()
        except AssertionError:
            pass
        else:
            raise AssertionError('audio window negative control was accepted')


CONTROLS = ((0x80000c50, 16), (0x80000035, 2), (0x8000002e, 2))
CODES = dict(PLAY=0x28, STOP=0x27, MIDI=0x35, MIXER=0x30, REC1=0x2b,
             CUE=0x2a, NO=0x32, LEFT=0x34, RIGHT=0x21, YES=0x31)


def files(project):
    return {str(p.relative_to(project)): p.read_bytes() for p in project.rglob('*') if p.is_file()}


def fixture(project, out):
    """Derive THRU/NONE from the supplied saved project, never edit it."""
    before = files(project)
    target = out / 'project'
    assert not target.resolve().is_relative_to(project.resolve()), 'fixture must live outside source project'
    if target.exists(): shutil.rmtree(target)
    shutil.copytree(project, target)
    for t in range(1, 9):
        ot_project.thru_track(target, t, '017f0000400000' if t == 1 else '00400000400000', guard=False)
        for slot in ('fx1', 'fx2'):
            ot_project.set_fx(target, slot, t, 0, page=[0]*6, page2=[0]*6, guard=False)
    # Feed the same independent stereo signal to both output destinations.
    # No sample files are needed by these THRU machines.
    for p in target.glob('project.*'):
        data = p.read_bytes()
        for name, value in [('MAIN_TO_CUE', 1), ('TRACK_CUE_MASK', 0),
                            ('MASTER_TRACK', 0), ('BANK', 0), ('PATTERN', 0),
                            ('METRONOME_ENABLED', 0), ('GATE_AB', 127), ('GATE_CD', 127),
                            ('GAIN_AB', 64), ('GAIN_CD', 64), ('MAIN_LEVEL', 64), ('CUE_LEVEL', 64)]:
            data = re.sub(rb'(?m)^' + name.encode() + rb'=\d+', name.encode() + b'=' + str(value).encode(), data)
        p.write_bytes(data)
    assert files(project) == before, 'source saved project was modified'
    card, _ = emu_card.stage_project(target, 'VMTEST', 'PROBE', tree=str(out / 'tree'))
    card_path = out / 'card.img'
    card_path.write_bytes(card)
    wav = out / 'input.wav'
    rng = [random.Random(0x71500 + c) for c in range(4)]
    with wave.open(str(wav), 'wb') as w:
        w.setparams((4, 3, 44100, 0, 'NONE', 'not compressed'))
        w.writeframes(b''.join((rng[c].randrange(-0x200000, 0x200000) & 0xffffff).to_bytes(3, 'little')
                              for _ in range(44100 * 9) for c in range(4)))
    return card_path, wav


def timeline(active):
    """Real key stream; stock receives only the identical transport timeline."""
    events = []
    def key(ms, name, down):
        events.append((ms, f'key {CODES[name]:#x} {"down" if down else "up"}'))
    def tap(ms, name):
        key(ms, name, True); key(ms+80, name, False)
    def chord(ms, name):
        key(ms, name, True); key(ms+80, 'MIXER', True)
        key(ms+160, 'MIXER', False); key(ms+240, name, False)
    tap(100, 'YES')
    tap(400, 'PLAY')
    if active:
        chord(900, 'MIDI')                         # tracks
        tap(1500, 'RIGHT')                         # inputs
        tap(1950, 'RIGHT')                         # outputs
        tap(2400, 'LEFT'); tap(2750, 'LEFT')       # page changes
        tap(3100, 'NO'); chord(3350, 'REC1')       # close/reopen inputs
        tap(3900, 'MIXER'); chord(4150, 'CUE')     # close/reopen outputs
    tap(4800, 'STOP')                              # visible transport
    tap(5250, 'PLAY')
    if active:
        tap(5800, 'NO'); chord(6050, 'MIDI')       # repeated reopen
    tap(6650, 'STOP')
    events.append((AUDIO_WINDOW_MS + AUDIO_DRAIN_MS, 'quit'))
    return '\n'.join(f'{ms} {event}' for ms, event in sorted(events)) + '\n'


def transport_handlers(model):
    """Read each model's stock key table; zero means no release callback."""
    stock = STOCK.read_bytes()
    table = 0x400c01f4 if model == 'mkii' else 0x400bfc10
    handlers = {}
    for i in range(64):
        offset = table - 0x40000400 + 26*i
        record = stock[offset:offset+26]
        if record[0] == 0xff: break
        if record[0] in (CODES['PLAY'], CODES['STOP']):
            handlers[record[0]] = struct.unpack('>2I', record[2:10])
    assert set(handlers) == {CODES['PLAY'], CODES['STOP']}
    return handlers


def validate_transport(log, active, symbols, handlers):
    """Merge PC and memory observations by native instruction count."""
    expected = [(name, edge, address) for name in ('PLAY','STOP','PLAY','STOP')
                for edge,address in zip(('down','up'), handlers[CODES[name]]) if address]
    addresses = {address for _,_,address in expected}
    events = []
    for hit in re.finditer(r'\[\s*(\d+)\] at (0x[0-9a-f]+) d0=.*?\[sp \S+: \S+ (0x[0-9a-f]+|0) (0x[0-9a-f]+|0) ', log):
        instruction, pc = int(hit[1]), int(hit[2],16)
        if pc == PINS['frame'][2]: events.append((instruction, 'block', 0, None))
        if pc in addresses: events.append((instruction, 'handler', pc, (int(hit[3],0), int(hit[4],0))))
    for hit in re.finditer(r'\[\s*([0-9.]+)\] \[(0x[0-9a-f]+)\] <- (0x[0-9a-f]+|0) \(4\).*? i=(\d+)', log):
        sample, address, value, instruction = float(hit[1]), int(hit[2],16), int(hit[3],0), int(hit[4])
        if address in (symbols['vm_window'], 0x800065b8):
            events.append((instruction, 'window' if address == symbols['vm_window'] else 'state', value, sample))
    window, blocks, observed, states = 0, 0, [], []
    for instruction, kind, value, sample in sorted(events):
        if kind == 'block': blocks += 1
        elif kind == 'window': window = value
        elif kind == 'handler': observed.append({'pc':value, 'visible':bool(window), 'key_code':sample[0], 'pressed':sample[1], 'audio_block':blocks})
        elif value or states: states.append({'value':value, 'visible':bool(window), 'sample':sample, 'audio_block':blocks})
    assert blocks > 19000, ('missing completed DSP block observations', blocks)
    assert [event['pc'] for event in observed] == [a for _,_,a in expected], ('transport handlers missing or reordered', observed, expected)
    visibility = [False] + [active]*(len(expected)-1)
    assert [event['visible'] for event in observed] == visibility, ('transport handler visibility', observed)
    assert [(event['key_code'], event['pressed']) for event in observed] == [(CODES[name], int(edge == 'down')) for name,edge,_ in expected], ('transport press/release arguments', observed)
    # This stock STOP path writes stopped/end state 0 twice; retain both
    # raw writes but assert the changes of state (1 means running).
    transitions = [event for i,event in enumerate(states) if not i or event['value'] != states[i-1]['value']]
    assert [event['value'] for event in transitions] == [1,0,1,0], ('sequencer did not PLAY/STOP twice', states)
    assert [event['visible'] for event in transitions] == [False,active,active,active], ('transport state visibility', states)
    for event,(name,edge,_) in zip(observed,expected): event.update(key=name, edge=edge)
    return {'handlers':observed, 'states':states, 'transitions':transitions, 'completed_blocks':blocks}

def capture(model, image, out, card, wav, active, symbols):
    out.mkdir(parents=True, exist_ok=True)
    script = out / 'keys.txt'; script.write_text(timeline(active))
    # This is Rtos::setMainLevelLive's native kernel post, with its exact
    # stack ABI. The script gives the sys task time to fill the gain table.
    argv = [str(ROOT/'out/emu/ot_emu'), '--image', str(image), '--card', str(card),
            '--mount', '--set', 'VMTEST', '--project', 'PROBE', '--internal-clock',
            '--rtc', 'off', '--dsp', '--audio-in', str(wav), '--audio-out', str(out/'audio'),
            '--card-out', str(out/'result.img'), '--live-script', str(script),
            '--step', '-:poke:0x46c00000=0x04;0x46c00001=0x40;0x46c00002=0;0x46c00003=0',
            '--step', '-:call:0x40000c3c,0x460d17ae,0x46c00000']
    if not active:
        restore = ';'.join(f'{site+i:#x}={value:#x}' for site,expected,_ in PINS.values()
                           for i,value in enumerate(bytes.fromhex(expected)))
        argv += ['--step', '-:poke:' + restore]
    ranges = [(a,n,f'control-{i}.bin') for i,(a,n) in enumerate(CONTROLS)]
    ranges += [(0x80003c60,4,'gain.bin')]
    argv += ['--step', '-:dump:' + ';'.join(f'{a:#x},{n}={out / ("before-"+name)}' for a,n,name in ranges)]
    ranges += [(symbols[name],4,name+'.bin') for name in ('vm_window','vm_page','vm_frame_count','vm_pending_releases')]
    ranges += [(site,len(bytes.fromhex(expected)),f'hook-{name}.bin')
               for name,(site,expected,_) in PINS.items()]
    argv += ['--mem-dump', ';'.join(f'{a:#x},{n}={out/name}' for a,n,name in ranges)]
    if model == 'mkii': argv += ['--mkii']
    handlers = transport_handlers(model)
    (out/'transport-handlers.json').write_text(json.dumps(handlers)+'\n')
    watches = [symbols[n] for n in ('vm_open','vm_draw')] + [PINS['frame'][2]]
    watches += sorted({a for pair in handlers.values() for a in pair if a})
    argv += ['--watch-pc', ','.join(hex(a) for a in watches),
             '--watch-mem', f'{symbols["vm_page"]:#x},4;{symbols["vm_window"]:#x},4;0x800065b8,4']
    (out/'command.json').write_text(json.dumps(argv, indent=2)+'\n')
    with (out/'port.log').open('w') as log:
        subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
    return validate_capture(out, card, active, symbols)


def validate_capture(out, card, active, symbols):
    """Check the complete capture and native control/PC/card evidence."""
    log = (out/'port.log').read_text()
    assert 'ended on quit' in log and 'DID NOT RETURN' not in log, log[-3000:]
    assert int.from_bytes((out/'gain.bin').read_bytes(),'big'), 'native main gain was not initialized'
    handlers = {int(k):v for k,v in json.loads((out/'transport-handlers.json').read_text()).items()}
    transport = validate_transport(log, active, symbols, handlers)
    controls = []
    ui = {'open_pages':[], 'page_transitions':[], 'draws':0}
    for i in range(len(CONTROLS)):
        value = (out/f'control-{i}.bin').read_bytes()
        assert value == (out/f'before-control-{i}.bin').read_bytes(), f'live control {i} changed'
        controls.append(value.hex())
    if active:
        targets = dict(frame='vm_frame', mixer='vm_mixer', midi='vm_midi',
                       ui='vm_tick', recorder_router='vm_rec_router')
        for name, (_, expected, _) in PINS.items():
            opcode = b'\x4e\xb9' if name == 'recorder_router' else b'\x4e\xf9'
            patch = opcode + struct.pack('>I', symbols[targets[name]])
            patch += b'\x4e\x71' * ((len(bytes.fromhex(expected))-6)//2)
            assert (out/f'hook-{name}.bin').read_bytes() == patch, 'active composed hook missing'
        def val(name): return int.from_bytes((out/(name+'.bin')).read_bytes(),'big')
        assert val('vm_window') and val('vm_page') == 0, 'real chord did not leave tracks page open'
        assert val('vm_frame_count') > 1000 and val('vm_pending_releases') == 0
        opens = re.findall(r'at ' + hex(symbols['vm_open']) + r' d0=(0x[0-9a-f]+|0) ', log)
        assert [int(v,0) for v in opens] == [0,1,2,0], ('missing real chord', opens)
        pages = re.findall(r'\[' + hex(symbols['vm_page']) + r'\] <- (0x[0-9a-f]+|0) \(4\)',log)
        assert [int(v,0) for v in pages][-8:] == [0,1,2,1,0,1,2,0], ('missing page transition', pages)
        draws = len(re.findall(r'at ' + hex(symbols['vm_draw']) + r' d0=', log))
        assert draws > 120, ('playing redraw stopped', draws)
        ui = {'open_pages':[int(v,0) for v in opens],
              'page_transitions':[int(v,0) for v in pages][-8:], 'draws':draws}
    else:
        assert not re.search(r'at ' + hex(symbols['vm_open']) + r' d0=', log), 'reference opened module'
        assert not re.search(r'at ' + hex(symbols['vm_draw']) + r' d0=', log), 'reference drew module'
        for name in ('vm_window','vm_frame_count','vm_pending_releases'):
            assert not int.from_bytes((out/(name+'.bin')).read_bytes(),'big'), 'reference executed meter runtime'
        for name,(_,expected,_) in PINS.items():
            assert (out/f'hook-{name}.bin').read_bytes().hex() == expected, 'reference stock hook was not restored'
    with wave.open(str(out/'audio_core0.wav'), 'rb') as w:
        assert w.getnchannels() == 8 and w.getsampwidth() == 3 and w.getframerate() == 44100
        pcm = w.readframes(w.getnframes())
    marker = re.search(r'transport start at frame (\d+)', log)
    assert marker, 'capture has no first DSP command boundary'
    # The DSP runs during the boot; align only by the instrument's explicit
    # first-command marker, never by searching/correlating the resulting audio.
    start_sample = int(marker.group(1))
    pcm = pcm[start_sample*24:]
    selected = b''.join(pcm[i+6:i+18] for i in range(0,len(pcm),24))
    raw_samples = len(selected)//12
    selected = audio_window(selected)
    # Each MAIN/CUE stereo side must sound, with independent L/R content.
    channels = []
    for c in range(4):
        channel = b''.join(selected[i+c*3:i+c*3+3] for i in range(0,len(selected),12))
        assert any(channel), f'output {c} is silent'
        channels.append(channel)
    assert channels[0] != channels[1] and channels[2] != channels[3], 'stereo input collapsed'
    (out/'main-cue.pcm24').write_bytes(selected)
    persisted = emu_card.extract_image((out/'result.img').read_bytes())
    original = emu_card.extract_image(card.read_bytes())
    project_files = {k:v for k,v in original.items() if '/PROBE/' in k.upper()}
    assert project_files, 'fixture project files were not found in card'
    for path, data in project_files.items():
        assert persisted.get(path) == data, f'persisted project file changed: {path}'
    return selected, {'samples':len(selected)//12, 'raw_samples':raw_samples,
                     'window_ms':AUDIO_WINDOW_MS, 'start_marker':start_sample, 'controls':controls,
                     'level_bytes':list(bytes.fromhex(controls[0])[::2]), 'ui':ui,
                     'transport':transport, 'pcm24_sha256':hashlib.sha256(selected).hexdigest(), 'project_files':len(project_files)}


def run_audio_cases(model, out, project, composed):
    assert composed, 'audio requires the actual composed image'
    test_audio_window()
    out.mkdir(parents=True, exist_ok=True)
    report = {'model':model, 'passed':False, 'precision':24, 'capture':'core0 ESAI MAIN/CUE',
              'baseline':'common composed boot, five stock handlers restored before frame engine',
              'scenarios':['closed','tracks','inputs','outputs','navigation','reopen','visible PLAY/STOP']}
    try:
        card, wav = fixture(project, out)
        ref, report['stock_handlers'] = capture(model, composed[0], out/'stock-handlers', card, wav, False, composed[1])
        actual, report['module'] = capture(model, composed[0], out/'module', card, wav, True, composed[1])
        assert_audio_equal(ref, actual)
        assert report['stock_handlers']['controls'] == report['module']['controls']
        # Count actual stock frame continuations, not scheduler sample/16:
        # UI dispatch follows ISR return, so these blocks are completed.
        # Wrappers may cost instructions within the same delivered block.
        assert [e['audio_block'] for e in report['stock_handlers']['transport']['handlers']] == [e['audio_block'] for e in report['module']['transport']['handlers']], 'transport callback block changed'
        reference_states = report['stock_handlers']['transport']['states']
        actual_states = report['module']['transport']['states']
        assert [(e['value'],e['audio_block']) for e in reference_states] == [(e['value'],e['audio_block']) for e in actual_states], 'actual transport timeline changed'
        report['passed'] = True
    finally:
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def input_clip_cases(model, out, project, composed):
    """One real ADC-stream rail at sample 7 of a block, never a RAM poke."""
    from volume_mixer_probe import boot_port, key, run, peek, u32, watch, hits
    from verify_volume_mixer import load_composed, chord, holds
    out.mkdir(parents=True, exist_ok=True)
    (out/'report.json').write_text(json.dumps({'model':model,'passed':False})+'\n')
    wav = out/'rails.wav'
    # WAV order C,D,A,B. Isolated positive and negative rails, all four
    # inputs, 600 ms apart. Every other sample is below rail, so no hold reload.
    positions = [(16007 + n*26464, (c+2)%4, c, rail)
                 for n,(c,rail) in enumerate((c,r) for c in range(4) for r in (0x7fffff,-0x800000))]
    # A sub-rail carrier holds stock's input gate open. With silence before
    # a pulse, the stock gate's attack attenuates an ADC rail before this
    # module's documented input arena; that is not a rail at our tap.
    carrier = b''.join((v & 0xffffff).to_bytes(3,'little')
                       for v in (0x100000,-0x180000,0x200000,-0x280000))
    stream = bytearray(carrier*(positions[-1][0]+30000))
    for pos,wchan,_,rail in positions:
        stream[pos*12+wchan*3:pos*12+wchan*3+3] = (rail & 0xffffff).to_bytes(3,'little')
    with wave.open(str(wav),'wb') as w:
        w.setparams((4,3,44100,0,'NONE','not compressed')); w.writeframes(stream)
    fixture(project, out / 'fixture')
    project = out / 'fixture/project'
    events, cadence = [], []
    with boot_port(model, composed[0], out, project=project, audio_input=wav) as p:
        p.proc.command('frame off','ok')
        s = load_composed(p,composed)
        chord(p,'REC1')
        assert u32(p,s['vm_page']) == 1 and u32(p,s['vm_window'])
        frame_end = PINS['frame'][2]
        watch(p,[s['vm_draw'],s['vm_frame'],frame_end])
        key(p,'PLAY',True,ms=0)
        p.proc.command('frame on','ok')
        initial = u32(p,s['vm_frame_count'])
        last = [0]*4
        active = {}
        iteration = 0
        count = 0
        while count < 14500:
            # Poll finely around each known input pulse to observe the full
            # reload, rather than inferring it from a later countdown.
            near = any(abs(count - pos//16) < 24 for pos,_,_,_ in positions)
            run(p,0.08 if near else 5)
            records = hits(p)
            frame_records = [r for r in records if r[1] in (s['vm_frame'],frame_end)]
            # run(ms) can stop halfway through an ISR. Finish that producer
            # before pairing its count with the independently aged holds.
            for _ in range(100):
                if not frame_records or frame_records[-1][1] == frame_end:
                    break
                run(p,0.01)
                more = hits(p); records.extend(more)
                frame_records.extend(r for r in more if r[1] in (s['vm_frame'],frame_end))
            else:
                raise AssertionError('frame producer did not finish')
            cadence.extend(r for r in records if r[1] == s['vm_draw'])
            count = u32(p,s['vm_frame_count']) - initial
            if iteration == 16: key(p,'PLAY',False,ms=0)
            iteration += 1
            assert iteration < 10000, 'playing frame producer did not advance to the required duration'
            assert u32(p,s['vm_window']), 'PLAY evicted the volume window'
            current = holds(p,s['vm_clip_hold'])
            snap = holds(p,s['vm_snapshot_holds'])
            for c,h in enumerate(current):
                if h and not last[c]:
                    assert len(events) < len(positions), 'unexpected additional rail'
                    expected = positions[len(events)]
                    assert c == expected[2], (c,expected)
                    assert h == 1379, ('did not observe the full >=500ms reload',c,h,count)
                    assert all(v == 0 for j,v in enumerate(current) if j != c), 'input clip crosstalk'
                    active[c] = {'channel':c, 'rail':expected[3], 'first_frame':count,
                                 'first_hold':h, 'expiry':count+h, 'snapshot_seen':False}
                    events.append(active[c])
                if h:
                    assert count+h == active[c]['expiry'], ('single rail reloaded or hold did not age once/block',count,h,active[c])
                    if snap[c]: active[c]['snapshot_seen'] = True
                elif last[c]:
                    e = active[c]
                    assert e['expiry'] <= count <= e['expiry']+16
                    assert e['snapshot_seen'], 'between-refresh rail never reached display snapshot'
                    e['expired_frame'] = count
            last = current
        assert len(events) == 8 and all('expired_frame' in e for e in events), events
        # 5.25s playing with no page changes: 25Hz, no catch-up draws.
        elapsed = (u32(p,s['vm_frame_count'])-initial)*16/44100
        assert abs(len(cadence)-elapsed*25) <= 2, (len(cadence),elapsed)
        p.proc.command('watch off','ok')
    report = {'model':model, 'passed':True, 'events':events,'playing_draws':len(cadence),'seconds':elapsed,
              'hold_blocks':1379, 'hold_ms':1379*16/44.1}
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report
