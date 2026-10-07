# USB panel mirror design and evidence

USB PANEL MIRROR observes accepted panel output and publishes full read-only
snapshots through vendor EP0. The host protocol definition is
[protocol.json](../../modules/usb-panel-mirror/protocol.json); its generated
[protocol.inc](../../modules/usb-panel-mirror/protocol.inc) and
[host parser](../../tools/panel/usb_mirror_protocol.py) must agree. The protocol
is experimental. [Operator setup](../guide/USB_PANEL_MIRROR.md) describes safe
host access and the read-only HTTP boundary.

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
DMA ownership, replacement setup, abort, reset and audio/MIDI coexistence need
final-image port coverage and physical driver traces; source inspection alone
is insufficient. Pending/ready reads, cookie retries, expiry, release and epoch
changes must remain bounded. Defaults are 1000 ms lease, 200 ms minimum host
polling and a 10 Hz publisher ceiling. The host uses a single cancellable worker
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
Port boot capture matches raw accepted UART output byte-for-byte for MKI/MKII:
6919/8156 bytes respectively, including 2522 early bytes, complete 128-block
LCD coverage and no observer faults. The first MAIN EP0 probe observed
PENDING/READY, a 1580-byte body, CRC and release across 59 setups with zero
stalls. These are port results. The broader DMA/abort/coexistence and carrier
matrix remains in progress; there is no unit, audio workload, flash, hardware
latency/headroom measurement or driver trace in this record.

The generic port instrumentation uses `--main-park PARK:RESUME` only at an
explicit safe park, resuming the firmware's service path, and `--panel-tx FILE`
for raw UART at normal batch/USB-hold exit. A borrowed-call return does not
permanently bypass the main loop. The instrument has synthetic timer/UART tests;
it does not turn port time into hardware timing.

Firmware-free protocol, transport, backend and route tests are described in
[TESTING](../contributing/TESTING.md). Port verification must additionally compare
accepted UART output to the canonical state, poison initial memory, exercise
fragmentation and bypass invalidation, and deliberately disable capture or
dispatch to prove the verifier catches a missing implementation. It must cover
lease expiry/reset, interrupted publication, CRC/chunk faults, EP0 DMA aborts,
descriptor identity and existing audio/input/MIDI/alignment checks across the
explicit selection matrix. Synthetic Python models alone do not verify assembly.

Targets of at most 250 ms p95 visible latency and at most 10 microseconds masked
steady-state span are hardware budgets, not measured results. Boot FIFO drain
has a different startup budget. Five host polls per second cannot establish ten
visible updates per second. Physical static-screen late attach, sustained audio,
multiple viewers, unplug/reconnect, driver preservation and USB traces remain
required before claiming hardware support.
