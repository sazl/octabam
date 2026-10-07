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
time measurement. The actual ROM-to-DRAM complete-LCD-byte path executes
56 instructions; the parser alone reaches 42. The proposed physical masked-span ceiling is 10 us,
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

A synthetic maximum 1,858-byte body takes 82,518 executed instructions.
The longest masked publisher span is 20 instructions, with 29 masked
instructions in total. These are emulator counts, not hardware timings.

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

The USB model can deliver an old primed reply immediately after a new
SETUP, before executing any guest instruction. Controller investigation
found no justified automatic cancellation change: Linux ChipIdea and
TinyUSB drivers explicitly flush outstanding control transfers. The
probe therefore forces bounded guest service through a borrowed stock
getter after each SETUP, then checks the new IN data. SETUP acknowledgment
alone would not prove the flush finished. This establishes the firmware
flush/reply sequence; pre-ISR physical timing remains unmeasured.

With audio streaming, the verifier multiplexes the borrowed getter's reply
with bounded ISO polling and drains every response. Each continuous worker
run permits at most 4,096 ISO cycles. Duplex cycles reserve IN3 and OUT3
replies before one serialized send, then await both under one command
deadline. OUT uses the preceding IN packet's frame count (initially 11 at
HS bInterval2), matching the existing input gate's modeled cadence. At startup and restart, at most
16 IN-only polls from the 80-poll settling allowance wait for two state7
visits via the input frame counter: SET_INTERFACE acknowledges before that
deferred work arms OUT. Every submitted OUT must still complete in full.
A synchronous getter alone would deadlock the port's ISO starvation hold. This host-side fence
does not change the model or introduce a device debug endpoint.

## Checks

```sh
make bus REMIX=usb-panel-main
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-main
make bus REMIX=usb-panel-standalone
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-standalone
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-main --matrix --model both --speed both --jobs 4
.venv/bin/python tools/verify/verify_usb_panel.py usb-panel-main --falsify
.venv/bin/python -m unittest discover -s tools/verify/tests -p 'test_usb_panel_*.py'
```

The selected-image gate is declared in the module manifest and defaults
to both MKI/MKII and HS/FS. It fingerprints the image, checks real bench
control transfers and independent raw UART state, and exercises static
screens, retries, malformed requests, reset/stale tokens, MIDI, and
identifiable concurrent audio samples where selected. Warmup, active lease,
and restart have separate sample counts: warmup cannot satisfy the active
interval. After bounded settling, empty packets and unexpected nonzero
samples fail; more than eight consecutive packets without identifiable
samples in any one channel fail. Active output counter deltas must advance; HS requires zero
new underruns/overruns/bank duplicates/reprimes. At FS, the existing counter
named `underruns` counts speculative short packet-build attempts. A full
descriptor queue returns before that increment, so its rate need not match
across warmup, active and restart. Instead, source control flow bounds it
to one per 16-sample producer block, plus one frame already in progress at
the first counter snapshot. Independently, every settled FS packet must
carry 43..45 frames, and K consecutive packets must carry between
floor(43.9K) and ceil(44.3K) frames, from the servo's clamped step and
accumulator. Within an uninterrupted window with unchanged anchor, queued
frames (`consumed`) and received frames may differ by at most 180: four
descriptors of at most 45 frames. Other measured fault deltas remain zero.
Stopped worker boundaries account for every reply; stored counter windows
exclude settling and the later READY READ fence even when displayed sample
totals include that fence. Raw before/after counters preserve cumulative
startup faults separately from settled deltas. These modeled checks are
not physical dropout measurements. Applicable input packet/frame counters
must advance without bad or partial packets. Input destination routing remains the independent
input gate's responsibility.

These samples use a controlled synthetic bench fixture. The stock ColdFire
ECHO FREEZE routine normally modifies injected read-back words before the
USB producer. Its two-bank selector array at `0x80000eb4` is set to `7`,
which selects the stock delay bypass branch at `0x40003510/1c`. Final dumps
must retain all 16 selectors and show each track's cached wet/send/feedback
zero and dry `7fffffff`. No firmware instruction or model behavior changes.
Marker low bytes include a guard against Q31 unity truncation; FS expected
stereo sums are derived from the eight independently labeled track words.
This fixture does not represent a busy operator project or prove physical
audio continuity.

The same lease remains active during UAC2 CUR/RANGE/validity, accepted
44.1-kHz and rejected 48-kHz SET CUR, and a deferred control OUT superseded
by a new SETUP. A READY READ is primed with audio still open, then reset;
the gate checks teardown, stale-token rejection, re-enumeration and tagged
audio restart. Audio carriers expect exactly four deliberate STALLs,
standalone three. UART alignment rejects future generations, absent
message boundaries, and boundaries missing any of the 128 LCD blocks.
Its output and
private captures live under `out/verify_usb_panel/`. ColdFire/Unicorn
synthetic tests are optional instruments in firmware-free CI; missing
instruments are explicit skips. Missing image/runtime or selected-image
handshake failures fail the final-image gate. A port run establishes
modeled logic only: real cache/bus effects, interrupt latency, host-driver
ownership, audio quality and sustained physical coexistence remain open.

`--matrix` explicitly builds temporary registry-validated carriers for
every accepted output/input pair, records each image hash, and restores
the requested carrier afterward. Four independent emulator processes may
read each immutable image; builds remain serial. Ordinary gate execution
never rebuilds or substitutes its selected image. `--falsify` privately
restores the stock capture, dispatch, or publisher sites and requires all
three altered images to fail the same gate. No firmware-bearing artifacts
belong in version control.
The generated matrix uses BUILD=0, XBUS=1, SPEC=1; cleanup restores the
requested carrier using the original caller's build environment, including
BUILD, XBUS and SPEC, on both success and failure.
