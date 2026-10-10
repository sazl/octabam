#!/usr/bin/env python3
"""Measured stock 1.40C mixer contracts. Generated evidence stays under out/.

Uses the real firmware's key dispatcher, window compositor and panel UART.
The temporary mailbox is injected only after stock key/cadence measurements;
it calls stock APIs in the UI task, never synthesizes a framebuffer.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import shutil
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/panel')]
import toolpath  # noqa: E402,F401
import emu_card  # noqa: E402
from panel_server import PortProc, PortRt  # noqa: E402
from panel_link import PanelLink  # noqa: E402

STOCK = ROOT / 'out/raw/section_3_MAIN_OS.bin'
KEYS = {'MIDI': (0x26, 0x20), 'MIXER': (0x26, 1), 'REC1': (0x25, 8),
        'CUE': (0x25, 4), 'FUNC': (0x25, 0x20), 'NO': (0x26, 4),
        'YES': (0x26, 2), 'TEMPO': (0x23, 1), 'PLAY': (0x25, 1),
        'STOP': (0x24, 0x80), 'LEFT': (0x26, 0x10), 'RIGHT': (0x24, 2)}
PINS = {'mixer': (0x4007d468, '4ab9460e7424', 0x4007d46e),
        'midi': (0x40055678, '2f032f02262f000c', 0x40055680),
        'ui': (0x40056c92, '4ab946104ca8', 0x40056c98),
        'frame': (0x4000d98e, '203980004800', 0x4000d994),
        'recorder_router': (0x40030ffc, '4eb940097924', 0x40031002)}
SCRATCH = 0x45ff0000  # probe only: no project/audio pool is mounted


def peek(port, addr, size=4):
    return bytes(port.rt.uc.mem_read(addr, size))


def poke(port, addr, data):
    port.rt.uc.mem_write(addr, data)


def u32(port, addr):
    return int.from_bytes(peek(port, addr), 'big')


def run(port, ms):
    # Bound each request and feed only genuine panel UART output.
    left = ms
    while left > 0:
        part = min(100, left)
        reason = port.rt.run(ms=part)
        assert reason in ('time', 'gate'), reason
        port.rt.poll_tx()
        port.link.feed(bytes(port.rt.uart64.tx[port.tx_pos:]))
        port.tx_pos = len(port.rt.uart64.tx)
        left -= part


def key(port, name, down=True, ms=80):
    row, mask = KEYS[name]
    old = port.rows.get(row, 0)
    port.rows[row] = old | mask if down else old & ~mask
    port.proc.command(f'key {row} {port.rows[row]}', 'ok')
    run(port, ms)


def tap(port, name):
    key(port, name)
    key(port, name, False)


def screen(port, path=None):
    if path is not None:
        Path(path).write_bytes(port.link.render_png(3))
    return port.link.lcd_rows()


@contextlib.contextmanager
def boot_port(model: str, image: Path, out: Path, project: Path | None = None,
              audio_input: Path | None = None):
    assert model in ('mki', 'mkii')
    out.mkdir(parents=True, exist_ok=True)
    tree = out / 'card-tree'
    (tree / 'OCTABAM/AUDIO').mkdir(parents=True, exist_ok=True)
    if project is not None:
        shutil.copytree(project, tree / 'OCTABAM/PROBE', dirs_exist_ok=True)
    card = out / 'card.img'
    card.write_bytes(emu_card.build_image(str(tree), size_mb=64))
    argv = [str(ROOT / 'out/emu/ot_emu'), '--image', str(image), '--card', str(card), '--interactive']
    if project is not None:
        argv.extend(['--mount', '--set', 'OCTABAM', '--project', 'PROBE', '--internal-clock'])
    if audio_input is not None:
        argv.extend(['--dsp', '--audio-in', str(audio_input), '--rtc', 'off'])
    if model == 'mkii':
        argv.append('--mkii')
    proc = PortProc(argv, log_path=out / 'port.log')
    try:
        proc.wait_ready(900)
        class Port: pass
        port = Port()
        port.proc, port.rt, port.link = proc, PortRt(proc), PanelLink()
        port.rows, port.tx_pos, port.out = {}, 0, out
        run(port, 400)
        tap(port, 'YES')  # dismiss clock dialog through the firmware
        yield port
    finally:
        proc.quit()
        (out / 'boot-report.txt').write_text('\n'.join(proc.log))


def watch(port, addresses):
    port.proc.command('watch ' + ','.join(hex(a) for a in addresses), 'ok')
    hits(port)


def hits(port):
    return [[int(x, 16) for x in record.split(':')]
            for record in port.proc.command('hits', 'hits').split()[2:]]


def assemble(source, out, base=SCRATCH):
    out.mkdir(parents=True, exist_ok=True)
    (out / 'probe.s').write_text(source)
    subprocess.run(['m68k-elf-as', '-mcpu=54455', '-o', str(out / 'probe.o'), str(out / 'probe.s')], check=True)
    subprocess.run(['m68k-elf-ld', '-Ttext', hex(base), '-o', str(out / 'probe.elf'), str(out / 'probe.o')], check=True)
    subprocess.run(['m68k-elf-objcopy', '-O', 'binary', str(out / 'probe.elf'), str(out / 'probe.bin')], check=True)
    nm = subprocess.check_output(['m68k-elf-nm', str(out / 'probe.elf')], text=True)
    symbols = {parts[2]: int(parts[0], 16) for line in nm.splitlines() if len(parts := line.split()) == 3}
    return (out / 'probe.bin').read_bytes(), symbols


MAILBOX_SOURCE = r'''
.text
.globl _start
_start:
 lea %sp@(-60),%sp
 movem.l %d0-%d7/%a0-%a6,%sp@
 addql #1,ticks
 tstl request
 beq.w done
 lea args,%a0
 movel %a0@(28),%sp@-
 movel %a0@(24),%sp@-
 movel %a0@(20),%sp@-
 movel %a0@(16),%sp@-
 movel %a0@(12),%sp@-
 movel %a0@(8),%sp@-
 movel %a0@(4),%sp@-
 movel %a0@,%sp@-
 moveal request,%a0
 jsr %a0@
 lea %sp@(32),%sp
 movel %d0,result
 clrl request
done:
 movem.l %sp@,%d0-%d7/%a0-%a6
 lea %sp@(60),%sp
 tstl 0x46104ca8
 jmp 0x40056c98
close:
 addql #1,evictions
 pea window
 jsr 0x40055db4
 addql #4,%sp
 pea layer
 jsr 0x4003146c
 addql #4,%sp
 rts
.balign 4
ticks: .long 0
request: .long 0
result: .long 0
args: .space 32
window: .long 0
evictions: .long 0
layer: .long 0,keys,encs,0,0,-1,-1
keys:
 .byte 0x32,0
 .long close,0,0,0,0
 .word 0,0
 .byte 0xff,0
 .long 0,0,0,0,0
 .word 0,0
encs:
 .byte 0xff,0
 .long 0,0,0,0,0
'''


def install_mailbox(port):
    blob, syms = assemble(MAILBOX_SOURCE, port.out / 'mailbox')
    assert len(blob) < 4096
    poke(port, SCRATCH, blob)
    addr, expected, _ = PINS['ui']
    assert peek(port, addr, 6).hex() == expected
    poke(port, addr, b'\x4e\xf9' + struct.pack('>I', SCRATCH))
    port.mailbox = syms
    run(port, 100)
    assert u32(port, syms['ticks']) >= 4


def call(port, addr, *args):
    """Execute a stack-ABI call inside the stock UI task (probe mailbox)."""
    s = port.mailbox
    assert len(args) <= 8 and not u32(port, s['request'])
    poke(port, s['args'], struct.pack('>8I', *(list(args) + [0] * (8 - len(args)))))
    poke(port, s['request'], struct.pack('>I', addr))
    for _ in range(20):
        run(port, 20)
        if not u32(port, s['request']):
            return u32(port, s['result'])
    raise AssertionError(f'UI call {addr:#x} did not return')


def layer_list(port):
    result, ptr = [], u32(port, 0x460d165c)
    while ptr:
        assert ptr not in result and len(result) < 32
        result.append(ptr)
        ptr = u32(port, ptr)
    return result


def state(port, label):
    rows = screen(port, port.out / f'{label}.png')
    return {'label': label, 'midi': u32(port, 0x80000012),
            'cue_held': peek(port, 0x460d168f, 1).hex(),
            'key_rows': peek(port, 0x46100b18, 8).hex(),
            'record_state': peek(port, 0x460d1736, 24).hex(),
            'rec1_held': u32(port, 0x460d10d0),
            'cue_mask': peek(port, 0x80000009, 1).hex(),
            'mixer_window': u32(port, 0x460e7424),
            'lcd_sha256': hashlib.sha256(bytes(v for row in rows for v in row)).hexdigest(),
            'hits': hits(port)}


def disassemble(out):
    ranges = [(0x4007d468, 0x4007d4a0), (0x40055678, 0x4005571e),
              (0x40056c72, 0x40056ca0), (0x4000d982, 0x4000d9b0),
              (0x4007e760, 0x4007e818), (0x40030ffc, 0x40031008),
              (0x40097924, 0x40097a64)]
    text = '\n'.join(subprocess.check_output([
        'm68k-elf-objdump', '-D', '-b', 'binary', '-m', 'm68k:isa-c',
        '--adjust-vma=0x40000400', f'--start-address={start}', f'--stop-address={end}', str(STOCK)], text=True)
                     for start, end in ranges)
    (out / 'stock-disassembly.txt').write_text(text)


FRAME_SOURCE = r'''
.text
.globl _start
_start:
 lea %sp@(-60),%sp
 movem.l %d0-%d7/%a0-%a6,%sp@
 lea early,%a2
 bsr.w capture
 addql #1,early_count
 movel 0x80004800,%d0
 movel %d0,old_counter
 movem.l %sp@,%d0-%d7/%a0-%a6
 lea %sp@(60),%sp
 movel 0x80004800,%d0
 jmp 0x4000d994
late_hook:
 lea %sp@(-60),%sp
 movem.l %d0-%d7/%a0-%a6,%sp@
 lea late,%a2
 bsr.w capture
 addql #1,late_count
 movel 0x80004800,%d0
 movel %d0,new_counter
 lea early,%a0
 lea late,%a1
 movel #386,%d2
compare:
 movel %a0@+,%d0
 cmpl %a1@+,%d0
 beq.s equal
 addql #1,mismatches
equal:
 subql #1,%d2
 bne.s compare
 movem.l %sp@,%d0-%d7/%a0-%a6
 lea %sp@(60),%sp
 clrl 0x46104d4e
 jmp 0x4000d9a6
capture:
 movel 0x800000e4,%d0
 movel %d0,%a2@+
 andil #1,%d0
 lsll #8,%d0
 lsll #2,%d0
 addil #0x80003190,%d0
 moveal %d0,%a0
 movel #256,%d2
 bsr.s copy
 movel 0x46104d00,%d0
 movel %d0,%a2@+
 andil #7,%d0
 lsll #8,%d0
 addil #0x80005660,%d0
 moveal %d0,%a0
 moveq #64,%d2
 bsr.s copy
 lea 0x80005e60,%a0
 moveq #64,%d2
copy:
 movel %a0@+,%a2@+
 subql #1,%d2
 bne.s copy
 rts
.balign 4
early_count: .long 0
late_count: .long 0
mismatches: .long 0
old_counter: .long 0
new_counter: .long 0
early: .space 1544
late: .space 1544
'''


def frame_contract(port):
    base = SCRATCH + 0x4000
    blob, s = assemble(FRAME_SOURCE, port.out / 'frame-probe', base)
    assert len(blob) < 4096
    poke(port, base, blob)
    sites = {PINS['frame'][0]: s['_start'], 0x4000d9a0: s['late_hook']}
    originals = {addr: peek(port, addr, 6) for addr in sites}
    for addr, target in sites.items():
        poke(port, addr, b'\x4e\xf9' + struct.pack('>I', target))
    port.proc.command('frame on', 'ok')
    run(port, 25)
    port.proc.command('frame off', 'ok')
    count = u32(port, s['early_count'])
    assert count == u32(port, s['late_count']) and count >= 60
    assert u32(port, s['mismatches']) == 0
    assert u32(port, s['new_counter']) == (u32(port, s['old_counter']) + 1) & 3
    snapshot = peek(port, s['early'], 1544)
    for addr, data in originals.items():
        poke(port, addr, data)
    return {'frames': count, 'sample_and_selector_mismatches': 0,
            'track_bank': int.from_bytes(snapshot[:4], 'big'),
            'input_page': int.from_bytes(snapshot[1028:1032], 'big'),
            'compared_words_per_frame': 386,
            'note': 'Both sites read identical complete source arenas; no project/DSP audio is asserted here.'}


def key_table_contract(port, model):
    table = 0x400bfc10 if model == 'mki' else 0x400c01f4
    wanted = {0x30: (0x4007d468, 0x4007d320),
              0x35: (0x40055678, 0x40055678),
              0x2b: (0x40030e6c, 0x40030e6c),
              0x2a: (0x4004e978, 0x4004e968)}
    observed = {}
    for i in range(64):
        record = peek(port, table + 26 * i, 26)
        code = record[0]
        if code == 0xff:
            break
        if code in wanted:
            callbacks = struct.unpack('>2I', record[2:10])
            assert callbacks == wanted[code]
            observed[hex(code)] = [hex(x) for x in callbacks]
    assert len(observed) == len(wanted)
    return {'table': hex(table), 'press_release_handlers': observed}


def occupied_claims(registry=None):
    """Qualify selected hooks against the ledger's fixed-address write spans."""
    if registry is None:
        from remix.registry import modules
        registry = modules()
    result = []
    for name, module in registry.items():
        spans = list(module.write_spans())
        # The ledger also reserves six bytes at a fixed linked-unit base.
        spans.extend(('cave', unit.cave_addr, 6, f'linked unit {unit.label}')
                     for unit in module.linked if unit.cave_addr is not None)
        for kind, start, size, label in spans:
            result.append({'module': name, 'kind': kind, 'site': hex(start),
                           'size': size, 'label': label})
            for pin, (addr, data, _) in PINS.items():
                assert addr + len(data) // 2 <= start or start + size <= addr, (
                    f'{name}: {kind} {label} at {start:#x}+{size} overlaps {pin}')
    return result


def test_occupied_claims():
    """Reject non-detour writes and the asserted tail beyond a shorter pad."""
    from remix.schema import Detour, Kind, Module, Poke
    addr = PINS['ui'][0]
    cases = [
        Module('probe-poke', 'PROBE POKE', Kind.CF_PATCH, 'synthetic overlap',
               pokes=(Poke(addr + 5, b'\0', b'\0'),)),
        Module('probe-detour', 'PROBE DETOUR', Kind.CF_PATCH, 'asserted tail',
               detours=(Detour(addr - 7, bytes(8), target=0, pad_to=6),)),
    ]
    for module in cases:
        try:
            occupied_claims({module.key: module})
        except AssertionError as exc:
            assert 'overlaps ui' in str(exc), str(exc)
        else:
            raise AssertionError(f'{module.key} overlap was not rejected')
    adjacent = Module('probe-adjacent', 'PROBE ADJACENT', Kind.CF_PATCH, 'adjacent',
                      pokes=(Poke(addr + 6, b'\0', b'\0'),))
    result = occupied_claims({adjacent.key: adjacent})
    assert result[0]['kind'] == 'poke' and result[0]['size'] == 1


def run_contract_probe(model: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    report = {'model': model, 'passed': False, 'traces': [], 'pins': PINS,
              'occupied_claims': occupied_claims(),
              'stock_sha256': hashlib.sha256(STOCK.read_bytes()).hexdigest()}
    try:
        disassemble(out)
        with boot_port(model, STOCK, out) as p:
            for name, (addr, expected, _) in PINS.items():
                assert peek(p, addr, len(expected) // 2).hex() == expected, name
            assert bool(u32(p, 0x46c8d18c)) == (model == 'mkii')
            report['key_table'] = key_table_contract(p, model)
            watch(p, [PINS['ui'][0]])
            run(p, 1000)
            idle = hits(p)
            assert 59 <= len(idle) <= 61, len(idle)
            report['idle_ui_hits_per_second'] = len(idle)
            watch(p, [PINS['mixer'][0], PINS['midi'][0], 0x40030e6c, 0x40097924])
            mixer_before = state(p, 'standalone_mixer_before')
            tap(p, 'MIXER')
            mixer_open = state(p, 'standalone_mixer_open')
            assert mixer_open['mixer_window']
            tap(p, 'MIXER')
            mixer_closed = state(p, 'standalone_mixer_closed')
            assert mixer_closed['mixer_window'] == 0
            report['traces'].extend([mixer_before, mixer_open, mixer_closed])
            for modifier in ('MIDI', 'REC1', 'CUE'):
                for order in ('modifier_first', 'mixer_first'):
                    tag = modifier.lower() + '_' + order
                    before = state(p, tag + '_before')
                    key(p, modifier)
                    down = state(p, tag + '_down')
                    key(p, 'MIXER')
                    opened = state(p, tag + '_opened')
                    assert opened['mixer_window'] != 0
                    assert any(h[1] == PINS['mixer'][0] for h in opened['hits'])
                    first, second = (modifier, 'MIXER') if order == 'modifier_first' else ('MIXER', modifier)
                    key(p, first, False)
                    release1 = state(p, tag + '_release1')
                    key(p, second, False)
                    after = state(p, tag + '_after')
                    assert after['cue_held'] == '00'
                    assert after['rec1_held'] == 0
                    if modifier == 'REC1':
                        assert down['rec1_held'] == 1
                    if modifier == 'CUE':
                        assert down['cue_held'] == '01'
                    assert after['key_rows'] == '00' * 8
                    assert after['record_state'] == before['record_state']
                    assert after['cue_mask'] == before['cue_mask']
                    assert down['midi'] == before['midi']
                    if modifier == 'MIDI':
                        assert after['midi'] == 1 - before['midi']
                        assert any(h[1] == PINS['midi'][0] and h[8:10] == [0x35, 0]
                                   for st in (release1, after) for h in st['hits'])
                    else:
                        assert after['midi'] == before['midi']
                    report['traces'].extend([before, down, opened, release1, after])
                    tap(p, 'NO')
                before = state(p, modifier.lower() + '_tap_before')
                tap(p, modifier)
                after = state(p, modifier.lower() + '_tap_after')
                if modifier == 'MIDI':
                    assert after['midi'] == 1 - before['midi']
                    tap(p, 'MIDI')
                report['traces'].extend([before, after])
            for name in ('MIDI', 'MIXER'):
                before = state(p, 'func_' + name.lower() + '_before')
                key(p, 'FUNC'); tap(p, name); key(p, 'FUNC', False)
                after = state(p, 'func_' + name.lower() + '_after')
                assert before['midi'] == after['midi']
                assert before['lcd_sha256'] != after['lcd_sha256']
                report['traces'].extend([before, after])
                tap(p, 'NO')
            watch(p, [PINS['frame'][0], 0x4000d99a, 0x4000d9a0])
            p.proc.command('frame on', 'ok')
            run(p, 20)
            frame_hits = hits(p)
            counts = {hex(a): sum(h[1] == a for h in frame_hits)
                      for a in (PINS['frame'][0], 0x4000d99a, 0x4000d9a0)}
            assert len(set(counts.values())) == 1 and min(counts.values()) >= 50, counts
            report['frame_hits_20ms'] = counts
            report['frame_trace'] = frame_hits
            p.proc.command('frame off', 'ok')
            p.proc.command('watch off', 'ok')
            report['frame_source_contract'] = frame_contract(p)
            install_mailbox(p)
            layers_before = layer_list(p)
            s = p.mailbox
            window = call(p, 0x4005829c, 128, 64, 0xffffffff, 0, 5, s['close'])
            assert window
            poke(p, s['window'], struct.pack('>I', window))
            call(p, 0x40031494, s['layer'])
            assert layer_list(p) == layers_before + [s['layer']]
            call(p, 0x40035624, window + 36)
            # Draw four isolated corner squares via the stock BOX primitive.
            for x, y in ((0, 0), (125, 0), (0, 61), (125, 61)):
                call(p, 0x40012254, window + 36, x, y, x + 2, y + 2, 1)
            poke(p, 0x46c7c72c, struct.pack('>I', 1))
            run(p, 100)
            rows = screen(p, out / 'custom-128x64.png')
            assert all(rows[y][x] for x, y in ((0, 0), (127, 0), (0, 63), (127, 63))), 'window clips edge pixels'
            assert sum(sum(row) for row in rows) == 36, 'corner squares have unexpected pixels'
            tap(p, 'TEMPO')  # same class: real stock create evicts our custom window
            assert u32(p, s['evictions']) == 1
            assert u32(p, s['window']) == 0
            assert s['layer'] not in layer_list(p)
            screen(p, out / 'evicted.png')
            tap(p, 'NO')
            assert layer_list(p) == layers_before
            key(p, 'CUE')
            cue_layers = layer_list(p)
            window = call(p, 0x4005829c, 128, 64, 0xffffffff, 0, 5, s['close'])
            poke(p, s['window'], struct.pack('>I', window))
            call(p, 0x40031494, s['layer'])
            tap(p, 'NO')
            assert not u32(p, s['window']) and layer_list(p) == cue_layers
            assert u32(p, 0x460d168c) == 1
            key(p, 'CUE', False)
            assert u32(p, 0x460d168c) == 0
            assert layer_list(p) == layers_before
            report['window'] = {'size': [128, 64], 'all_four_corners': True,
                                'eviction_callbacks': 1, 'layer_removed': True,
                                'explicit_close_with_cue_held': True,
                                'layers_before': layers_before}
            report['limitations'] = [
                'Stock key baselines use a fresh card, not every recorder configuration.',
                '0x40030e6c can route REC1 edges to 0x40097924 depending on recorder configuration; production must prevent this for a consumed chord.',
                'Source equivalence is measured in the ColdFire frame ISR without a playing DSP project; audio stage meaning follows existing USB/STEM evidence and needs module audio gates.',
                'Hardware timing is unmeasured.']
            report['passed'] = True
    finally:
        (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def project_recorder_contract(model, project, out):
    """A copied real project plus one temporary RAM mode change exposes REC1's
    recording-producing path. This is a contract finding, not a chord fix.
    """
    report = {'model': model, 'project_supplied': True, 'passed': False, 'traces': []}
    try:
        with boot_port(model, STOCK, out, project=project) as p:
            bank = peek(p, 0x100b14cf, 1)[0]
            track = peek(p, 0x100b14cc, 1)[0]
            mode_addr = u32(p, 0x46c82456) + 0x8eda2 + bank * 0x18b2 + track
            old_mode = peek(p, mode_addr, 1)
            report.update(mode_address=hex(mode_addr), original_mode=old_mode.hex(), track=track)
            watch(p, [0x40030ffc, 0x40097924, 0x400977cc, 0x40005178])
            for phase in ('saved_project', 'mode4_recorder_path'):
                if phase == 'mode4_recorder_path':
                    poke(p, mode_addr, b'\x04')
                before = peek(p, 0x800049d8 + track * 0xa8, 0xa8)
                for name, down in [('REC1', True), ('MIXER', True), ('MIXER', False), ('REC1', False)]:
                    key(p, name, down)
                    trace = {'phase': phase, 'key': name, 'down': down,
                             'held': u32(p, 0x460d10d0), 'hits': hits(p),
                             'recorder_record': peek(p, 0x800049d8 + track * 0xa8, 0xa8).hex()}
                    report['traces'].append(trace)
                    if phase == 'mode4_recorder_path' and name == 'REC1':
                        router = [h for h in trace['hits'] if h[1] == 0x40097924]
                        assert len(router) == 1 and router[0][10:12] == [0x2b, int(down)]
                        if down:
                            assert any(h[1] == 0x400977cc for h in trace['hits']), 'record command not observed'
                            assert any(h[1] == 0x40030ffc for h in trace['hits'])
                tap(p, 'NO')
                hits(p)
            poke(p, mode_addr, old_mode)
            report['required_behavior'] = (
                'Defer the recorder-producing call at 0x40030ffc on REC1 press; '
                'cancel saved press and release for MIXER. For a standalone '
                'release replay saved press then release. This changes recording '
                'start from press to release; no non-MIXER flush contract is proved.')
            report['passed'] = True
    finally:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'recorder-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=('mki', 'mkii', 'both'), default='both')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/volume-mixer/contracts')
    parser.add_argument('--project', type=Path, required=True,
                        help='saved project required for complete recorder-path qualification; copied onto scratch cards')
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error('--project must name an existing saved-project directory')
    test_occupied_claims()
    for model in ('mki', 'mkii') if args.model == 'both' else (args.model,):
        result = run_contract_probe(model, args.out / model)
        print(f'{model}: stock baseline verified, UI {result["idle_ui_hits_per_second"]} Hz; recorder proof pending', flush=True)
        project_recorder_contract(model, args.project, args.out / model / 'project-recorder')
        print(f'{model}: PASS complete stock contracts including recorder-path proof', flush=True)

if __name__ == '__main__':
    main()
