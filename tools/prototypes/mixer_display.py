#!/usr/bin/env python3
"""Throwaway mixer UI: simulated data poked into the emulator LCD, no OS patch.

Run with .venv/bin/python3 tools/prototypes/mixer_display.py [--interactive].
Numbers are proposed volume positions; filled bars are simulated signal levels.
"""
import argparse
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tools/emu"), str(ROOT / "tools/panel")]
import emu_card
import lcd_view as lcd
from panel_server import PortProc

# Original 3x5 bitmap font, deliberately independent of firmware assets.
PATTERNS = {
    "A":"010/101/111/101/101", "B":"110/101/110/101/110", "C":"011/100/100/100/011",
    "D":"110/101/101/101/110", "E":"111/100/110/100/111", "F":"111/100/110/100/100",
    "G":"011/100/101/101/011", "H":"101/101/111/101/101", "I":"111/010/010/010/111",
    "J":"001/001/001/101/010", "K":"101/101/110/101/101", "L":"100/100/100/100/111",
    "M":"101/111/111/101/101", "N":"101/111/111/111/101", "O":"010/101/101/101/010",
    "P":"110/101/110/100/100", "Q":"010/101/101/111/011", "R":"110/101/110/101/101",
    "S":"011/100/010/001/110", "T":"111/010/010/010/010", "U":"101/101/101/101/111",
    "V":"101/101/101/101/010", "W":"101/101/111/111/101", "X":"101/101/010/101/101",
    "Y":"101/101/010/010/010", "Z":"111/001/010/100/111",
    "0":"111/101/101/101/111", "1":"010/110/010/010/111", "2":"110/001/010/100/111",
    "3":"110/001/010/001/110", "4":"101/101/111/001/001", "5":"111/100/110/001/110",
    "6":"011/100/111/101/111", "7":"111/001/010/010/010", "8":"111/101/111/101/111",
    "9":"111/101/111/001/110", "/":"001/001/010/100/100", "-":"000/000/111/000/000",
    "<":"001/010/100/010/001", ">":"100/010/001/010/100", " ":"000/000/000/000/000",
    "!":"010/010/010/000/010",
}
NAMES = ("INTERNAL TRACKS", "EXTERNAL INPUTS", "MAIN / CUE")
VOLUMES = ((108, 96, 112, 88, 100, 76, 104, 92), (96, 96, 80, 88), (110, 96))
SIGNALS = ((.74, .66, .55, .49, .89, .81, .37, .43, .66, .59, .26, .31, .81, .72, .47, .42),
           (.76, 1.12, .43, .66), (.79, .68, .51, .60))


def render(page, phase=0.0):
    rows = [[0] * 128 for _ in range(64)]

    def rect(x, y, w, h, value=1):
        for yy in range(max(0, y), min(64, y+h)):
            for xx in range(max(0, x), min(128, x+w)):
                rows[yy][xx] = value

    def text(s, x, y, value=1):
        for ch in s:
            for dy, line in enumerate(PATTERNS[ch].split("/")):
                for dx, bit in enumerate(line):
                    if bit == "1":
                        rect(x+dx, y+dy, 1, 1, value)
            x += 4

    def centered(s, cx, y):
        text(s, cx-(4*len(s)-1)//2, y)

    rect(0, 0, 128, 9)
    text(NAMES[page], 3, 2, 0)
    text(f"{page+1}/3", 114, 2, 0)
    # Signal and volume share a normalized 0..127 rail. A cut-through
    # horizontal marker is fixed at 100; the exterior tick is the volume.
    def rail(x, width, signals, volume):
        top, bottom = 19, 46
        rect(x, top, width, 1)
        rect(x, top, 1, bottom-top+1)
        rect(x+width-1, top, 1, bottom-top+1)
        rect(x, bottom, width, 1)
        inner_width = width-2
        for i, level in enumerate(signals):
            start = x+1+i*(inner_width//len(signals))
            fill_width = inner_width if len(signals) == 1 else inner_width//2-1
            h = round(26*max(0, min(1, level)))
            rect(start, bottom-h, fill_width, h)
        reference_y = bottom-round(27*100/127)
        # A dark cut with lit edges remains visible on empty and filled rails.
        rect(x+1, reference_y, width-2, 1, 0)
        rect(x-1, reference_y, 2, 1)
        rect(x+width-1, reference_y, 2, 1)
        tick_y = bottom-round(27*volume/127)
        rect(x+width+1, tick_y, 2, 1)
        rect(x+width+2, tick_y-1, 1, 3)

    def signal(i):
        return max(0, SIGNALS[page][i] + .10*math.sin(phase+i*1.7))

    if page == 0:
        for i, vol in enumerate(VOLUMES[page]):
            x = 1+i*16
            centered(str(i+1), x+7, 11)
            rail(x+1, 11, (signal(2*i), signal(2*i+1)), vol)
            centered(str(vol), x+7, 49)
    elif page == 1:
        for i, vol in enumerate(VOLUMES[page]):
            x = 8+i*30
            clipping = signal(i) >= 1
            label = "ABCD"[i] + ("!" if clipping else "")
            if clipping:
                rect(x+5, 10, 11, 7)
                text(label, x+7, 11, 0)
            else:
                centered(label, x+10, 11)
            rail(x+4, 13, (signal(i),), vol)
            centered(str(vol), x+10, 49)
    else:
        for i, vol in enumerate(VOLUMES[page]):
            x = 12+i*64
            centered(("MAIN", "CUE")[i], x+16, 11)
            rail(x+2, 10, (signal(2*i),), vol)
            rail(x+21, 10, (signal(2*i+1),), vol)
            centered(str(vol), x+16, 49)
            text("L", x-4, 31)
            text("R", x+35, 31)
    rect(0, 56, 128, 1)
    text("<", 2, 59)
    centered("0-127  -100  SIM", 64, 59)
    text(">", 122, 59)
    return rows


def plane(rows):
    data = bytearray(1024)
    for y, row in enumerate(rows):
        for x, bit in enumerate(row):
            if bit:
                data[x*8+(63-y)//8] |= 1 << (7-(63-y)%8)
    return data


class Prototype:
    def __init__(self, proc, path):
        self.proc, self.path = proc, path
        self.page, self.phase, self.held = 0, 0.0, set()

    def show(self):
        self.proc.command("poke 0x46c7e0ea " + plane(render(self.page, self.phase)).hex(), "ok")
        self.proc.command("run 0", "ok")
        data = lcd.read_plane(self.path)
        if lcd.screen(data) != render(self.page, self.phase):
            raise RuntimeError("Emulator LCD readback differs from proposed panel")
        return data

    def key(self, code, down):
        if not down:
            self.held.discard(code)
            return
        if code in self.held:
            return
        self.held.add(code)
        if code == 0x30:
            for modifier, page in ((0x35, 0), (0x2b, 1), (0x2a, 2)):
                if modifier in self.held:
                    self.page = page
                    break
        elif code == 0x34:
            self.page = max(0, self.page-1)
        elif code == 0x21:
            self.page = min(2, self.page+1)

    def enc(self, n, delta):
        pass

    def pot(self, value):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", action="store_true", help="capture all three panels (default)")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--model", choices=("mki", "mkii"), default="mkii")
    ap.add_argument("--image", type=Path, default=ROOT / "out/raw/section_3_MAIN_OS.bin")
    ap.add_argument("--out", type=Path, default=ROOT / "out/mixer-prototype")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    tree = a.out / "card-tree"
    (tree / "OCTABAM/AUDIO").mkdir(parents=True, exist_ok=True)
    card = a.out / "card.img"
    card.write_bytes(emu_card.build_image(str(tree), size_mb=64))
    path = a.out / "lcd.bin"
    argv = [str(ROOT / "out/emu/ot_emu"), "--image", str(a.image),
            "--card", str(card), "--interactive", "--lcd", str(path)]
    if a.model == "mkii":
        argv.append("--mkii")
    proc = PortProc(argv)
    try:
        proc.wait_ready(300)
        proc.command("poke 0x46c7d34c " + bytes(280).hex(), "ok")
        ui = Prototype(proc, path)
        for page, name in enumerate(("internal", "inputs", "outputs")):
            ui.page = page
            lcd.png(lcd.screen(ui.show()), str(a.out / f"{name}.png"), scale=6)
            print(f"Captured emulator LCD: {a.out / (name+'.png')}")
        ui.page = 0
        if a.interactive:
            # Relocated uv Python can retain a build-time Tcl lookup path.
            # Use its bundled scripts when present; respect explicit overrides.
            for variable, directory in (("TCL_LIBRARY", "tcl9.0"), ("TK_LIBRARY", "tk9.0")):
                bundled = Path(sys.base_prefix) / "lib" / directory
                if bundled.is_dir():
                    os.environ.setdefault(variable, str(bundled))
            import tkinter as tk
            root = tk.Tk()
            root.title(f"Mixer prototype - {a.model.upper()} emulation - simulated meters")
            canvas, x, y, scale = lcd.build_panel(tk, root, ui)
            # Keyboard equivalents permit actual held-key chords with one mouse.
            for char, code in (("m", 0x35), ("r", 0x2b), ("c", 0x2a), ("v", 0x30)):
                root.bind(f"<KeyPress-{char}>", lambda e, k=code: ui.key(k, True))
                root.bind(f"<KeyRelease-{char}>", lambda e, k=code: ui.key(k, False))
            tk.Label(root, text=f"{a.model.upper()} emulation; shared MKII control skin. Hold M / R / C, then V = MIDI / REC1 / CUE + MIXER. Left/right change panels. Stereo: left bar L, right bar R. Simulated data.").pack()

            def update():
                ui.phase += .13
                rows = lcd.screen(ui.show())
                # Put native pixels into a native image, then scale nearest-neighbor.
                native = tk.PhotoImage(width=128, height=64)
                native.put(" ".join("{"+" ".join("#%02x%02x%02x" % (lcd.ON if b else lcd.OFF) for b in row)+"}" for row in rows))
                canvas.delete("prototype-lcd")
                canvas.image = native.zoom(scale)
                canvas.create_image(x, y, anchor="nw", image=canvas.image, tags="prototype-lcd")
                root.after(100, update)
            update()
            root.mainloop()
        (a.out / "port.log").write_text("\n".join(proc.log)+"\n")
    finally:
        if proc.alive():
            proc.quit()


if __name__ == "__main__":
    main()
