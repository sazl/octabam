# Track volume mixer design

## Approved display and scope

Add an optional `volume-mixer` ColdFire module for the user's OS 1.40C.
The approved prototype is `tools/prototypes/mixer_display.py`; the revised
captures are in `out/mixer-prototype-v2/{mki,mkii}/`. Preserve its native
128 × 64 layout on MKI and MKII. This feature displays live audio activity
and existing volume settings; it introduces no independent gain controls.

One view contains three pages, in order:

1. MIDI held, then MIXER: T1–T8, each with L/R bars and one track LEVEL value.
2. REC1 held, then MIXER: A–D, with independent activity and clip indicators.
   During native HOLD recording use MIDI+MIXER, then RIGHT; REC1 timing stays stock.
3. CUE held, then MIXER: MAIN and CUE, each with L/R bars and one volume value.

LEFT and RIGHT move between pages and stop at the endpoints. Each volume
scale spans 0–127 and has a fixed horizontal 100 reference. A separate
side marker and number indicate the current setting. The 100 reference
is neither unity gain nor the signal clipping threshold. Left/right
subbars are L/R. Input clipping inverts the affected channel label and
adds `!`, as in the approved B! example.

MIXER alone and FUNC+MIXER retain stock behavior. NO or MIXER closes the
custom view. Transport remains usable. Closing or eviction removes the
custom input layer and restores the underlying display. Opening with MIDI
must not leave MIDI mode changed; accepted REC1/CUE shortcuts must not perform
unrelated recording or cue actions. Native HOLD recording keeps its stock
REC1 edges and uses the alternate input-page gesture above. Modifiers must be released without stuck keys.

## Sources and meaning

Signal bars show block peaks, separately from volume positions. Track
activity is post-FX, **pre-LEVEL**, matching the existing verified USB tap;
a lowered volume marker does not itself lower these signal bars. MAIN/CUE
bars show the final digital output feeds. Inputs show raw input audio.
This separation is deliberate and must be documented in the module README.

Existing controls are read without writing them:

| Value | Source |
| --- | --- |
| T1–T8 LEVEL | bytes at `0x80000c50 + 2*track` (two-byte stride) |
| MAIN / CUE | bytes `0x80000035` / `0x80000036` |
| A and B gain | shared byte `0x8000002f` |
| C and D gain | shared byte `0x8000002e` |

A/B show their common stock gain; C/D likewise. Four independent input
meters do not imply four independent stock gain controls. DIR AB/CD are
separate routing controls and are not shown as input gain.

Audio blocks contain 16 stereo frames of signed 24-bit samples,
left-justified in 32-bit words:

| Audio | Source |
| --- | --- |
| Tracks | `0x80003190 + ([0x800000e4] & 1)*0x400 + track*0x80`; L/R at sample*8 / sample*8+4 |
| MAIN / CUE | `0x80005e60` / `0x80005ee0`; L/R at sample*8 / sample*8+4 |
| Inputs | `0x80005660 + ([0x46104d00] & 7)*0x100`; C/D at +0/+4, A/B at +0x80/+0x84, sample stride 8 |

**Measured input-tap qualification (10 October 2026, Task 5):** this stock
input arena follows stock input conditioning, including the input gate.
An isolated positive ADC-stream rail over preceding silence reached this
arena as `0x5511f9` with saved GATE 127 and `0x04c827` with GATE 0; it was
correctly not classified as a rail. A sub-rail carrier keeps that gate open
for the real-input test, which then delivers isolated positive and negative
rails to each input. The indicator detects digital rails at this tap.
Upstream ADC or analog overload attenuated before the tap is not proven
detectable; qualifying an earlier tap requires hardware evidence. This
measurement qualifies “raw input” throughout this design without changing
the chosen source or adding gain controls.

Evidence: `modules/usb-audio-out-tracks-main-cue/usbaudio.s`,
`docs/firmware/STEM_REC.md` sections 18.1 and 18.5–18.7, and
`tools/panel/KEYMAP.md` encoder measurements. Use magnitudes that handle
negative full scale without signed overflow. Normalize signal peaks to
0–127 for display; this is an amplitude scale, not a dB scale.

Clip detection examines raw samples before display normalization: either
24-bit signed rail (`0x7fffff` or `-0x800000`) activates the indicator for
500 ms after the last rail sample. Both polarities and each input are
independent. This proves digital rail detection; an analog overload that
does not produce digital rail samples requires a measured ADC overload
status source before claiming detection. The hardware validation must
compare the indicator against an overdriven external input.

## Runtime architecture and composition

Use `modules/volume-mixer/manifest.py` and a DRAM-linked GNU-as runtime,
with a dedicated test remix at `remixes/test/volume-mixer/`. Own state
includes page, window/layer lifecycle, per-channel peak snapshots and clip
hold counters. Do not allocate in DSP scratch or alter DSP processing.

Sample completed audio blocks at a proven frame-completion point; publish
bounded snapshots to the UI, which performs all drawing. The interrupt
must never create windows, draw, allocate, or scan unrelated memory.
Process all 16 samples when detecting clipping, so peaks between UI
refreshes remain visible. Display at 25 Hz with immediate page changes.
Skip peak work while closed and reset stale peaks on reopening.

The stock MIXER press is `0x4007d468` on both model keymaps. Its initial
whole instruction is `tst.l 0x460e7424`, six bytes, with continuation
`0x4007d46e`. An asserted detour can intercept recognized chords and replay
stock behavior otherwise. Existing custom-window/layer patterns are in
`modules/tuner/upstream/tuner/tuner.s` and `docs/firmware/MAINMENU.md`.

Do not blindly reuse TUNER's UI-loop site `0x40056c72` or frame-tail site
`0x4000d99a`: both are claimed. USB audio claims `0x4000d9a0`, and CF METER
claims the epilogue at `0x4000d9a6`. Before choosing hooks, investigate the
stock redraw registration `0x4007e760` and other complete instruction sites.
Every final hook is disassembled, tested and declared through the ledger.
An unavoidable conflict is declared by module name; no silent collision,
shared mutable build patch, or upstream module rewrite is permitted.

## Required implementation probes

These are work items with pass/fail outcomes, rather than assumed firmware
behavior:

- Trace MIDI/REC1/CUE press, MIXER press, and releases on both models.
  Record state before and after; the chord must leave unrelated mode and
  recorder/cue state unchanged. If stock MIDI changes mode on press,
  use deferred standalone MIDI handling or verified reversible restoration.
- Create and destroy a 128 × 64 custom window through stock APIs; verify
  every approved pixel fits and that close/eviction unregisters its layer.
- Resolve the stock redraw registration and choose noncolliding UI/audio
  hooks, or declare the exact required module conflicts.
- Drive distinct samples per track/channel/input and compare snapshots
  with known source blocks; establish safe buffer timing at the chosen hook.
- Read volume values before and after real stock encoder changes on both
  models; prove A/B and C/D pair behavior and MAIN/CUE identification.

Do not implement a production hook on a probe that fails. Update this
record with the proved hook contract before module implementation.

## Verification and delivery

Add `tools/verify/verify_volume_mixer.py` as the module's gate. Cover both
model modes, all three chords, modifier releases, page order and clamping,
close/reopen/eviction, standalone stock controls, and transport. Render
known silence, L-only/R-only signals, unequal track signals, both signed
rails, and input clipping between redraws. Check zero/max/100 volume markers,
clip hold/recovery, and exact pixel bounds. Verify unchanged audio output
with the view closed and open, and measure added frame/UI work.

Run `make check REMIX=volume-mixer` and the relevant compatibility remixes.
Use actual branch-built emulator/hosts as required by repository rules.
Document measured emulator behavior and hardware behavior separately.
Before a PR, rebase onto origin/main and run the CONTRIBUTING.md reach gates
on that rebased tree. No OS bytes or screenshots containing stock firmware
assets are committed. Hardware validation on both models remains necessary
before declaring the feature hardware-proven.

## Measured stock contracts (10 October 2026)

`.venv/bin/python3 tools/verify/volume_mixer_probe.py --model both
--project out/volume-mixer/source-project --out out/volume-mixer/contracts`
boots the branch-built
ColdFire port on the user's stock image, injects panel events and reads the
firmware's UART LCD stream. The project directory is a disposable copy of
a locally supplied saved project; the probe makes its own scratch-card
copies and requires it for a complete result. Omitting `--project` is an
argument error. Both models have the following contracts:

| Hook | Asserted bytes | Continuation and replay |
| --- | --- | --- |
| MIXER | `0x4007d468`: `4ab9460e7424` | `0x4007d46e`, `tst.l 0x460e7424`; preserve its flags for the stock branch |
| MIDI | `0x40055678`: `2f032f02262f000c` | `0x40055680`, push d3, push d2, load code from sp+12 |
| UI | `0x40056c92`: `4ab946104ca8` | `0x40056c98`, `tst.l 0x46104ca8`; preserve registers, recreate the tested flags |
| Frame | `0x4000d98e`: `203980004800` | `0x4000d994`, `move.l 0x80004800,d0` after sampling |
| REC1 recorder call | `0x40030ffc`: `4eb940097924` | `0x40031002`, stock call `0x40097924`; continuation discards five stack arguments |

The canonical `Module.write_spans()` registry entries and the ledger’s fixed
linked-unit cave reservations do not overlap these spans. The probe checks
caves, hooks, detours, pokes, table references and symbol references using
the ledger’s conservative lengths, and records each occupied span’s kind. TUNER's frame
hook at `0x4000d99a` and UI hook at `0x40056c72`, USB's `0x4000d9a0`,
CF METER's `0x4000d9a6`, and STEM REC's `0x40004b12` stay distinct.
Consequently the selected hooks do not require a TUNER conflict.

At the frame site, the remaining instructions only advance the modulo-four
counter before TUNER/USB's established sites. They do not write any audio
source or either source selector. A temporary probe copies the selected
track, input and MAIN/CUE blocks plus their selectors at both `0x4000d98e`
and `0x4000d9a0`, comparing 386 words on every frame. Both models report
zero differences. The counter advances by one modulo four between sites.
The ISR restores d0–d7/a0–a6 from its original stack at `0x4000d9a6`;
the stub must restore its own stack depth and replay the load into d0.
The following `addq`, `moveq`, `and` establish the counter and flags, so
entry CCR is dead. The producer must not use EMAC; this probe does not
qualify its state at the selected hook.
This proves local source equivalence to the existing taps, not playing
DSP audio or hardware timing. Those remain separate module gates.

The UI site runs 60 times in one idle second on both models. It is after
queue receive and the stock UI callbacks. Preserve d2–d7/a2–a6, which
carry loop constants, and preserve d0/d1/a0/a1 as well to avoid depending
on unproved downstream liveness. Replay the `tst` last. The investigated
`0x4007e760` manages a linked list and rebuilds eight indexed 14-byte UI
records; it is not an established arbitrary periodic callback API, so
this design uses the measured UI site. To average 25 draws per second on
this 60 Hz task, advance an absolute deadline by 40 ms and skip missed
periods. Resetting the deadline to `now + 40 ms` after every draw would
produce 20 Hz. This scheduling ruling replaces the plan's reset-to-now
wording; timer-wrap and missed-period behavior still need runtime tests.

MKI's key table is `0x400bfc10`; MKII's is `0x400c01f4`. Both dispatch
MIXER press to `0x4007d468`, MIDI press/release to `0x40055678`, REC1
press/release to `0x40030e6c`, and CUE press/release to
`0x4004e978`/`0x4004e968`. MIDI's handler receives `(0x35, edge)` with
edge 1 on press and 0 on release. Ordinary audio-mode MIDI presses do
not change `0x80000012`; release toggles it. Consumed MIDI releases must
therefore be suppressed even when the custom window has already closed.
CUE's held long is `0x460d168c`; ordinary REC1's held long is
`0x460d10d0`. Raw held rows are at `0x46100b18` and remain the reliable
chord source. A cached release handler survives pushing a new layer.
FUNC+MIDI and FUNC+MIXER open their stock views without changing MIDI mode.
Both release orders, standalone taps, and the stock mixer open/close path
are covered by captured state and LCD traces.

**REC1 depends on the native recorder trigger mode.** The router call at
`0x40030ffc` receives `(track, TRIG, key, edge, arg)`: TRIG is ONE=0,
ONE2=1, HOLD=2, distinct from the machine type (PICKUP=4). ONE/ONE2
recording presses are deferred until release so a subsequent MIXER can
consume both edges. Their standalone recording starts on release. A ONE2 take finished at the same subsequent
REC2 event is shorter by that withheld press interval; only HOLD preserves
its native held recording duration. The saved arguments need an independent stack copy because the stock router
rewrites its caller's arguments for a tail call.

HOLD press and release route natively. When REC1 actually entered that
recording-sensitive path, REC1+MIXER uses the stock mixer; open the input
page using MIDI+MIXER, then RIGHT. Ordinary REC1 with no recording role
retains the shortcut. This replaces the earlier unconditional-deferral
ruling: a running stock HOLD captured 25,632 samples during a 580 ms
press, while the old deferral captured none of that interval and began
recording after release, remaining active 200 ms later.

Deferred presses cancel on track/part/bank, machine, TRIG and stock
recording-context changes. A raw physical release can precede its queued
UI callback, so raw-up alone must not cancel a pending standalone press.
Native release completes same-context replay. A fresh routed press clears
any consumed-release marker left when a prior release took another stock
path. HOLD shortcut eligibility expires on raw release, even if the outer
handler no longer calls the router. Tests cover bypassed releases and the
next gesture, both models and both release orders.

Native UART encoder round trips qualify the displayed control sources:
T1/T2/T8 LEVEL changes only the byte at `0x80000c50 + 2*track`, leaving
the companion byte and other tracks intact. Stock store `0x4004ec5a` is
`moveb`, not a halfword store. AB/CD gains update both displayed members
of their pair; MAIN/CUE update their respective output number. Tests
restore controls by inverse encoder turns and verify renderer numbers,
separately from direct-memory raster fixtures.

A full display needs `WCREATE(128, 64, -1, 0, 5, closed_callback)`:
the stock constructor adds one to its x origin, so an x argument of zero
would clip the last column. The drawing surface is `window + 36`, not
the window handle itself. Stock BOX drawing produces exactly four 3×3
corner squares (36 lit pixels), including all four physical LCD corners,
on both models. Opening stock TEMPO evicts class 5, calls the custom
callback exactly once and removes the custom input layer. NO explicitly
closes the custom window while CUE is still held; CUE's own sublayer
remains until its cached release clears held state and removes it.
All screenshots and raw disassembly stay under `out/volume-mixer/`.
