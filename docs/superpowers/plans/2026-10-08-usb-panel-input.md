# USB panel physical inputs implementation

Spec: `docs/superpowers/specs/2026-10-08-usb-panel-input-design.md`.
User authorized autonomous execution with parallel subagents. All work stays
in the existing usb-panel-debug worktree; no push, flash, or firmware commit.
Preserve the pending PDBG13 cache/EP0 fixes and unrelated working changes.

## Task 1: Protocol and cached hardware view

Own `protocol.json`, generated `protocol.inc`, `usb_mirror_protocol.py`,
`hardware_backend.py`, `panel_backend.py`, related host/protocol tests.
Implement schema 2's 168-byte input suffix, capability 0x40, minor 1 and
maximum 2026 bytes, retaining schema 1 decoding. Define immutable input
state containing `known`, `fader`, `keys`, `press_counts`, `encoder_counts`.
Expose `/status` `inputs` as those fields (arrays of 8, 64 and 7 pairs),
with null when unsupported; include `input_observation` capability.
Keep PanelLink restricted to the output prefix. Write failing tests for
malformed/unknown input state, limits, schema compatibility and real cached
HTTP delivery; implement and run the relevant unittest files.

## Task 2: Firmware observation and coherent serialization

Own new `panel_input.s`, manifest, capture/snapshot assembly, usb_panel.s
only as required for schema/flags, assembled-observer tests and image gate.
Disassemble stock parser before choosing asserted detour spans. Observe
accepted keys, encoder deltas, calibrated fader. Preserve registers/SR and
replay displaced instructions. Include input changes in coherence checks,
serialize the exact suffix within the current allocation. Coordinate
constants with Task 1. Write failing synthetic assembled tests for edge
counts, both directions, wrap, calibration, preserved state, mutation during
copy and frozen leases. Run focused tests and report bounded observer cost.

## Task 3: Browser controls and quiet image refresh

Own panel.html, optional dedicated input JS module/route, browser tests.
Consume the Task 1 status shape, instance+epoch baselines and negotiated
input capability. Render holds, 200ms short-tap/push highlights, 250ms
direction indicators, cap rotation and calibrated fader without enabling
hardware controls. Clear transient input state on stale/disconnect. Retain
verified pixels during refresh; remove syncing text/overlay/dimming and
keep errors in the header. Write meaningful failing browser tests and run
them with available local instruments, documenting any unavailable ones.

## Task 4: Integration and verification

Review each task for spec and quality, fix interface issues and run
`make test-panel`, selected szpanel gate and full standalone check BUILD=82.
Temporarily exclude copied personal szpanel from curated-registry selftest
only and restore it in finally; selected gate still proves that remix.
Build VERSION=PDBG14 BUILD=82 with the operator's stock SYX. Archive local
artifacts and hashes; no hardware flashing. Update module/guide/evidence
docs and run diff/doc checks. Request a fresh whole-change review and resolve
actionable findings. Report exact local evidence and hardware acceptance
still requiring operator flash.

## Review focus

No phantom historical presses; held-at-start keys; short tap between polls;
opposite encoder turns; -128 delta; counter wrap; calibration orientation;
input interruption during publication; schema 1 fallback; lease DMA safety;
UI quiet refresh retaining CRC-verified pixels; no writable hardware routes.
