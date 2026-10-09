#!/usr/bin/env python3
"""USB-MIDI receive path under the ColdFire port: packets larger than the
firmware's 32-byte MIDI FIFO reach the MIDI parser whole and in order.

Boots out/mainos_bus.bin with the USB device model, enumerates at high
speed, and sends three EP2 OUT transfers, each ONE packet:

  * 16 note-ons (16 USB-MIDI events, 48 MIDI bytes, 64 B on the bus);
  * one 68-byte SysEx, F0 .. F7 (23 events, 92 B);
  * 128 note-ons (512 B, the high-speed bulk max packet);

then re-enumerates at full speed (64-byte max packet, the firmware's own dTD)
and sends the 16 note-ons again.

midi_rx_enqueue (0x40092bbc) has no full check and the framer that drains the
FIFO (0x40092bf4) cannot run inside the USB interrupt. The parser's output is
the byte stream the framer appends to the message buffer at 0x46100b84 (one
store per byte; the watch's log is read after the port exits), compared with
the bytes sent. Each transfer must also be ACCEPTED whole by the EP2 OUT dTD
(the dormant init primes it at 64 bytes).

SKIPs when the remix has no USB MIDI or the port is not built (`make emu-cf`).
What this cannot see: timing against DIN traffic arriving during a packet,
and the controller's behaviour on the unit (the dTD re-size is port-only).
"""
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1])); import toolpath  # noqa: E402,F401
from port_image import launch_args  # noqa: E402
import usb_host  # noqa: E402  (tools/harness)

from remix import registry  # noqa: E402

EMU = ROOT / "out/emu/ot_emu"
IMAGE = ROOT / "out/mainos_bus.bin"
MSG_BUF = 0x46100b84        # the framer's completed-message buffer (8 KB), one byte store per parsed byte
SETTLE_S = {"16 note-ons": 3.0, "68-byte SysEx": 4.0, "128 note-ons": 14.0, "16 note-ons, full speed": 3.0}   # wall time for the port to drain the FIFO


def events(raw):
    """USB-MIDI event packets (cable 0) for a raw MIDI byte stream of channel messages and one SysEx."""
    out, i = b"", 0
    if raw[0] == 0xf0:
        while i < len(raw):
            c = raw[i:i + 3]
            cin = 4 if len(c) == 3 and i + 3 < len(raw) else {1: 5, 2: 6, 3: 7}[len(c)]
            out += bytes([cin]) + c + bytes(3 - len(c))
            i += 3
        return out
    while i < len(raw):
        out += bytes([raw[i] >> 4]) + raw[i:i + 3]
        i += 3
    return out


def main():
    if not EMU.is_file():
        print("  [SKIP] verify_usbmidi_rx: the port is not built (make emu-cf)")
        return 0
    if not IMAGE.is_file():
        print("  [FAIL] verify_usbmidi_rx: no out/mainos_bus.bin (make bus)")
        return 1
    if "USB MIDI" not in registry.remix(os.environ.get("REMIX")).modules:
        print("  [SKIP] verify_usbmidi_rx: the remix has no USB MIDI")
        return 0
    notes16 = b"".join(bytes([0x90, 0x30 + i, 0x64]) for i in range(16))
    sysex = bytes([0xf0]) + bytes(range(1, 67)) + bytes([0xf7])
    notes128 = b"".join(bytes([0x90 + (i >> 5), i & 0x7f, 0x40 + (i & 0x3f)]) for i in range(128))
    cases = [("16 note-ons", notes16), ("68-byte SysEx", sysex), ("128 note-ons", notes128), ("16 note-ons, full speed", notes16)]
    sock = f"/tmp/ot-usbrx-{os.getpid()}.sock"
    log = ROOT / "out/verify_usbmidi_rx.log"
    with open(log, "w") as lf:
        emu = subprocess.Popen(launch_args([str(EMU), "--image", str(IMAGE), "--usb-host", sock, "--usb-hold-ms", "180000",
                                "--watch-mem", f"{MSG_BUF:#x},{sum(len(r) for _, r in cases) + 64}"]),
                               cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT)
    fails = []

    def check(what, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {what}{'  ' + detail if detail else ''}")
        if not ok:
            fails.append(what)

    accepted = {}
    try:
        b = usb_host.Bench(sock, timeout=60.0)
        usb_host.enumerate_device(b, hs=True)
        for name, raw in cases:
            if name.endswith("full speed"):
                usb_host.enumerate_device(b, hs=False)
            pkt = events(raw)
            accepted[name] = (b.ep_out(2, pkt), len(pkt))
            time.sleep(SETTLE_S[name])
    except Exception as e:  # noqa: BLE001 -- a hang or a stall is the finding
        check(f"the host script completed ({type(e).__name__}: {e})", False)
    finally:
        try:
            b.sock.close()
        except NameError:
            emu.kill()
    try:
        rc = emu.wait(timeout=180)
    except subprocess.TimeoutExpired:
        emu.kill()
        rc = -1
    check("the port exited cleanly after the client hung up", rc == 0, f"exit {rc}")
    # the message buffer as the parser left it: the last byte stored at each address
    mem = {}
    for l in log.read_text(errors="replace").splitlines():
        m = re.search(r"\[0x([0-9a-f]+)\] <- (0x[0-9a-f]+|0) \(1\)", l)
        if m and int(m.group(1), 16) >= MSG_BUF:
            mem[int(m.group(1), 16)] = int(m.group(2), 16)
    at = 0
    for name, raw in cases:
        got_n, sent_n = accepted.get(name, (0, 0))
        check(f"{name}: the EP2 OUT dTD accepts the whole {sent_n}-byte packet", got_n == sent_n and sent_n > 0, f"accepted {got_n} of {sent_n}")
        got = bytes(mem.get(MSG_BUF + at + i, 0xff) for i in range(len(raw)))
        bad = next((i for i in range(len(raw)) if got[i] != raw[i]), None)
        check(f"{name}: all {len(raw)} MIDI bytes reach the parser, in order", bad is None,
              "" if bad is None else f"first difference at byte {bad}: sent {raw[bad]:#04x} parsed {got[bad]:#04x}; "
              f"sent {raw[:12].hex()}.. parsed {got[:12].hex()}..")
        at += len(raw)
    print(f"verify_usbmidi_rx: {'OK' if not fails else str(len(fails)) + ' FAILED'} ({log})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
