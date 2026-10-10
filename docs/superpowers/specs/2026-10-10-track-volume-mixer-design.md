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
must not leave MIDI mode changed; REC1/CUE chords must not perform unrelated
recording or cue actions. Modifiers must be released without stuck keys.

## Sources and meaning

Signal bars show block peaks, separately from volume positions. Track
activity is post-FX, **pre-LEVEL**, matching the existing verified USB tap;
a lowered volume marker does not itself lower these signal bars. MAIN/CUE
bars show the final digital output feeds. Inputs show raw input audio.
This separation is deliberate and must be documented in the module README.

Existing controls are read without writing them:

| Value | Source |
| --- | --- |
| T1–T8 LEVEL | halfwords at `0x80000c50 + 2*track` |
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
