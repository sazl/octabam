# USB PANEL MIRROR

Experimental read-only LCD and LED snapshots over vendor EP0, beside USB
MIDI and one existing audio output layout, or the exclusive
[standalone adapter](../usb-panel-mirror-standalone/README.md). Original
ColdFire implementation: Sami Zeinelabdin. No hardware or native USB host
coexistence claim has been established. Shipped remix selections are unchanged.

`protocol.json` is the authoritative version 1.0 contract; `protocol.inc`
is generated from it. Requests are exactly device-recipient vendor IN
`c0/57` INFO, `c0/58` BEGIN, `c0/59` READ and `c0/5a` RELEASE. Existing
`55` output counters and `56` input counters keep their owners. The current
advertised response ceiling is 64 bytes, minimum poll interval 200 ms,
lease 1,000 ms, and publication ceiling 10 Hz. The conservative polling
profile provides at most five fresh polls per second; physical latency
and ten visible updates per second remain unmeasured targets.

## Capture and initialization

The tap observes **accepted bytes**, after the stock producer index store
at `0x40010aea` (single byte) and `0x40010b6a` (bulk), before the stock SR
restore. Both producer paths already hold SR `2700`; this serializes
capture in the same order as the real ring. The displaced store, every
register, its CCR result, stack, and subsequent UART scheduling survive.
There is no allocation, callback into the OS, CRC or copy of a full frame
inside this masked span. The verifier's synthetic ColdFire execution
checks the 128-instruction added-work budget; that count is not a physical
time measurement. The proposed physical masked-span ceiling is 10 us,
unmeasured.

A critical ordering detail: stock entry calls `0x4000f9b4` at
`0x400004ba`, which initializes the panel **before** the platform loader at
`0x4000050c`. A direct DRAM capture detour crashes this boot. `panel_boot.s`
therefore resides in a declared image cave and holds an initialized,
bounded **3,072-byte FIFO** and a zero dispatch pointer. Both model probes
accepted **2,522 bytes** before loader completion, leaving 550 bytes of
headroom. The verified displaced call at `0x40000512` replays SRAM setup,
drains those bytes into the loaded DRAM observer once, then publishes its
function pointer. FIFO overflow sets a sticky fault and leaves the mirror
unavailable; it never writes past the buffer or stops the real UART.
This early handoff happens during boot, before the runtime service starts.

The observer retains 1,024 LCD bytes, 128 block-presence bytes, 32 LED row
values/presence bytes each, 256 LED levels/presence bytes each, backlight,
framing state, generation and diagnostic counters. Complete messages
commit atomically under the existing stock producer mask; partial frames
never commit. Unknown opcodes, malformed LCD columns, or generation
exhaustion fail closed. Full LCD readiness requires all 128 distinct
blocks, rather than a byte count. Palette and known boot commands are
framed but not replayed in a snapshot. Capture continues without a host.

Direct programming and text output bypass the ring. Declared shims cover
the main panel programmer, its word helper, four generic text/control
helpers, and the installed diagnostic/HALT writer. Once the main service
is live, their first direct transmit invalidates the observer before
altering the panel. Startup programmer traffic precedes the final normal
screen initialization; the MKI/MKII probes independently compare that
complete screen. The embedded boot-loader routines around `0x400dea6c`
and `0x400dfdfa` belong to a separate loader execution boundary; mirroring
is not supported during OS loading, programming, diagnostics or HALT.
A diagnostic that halts the CPU cannot send a final USB status; host
liveness must expire, never imply that its retained pixels remain live.

## Publication and memory

Main's priority-zero service loop owns `0x4001fc96`, explicitly conflicting
with **CF METER IDLE**. It replays final stock initialization once and
attempts at most one bounded snapshot per opportunity. Copying, canonical
serialization and CRC run with interrupts enabled. A short final critical
section verifies generation, epoch, token and pending state, then commits
READY as the last store. A raced attempt remains PENDING for a later
opportunity; the USB interrupt never waits for the publisher. Frozen body
storage is separate from the live shadow.

The real loop branches from `mirror_idle_park` to `mirror_idle_resume`.
The emulated gate explicitly configures those symbols with `--main-park`;
only the branch is idle-skipped. A borrowed bench call returns to the
resume point, so a static display still publishes after such calls.

The current MAIN build uses 3,602 bytes of image-resident boot code/FIFO
and 6,370 bytes of initialized DRAM object content before linker alignment:
2,604 capture, 2,694 snapshot, 1,072 EP0. The private 64-byte reply is
page-aligned, followed by 32 bytes of emulator-test scratch. Alignment can
cost up to 4,095 additional bytes. All module state is initialized data;
there is no uninitialized `.bss`, separate task stack, absolute free-RAM
claim, or platform arena enlargement. Wrappers reserve 64 stack bytes;
publisher and control entry each save 44 bytes plus bounded call frames.

## USB lifetime and composition

The stock sender retains a pointer in its EP0 IN dTD. Returning from it or
receiving another SETUP does not establish DMA completion. The handler
writes only through the private buffer's uncached alias. Before changing
it, it issues one EP0-IN-only flush and checks FLUSH, PRIME and STATUS.
If any remains active, it stalls without touching the buffer or lease.
There is no busy wait, OUT flush, audio endpoint change, cacheable DMA
reply, stack-backed reply, or borrowed counter/SRAM buffer.

The audio dispatch, reset and session-end calls are conditional on the
feature selection. Unrelated controls follow the original path, including
the later input-counter hook. Reset/session end retire leases and advance
the epoch without discarding valid physical LCD state. The standalone
adapter owns mutually exclusive stock control/lifecycle detours and the
same EP0 descriptor-page correction as existing audio (credited there).

The immediate-IN-after-aborting-SETUP behavior of the USB model is under
separate controller review. The present candidate probe explicitly lets
the guest process the replacement SETUP before checking its new IN data;
that proves the firmware flush/reply sequence, not autonomous controller
cancellation before the ISR. Do not treat this remaining instrument issue
as hardware DMA signoff.

## Checks

```sh
make bus REMIX=usb-panel-main
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-main
make bus REMIX=usb-panel-standalone
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-standalone
.venv/bin/python -m unittest discover -s tools/verify/tests -p 'test_usb_panel_*.py'
```

The selected-image gate is declared in the module manifest and defaults
to both MKI/MKII and HS/FS. It fingerprints the image, checks real bench
control transfers and independent raw UART state, and exercises static
screens, retries, malformed requests, reset/stale tokens, MIDI, and
identifiable concurrent audio samples where selected. Its output and
private captures live under `out/verify_usb_panel/`. ColdFire/Unicorn
synthetic tests are optional instruments in firmware-free CI; missing
instruments are explicit skips. Missing image/runtime or selected-image
handshake failures fail the final-image gate. A port run establishes
modeled logic only: real cache/bus effects, interrupt latency, host-driver
ownership, audio quality and sustained physical coexistence remain open.
