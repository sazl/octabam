# `volume-mixer` — VOLUME MIXER

A read-only, full-screen 128 × 64 volume view for OS 1.40C on MKI and MKII.
Hold **MIDI**, **REC1** or **CUE**, then press **MIXER** to open the track,
input or output page respectively. LEFT/RIGHT change pages and stop at the
ends; NO or MIXER closes the window. Ordinary MIXER, MIDI and FUNC chords
use stock behavior. Encoders and PLAY/STOP continue through stock dispatch.
No independent gain control is added.

Tracks show separate L/R activity **post-FX, pre-LEVEL** alongside each
track's existing LEVEL byte (two-byte track stride). Inputs A–D show raw input activity; A/B display
their common stock gain, and C/D theirs. MAIN/CUE show separate L/R final
digital output feeds beside existing main/cue volume. Every scale is 0–127
with a fixed horizontal 100 reference. The input tap is the stock ColdFire input arena, after the stock input
gate. An ADC rail attenuated by that gate does not reach this meter as a
rail. These are digital signal stages, not measurements of analog headroom.

The producer scans every sample of every 16-frame block. Input labels
invert and gain `!` after either signed digital rail, held for 1379 blocks
(500.317 ms at 44.1 kHz). This detects the digital sample rails, not analog
headroom or ADC overload that never reaches a rail. Peaks accumulate
between UI snapshots. Refresh is 25 Hz with immediate page changes;
late UI ticks draw once and reset cadence without catch-up bursts. Closed
views skip collection; reopening discards stale peaks and clips.

## Key and window cost

For native recorder TRIG=ONE or ONE2, REC1's recording press is deferred
until release so REC1 then MIXER can consume both recording edges.
Standalone recording in that context starts **on release**. ONE2 takes ended
at the same later REC2 time are shorter by that deferred interval; this is
an intentional timing tradeoff, not recording-duration invariance. TRIG=HOLD
keeps stock press-to-start/release-to-stop timing; when REC1 is recording,
REC1+MIXER opens the stock mixer. Reach the input page with **MIDI+MIXER,
then RIGHT**. REC1 without a recording role still opens inputs directly.

Deferred presses cancel when the selected track, part, bank, machine,
TRIG or stock recording context changes. Consumed releases survive close
and eviction; a later press retires any stale consumed marker after a
release routed elsewhere. Raw-up alone cannot cancel a deferred press:
the queued release callback may still need it. CUE releases use stock.

The full-screen class-5 stock window is evicted by another class-5 view
(for example the stock TEMPO opener). Its close callback removes the
custom input layer once. MIDI/REC1 release bookkeeping survives eviction.
TUNER uses different hook spans, so the ledger allows their composition;
stock window priority still determines which view remains visible.

## Build and ownership

Two GNU-as units, `meter.s` and `panel.s`, are linked together in the
platform's DRAM runtime (`Kind.CF_PATCH`), depacked at boot. The platform
reserves 10 MiB from sample/recorder memory when any DRAM module is present.
No DSP code, FX id, ROM cave, parameter descriptor or independent audio
buffer is claimed. Per-module state lives in the linked image; it does
not claim fixed runtime RAM elsewhere. The frame interrupt only samples
proved audio arenas and updates integers; window creation, snapshots,
normalization and drawing run in the UI task.

All stock detours assert complete instruction bytes before writing:

| Site | Target | Kind / displaced span |
| --- | --- | --- |
| `0x4000d98e` | `vm_frame` | JMP, 6 bytes; replay counter load |
| `0x4007d468` | `vm_mixer` | JMP, 6 bytes; stock MIXER fallback |
| `0x40055678` | `vm_midi` | JMP + NOP, 8 bytes; replay prologue |
| `0x40030ffc` | `vm_rec_router` | JSR, 6 bytes; callable router replacement |
| `0x40056c92` | `vm_tick` | JMP, 6 bytes; replay UI test |

The ledger refuses another module occupying any asserted span, naming
both owners. No declared TUNER conflict is required. The source is local
rather than a port of an author's separate build. Reused approaches are
credited to Tim Hastie's [octatrick-modules](https://github.com/timhastie/octatrick-modules)
(TUNER's DRAM-linked integer observer, stock window/input lifecycle and UI
refresh) and octabam's USB AUDIO/STEM source-arena investigation and
platform placement/stock drawing harnesses. Upstream sources remain
untouched; no Elektron firmware or font bytes are stored in the repo.

## Port measurements and gates

Proof is `PORT`: arithmetic, key lifecycle, firmware LCD pixels, refresh
and playing DSP audio are tested under both model modes. No hardware
proof is claimed.
Observed instruction counts are identical on MKI/MKII; they are emulator
instructions, **not chip cycles, hardware timing or WCET**:

| Work | Instructions |
| --- | ---: |
| Producer, silence / negative rails | 4766 / 5302 |
| Closed collection | 3 |
| Snapshot / masked exchange | 514 / 143 |
| Masked reset | 160 |
| UI hook, not due (no pending gesture) | 23 |
| Tracks / inputs / outputs redraw | 22321 / 13929 / 13019 |

Two seconds without keys produced 49 draws on each model (2.0022 / 2.0027 s).
Interrupt-masked snapshot/reset operations restore the saved SR. The
producer and UI-hook gates trace preserved registers and stack depth.

```bash
make modules
make docs
OT_PROJECT=<saved-project-copy> make check REMIX=volume-mixer
OT_PROJECT=<saved-project-copy> .venv/bin/python3 tools/verify/verify_volume_mixer.py volume-mixer
.venv/bin/python3 tools/verify/verify_volume_mixer.py --case registration
```

The registered default gate runs every arithmetic/source/key/panel/timing
and playing-DSP/recorder case on both models against the **actual composed runtime**, verifies its
boot-depacked executable bytes and installed detours, then calls its
symbols through test-only wrappers. Only the call mailbox/wrappers are
injected. If TUNER occupies the mailbox's UI queue-head site, the test
mailbox chains its validated composed target; PC/register/stack traces
prove TUNER still executes and replays the queue-head instruction.
Focused arithmetic/UI `--case` runs preserve standalone source-linked
fixtures for local debugging; audio/input/recorder cases always use the composed
image. UI and audio cases require a saved project (`--project`,
`OT_PROJECT`, or ignored `out/volume-mixer/source-project`); missing data
is an error. Every boot uses a disposable card copy. Generated firmware,
disassembly and LCD captures stay under ignored `out/volume-mixer/`.

The audio fixture derives THRU machines with FX NONE in both slots from
the supplied saved project. Seeded four-channel 24-bit input produces
nonzero, independent stereo output. The reference and test share the
composed DSP/loader boot; the reference then restores all five exact
stock hook spans, verified by readback, before the frame engine starts.
This controls the DSP input phase: separately booting raw stock and the
remix produced an unrelated eight-sample offset. No output shift or
correlation is allowed by the passing gate. It compares every packed
24-bit MAIN/CUE sample in a predeclared 7200 ms (317520 samples) window
from that marker, running the script to 7400 ms so the capture includes
the complete window. Raw lengths are reported; only the extra drain tail
is excluded. Short captures and any in-window changed byte fail. This
bounded comparison covers closed view, all pages, navigation, repeated
close/reopen and PLAY/STOP while visible. Model-specific stock key-table
callbacks must receive the exact transport press/release arguments; window
and sequencer-state writes establish which transitions occur while visible.
Live LEVEL, paired input gains,
MAIN/CUE values and persisted project files must remain identical.

The real-input gate feeds one isolated positive and negative rail to each
of A–D over a sub-rail carrier that keeps the stock input gate open. With
silence before a rail, stock gating attenuates it at this tap (the supplied
project's GATE 127 gave `0x5511f9` from `0x7fffff`; GATE 0 gave `0x04c827`).
Every pulse must reload only its own hold to 1379 blocks, reach a display
snapshot between refreshes, then age and expire without another rail.
Playing refresh is checked against completed frame time.

These are deterministic lockstep port results. They do not prove
startup equivalence to an unmodified stock image, physical ADC overload
detection, chip timing, asynchronous hardware races or uninterrupted
UI/audio on a physical MKI/MKII.

Native encoder round trips on both models cover T1/T2/T8 LEVEL, paired AB/CD
gains and MAIN/CUE. The recorder gate uses persisted PICKUP assignments and
the real frame engine, observes both native recorder banks, and checks held
interval progression plus final length/state for ONE, ONE2 and HOLD. Direct
control-memory raster fixtures are separate from this source qualification.
