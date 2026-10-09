# Physical panel input mirroring

The operator has confirmed that PDBG13 boots and displays LCD/LED snapshots
on an Octatrack MKI connected to macOS. The next change must mirror physical
crossfader movement, button holds and taps, encoder pushes, and encoder turn
direction. Routine image refresh must not display “syncing” in the header
or cover or dim the LCD. This extends the firmware/host interface; the
working cache synchronization and EP0 delivery fixes remain prerequisites.

## Selected approach

Extend the existing CRC-protected, immutable snapshot with bounded input
state and cumulative activity counters. Observe the stock input parser's
accepted reports rather than reading UART hardware a second time. Continue
using one USB worker and the existing lease, expiration and cancellation rules.

Reading only the current key mask would miss taps between polls. A retained
event queue would require overflow management and per-client consumption.
Cumulative press and directional detent counters retain brief activity
without queue growth or consuming events when a browser tab reads them.

## Firmware capture

Add a dedicated linked ColdFire input observer in the USB panel module.
Detours must assert whole stock instructions, replay them exactly, preserve
all general registers, stack and SR, and declare every claimed span.
Disassemble candidate parser hooks before selecting their final addresses.
Capture only complete, accepted reports; no interrupt-time allocation,
waiting, USB operation, CRC computation or full snapshot copy is allowed.

The stock parser handles key rows 0x20–0x27, encoder rows 0x30–0x36 and
the crossfader at 0x40. MKI also receives its physical fader through
dedicated UART2 callbacks, so observation must cover the shared normal
and factory-inverted callback commits. Key row 7 contains encoder pushes A–F and LEVEL.
Retain eight held-key row masks and a 16-bit counter for each rising edge
among their 64 bits. Increment counters only for a change from released to
held; repeated reports do not count as new presses. Obtain an initial key
baseline after stock initialization so already-held keys are represented
without manufacturing a new press.

Each of the seven encoders has separate clockwise and counterclockwise
16-bit cumulative detent counters. Signed deltas increment the appropriate
counter by their magnitude, including -128. Separate counters retain both
directions even if opposite turns would cancel in a signed total.

Capture the fader after the stock calibration calculation, rather than
assuming the raw ADC is linear. Normalize its calibrated value to 0=A/left,
127=B/right using the existing firmware-to-browser convention. Mark it
unknown until a calibrated report has been observed, unless the stock last
position already holds a validated value in 0..127 at final initialization.
The stock sentinel remains unknown. Unknown is not zero.

All input changes increment the snapshot's coherence generation. Input
state participates in the existing optimistic copy and final generation
check, so activity during serialization abandons the candidate. Counter
arithmetic wraps modulo 65536. Session/epoch changes establish a new host
baseline; there is no replay of old activity after reconnect.

## Protocol

Keep the OTPM header, response sizes, request IDs and lease machinery.
Introduce body schema 2 and protocol minor 1; updated hosts also accept the
existing schema 1 so PDBG13 continues to supply LCD/LED-only views. Old
hosts reject schema 2 explicitly rather than misinterpreting input bytes.

Schema 2 retains the complete canonical schema-1 LCD/LED/backlight prefix
and appends exactly one 168-byte input record:

| Offset | Field |
|---|---|
| 0 | Marker 0xb8 |
| 1 | Input-record version 1 |
| 2 | Known flags: bit 0 key baseline valid, bit 1 calibrated fader valid, bit 2 encoder counters valid; other bits zero |
| 3 | Fader position 0–127, zero when unknown |
| 4–11 | Eight held-key masks, row order 0–7 |
| 12–139 | 64 big-endian unsigned 16-bit rising-edge counters, row then bit order |
| 140–167 | Seven pairs of big-endian unsigned 16-bit detent counters, clockwise then counterclockwise |

Add input support capability bit 0x40 to schema-2 firmware. Unknown key or
encoder families contain zero masks/counters. Maximum canonical body size
becomes 2026 bytes (1858 + 168), within the existing 2048-byte allocation.
CRC covers the entire record. Validate marker, version, length, reserved
flags, position, capability/schema agreement and unknown-family values.
Do not feed the input suffix to the existing output-wire PanelLink decoder.
The definition JSON remains authoritative and regenerates assembly constants.

## Host and browser

Publish input state alongside the existing immutable view. HTTP reads use
cached state and never start extra USB acquisitions. Advertise input
observation separately from emulator control capability: hardware remains
read-only and no key injection endpoint is enabled. Carry cached input data
in `/status`; preserve existing LED streaming and emulator behavior.
Hardware input status polls use a 100 ms browser interval; emulator and
failed-request polls retain 350 ms. The USB worker includes transfer work
inside its negotiated start-to-start interval, anchored immediately before
INFO after discovery, without catch-up bursts or faster USB starts.
`inputs_epoch` and `inputs_connection_id` identify the accepted input
snapshot session, independently of a newer INFO heartbeat. Ordinary same-session acquisitions keep the
verified cached view live; initial and reconnect acquisition remain syncing.
A failed acquisition remains stale through retries until a newly verified
snapshot succeeds, so retry heartbeats cannot revive obsolete held keys.

Each tab tracks its own counter baseline keyed by backend instance,
accepted USB connection incarnation and firmware epoch. Its first observation displays current holds and fader
position without replaying historic taps or turns. A changed press counter
produces a 200 ms highlight even if the key is already released. Held keys
stay highlighted until released. Knob pushes use the same behavior.
Directional encoder counters drive a visible clockwise/counterclockwise
indicator with no absolute-position marker or accumulated cap angle; both directions within one poll
are shown as activity in both directions without claiming their order.
Turn indicators expire after 250 ms without fresh activity. Re-reading the
same snapshot must not repeat an animation. Modular differences handle
counter wrap. These are activity indicators, not absolute knob positions.

On stale or disconnected input, clear holds and activity indicators so no
control looks stuck. Keep the last fader position with its stale state
available in diagnostics; unknown fader state must not appear measured.

Remove routine syncing text from both header and LCD overlay. Once a
verified LCD image exists, keep it visible at full brightness while its
replacement loads. Before the first image, use a neutral blank screen.
Connection errors remain visible in the header, without an LCD overlay.
Keep diagnostics in `/status` for investigation.

## Verification and hardware acceptance

Tests execute assembled input observers with synthetic reports and verify
register/SR/stack preservation, rising-edge semantics, encoder signs and
wrap, calibrated fader orientation, and bounded interrupt work. Snapshot
tests cover input changes during copy, lease immutability, maximum size and
CRC. Host tests cover both schemas, malformed records, reset/reconnect
baselines and cached HTTP delivery. Browser tests cover short taps, holds,
encoder pushes, each direction and opposite-direction activity, fader
updates, stale clearing, multiple tabs and quiet image refresh.

Run the module's selected-image gate for szpanel and the full standalone
check with the curated remix registry, plus relevant panel tests. Build a
fresh diagnostic image locally with the operator's stock OS; no firmware
or hardware capture is committed. Operator hardware acceptance includes
cold boot with USB connected, held and quick-tapped buttons, A–F/LEVEL
pushes and turns in each direction, fader endpoints/midpoint, and absence
of syncing flicker. Local tests cannot establish physical interrupt timing,
audio coexistence or MKII behavior.
