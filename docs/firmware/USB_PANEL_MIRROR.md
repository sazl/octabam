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
Before writing the reply it issues one EP0-IN-only flush and checks FLUSH,
PRIME and STATUS. If the endpoint remains active, it stalls without modifying
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

Final digital validation uses verifier checkpoint `dd7e4cfe`, SHA-256
`bf0877408affd378132bca5211265f78e5ecf8afae05149a8676057758a79a45`.
It completed all 20 registry-accepted output/input selections on MKI/MKII at
HS/FS, all four standalone dimensions, and three private negative controls.
All commands returned zero; original selected MAIN image/runtime were restored
byte-for-byte and temporary carriers/processes removed.

| proof | final result and scope |
|---|---|
| audio-selection matrix | 80/80 runs, 240 measured audio windows; exact four deliberate STALLs per run |
| standalone | 4/4 runs, no audio interfaces; exact three deliberate STALLs per run |
| falsification | 3/3 privately altered capture/dispatch/publisher images rejected by the same verifier |
| output counters | HS measured fault deltas zero; FS short-build counts 0–444 satisfy the reviewed bounds, other measured deltas zero |
| concurrent input diagnostics | 30 HS runs advance input counters and require zero bad/partial packet deltas; input underrun deltas 0–2, no all-zero-input claim |
| project alignment | genuine SKIP without an authorized source project; strict release acceptance blocked |

The private matrix JSON hash is
`e03f9179c1b8b197595c933194b1d678537b0f3de3e3d6c3d6d31135805e258a`.
Per-image/runtime fingerprints, raw counter endpoints, dimension audit and
restoration evidence stay in ignored/local output, not in repository fixtures.
Final combined-root gate results remain pending separately from this checkpoint.

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
