# USB panel mirror design and evidence

USB PANEL MIRROR observes accepted panel output and publishes full read-only
snapshots through vendor EP0. The host protocol definition is
[protocol.json](../../modules/usb-panel-mirror/protocol.json); its generated
[protocol.inc](../../modules/usb-panel-mirror/protocol.inc) and
[host parser](../../tools/panel/usb_mirror_protocol.py) must agree. The protocol
is experimental. [Operator setup](../guide/USB_PANEL_MIRROR.md) describes safe
host access and the read-only HTTP boundary.

The original ColdFire mirror implementation is by Sami Zeinelabdin. The
standalone adapter's EP0 descriptor-page correction follows the credited
markandrus/octemu USB audio implementation; the existing panel/port history
retains Tim Hastie's attribution in [the panel README](../../tools/panel/README.md).

## Boot boundary and accepted-wire observer

The original audit assumed panel traffic began after the DRAM platform loader.
That premise was wrong: accepted panel bytes arrive before the loader at
`0x4000050c`, and a direct detour into an unloaded DRAM runtime faults. The
observer therefore starts in original ROM-resident code with initialized state
and a fixed 3072-byte FIFO. It hooks both accepted ring commits,
`0x40010aea` and `0x40010b6a`, preserving the displaced instruction, caller
registers and SR. It observes accepted bytes in wire order, rather than the
producer's attempted writes or arbitrary framebuffer RAM.

After the loader, the `0x40000512` handoff runs the displaced SRAM setup,
drains the ordered FIFO while serialized, then switches dispatch to the DRAM
observer. Overflow fails closed; it never wraps and invents a complete display.
This boot-only drain is distinct from steady-state publication. Direct runtime
panel programming/text helpers that bypass the accepted ring invalidate the
mirror rather than retaining a plausible stale display.

`pm_accept` takes the accepted byte in `d2.b`, serialized by its caller's stock
SR mask, and commits only complete messages. It clobbers `d0/d3/d4/a0/a1` inside
the wrapper and never calls the OS. Unknown opcodes or invalid LCD columns
invalidate the observer; generation exhaustion also fails closed. All buffers,
validity bits and counters have explicit initialized data: the platform does
not load an implicit `.bss` zeroing contract.

The wrappers preserve the displaced store, caller registers, CCR and stack.
Synthetic assembled-code checks bound added capture work to 128 instructions;
instruction counts are not microseconds. Direct programming, polled text/control
helpers and diagnostic/HALT output invalidate live mirror state. The separate
OS-loader boundary is outside mirror support; a halted CPU cannot deliver a
final USB status, so the host must expire contact with its retained frame.

The maintained shadow has 1024 LCD bytes and 128 block-validity bytes, 32 LED
row values/validity bytes, 256 LED level values/validity bytes, a backlight
value/known flag, bounded framing storage and generation/health state. An LCD
snapshot becomes ready only after all 128 blocks have been observed. The shadow
is maintained even without an attached host, supporting a later static-screen
attach; physical late-attach acceptance is still required.

## Canonical snapshot and wire envelope

The body contains all 128 LCD commands in ascending page and 8-column block
order: `0x10 | page`, column, eight data bytes (1280 bytes total). Optional known
LED rows follow in ascending row order, then level commands in ascending LED ID,
then known backlight. Unknown optional state is omitted and flags distinguish
supported from known. Up to 32 rows, 256 IDs and one backlight pair produce a
maximum body of 1858 bytes inside a fixed 2048-byte capacity. This is state, not
an unbounded history. CRC-32/ISO-HDLC equals Python `zlib.crc32(body)`.
Known boot and palette commands are framed but omitted from the canonical body;
this snapshot does not establish palette fidelity or replay diagnostic history.

| vendor IN request (`bmRequestType=0xc0`) | operation |
|---|---|
| `0x57` INFO | schema, model, geometry, capabilities, limits, build prefix and contact |
| `0x58` BEGIN | bounded cookie acquisition; PENDING while publication completes |
| `0x59` READ | token/offset-addressed chunks of one frozen body |
| `0x5a` RELEASE | idempotent lease release |

Existing requests `0x55` and `0x56` keep their allocations. The 32-byte,
big-endian OTPM v1 envelope contains version/status/kind, epoch, generation,
16-bit token, total/offset/payload lengths, full-body CRC and flags. INFO adds
32 bytes; its 11-byte build string is a prefix, not a full build identity.
The actual initial firmware response profile is 64 bytes. Host support for
128/256-byte response sizes does not prove those profiles on an image. Chunk
assembly enforces identity, exact bounds, coverage, matching overlaps and CRC
before publication. No partial body becomes a visible frame.

## Publication, DMA and lifecycle

BEGIN records lease state without waiting for a full snapshot. A priority-zero
main service publishes into a fixed private body, outside USB and audio IRQs.
It copies canonical state and computes CRC with interrupts enabled. It checks
generation, epoch, token, active state and pending lease again in a short masked
commit; READY is the last publication store. A concurrent change causes the
attempt to be abandoned. A leased body remains frozen while the observer can
continue updating its shadow. No synchronous full-copy or CRC belongs in EP0.

The EP0 reply is private, aligned, explicitly initialized and in uncached DRAM.
It uses the stock reply/DMA path without borrowing the MIDI response buffer.
Before writing the reply it checks EP0 IN FLUSH, PRIME and STATUS. An
already-idle endpoint needs no flush. If an old IN transfer remains, it
issues one EP0-IN-only flush and checks again without waiting. If the endpoint remains active, it stalls without modifying
the reply or lease. No busy wait, OUT flush or audio/MIDI endpoint change belongs
in this path. Replacement SETUP alone does not prove DMA completion: the current
port probe lets the guest process SETUP before checking the replacement IN data.
That checks software flushing, while controller cancellation before the ISR and
physical DMA timing remain unproven.
The verifier's bounded guest-service barrier calls the stock accepted-ring-count
getter after SETUP and before IN/status. During audio it must also service ISO
traffic; SETUP acknowledgment or a wall-clock sleep is insufficient evidence
that the guest flush ran. This is a host instrument, not a device debug endpoint.

Pending/ready reads, cookie retries, expiry, release and epoch changes must remain
bounded. Defaults are 1000 ms lease, 200 ms minimum host polling and a 10 Hz
ceiling on **successful publications**. A raced copy remains PENDING and can retry
at later service opportunities; this ceiling does not limit copy attempts to ten
per second or establish a CPU budget under continuous display changes. Copy/CRC
and masked commit instruction counts must be interpreted separately from
physical elapsed time. The host uses a single cancellable worker
and atomic cached views; its own connection incarnation prevents mixing leases
across reconnects, even when firmware metadata repeats.

USB MIDI is required, with exactly one dispatcher owner (a selected supported
audio output or USB PANEL MIRROR STANDALONE). CF METER IDLE conflicts because
both own the priority-zero service loop. Feature-off images must retain their
existing descriptors and dispatch behavior. A mirror-promising carrier missing
symbols or handshake must fail; an unrelated feature-absent image can report an
explicit N/A. The owning image-stage gate receives the exact REMIX and BUILD,
after restoration of the shipping image and ordinary set/USB checks.

## Evidence and falsification

The OS MAIN audited here has SHA-256
`164f31224bf61181e3f50e7dec40df9afcae5b16dbf6e4c0d0cc5e986af0a84e`.
Capture-enabled and disabled port images emitted byte-identical MKI/MKII runtime
UART vectors: 6919/8156 bytes respectively. The 2522 accepted bytes before
loader completion are a separate FIFO observation; UART emitted before RTOS
instrumentation is absent from those vectors. Both models had complete 128-block
LCD coverage and no observer faults. The first MAIN EP0 probe observed
PENDING/READY, a 1580-byte body, CRC and release across 59 setups with zero
stalls. These initial port results are supplemented by the final reviewed
selected-image validation below. There is no physical unit, project audio
workload, flash, hardware latency/headroom measurement or driver trace.

Fresh digital validation after the INFO generation fix used candidate `8c8ff16d`,
verifier SHA-256
`2724635f00bddf75c2317ea0e2cf866fb8988456e3de34c3e6b076424dfaf826`.
It completed all 20 registry-accepted output/input selections on MKI/MKII at
HS/FS, all four standalone dimensions, and three private negative controls.
All commands returned zero; original selected MAIN image/runtime were restored
byte-for-byte and temporary carriers/processes removed.

| proof | final result and scope |
|---|---|
| audio-selection matrix | 80/80 runs, 240 measured audio windows; exact four deliberate STALLs per run |
| standalone | 4/4 runs, no audio interfaces; exact three deliberate STALLs per run |
| falsification | 3/3 privately altered capture/dispatch/publisher images rejected by the same verifier |
| output counters | HS measured fault deltas zero; FS short-build counts 0–446 satisfy the reviewed bounds, other measured deltas zero |
| concurrent input diagnostics | 30 HS runs advance input counters and require zero bad/partial packet deltas; input underrun deltas 0–2, no all-zero-input claim |
| project alignment | genuine SKIP without an authorized source project; strict release acceptance blocked |

The private matrix JSON hash is
`d0577f88c1f6c0baa529341a5a4f5038693ab0fa5e500705cf4277f750e21b01`.
Per-image/runtime fingerprints, raw counter endpoints, dimension audit and
restoration evidence stay in ignored/local output, not in repository fixtures.
See the final PR's reached-gate record for combined-root results and their exact
commit. All 84 normal cases also check initialized INFO generation, its advance
across three complete updates, and the subsequent validated snapshot generation.
The later browser lifecycle/retry changes leave this firmware and verifier intact.
The restored MAIN image SHA-256 is
`7347b60d19ae6e3ef7946d3b98068055a779fefd7e37bf7e11e2c6a8cf0e2cfe`.

Earlier `dd7e4cfe` evidence remains a separate checkpoint: verifier SHA-256
`bf0877408affd378132bca5211265f78e5ecf8afae05149a8676057758a79a45`,
private matrix JSON SHA-256
`e03f9179c1b8b197595c933194b1d678537b0f3de3e3d6c3d6d31135805e258a`.
Its FS short-build range was 0–444; those earlier bytes/results are not the
post-INFO-fix matrix above.

Feature-off comparison of the reviewed core at fixed BUILD=0, XBUS=1, SPEC=1
found all 25 successful whole images, all 27 normalized build reports and all
807 corresponding linked/layout artifacts identical to the trusted base.
The two WAVE LOAD/CF METER short-branch assembler failures occur on both trees;
they are preserved baseline failures, not successful builds. The separate
24-configuration refhash check passed on the root's rebased core tree. These
checks establish unchanged feature-off build output, not hardware coexistence.

Independent existing-gate coverage at the reviewed core checkpoint comprises
20 output selections through an image-validated private park wrapper, 15 native
input selections and five explicit input-absent N/A cases. These use MKI:
outputs exercise HS/FS and input DSP/recorder routing exercises HS only. After
the permanent launch helper landed, canonical unwrapped output and input gates
both passed again on the same MAIN+ABCD image and runtime ELF. The output gate's
tag oracle ignores unrecognized sample values; its passes do not establish
all-sample purity. At FS only MASTER and MAIN run that word oracle; the other
three layouts check packets without summed-track/main word identity. Neither
those checks nor native input routing establish
MAIN/CUE alignment or concurrent mirror-lease input routing.
No authorized source project is present: `verify_usb_align` reports genuine SKIP,
so project alignment remains unverified and blocks strict release acceptance.

The mirror verifier uses a controlled synthetic audio source fixture, not an
operator project. The stock ColdFire ECHO FREEZE routine can transform injected
track words; the fixture selects its stock bypass branch with both banks of
eight selectors and independently checks those selectors and each track's
cached zero wet/send/feedback and Q31-unity dry coefficients in final dumps.
FS expected sums derive from the labeled source words. In settled matched
windows, HS requires zero new underruns, overruns, bank duplicates and reprimes.
The FS counter named `underruns` also records speculative short packet builds;
its rate need not match between phases. For positive 16-aligned produced-frame
delta P, the source permits short-build delta U within `0 <= U <= P/16 + 1`.
Each FS packet must contain 43–45 frames, with every K-packet prefix totaling
between `floor(43900*K/1000)` and `ceil(44300*K/1000)` frames. In a paused,
uninterrupted window with unchanged anchor and all IN replies counted, consumed
delta C and host frames H must satisfy `abs(C-H) <= 180`, the capacity of four
45-frame queued packets. Raw counts remain visible and other measured output fault
deltas must be zero. Pre-fence counter-window totals stay distinct from displayed
totals that include the later READY READ service fence.

Startup/reset cumulative reprimes can increase outside those settled windows.
The repeated 16-frame source tags test finite content/liveness, not unbounded
sample chronology. The mirror gate checks input progress and bad/partial packets;
it does not require every input diagnostic counter to be zero or prove DSP input
destinations. The independent input gate establishes that routing separately.
At FS input is absent, even on a duplex-enabled carrier. None of these modeled
checks claim zero FS underruns or physical dropout-free audio. The corrected
full carrier run passed with the exact scope recorded above.

Synthetic assembled-code diagnostics counted 56 executed instructions on the
tested new-LCD complete-byte path through the ROM tap and DRAM dispatch,
42 on the longest direct parser path, and 82518 for maximum-body serialization,
copy and CRC. The publisher's longest masked interval was 20 instructions,
with 29 masked instructions total including its separate expiry section.
These counts describe the tested paths and do not measure physical latency,
cache/bus contention or continuous-change CPU headroom.

The generic port instrumentation uses `--main-park PARK:RESUME` only at an
explicit safe park, resuming the firmware's service path, and `--panel-tx FILE`
for raw UART at normal batch/USB-hold exit. A borrowed-call return does not
permanently bypass the main loop. The instrument has synthetic timer/UART tests;
it does not turn port time into hardware timing.

Production port launches use an image-bound scheduling adapter. It validates
the selected image against associated linked loader/runtime metadata and the
actual publisher entry/call/park/resume instructions before adding a park pair.
An image filename or symbol name alone is insufficient. Default discovery leaves
unknown archived/external images unchanged when current metadata is missing or
unrelated; it never guesses a marker. A positively associated mirror must pass
strict instruction and explicit-marker checks. An explicit runtime metadata
override also requires strict association. An archived CF METER image booted
through this wrapper with stale mirror metadata, preserving its legacy idle
behavior; this short boot is not a complete firmware gate result.

Firmware-free protocol, transport, backend and route tests are described in
[TESTING](../contributing/TESTING.md). Port verification must additionally compare
accepted UART output to the canonical state, poison initial memory, exercise
fragmentation and bypass invalidation, and deliberately disable capture or
dispatch to prove the verifier catches a missing implementation. It must cover
lease expiry/reset, interrupted publication, CRC/chunk faults, EP0 DMA aborts,
descriptor identity and existing audio/input/MIDI/alignment checks across the
explicit selection matrix. Synthetic Python models alone do not verify assembly.

Host proof includes encoded EP0 exchanges through the real `SnapshotClient`,
`HardwareBackend` and HTTP handler: complete PNG/text/LED views, shared HTTP/SSE
consumers, denied controls, retained views while disconnected and epoch recovery.
Chromium 151 on Linux with Node 24 exercised the source-aware browser and legacy
emulator controls using a fake HTTP source. Separate production-server smoke
runs booted stock and the MAIN mirror image, checked the borrowed main call's
live gain result and exercised AMP attack keys/knob through the real port.
Those smoke runs used no project and sound off. Earlier full-suite counts
(266 on host integration,
320 on the server-caller branch) describe those trees, not a final all-tree run.

Targets of at most 250 ms p95 visible latency and at most 10 microseconds masked
steady-state span are hardware budgets, not measured results. Boot FIFO drain
has a different startup budget. Five host polls per second cannot establish ten
visible updates per second. Physical static-screen late attach, sustained audio,
multiple viewers, unplug/reconnect, driver preservation and USB traces remain
required before claiming hardware support.


## MKI boot/cache finding — 8 Oct 2026

The PR's early post-loader handoff fetched instructions directly from the
newly unpacked DRAM runtime. The loader's uncached data writes and raw hash
check do not synchronize the CPU instruction cache. Stock performs its
later cache initialization after this new early execution boundary. The
ColdFire port has no instruction-cache model, so its passing boot cannot
exclude this failure.

Operator-flashed MKI probes isolated the boundary with USB disconnected
requested (explicitly confirmed for the original failing PDBG0):

| probe | change | result |
|---|---|---|
| PDBG2 | restore only early handoff to stock | boots |
| PDBG6 | handoff reduced to stock SRAM call and continuation JMP | logo hang |
| PDBG7 | same stub via alternate SDRAM address | logo hang |
| PDBG8 | same 12-byte stub in free OS image space | boots |
| PDBG9 | OS-resident IC-only CPUSHL sweep before unchanged DRAM stub | boots |
| PDBG10 | same cache sweep with complete panel feature enabled | boots |

This is strong hardware evidence for missing instruction-cache coherence
at the early handoff. It does not establish precisely which stale cache
line was fetched, or prove MKII hardware behavior. The module fix enters
`panel_cache.s` from `0x40000512`, then transfers to the linked
`pm_after_loader` without changing its SRAM call or FIFO processing. The
wrapper preserves SR, registers, SP, CACR/ACRs and the data cache. It
covers index/way mode plus the panel range through both SDRAM windows
for physical-address mode. The fixed bridge fields are declared symbol
refs, resolved after the DRAM link. See the
[MCF54455 reference manual](https://www.nxp.com/docs/en/reference-manual/MCF54455RM.pdf),
sections 6.3.1 and 6.4.8, for CACR/CPUSHL semantics.

The new assembly test verifies instruction encoding, range coverage and
entry-state preservation. The final-image gate rejects a handoff that
bypasses the cache wrapper or carries mismatched runtime bridge pointers.
Neither test reproduces physical cache coherence; hardware remains the
acceptance test for this defect.

On PDBG10, physical EP0 INFO returned protocol 1.0, all 128 LCD blocks
valid, epoch 2 and an advancing generation. Capture subsequently stalled
on BEGIN; some discovery attempts returned malformed standard STRING
descriptors. Six separately spaced language-descriptor reads succeeded.
These observations establish protocol reachability, not reliable snapshots.
The exporter issues EPFLUSH then immediately checks FLUSH/PRIME/STATUS;
real flush completion is asynchronous (manual section 10.3.4.19). That is
a candidate for further USB investigation, not a proved cause of the
physical stalls or string errors. No host retries or unsafe reply-buffer
reuse were added to mask these errors.

PDBG11, built from the repository module fix (BUILD 80), was confirmed by
the operator to boot on MKI with USB connected. This extends the boot
evidence to the source-built fix; reliable USB snapshots, MKII hardware
and audio coexistence remain separate acceptance checks.


## Physical EP0 termination investigation — 8 Oct 2026

PDBG11 physical traffic included a valid 64-byte INFO, then a zero-byte
BEGIN, stalls, and later another zero-byte INFO. Stock EP0 IN queue-head
capabilities were `0x00400000`: 64-byte packets with automatic ZLP enabled
(ZLT=0). For an IN dTD exhausted by a full packet, the controller appends
a zero-length packet before retiring the dTD (MCF54455RM section
10.5.2.1.1). The host already has its requested 64 bytes and proceeds to
status; this extra packet is unnecessary for exact-wLength panel replies.
The port retires the transfer after moving the requested data and omits
this automatic-ZLP behavior.

PDBG12 changed only the stopped-controller initialization literal to
`0x20400000` (ZLT=1). The operator confirmed MKI boot with USB connected.
Six requests spaced by 200 ms then returned valid INFO/BEGIN responses,
including PENDING followed by READY; normal host capture still stalled.
This supports the ZLP hypothesis but is not yet a reliable-delivery proof.

PDBG13 combines that initialization policy with an idle-first guard. The
old code initiated a new asynchronous flush even on an idle endpoint,
then stalled if its immediate read still showed FLUSH. The regression
fixture deliberately leaves a flush pending: idle requests must avoid
issuing it, while a busy endpoint must preserve reply bytes and lease
state until cancellation completes. There is no polling loop or relaxed
DMA safety check. PDBG13 boots with USB connected on the operator’s MKI.
Physical capture validated all 128 LCD blocks and a 1580-byte snapshot
(CRC32 3690781866). A 30-second watch acquired 66 validated snapshots
with no reported errors; the hardware HTTP backend served status, screen
PNG and LED state with HTTP 200. This is MKI/macOS delivery evidence,
not a measurement of latency, audio coexistence or MKII behavior.

ZLT is configured before controller enable, never by rewriting a live
queue-head capability. Panel replies either equal wLength or end in a
short packet. The supported configuration lengths are MIDI124, audio250
and audio+input334; none needs a terminating ZLP for an overlong host
request. Stock device/qualifier/string and ordinary status replies also
end short; audio counter replies are 60 bytes and UAC2 metadata replies
are shorter. Any future shorter-than-wLength EP0 reply ending at a
multiple of 64 bytes needs explicit termination; the carrier-length
regression catches that descriptor-growth case. Descriptor bytes and
non-panel remix behavior remain unchanged.


## Physical input extension — schema 2

The mirror now includes complete accepted panel-to-CPU reports in a fixed
168-byte suffix, under the same CRC, generation check and immutable lease
as LCD/LED state. Protocol 1.1 / schema 2 adds support bit 0x40 and raises
the maximum body to 2,026 bytes inside the existing 2,048-byte allocation.
The updated host also accepts protocol 1.0 / schema 1 images such as PDBG13.

Four asserted whole-instruction hooks observe accepted inputs: key row
load at `0x400923c0`, encoder row load at `0x400924e4` before descriptor
enable filtering, and final calibrated fader stores at `0x40092fc2`
(normal callback) and `0x40092fa2` (factory-inverted callback). These
callbacks cover MKI UART2 and the MKII panel parser; the previous
`0x400925fe` call-site hook missed physical MKI fader reports.
The parser is installed by `0x40092674`, called during late main init at
`0x4001f948`, after runtime loading and instruction-cache synchronization.
The hooks preserve registers, post-displaced-instruction SR and stack,
replay stock instructions and make no USB calls. Initialization seeds
held key rows after final stock init without inventing rising edges.
The fader seeds from stock last position `0x400d16cc` only when it is
within 0..127. The stock uninitialized sentinel remains unknown until
a valid report; no midpoint is invented.

Eight key masks and 64 unsigned 16-bit rising-edge counters retain held
buttons and short taps. Key row 7 supplies encoder pushes. Seven pairs of
16-bit clockwise/counterclockwise detent counters preserve both directions,
including signed -128, without an event queue. Browser fader orientation
is `127 - calibrated_stock_value`: A/left=0, B/right=127.

Separate output-only generation and frozen output-generation fields keep
the independent UART-boundary verifier valid when input-only activity
advances the shared coherence generation. They are initialized module
data, not public protocol fields. The boot comparison permits exactly
the new four-byte output counter and 166-byte mutable input state; input
marker/version bytes, snapshot metadata and neighboring bytes remain pinned.
The port launcher asserts the exact seed BSR target from the associated ELF.

The host publishes detached input arrays, `inputs_epoch` and
`inputs_connection_id` from the accepted snapshot. Routine same-session
acquisition retains a live cached view; failed acquisition stays stale
through retries until successful verification. Tabs baseline counters on
backend instance plus accepted connection incarnation and epoch, including
reconnects that reuse epoch 1 or occur while a tab is suspended.

Input observation grants no writable controls. Held buttons remain
highlighted; released taps/pushes show 200 ms activity; turns show separate
250 ms direction indicators, without an absolute-position marker or accumulated cap angle. Stale observations clear
transient input state. Routine hardware refresh keeps verified LCD pixels
at full brightness and removes syncing text/overlays; errors remain in
the header.

Synthetic assembled tests cover observer state preservation, edge counts,
wrap, direction, calibrated fader values, interrupted copies and immutable
leases. Real Chrome tests cover visible indicators, multiple tabs and
reconnect baselines. Observed wrapper counts are bounded by 128 instructions
(maximum measured key row: 106); these are not physical cycle measurements.
Borrowed emulator stock calls can exceed the one-second lease in virtual
time: the selected-image gate reports canonical STALE expiry separately
from any successful immutable-body reread, then acquires a fresh validated
input snapshot. It never relaxes the firmware lease or DMA safety policy.
Physical input acceptance requires a fresh operator-flashed image; PDBG13's
hardware proof covers LCD/LED delivery only.


## PDBG14 local input evidence — 8 October 2026

The personal `szpanel` BUILD82 selected gate and full
`usb-panel-standalone` BUILD82 check passed. Both gates ran the accepted-input
parser/snapshot checks on MKI and MKII at high and full USB speed. The
project-dependent `verify_set` gate skipped because no project was supplied.
The copied personal remix was temporarily outside the curated registry
for the shared selftest/doc gate and restored afterward; its selected
gate ran separately.

The panel suite passed 146 USB and 91 panel tests, including real Chrome
scenarios. Later regression checks passed 74 tests; the final verifier suite
passed 30 tests after the lease/MIDI/epoch review fixes. Independent review
found no remaining actionable issue.

A cold personal build produced `OCTATRACK_PDBG14.bin` (BUILD82), with card
container checksum/payload round-trip and SYX-extracted MAIN byte identity.
The cold MAIN matches the selected-gate MAIN SHA256
`c5c5277f413bcdb6774d054d7ab4d47b872e17ddd382154c63d3675dfe8aa03c`.
Artifacts, hashes and logs stay local in `out/panel-bisect/PDBG14/`.
At packaging time this image had not been flashed. Subsequent operator
feedback and the follow-up fixes are recorded below.


## PDBG15 follow-up evidence — 8 October 2026

The operator's PDBG14 report exposed the untested physical MKI fader path:
its calibrated UART2 callbacks bypassed the panel parser hook. Cached
hardware status retained `known=5` and `fader=null` even after movement.
PDBG15 replaces that call-site hook with both stock callback commit stores
and seeds a valid stock last position. The image gate checks the actual
MKI callback registration and both normal/inverted callbacks; it does not
claim ADC or UART2 wire timing coverage.

A separate browser transform-origin mismatch clipped lower controls in
short windows. Real Chrome now checks the whole panel and fader positions
0, 64 and 127 at four desktop/short viewport sizes. Endless encoders have
no position marker or accumulated angle, only transient direction feedback.

The host includes acquisition cost in its negotiated polling period, with
the clock anchored immediately before INFO after discovery. Regression
tests reject bursts after slow discovery or slow acquisition. Browser
hardware input status uses 100 ms cached polling, with emulator/error
polling still 350 ms. Real Chrome measured cached input delivery around
77–86 ms. After restarting the viewer, a cache-only physical PDBG14 sample
measured observed INFO contacts around 205 ms, versus 279 ms before the
patch; every sampled state was live and no errors were reported. Idle
snapshot cadence remained around 411 ms, so this is not a physical
button-to-screen latency measurement or a ten-Hz delivery claim.

Fresh verification: `make test-panel` passed 144 USB and 93 panel tests;
`make check-remix REMIX=szpanel BUILD=83` and
`make check REMIX=usb-panel-standalone BUILD=83` passed every runnable gate.
`verify_set` skipped without a supplied project. The copied personal remix
was temporarily excluded from shared curated-registry gates and restored.
Independent follow-up review passed after resolving a discovery-cadence
finding. Cold PDBG15 packaging passed card checksum/payload round-trip,
SYX-extracted MAIN identity and cached/cold MAIN identity. Local artifacts
and source/evidence records stay in `out/panel-bisect/PDBG15/`.
PDBG15 has not been flashed; physical fader and complete input latency
acceptance remain operator checks.
