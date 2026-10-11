# Track Volume Mixer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved three-page volume mixer as a composable OS 1.40C module, verified under both emulator model modes.

**Architecture:** A DRAM-linked ColdFire runtime samples completed audio blocks and publishes peak snapshots to its custom UI window. Asserted stock detours handle mixer chords and redraw; manifests declare every hook, state claim and unavoidable compatibility conflict. No DSP code or independent gain controls are added.

**Tech Stack:** ColdFire GNU assembly, Python verification, existing ot_emu/PortProc/PortRt/PanelLink, remix schema and gates.

**Spec:** `docs/superpowers/specs/2026-10-10-track-volume-mixer-design.md`

## Global Constraints

- Preserve its native 128 × 64 layout on MKI and MKII.
- This feature displays live audio activity and existing volume settings; it introduces no independent gain controls.
- LEFT and RIGHT move between pages and stop at the endpoints.
- Each volume scale spans 0–127 and has a fixed horizontal 100 reference.
- Track activity is post-FX, pre-LEVEL; MAIN/CUE bars show the final digital output feeds. Inputs show raw input audio.
- A/B show their common stock gain; C/D likewise.
- Process all 16 samples when detecting clipping; hold the indicator for at least 500 ms after the last rail sample.
- Display at 25 Hz with immediate page changes. Skip peak work while closed and reset stale peaks on reopening.
- The interrupt must never create windows, draw, allocate, or scan unrelated memory.
- No OS bytes are committed. Do not change upstream module sources or the build platform. Work only in the existing isolated worktree.

## Review Focus

- A modifier released after the window closes must not toggle MIDI mode or leave REC1/CUE held.
- Full negative scale must not overflow the magnitude calculation or evade clipping detection.
- A one-sample rail between UI redraws must remain visible for the full clip hold.
- Timer wrap, idle UI, and playback must retain refresh without catch-up bursts.
- Eviction and reopen must unregister old layers and discard old peaks without changing audio.

## File Structure and Interfaces

- `tools/verify/volume_mixer_probe.py`: stock-event and hook-contract investigation, outputs reports only under `out/volume-mixer/`.
- `modules/volume-mixer/meter.s`: frame producer, integer normalization, protected snapshot exchange; exports `vm_frame`, `vm_sample_block`, `vm_snapshot`, `vm_normalize` and state.
- `modules/volume-mixer/panel.s`: chords, lifecycle, stock drawing primitives, UI tick; exports `vm_mixer`, `vm_midi`, `vm_open`, `vm_close`, `vm_evicted`, `vm_left`, `vm_right`, `vm_draw`, `vm_tick`.
- `modules/volume-mixer/manifest.py` and `README.md`: pinned hooks, module identity `VOLUME MIXER`, ledger claims, proof and usage.
- `remixes/test/volume-mixer/{remix.py,README.md}`: module-only test remix, fallback NONE.
- `tools/verify/verify_volume_mixer.py`: registered module gate, emulator arithmetic/UI/audio checks; reusable Python helpers stay in the probe file.

Channel order is 24 channels: T1 L/R through T8 L/R, A/B/C/D, MAIN L/R, CUE L/R. Shared state is aligned `vm_peak_accum[24]` and `vm_snapshot_peaks[24]` longwords; `vm_clip_hold[4]` and `vm_snapshot_holds[4]` halfwords for A–D; `vm_window`, `vm_page`, `vm_pending_releases`, `vm_frame_count`, `vm_last_draw_time` longwords. The frame producer owns accumulators and clip countdowns; the UI owns snapshots, page and lifecycle. Opening/closing and snapshot transfer briefly mask interrupts with saved/restored SR.

Assembly conventions: callable helpers receive arguments in registers, preserve d2–d7/a2–a6, and return with rts. `vm_sample_block` consumes a0 pointing to one 16-frame stereo block and d0 naming the first channel index; `vm_normalize` consumes d0 magnitude in 24-bit units and returns d0 in 0–127. Hook stubs replay all displaced instructions and jump to the proved continuation, preserving live registers and flags as required by each stock site.

### Task 1: Prove stock key, window and hook contracts

**Files:** Create `tools/verify/volume_mixer_probe.py`; update the spec with measured contracts.

**Interfaces:** `run_contract_probe(model: str, out: Path) -> dict` returns key-state traces, window lifecycle evidence, pinned instruction bytes/continuations, hook cadence and occupied claims. `boot_port(model: str, image: Path, out: Path)` returns a context-managed stock/module port with scratch FAT card. Export helpers `key`, `peek`, `poke`, `run`, `screen` for the later verifier; use existing port classes rather than a second protocol implementation.

- [ ] Implement a stock-only probe CLI `--model {mki,mkii,both} --out <dir>` using the user's local image and worktree-built port, with evidence assertions for each result.
- [ ] Drive MIDI/REC1/CUE down, MIXER down, both release orders, and ordinary taps. Compare MIDI state at `0x80000012`/`0x80000015`, cue held state, recorder held state and LCD output; also measure FUNC+MIDI and FUNC+MIXER baseline.
- [ ] Disassemble the mixer opener `0x4007d468` and MIDI handler `0x40055678`, and confirm both key tables invoke them. Candidate MIXER span is six bytes `4ab9460e7424`, continuation `0x4007d46e`; MIDI candidate is eight bytes `2f032f02262f000c`, continuation `0x40055680`. Pin only bytes confirmed by the probe.
- [ ] Probe a full-size custom stock window, eviction callback and layer removal using existing TUNER patterns. Investigate redraw registration `0x4007e760`, candidate UI site `0x40056c92`, and free instruction spans before frame tail `0x4000d99a`. Record register/flag liveness, displaced instructions and buffer selectors at the chosen points.
- [ ] Prefer noncolliding sites only when the cadence and completed-buffer contract are proved. If none is proved, select the established TUNER sites `0x40056c72` and `0x4000d99a` and declare an explicit `TUNER` conflict. Never overlap USB `0x4000d9a0`, CF METER `0x4000d9a6`, or STEM REC `0x40004b12`.
- [ ] Run `.venv/bin/python3 tools/verify/volume_mixer_probe.py --model both --out out/volume-mixer/contracts`. Passing means both model traces satisfy the selected contract; missing evidence is failure. Update the spec, commit probe and evidence descriptions, and complete task review before production stubs are written.

### Task 2: Implement and verify the audio peak/snapshot producer

**Files:** Create `modules/volume-mixer/meter.s`; create arithmetic/source cases in `tools/verify/verify_volume_mixer.py`.

**Interfaces:** Produce the declared meter helpers and state. Frame hook location/replay comes from Task 1. All audio addresses and channel mappings come from the approved spec.

- [ ] Write failing borrowed-call emulator cases `test_signed_peaks`, `test_channel_sources`, `test_clip_hold`, `test_snapshot_reset`, and `test_closed_no_work`; invoke the linked assembly, not a Python reimplementation. Assert normalization of magnitudes 0, `0x400000`, `0x7fffff`, `0x800000` equals 0, 64, 127, 127; assert L-only/R-only inputs remain distinct.
- [ ] Implement peak accumulation across all 16 samples and all 24 channels while open. Shift signed samples right by eight before magnitude conversion. Normalize outside the ISR with `min(127, (magnitude*127 + 0x400000) >> 23)`.
- [ ] Detect either signed input rail before normalization. Reload the affected input's hold to 1379 audio blocks; otherwise decrement without underflow. Test a single clipped sample at each position and both polarities, plus neighboring non-rail values and another input remaining clear.
- [ ] Implement `vm_snapshot`: save SR, mask interrupts, copy 24 peak longwords and four hold halfwords, clear peak accumulators, restore SR. Normalize and draw after restoring SR. Reset peaks/holds safely on reopen and skip frame work when closed.
- [ ] Run the verifier's `--case arithmetic` and `--case sources` cases. Record producer and interrupt-mask instruction costs and confirm source memory remains unchanged. Commit and complete task review.

### Task 3: Implement firmware chords, window lifecycle and approved drawing

**Files:** Create `modules/volume-mixer/panel.s`; extend the verifier with key/window/pixel/timing cases.

**Interfaces:** Consume Task 2 snapshots and the proved Task 1 hook contracts. `vm_open` consumes d0 page 0–2. `vm_draw` reads only stable snapshots and stock control values. Modifier-consumption flags survive close until the corresponding releases arrive.

- [ ] Write failing cases `test_chords_both_models`, `test_release_after_close`, `test_navigation`, `test_eviction`, `test_pixels`, and `test_refresh_wrap`. Check mode/cue/record state against stock after both release orders, standalone controls, FUNC chords, and close-before-release.
- [ ] Implement pinned MIXER interception and MIDI-release consumption. Add `vm_rec_router` at the proved REC1 router callsite: defer recording-producing press calls until release and cancel both edges for a recognized mixer chord; preserve the stock handler's held state and UI. Document and test standalone REC1's release-time trigger in that recorder mode. Open one 128 × 64 window and input layer; left/right clamp page to 0–2. NO/MIXER close it. Eviction unregisters the layer exactly once. Forward transport and unrelated keys through verified stock handlers.
- [ ] Draw the approved three pages using stock primitives and fonts from the user's image. Read track LEVEL, paired input gains and MAIN/CUE settings from the spec's addresses. Check volume 0/100/127, stereo separation, 100-line pixels, numeric values and clipped input labels within 128 × 64. Remove SIM from the production footer.
- [ ] Read `now` from DTCN3 (`0xfc07c00c`) and redraw when unsigned `now - vm_last_draw_time >= 5,280,000` counts (132 MHz, 40 ms). Advance the deadline by one period to retain average25Hz across the measured60Hz UI ticks; after a delay of two or more periods reset to now, avoiding catch-up bursts. Page changes redraw immediately. Check timer wrap, 1–2 seconds without keys and playback separately against emulator elapsed time.
- [ ] Run verifier `--case keys`, `--case panels`, and `--case timing` on both models; capture actual firmware-driven panels. Commit and complete task review.

### Task 4: Register the module and composition/boot gates

**Files:** Create manifest/module README and the test remix; update repository indexes through existing `make docs`.

**Interfaces:** Export `MODULE` with key `VOLUME MIXER`, kind CF_PATCH, DRAM-linked meter/panel units, all proved detours and claims, and `Gate("tools/verify/verify_volume_mixer.py", venv=True)`. Export test `REMIX` named `volume-mixer`, module `VOLUME MIXER`, fallback NONE. Gate without `--case` runs all available required cases and reports missing prerequisites explicitly.

- [ ] Write/execute failing registry and claims checks, including selection with a module occupying a chosen site. If Task 1 selected TUNER's hooks, require a named refusal when TUNER is selected; otherwise prove no overlap.
- [ ] Register the module, remix and gate with truthful proof status. Document signal stages, paired gains, digital-rail limits, commands, hook compatibility and measured frame costs. Credit reused patterns and keep upstream sources untouched.
- [ ] Run `make modules`, `make docs`, and `make check REMIX=volume-mixer`; require boot plus the actual module gate, not assembly alone. Commit and complete task review.

### Task 5: Validate audio invariance, integration and review readiness

**Files:** Complete verifier audio/composition cases and module/test-remix measured documentation.

**Interfaces:** Full verifier accepts optional local project via `OT_PROJECT`; it must never silently label a skipped project run as passing. A locally supplied project is necessary for end-to-end playing audio and transport checks.

- [ ] Compare deterministic stock versus module runs with known stereo input and an actual saved project: view closed, each page open, page changes and repeated close/reopen. Assert bit-identical MAIN/CUE audio and unchanged persisted project controls. Test playback/stop while visible and raw input clipping independently of display refresh.
- [ ] Run compatible USB audio, STEM REC and CF METER compositions through their required checks. Report any declared TUNER conflict explicitly. Confirm the port/host belongs to this branch; build an isolated host only if its source changed.
- [ ] Run `.venv/bin/python3 tools/verify/verify_volume_mixer.py volume-mixer` and `OT_PROJECT=<local-project> make check REMIX=volume-mixer`. If no saved project is available, finish independent checks and report the precise unmet project gate; do not claim full audio validation.
- [ ] Before opening a PR, fetch/rebase onto origin/main and run `make reach RUN=1 KEEP=1` on that tree. Fix failures and list each command/result in the PR body. No hardware-proven claim until MKI and MKII unit checks are recorded.
- [ ] Request an independent whole-branch review of requirements, live-register preservation, interrupt work, snapshot safety, key releases and gate evidence. Resolve actionable findings and present the completed change with remaining hardware/project limitations.

## Final-review corrections

- Qualify recorder TRIG independently of PICKUP machine type. Preserve native
  HOLD intervals; its recording-sensitive REC1 shortcut uses stock MIXER,
  with MIDI+MIXER then RIGHT providing the inputs page. ONE/ONE2 defer only
  with proved running-recorder outcomes, including final state and duration.
- Cancel deferred presses on changed routing context without racing a queued
  release. Qualify consumed chord, bypassed release and next-gesture cleanup.
- LEVEL is a byte at a two-byte stride. Native T1/T2/T8 encoder round trips,
  pair-gain and MAIN/CUE edits must drive the renderer and restore controls.
- Re-run the registered stock-contract census while excluding only the exact
  investigated module; retain every other owner's complete write-span guard.
