#!/usr/bin/env python3
"""Volume mixer gate: execute linked ColdFire meter code in the stock UI task.

Arithmetic/source cases require no project or DSP. Later UI/audio cases extend
this gate; generated assembly, firmware and reports belong only under out/.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import struct
import sys

from volume_mixer_probe import (ROOT, STOCK, SCRATCH, assemble, boot_port, call,
                                PINS, hits, install_mailbox, peek, poke, run, u32, watch)

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('remix', nargs='?', default='volume-mixer')
    parser.add_argument('--case', choices=('arithmetic', 'sources'), required=True,
                        help='focused producer gates; full module gate is not yet implemented')
    parser.add_argument('--model', choices=('mki', 'mkii', 'both'), default='both')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/volume-mixer/verify')
    args = parser.parse_args()
    for model in ('mki', 'mkii') if args.model == 'both' else (args.model,):
        report = run_meter_cases(model, args.case, args.out / model / args.case)
        print(f'{model}: PASS {args.case}; instruction counts {report["instructions"]}', flush=True)

if __name__ == '__main__':
    main()
