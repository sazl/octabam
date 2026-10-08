# Viewing a physical Octatrack panel over USB

The browser can display LCD and available LED state from firmware that exports
USB PANEL MIRROR. Schema 2 additionally mirrors physical keys, encoder
pushes/turn direction and the calibrated crossfader. Schema 1 images such
as PDBG13 provide LCD/LED state only. This is an experimental, read-only viewer. The usual
`make panel REMIX=<name>` still runs the emulator and its controls. The native
macOS launcher remains emulator-only; use a browser for the physical viewer.

## Compatible firmware and evidence

The host requires the OTPM v1 handshake, complete LCD coverage, lease and CRC
capabilities. Stock firmware and an ordinary USB audio/MIDI remix do not export
this protocol. A selectable exporter must include USB PANEL MIRROR, USB MIDI,
and exactly one USB dispatcher: a supported audio output or USB PANEL MIRROR
STANDALONE. CF METER IDLE conflicts with its publisher. Consult the module and
remix indexes for actual selections and proof levels before installing an image.
Viewing does not build or flash firmware and needs no local OS image or project.

Protocol/transport tests use synthetic data. Boot capture and the initial EP0
probe have ColdFire port evidence; they do not establish physical USB driver
access, simultaneous audio, hardware timing or performance. MKI diagnostic flashes on 8 Oct 2026 reproduced a boot-logo hang and
restored full-feature boot with instruction-cache synchronization.
Earlier images received INFO but showed malformed string descriptors and
snapshot-BEGIN stalls. PDBG13 (BUILD=81) boots with USB connected and
delivered a complete capture plus 66 validated snapshots over 30 seconds
with no reported errors on the physical MKI/macOS setup. The hardware
backend also served LCD PNG and LEDs over HTTP. The operator confirmed the visible panel works. Reconnect behavior and
audio coexistence remain unmeasured. Final port validation passed
80 audio-profile runs, four standalone runs and three negative controls with
the scoped digital checks described in the
[design and proof limits](../firmware/USB_PANEL_MIRROR.md).

## Install host dependencies

The base Python dependencies remain empty. For the viewer, install the optional
host libraries with `uv sync --extra emu` (including PyUSB 1.3.1), and install
native libusb using your operating system's package manager. On Debian/Ubuntu,
`sudo apt install libusb-1.0-0` installs the runtime. On macOS, `brew install
libusb` is a common choice. The Python process and libusb must have the same
architecture: an arm64 Python needs arm64 libusb; an x86_64 Python needs x86_64
libusb. A missing-library diagnostic means discovery could not load that library,
not that the Octatrack lacks the mirror.

Linux access must be granted to the ordinary user for the audited device identity
`1935:0002`. An administrator can use a device-specific udev rule such as:

```udev
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="1935", ATTR{idProduct}=="0002", TAG+="uaccess"
```

Use your distribution's rules directory and reload rules; reconnect the USB
cable to apply access. `uaccess` requires an active local seat; on a headless
machine ask the administrator for a dedicated group with mode `0660` on this
specific device. Do not use a broad `0666` rule or run the server as root.

The transport performs device string discovery and mirror vendor IN requests.
It does not configure, reset, claim interfaces, change alternate settings,
detach or rebind drivers, or use audio/MIDI endpoints. Preserve the existing
audio driver. Nondisruptive Windows access has not been measured and is currently
unsupported; replacing a composite audio driver with Zadig is not a supported
setup step.

## Start and select a device

```sh
make panel-hardware
make panel-hardware USB_DEVICE='serial:UNIT_SERIAL' USB_POLL_HZ=5
make panel-hardware USB_DEVICE='topology:1-2.3' PANEL_PORT=8571
.venv/bin/python3 tools/panel/panel_server.py --source hardware --usb-poll-hz 5
```

Open <http://localhost:8563/> (or your selected port). The default source is
`port`; choose `--source hardware` explicitly outside the Make target. The server
binds loopback, starts one `HardwareBackend(selector=None, poll_hz=5)` worker,
and shares its immutable cached view among browser tabs. Tabs do not each acquire
a USB lease. Incompatible emulator arguments are rejected instead of starting
an emulator or silently changing sources. Stop with Ctrl-C.

With several compatible devices, select `serial:<serial>` or
`topology:<bus>-<port>.<port>`. Topology identifies a USB location, not a physical
unit. If no serial identity is available, disconnect requires an explicit server
restart before adopting a device at that location. The viewer must not silently
adopt a replacement unit. A stable serial selection permits reconnect to that
same serial. Restart after changing your intended selector.

## Status and recovery

A static LCD can remain unchanged while INFO heartbeats confirm contact. Status
separates last contact, last accepted snapshot and last display change; an old
snapshot is not evidence of a live device. Polling defaults to 5 Hz and respects
firmware minimum/rate limits. USB work counts within the negotiated interval,
so a 200 ms interval does not add another 200 ms after an acquisition. Slow
acquisitions never trigger catch-up bursts. The browser reads cached hardware
input status every 100 ms; emulator status retains its 350 ms interval.
Ten visible updates per second is a target, not a claim for this five-poll
profile. Physical button-to-screen latency and audio headroom remain unmeasured.

| response or symptom | meaning and next action |
|---|---|
| `503 frame_unavailable` | no validated frame yet; check connection, firmware handshake and status |
| `409 capability_unavailable` | optional LED state is unavailable; the LCD can still work |
| `409 unsupported_backend_operation` | an emulator control was requested in hardware mode; controls are unavailable |
| `404` | unknown route |
| permission error | correct device-specific user access, then reconnect/retry |
| unsupported firmware/protocol | install an explicitly verified compatible selection; the viewer cannot add firmware support |
| stale/disconnected | inspect cable and status; reconnect the selected serial or restart explicitly for a serial-less device |
| shutdown incomplete | a USB operation exceeded its timeout; wait for it to finish before restarting |

Hardware HTTP reads are limited to `/`, `/skin.js`, `/screen.png`, `/screen.txt`,
`/status`, `/map`, `/leds`, and `/leds/stream`. Every known emulator operation is
refused, including GET routes that mutate state. Keys, encoders, fader, transport,
project/card operations, uploads, audio controls and captures are unavailable.
Physical input observation does not enable browser control of the unit.

## Bounded diagnostic probe

```sh
.venv/bin/python3 tools/hw/usb_panel.py --selector serial:UNIT_SERIAL info
.venv/bin/python3 tools/hw/usb_panel.py --selector serial:UNIT_SERIAL capture --output out/panel-body.dat
.venv/bin/python3 tools/hw/usb_panel.py --selector serial:UNIT_SERIAL watch --count 10
.venv/bin/python3 tools/hw/usb_panel.py watch --duration 3
```

`capture` writes a validated canonical snapshot to the chosen local file;
`watch` requires a count or duration. Run one probe at a time, with the viewer
stopped. These commands do not write device state beyond mirror lease bookkeeping.
Keep captures under ignored `out/`; do not publish proprietary firmware or device
captures as CI fixtures. Browser acceptance used Chromium 151 on Linux with
Node 24 and a fake HTTP source, including two tabs and legacy emulator controls.
Encoded mirror exchanges also passed through the real SnapshotClient,
HardwareBackend and HTTP handler; these are host integration checks. Native
AppKit compilation/runtime and physical Linux/Windows acceptance remain
unmeasured. MKI/macOS snapshot and HTTP delivery passed on PDBG13;
visual comparison and reconnect acceptance remain open.


## Iterate with a connected Octatrack

Build in an isolated worktree with its own `out/emu`, shared native `.venv`
and `vendor`, and your own extracted stock MAIN. Keep all firmware artifacts
local. For your personal `szpanel` remix, the loop is:

```sh
export PATH="$PWD/.venv/bin:$PATH"
make check-remix REMIX=szpanel BUILD=83
make check REMIX=usb-panel-standalone BUILD=83
make image REMIX=szpanel BUILD=83 VERSION=PDBG15 SYX=/absolute/path/OCTATRACK_OS1.40C.syx
```

Choose a fresh BUILD for each flash. The check exercises the port and USB
device model; it cannot reproduce physical CPU caches or USB timing. Copy
the generated card image in USB DISK MODE, eject, install via OS UPGRADE,
and power-cycle with USB unplugged first. Recovery from a logo hang uses
the startup MIDI upgrade path and a USB-to-DIN MIDI interface; the onboard
USB connection is not the firmware-update transport. See
[BUILDING.md](BUILDING.md) for the flash and recovery procedure.

After confirming cold boot, connect USB in normal operating mode. Run one
client at a time:

```sh
.venv/bin/python tools/hw/usb_panel.py --verbose info
.venv/bin/python tools/hw/usb_panel.py capture --output out/panel-body.dat
.venv/bin/python tools/hw/usb_panel.py watch --duration 30 > out/panel-watch.jsonl
make panel-hardware
```

Inspect errors inside the watch log; exit zero alone does not prove success.
Compare the browser with the physical LCD and LEDs while changing screens,
then test idle display, reconnect and normal playback. Stop the viewer
before another capture client acquires the lease. The physical USB backend
is read-only and provides snapshots, not memory inspection, breakpoints,
hot-loading or panel key injection. Boot regressions still require an
operator-observed power cycle.
