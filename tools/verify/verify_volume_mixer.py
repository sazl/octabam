#!/usr/bin/env python3
"""Volume mixer gate: execute linked ColdFire meter code in the stock UI task.

Arithmetic/source cases require no project or DSP. Key/panel/timing cases run
the real stock UI with a disposable saved project. Playing DSP audio remains
a separate integration gate; generated firmware and captures stay under out/.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import struct
import sys

from volume_mixer_probe import (ROOT, STOCK, SCRATCH, assemble, boot_port, call,
                                PINS, MAILBOX_SOURCE, hits, install_mailbox, peek, poke, run, u32, watch,
                                key, tap, screen, layer_list)

METER = ROOT / 'modules/volume-mixer/meter.s'
BASE = SCRATCH + 0x8000
BUFFER = SCRATCH + 0xe000
WRAPPERS = r'''
.text
.globl _start
_start: rts
probe_normalize:
 movel %sp@(4),%d0
 bsr.w vm_normalize
probe_normalize_end: rts
probe_sample:
 moveal %sp@(4),%a0
 movel %sp@(8),%d0
 bsr.w vm_sample_block
probe_sample_end: rts
probe_collect:
 movel %sp@(4),%d7
probe_collect_loop:
 bsr.w vm_collect
probe_collect_return:
 subql #1,%d7
 bne.s probe_collect_loop
probe_collect_end: rts
probe_snapshot:
 movew %sr,%d0
 movew %d0,probe_before_sr
 bsr.w vm_snapshot
probe_snapshot_return:
 movew %sr,%d0
 movew %d0,probe_after_sr
probe_snapshot_end: rts
probe_reset:
 bsr.w vm_reset
probe_reset_end: rts
.balign 4
probe_before_sr: .word 0
probe_after_sr: .word 0
'''


def words(p, address, count=24):
    return list(struct.unpack(f'>{count}I', peek(p, address, count * 4)))


def holds(p, address):
    return list(struct.unpack('>4H', peek(p, address, 8)))


def sample_words(values):
    # Fixtures are signed 24-bit units; a nonzero discarded byte checks shift.
    return struct.pack('>32I', *(((x << 8) & 0xffffffff) | 0x5a for x in values))


def load_meter(p):
    assert METER.is_file(), 'live ColdFire meter implementation is missing'
    blob, symbols = assemble(METER.read_text() + WRAPPERS, p.out / 'meter', BASE)
    assert len(blob) < BUFFER - BASE, 'meter test overlaps its input fixture'
    poke(p, BASE, blob)
    return symbols


def reset(p, s, opened=True):
    call(p, s['probe_reset'])
    poke(p, s['vm_window'], struct.pack('>I', int(opened)))


def sample(p, s, values, first=0):
    blob = sample_words(values)
    poke(p, BUFFER, blob)
    call(p, s['probe_sample'], BUFFER, first)
    assert peek(p, BUFFER, 128) == blob, 'producer wrote its audio source'


def test_signed_peaks(p, s):
    """Catches unsigned abs, omitted >>8, channel crosstalk and missed frames."""
    for magnitude, want in ((0, 0), (0x400000, 64), (0x7fffff, 127), (0x800000, 127),
                            (0x100000, 16), (0x200000, 32), (0x600000, 95)):
        got = call(p, s['probe_normalize'], magnitude)
        assert got == want, f'normalize({magnitude:#x}): expected {want}, got {got}'
    for position in range(16):
        for value in (0x400000, -0x400000, 0x7fffff, -0x800000):
            for channel in (0, 1):
                reset(p, s)
                values = [0] * 32
                values[2 * position + channel] = value
                sample(p, s, values)
                want = [0] * 24
                want[channel] = abs(value)
                assert words(p, s['vm_peak_accum']) == want, (position, value, channel)
    reset(p, s)
    sample(p, s, [0x400000, -0x200000] * 16)
    sample(p, s, [0x100000, -0x600000] * 16)
    assert words(p, s['vm_peak_accum'])[:2] == [0x400000, 0x600000], 'peaks did not accumulate'


def test_clip_hold(p, s):
    """Catches rail-only-at-first-sample, shared clips and unsigned negative rail."""
    for channel in range(4):
        for position in range(16):
            for rail in (0x7fffff, -0x800000):
                reset(p, s)
                values = [0] * 32
                values[position * 2 + channel % 2] = rail
                sample(p, s, values, 16 + (channel // 2) * 2)
                want = [0] * 4
                want[channel] = 1379
                assert holds(p, s['vm_clip_hold']) == want, (channel, position, rail)
    reset(p, s)
    sample(p, s, [0x7ffffe, -0x7fffff] * 16, 16)
    assert holds(p, s['vm_clip_hold']) == [0] * 4, 'neighbors of rail clipped'
    sample(p, s, [0x7fffff, 0] + [0] * 30, 16)
    sample(p, s, [0] * 32, 16)
    assert holds(p, s['vm_clip_hold']) == [1378, 0, 0, 0], 'hold did not age one block'
    sample(p, s, [-0x800000, 0] + [0] * 30, 16)
    assert holds(p, s['vm_clip_hold']) == [1379, 0, 0, 0], 'new clip did not reload hold'
    # Full producer ages each input exactly once per 16-frame block.
    zero_sources(p)
    call(p, s['probe_collect'], 1378)
    assert holds(p, s['vm_clip_hold']) == [1, 0, 0, 0], 'hold expired before 500 ms'
    call(p, s['probe_collect'], 1)
    assert holds(p, s['vm_clip_hold']) == [0] * 4
    call(p, s['probe_collect'], 2)
    assert holds(p, s['vm_clip_hold']) == [0] * 4, 'hold underflowed'
    # Track/output rails must never activate input clips.
    sample(p, s, [-0x800000, 0x7fffff] * 16, 0)
    sample(p, s, [-0x800000, 0x7fffff] * 16, 20)
    assert holds(p, s['vm_clip_hold']) == [0] * 4


def test_snapshot_reset(p, s):
    """Catches non-cleared accumulation, cleared holds and stale reopen data."""
    reset(p, s)
    sample(p, s, [0x400000, -0x800000] * 16, 16)
    call(p, s['probe_snapshot'])
    want = [0] * 24
    want[16:18] = [64, 127]
    assert words(p, s['vm_snapshot_peaks']) == want
    assert words(p, s['vm_peak_accum']) == [0] * 24
    assert holds(p, s['vm_snapshot_holds']) == [0, 1379, 0, 0]
    assert holds(p, s['vm_clip_hold']) == [0, 1379, 0, 0]
    before = int.from_bytes(peek(p, s['probe_before_sr'], 2), 'big')
    after = int.from_bytes(peek(p, s['probe_after_sr'], 2), 'big')
    assert before & 0xff00 == after & 0xff00, 'snapshot left changed interrupt mask/mode'
    sample(p, s, [0x200000, 0] * 16, 16)
    call(p, s['probe_snapshot'])
    assert words(p, s['vm_snapshot_peaks'])[16:18] == [32, 0], 'old peak leaked into next snapshot'
    reset(p, s)
    assert words(p, s['vm_peak_accum']) == words(p, s['vm_snapshot_peaks']) == [0] * 24
    assert holds(p, s['vm_clip_hold']) == holds(p, s['vm_snapshot_holds']) == [0] * 4


def zero_sources(p):
    poke(p, 0x80003190, bytes(0x800))
    poke(p, 0x80005660, bytes(0x800))
    poke(p, 0x80005e60, bytes(0x100))


def source_arenas(p):
    return [peek(p, a, n) for a, n in ((0x80003190, 0x800), (0x80005660, 0x800),
                                      (0x80005e60, 0x100), (0x800000e4, 4), (0x46104d00, 4))]


def test_channel_sources(p, s):
    """Catches reversed track bank, input page mask/order and MAIN/CUE swaps."""
    for bank, page in ((1, 7), (0, 3)):
        reset(p, s)
        zero_sources(p)
        # High selector bits must be masked, not treated as an array index.
        poke(p, 0x800000e4, struct.pack('>I', bank | 0x20))
        poke(p, 0x46104d00, struct.pack('>I', page | 0x10))
        want = [0x10000 * (i + 1) for i in range(24)]
        for track in range(8):
            poke(p, 0x80003190 + bank * 0x400 + track * 0x80,
                 sample_words([want[track * 2], -want[track * 2 + 1]] * 16))
        for offset, first in ((0x80, 16), (0, 18)):
            poke(p, 0x80005660 + page * 0x100 + offset,
                 sample_words([want[first], -want[first + 1]] * 16))
        for address, first in ((0x80005e60, 20), (0x80005ee0, 22)):
            poke(p, address, sample_words([want[first], -want[first + 1]] * 16))
        before = source_arenas(p)
        call(p, s['probe_collect'], 1)
        assert words(p, s['vm_peak_accum']) == want, '24-channel mapping disagrees with selected sources'
        assert source_arenas(p) == before, 'meter changed source audio or selectors'
        assert u32(p, s['vm_frame_count']) == 1


def test_closed_no_work(p, s):
    """Catches collection/aging while closed, even with live rail sources."""
    reset(p, s, opened=False)
    zero_sources(p)
    poke(p, 0x80005e60, sample_words([-0x800000, 0x7fffff] * 16))
    poke(p, s['vm_peak_accum'], struct.pack('>24I', *([123] * 24)))
    poke(p, s['vm_clip_hold'], struct.pack('>4H', 10, 20, 30, 40))
    before = source_arenas(p)
    call(p, s['probe_collect'], 4)
    assert words(p, s['vm_peak_accum']) == [123] * 24
    assert holds(p, s['vm_clip_hold']) == [10, 20, 30, 40]
    assert u32(p, s['vm_frame_count']) == 0
    assert source_arenas(p) == before


def test_frame_hook(p, s):
    """Exercise the real ISR detour; catch stack/live-register/replay damage."""
    addr, expected, continuation = PINS['frame']
    original = peek(p, addr, 6)
    assert original.hex() == expected
    reset(p, s)
    poke(p, addr, b'\x4e\xf9' + struct.pack('>I', s['vm_frame']))
    try:
        watch(p, [s['vm_frame'], continuation])
        p.proc.command('frame on', 'ok')
        run(p, 20)
        p.proc.command('frame off', 'ok')
        records = hits(p)
        assert len(records) >= 100 and len(records) % 2 == 0
        for entry, exit in zip(records[::2], records[1::2]):
            assert entry[1] == s['vm_frame'] and exit[1] == continuation
            # All general registers other than replayed d0, plus stack depth.
            assert entry[3:7] == exit[3:7] and entry[12:] == exit[12:]
            assert 0 <= exit[2] <= 3, 'displaced counter load was not replayed'
        count = u32(p, s['vm_frame_count'])
        assert count == len(records) // 2
        poke(p, s['vm_window'], bytes(4))
        p.proc.command('frame on', 'ok')
        run(p, 20)
        p.proc.command('frame off', 'ok')
        assert u32(p, s['vm_frame_count']) == count, 'closed ISR collected audio'
    finally:
        p.proc.command('frame off', 'ok')
        p.proc.command('watch off', 'ok')
        poke(p, addr, original)


def instruction_costs(p, s):
    reset(p, s)
    zero_sources(p)
    result = {}
    for label, function, end, args in (
            ('producer_silence', 'vm_collect', 'probe_collect_return', (1,)),
            ('snapshot', 'vm_snapshot', 'probe_snapshot_return', ())):
        watch(p, [s[function], s[end]])
        call(p, s['probe_collect' if args else 'probe_snapshot'], *args)
        records = hits(p)
        assert len(records) == 2, (label, records)
        result[label] = records[1][0] - records[0][0]
    # Negative rails exercise abs and repeated clip reloads at every sample.
    rail_block = sample_words([-0x800000] * 32)
    for address in range(0x80003190, 0x80003990, 128):
        poke(p, address, rail_block)
    for address in range(0x80005660, 0x80005f60, 128):
        poke(p, address, rail_block)
    watch(p, [s['vm_collect'], s['probe_collect_return']])
    call(p, s['probe_collect'], 1)
    records = hits(p)
    assert len(records) == 2
    result['producer_negative_rails'] = records[1][0] - records[0][0]
    if 'vm_snapshot_masked' in s and 'vm_snapshot_unmasked' in s:
        watch(p, [s['vm_snapshot_masked'], s['vm_snapshot_unmasked']])
        call(p, s['probe_snapshot'])
        records = hits(p)
        assert len(records) == 2
        result['snapshot_masked_instructions'] = records[1][0] - records[0][0]
    watch(p, [s['vm_reset_masked'], s['vm_reset_unmasked']])
    call(p, s['probe_reset'])
    records = hits(p)
    assert len(records) == 2
    result['reset_masked_instructions'] = records[1][0] - records[0][0]
    poke(p, s['vm_window'], bytes(4))
    watch(p, [s['vm_collect'], s['probe_collect_return']])
    call(p, s['probe_collect'], 1)
    records = hits(p)
    assert len(records) == 2
    result['producer_closed'] = records[1][0] - records[0][0]
    p.proc.command('watch off', 'ok')
    return result


def run_meter_cases(model, case, out):
    report = {'model': model, 'case': case, 'passed': False, 'tests': []}
    try:
        with boot_port(model, STOCK, out) as p:
            p.proc.command('frame off', 'ok')
            install_mailbox(p)
            s = load_meter(p)
            tests = ([test_signed_peaks, test_clip_hold, test_snapshot_reset]
                     if case == 'arithmetic' else [test_channel_sources, test_closed_no_work, test_frame_hook])
            for test in tests:
                test(p, s)
                report['tests'].append(test.__name__)
                print(f'{model}: PASS {test.__name__}', flush=True)
            report['instructions'] = instruction_costs(p, s)
            report['passed'] = True
    finally:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report



PANEL = ROOT / 'modules/volume-mixer/panel.s'
PANEL_WRAPPERS = r"""
.text
probe_open:
 movel %sp@(4),%d0
 bra.w vm_open
probe_refresh:
 movel %sp@(4),%d0
 bra.w vm_refresh
"""


def load_panel(p):
    assert PANEL.is_file(), 'live ColdFire panel implementation is missing'
    # Independent test seam at the proved UI queue head. The production UI
    # detour remains installed at c92 and executes on every real task tick.
    source = MAILBOX_SOURCE.replace('tstl 0x46104ca8\n jmp 0x40056c98',
                                   'pea 0x460d1664\n jmp 0x40056c78')
    blob, syms = assemble(source, p.out / 'mailbox')
    poke(p, SCRATCH, blob)
    assert peek(p, 0x40056c72, 6).hex() == '4879460d1664'
    poke(p, 0x40056c72, b'\x4e\xf9' + struct.pack('>I', SCRATCH))
    p.mailbox = syms
    blob, s = assemble(METER.read_text() + PANEL.read_text() + WRAPPERS + PANEL_WRAPPERS,
                       p.out / 'panel', BASE)
    assert len(blob) < BUFFER - BASE
    poke(p, BASE, blob)
    for name, symbol in [('mixer', 'vm_mixer'), ('midi', 'vm_midi'),
                         ('recorder_router', 'vm_rec_router'), ('ui', 'vm_tick')]:
        addr, expected, _ = PINS[name]
        assert peek(p, addr, len(bytes.fromhex(expected))).hex() == expected
        opcode = b'\x4e\xb9' if name == 'recorder_router' else b'\x4e\xf9'
        patch = opcode + struct.pack('>I', s[symbol])
        patch += b'\x4e\x71' * ((len(bytes.fromhex(expected)) - 6) // 2)
        poke(p, addr, patch)
    run(p, 100)
    return s


def chord(p, modifier, order=False):
    key(p, modifier)
    key(p, 'MIXER')
    for name in ([modifier, 'MIXER'] if order else ['MIXER', modifier]):
        key(p, name, False)


def assert_released(p, s, before):
    assert u32(p, 0x80000012) == before, 'consumed MIDI toggled audio/MIDI mode'
    assert u32(p, 0x460d10d0) == 0, 'REC1 stayed held'
    assert u32(p, 0x460d168c) == 0, 'CUE stayed held'
    assert not u32(p, s['vm_pending_releases']), 'consumed modifier flag leaked'


def test_chords_both_models(p, s):
    """Catches wrong page, lost cached release, stock action leaking through chord."""
    before = u32(p, 0x80000012)
    layers = layer_list(p)
    for page, modifier in enumerate(('MIDI', 'REC1', 'CUE')):
        for order in (False, True):
            record = peek(p, 0x460d1736, 24)
            cue = peek(p, 0x80000009, 1)
            chord(p, modifier, order)
            assert peek(p, 0x460d1736, 24) == record, 'chord changed recorder state'
            assert peek(p, 0x80000009, 1) == cue, 'chord changed cue routing'
            assert u32(p, s['vm_window']), f'{modifier} did not open custom window'
            assert u32(p, s['vm_page']) == page
            assert not u32(p, 0x460e7424), 'chord opened stock MIXER'
            assert_released(p, s, before)
            tap(p, 'NO')
            assert not u32(p, s['vm_window']) and layer_list(p) == layers
    tap(p, 'MIDI')
    assert u32(p, 0x80000012) != before, 'standalone MIDI suppressed'
    tap(p, 'MIDI')
    assert u32(p, 0x80000012) == before
    tap(p, 'MIXER')
    assert u32(p, 0x460e7424) and not u32(p, s['vm_window'])
    tap(p, 'MIXER')
    for name in ('MIDI', 'MIXER'):
        key(p, 'FUNC')
        tap(p, name)
        key(p, 'FUNC', False)
        assert not u32(p, s['vm_window']), f'FUNC+{name} intercepted'
        assert u32(p, 0x80000012) == before
        tap(p, 'NO')


def test_release_after_close(p, s):
    """Catches clearing MIDI-consumption on close and stuck stock modifier layers."""
    before = u32(p, 0x80000012)
    layers = layer_list(p)
    for modifier in ('MIDI', 'REC1', 'CUE'):
        for closer in ('NO', 'MIXER', 'TEMPO'):
            key(p, modifier)
            tap(p, 'MIXER')
            if modifier == 'CUE' and closer == 'TEMPO':
                # CUE+TEMPO is stock's class-0 nudge popup, not the class-5
                # TEMPO window. Call the real opener to force class eviction.
                call(p, 0x40059ef0)
            else:
                tap(p, closer)
            assert not u32(p, s['vm_window']), (modifier, closer)
            key(p, modifier, False)
            assert_released(p, s, before)
            if closer == 'TEMPO':
                tap(p, 'NO')
            assert layer_list(p) == layers, (modifier, closer)


def test_navigation(p, s):
    """Catches wrapping at endpoints and page changes waiting for a timer tick."""
    chord(p, 'MIDI')
    ui_hook(p, s, False)
    try:
        old = 0
        for name, page in [('LEFT', 0), ('RIGHT', 1), ('RIGHT', 2), ('RIGHT', 2),
                           ('LEFT', 1), ('LEFT', 0), ('LEFT', 0)]:
            watch(p, [s['vm_draw']])
            tap(p, name)
            assert u32(p, s['vm_page']) == page
            assert len(hits(p)) == int(page != old), 'page change did not redraw immediately'
            old = page
    finally:
        p.proc.command('watch off', 'ok')
        ui_hook(p, s, True)
        tap(p, 'NO')


def test_eviction(p, s):
    """Catches duplicate layer removal and stale peaks across window eviction."""
    layers = layer_list(p)
    chord(p, 'MIDI')
    watch(p, [0x4003146c])
    tap(p, 'TEMPO')
    removed = [h for h in hits(p) if h[8] == s['VM_LAYER']]
    assert len(removed) == 1, 'custom layer must be removed exactly once'
    assert not u32(p, s['vm_window'])
    p.proc.command('watch off', 'ok')
    tap(p, 'NO')
    assert layer_list(p) == layers
    poke(p, s['vm_peak_accum'], struct.pack('>24I', *([0x7fffff] * 24)))
    chord(p, 'MIDI')
    assert words(p, s['vm_snapshot_peaks']) == [0] * 24
    tap(p, 'NO')


def test_recorder_deferral(p, s):
    """Real saved project mode 4: neither recording edge may escape a chord."""
    bank, track = peek(p, 0x100b14cf, 1)[0], peek(p, 0x100b14cc, 1)[0]
    address = u32(p, 0x46c82456) + 0x8eda2 + bank * 0x18b2 + track
    old = peek(p, address, 1)
    try:
        for mode in (old, b'\x04'):
            poke(p, address, mode)
            for closer in (None, 'NO', 'TEMPO'):
                for order in (False, True):
                    before_record = peek(p, 0x800049d8 + track * 0xa8, 0xa8)
                    watch(p, [0x40097924, 0x400977cc])
                    key(p, 'REC1')
                    assert not hits(p), 'recording press escaped before MIXER'
                    key(p, 'MIXER')
                    if closer:
                        key(p, 'MIXER', False)
                        tap(p, closer)
                        key(p, 'REC1', False)
                    else:
                        for name in (['REC1', 'MIXER'] if order else ['MIXER', 'REC1']):
                            key(p, name, False)
                    assert not hits(p), 'consumed REC1 called recording router'
                    assert peek(p, 0x800049d8 + track * 0xa8, 0xa8) == before_record
                    tap(p, 'NO')
                    assert not u32(p, 0x460d10d0)
            watch(p, [0x40097924, 0x400977cc])
            key(p, 'REC1')
            assert not hits(p), 'standalone recording must be deferred to release'
            key(p, 'REC1', False)
            records = hits(p)
            if mode == b'\x04':
                routers = [h for h in records if h[1] == 0x40097924]
                assert [h[10:12] for h in routers] == [[0x2b, 1], [0x2b, 0]], records
                assert any(h[1] == 0x400977cc for h in records)
    finally:
        poke(p, address, old)
        p.proc.command('watch off', 'ok')



def test_stock_forwarding(p, s):
    """Catches a custom layer swallowing PLAY/STOP or unrelated stock keys."""
    table = 0x400c01f4 if u32(p, 0x46c8d18c) else 0x400bfc10
    handlers = {}
    for i in range(64):
        record = peek(p, table + 26*i, 26)
        if record[0] == 0xff:
            break
        if record[0] in (0x28, 0x27):
            handlers[record[0]] = struct.unpack('>2I', record[2:10])
    assert set(handlers) == {0x28,0x27}
    addresses = sorted({x for pair in handlers.values() for x in pair if x})
    baseline = []
    for opened in (False, True):
        if opened:
            chord(p, 'MIDI')
        watch(p, addresses)
        tap(p, 'PLAY')
        tap(p, 'STOP')
        observed = [h[1] for h in hits(p)]
        if opened:
            assert observed == baseline, ('transport dispatch changed', baseline, observed)
            assert u32(p, s['vm_window']), 'transport closed mixer'
            p.proc.command('watch off', 'ok')
            tap(p, 'NO')
        else:
            baseline = observed
            assert baseline and all(pair[0] in baseline for pair in handlers.values())
    p.proc.command('watch off', 'ok')


def panel_costs_and_preservation(p, s):
    """Real UI hook pairs must preserve every general register and stack depth."""
    result = {}
    for page in range(3):
        call(p, s['probe_open'], page)
        watch(p, [s['vm_tick'], PINS['ui'][2]])
        run(p, 160)
        records = hits(p)
        assert len(records) >= 12 and len(records) % 2 == 0
        counts = []
        for entry, end in zip(records[::2], records[1::2]):
            assert entry[1] == s['vm_tick'] and end[1] == PINS['ui'][2]
            assert entry[2:7] == end[2:7] and entry[12:] == end[12:], 'UI hook corrupted registers'
            counts.append(end[0]-entry[0])
        result[str(page)] = {'min_instructions':min(counts), 'max_instructions':max(counts)}
        p.proc.command('watch off', 'ok')
        call(p, s['vm_close'])
    return result

def ui_hook(p, s, enabled):
    addr, expected, _ = PINS['ui']
    poke(p, addr, b'\x4e\xf9' + struct.pack('>I', s['vm_tick']) if enabled else bytes.fromhex(expected))



def assert_header_raster(p, s, rows, page):
    """A stock-rendered, safely inset glyph is the complete-raster oracle.

    Compare title and counter independently against that actual LCD raster;
    expected glyph assets are read/rendered from the user's image at runtime.
    This catches clipping by the final window compositor, beyond TEXT's own
    surface bounds checks, without embedding firmware font bytes in a test.
    """
    surface = u32(p, s['vm_window']) + 36
    call(p, 0x40035624, surface)
    strings = (('INTERNAL TRACKS', 'EXTERNAL INPUTS', 'MAIN / CUE')[page], f'{page+1}/3')
    widths = []
    for x, value in zip((3, 111), strings):
        poke(p, BUFFER, value.encode('ascii') + b'\0')
        width = call(p, 0x40012f30, 0x400ba876, 0xffffffff, BUFFER)
        widths.append(width)
        call(p, 0x40012bd8, 0x400ba876, surface, x, 32, 0xffffffff, BUFFER)
    poke(p, 0x46c7c72c, struct.pack('>I', 1))
    run(p, 40)
    reference = screen(p, p.out / f'header-reference-{page}.png')
    incomplete = []
    for name, x, width in zip(('title', 'counter'), (3, 111), widths):
        # Restrict the oracle to its safely inset band; stock CLEAR leaves
        # the window's bottom framing rows, which are unrelated to this text.
        glyph_rows = [row[x:x+width] for row in reference[20:40]]
        occupied = [y for y, row in enumerate(glyph_rows) if any(row)]
        assert occupied, f'{name} reference was not rendered'
        expected = glyph_rows[min(occupied):max(occupied)+1]
        assert len(expected) >= 5, f'{name} reference is itself clipped'
        actual = [[1-v for v in row[x:x+width]] for row in rows[:9]]
        matches = any(actual[y:y+len(expected)] == expected for y in range(10-len(expected)))
        if not matches:
            incomplete.append(name)
    assert not incomplete, f'page {page}: incomplete header glyphs: {incomplete}'
    # Leave the actual tested panel on screen for subsequent inspection.
    call(p, s['vm_draw'])
    run(p, 40)

def test_pixels(p, s):
    """Catches L/R collapse, hidden 100 line, wrong volume position, and clipping."""
    ui_hook(p, s, False)
    original = [(a, peek(p, a, n)) for a, n in ((0x80000c50,16),(0x8000002e,2),(0x80000035,2))]
    try:
        poke(p, 0x80000c50, struct.pack('>8H', 0,100,127,88,100,76,104,92))
        poke(p, 0x8000002e, bytes([80,96]))
        poke(p, 0x80000035, bytes([127,0]))
        for page in range(3):
            call(p, s['probe_open'], page)
            levels = [127,0] * 8 + [64,127,32,95] + [127,0,32,64]
            poke(p, s['vm_snapshot_peaks'], struct.pack('>24I', *levels))
            poke(p, s['vm_snapshot_holds'], struct.pack('>4H', 0,1379,0,0))
            watch(p, [s['vm_number']])
            call(p, s['vm_draw'])
            shown = [h[2] for h in hits(p)]
            expected = ([1,0,2,100,3,127,4,88,5,100,6,76,7,104,8,92],
                        [96,96,80,80], [127,0])[page]
            assert shown == expected, ('displayed numbers', page, shown)
            p.proc.command('watch off', 'ok')
            run(p, 40)
            rows = screen(p, p.out / f'page-{page}.png')
            assert len(rows) == 64 and all(len(row) == 128 for row in rows)
            assert sum(sum(row[20:110]) for row in rows[59:64]) > 30, 'footer font clipped below LCD'
            if page == 0:
                assert rows[40][3] and not rows[40][8], 'L/R bars lost separation'
                assert rows[25][1] and not rows[25][4] and rows[25][13], '100 reference missing'
                assert rows[46][14] and rows[25][30] and rows[19][46], '0/100/127 markers misplaced'
            if page == 1:
                assert rows[10][43] and not rows[10][13], 'clipped B label not inverted'
            assert_header_raster(p, s, rows, page)
            call(p, s['vm_close'])
    finally:
        for address, data in original:
            poke(p, address, data)
        ui_hook(p, s, True)


def test_refresh_wrap(p, s):
    """Catches signed timer math, 20 Hz reset drift, and catch-up bursts."""
    call(p, s['probe_open'], 0)
    ui_hook(p, s, False)
    period = 5280000
    try:
        for last, now, due, want in [(0xffff0000, 0x004f90ff, False, 0xffff0000),
                                      (0xffff0000, 0x004f9100, True, 0x004f9100),
                                      (100, period+100+123, True, period+100),
                                      (100, period*2+100, True, period*2+100),
                                      (100, period*3+100, True, period*3+100),
                                      (period*3+100, period*3+101, False, period*3+100)]:
            poke(p, s['vm_last_draw_time'], struct.pack('>I', last))
            watch(p, [s['vm_draw']])
            call(p, s['probe_refresh'], now)
            assert bool(hits(p)) == due, (last, now, due)
            assert u32(p, s['vm_last_draw_time']) == want
        p.proc.command('watch off', 'ok')
    finally:
        poke(p, s['vm_last_draw_time'], peek(p, 0xfc07c00c))
        ui_hook(p, s, True)
    watch(p, [s['vm_draw']])
    start = u32(p, 0xfc07c00c)
    run(p, 2000)
    elapsed = ((u32(p, 0xfc07c00c)-start) & 0xffffffff) / 132000000
    count = len(hits(p))
    assert abs(count - elapsed * 25) <= 1.5, (count, elapsed)
    p.proc.command('watch off', 'ok')
    p.timing_result = {'draws': count, 'seconds': elapsed}
    call(p, s['vm_close'])


def run_panel_cases(model, case, out, project):
    assert project and project.is_dir(), 'key/UI gate requires a disposable saved project'
    report = {'model':model, 'case':case, 'passed':False, 'tests':[]}
    try:
        with boot_port(model, STOCK, out, project=project) as p:
            p.proc.command('frame off', 'ok')
            s = load_panel(p)
            tests = {'keys':[test_chords_both_models, test_release_after_close, test_navigation,
                             test_eviction, test_stock_forwarding, test_recorder_deferral],
                     'panels':[test_pixels], 'timing':[test_refresh_wrap]}[case]
            for test in tests:
                test(p, s)
                report['tests'].append(test.__name__)
                print(f'{model}: PASS {test.__name__}', flush=True)
            if case == 'timing':
                report['ui_costs'] = panel_costs_and_preservation(p, s)
                report['tests'].append('panel_costs_and_preservation')
            if hasattr(p, 'timing_result'):
                report['timing'] = p.timing_result
            report['passed'] = True
    finally:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('remix', nargs='?', default='volume-mixer')
    parser.add_argument('--case', choices=('arithmetic', 'sources', 'keys', 'panels', 'timing'), required=True,
                        help='focused runtime gates; full module integration follows registration')
    parser.add_argument('--model', choices=('mki', 'mkii', 'both'), default='both')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/volume-mixer/verify')
    parser.add_argument('--project', type=Path, default=ROOT / 'out/volume-mixer/source-project')
    args = parser.parse_args()
    for model in ('mki', 'mkii') if args.model == 'both' else (args.model,):
        report = (run_meter_cases(model, args.case, args.out / model / args.case)
                  if args.case in ('arithmetic', 'sources') else
                  run_panel_cases(model, args.case, args.out / model / args.case, args.project))
        print(f'{model}: PASS {args.case}', flush=True)

if __name__ == '__main__':
    main()
