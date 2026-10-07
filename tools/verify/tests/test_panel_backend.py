"""Firmware-free behavior of the immutable panel read-view boundary."""
import dataclasses
import importlib
import pathlib
import struct
import subprocess
import sys
import threading
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
PANEL = ROOT / "tools/panel"
sys.path.insert(0, str(PANEL))
from panel_link import PanelLink
import panel_server


def png_pixels(png):
    """Decode the unfiltered RGB PNG without optional image packages."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, compressed = 8, bytearray()
    while pos < len(png):
        size = struct.unpack_from(">I", png, pos)[0]
        tag, body = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + size]
        crc = struct.unpack_from(">I", png, pos + 8 + size)[0]
        assert crc == zlib.crc32(tag + body)
        if tag == b"IHDR":
            assert struct.unpack(">IIBBBBB", body) == (128, 64, 8, 2, 0, 0, 0)
        if tag == b"IDAT":
            compressed.extend(body)
        pos += size + 12
    raw = zlib.decompress(compressed)
    assert len(raw) == 64 * 385
    assert all(raw[y * 385] == 0 for y in range(64))
    return [raw[y * 385 + 1:(y + 1) * 385] for y in range(64)]


class PanelBackendTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((PANEL / "panel_backend.py").exists(),
                        "missing immutable panel_backend read-view implementation")
        self.backend = importlib.import_module("panel_backend")

    def legacy_view(self, link):
        # Exercise the actual old renderer without constructing/starting a Panel.
        panel = object.__new__(panel_server.Panel)
        panel.link = link
        panel.frame = b""
        panel.screen_txt = ""
        panel.seq = 0
        panel.lock = threading.Lock()
        panel._feed_link = lambda: True
        panel._snapshot(None)
        return panel.frame, panel.screen_txt

    def test_render_matches_existing_renderer_for_patterns(self):
        for pattern in (bytes(1024), b"\xff" * 1024,
                        bytes((i * 37 + i // 128) & 255 for i in range(1024))):
            with self.subTest(pattern=pattern[:8]):
                link = PanelLink()
                link.frame[:] = pattern
                link.dirty = True
                expected = self.legacy_view(link)
                self.assertEqual(self.backend.render_link(link), expected)

    def test_render_preserves_edge_pixels_in_every_page(self):
        for page in range(8):
            with self.subTest(page=page):
                link = PanelLink()
                link.feed(bytes((0x10 + page, 0, 1)) + bytes(7))
                link.feed(bytes((0x10 + page, 120)) + bytes(7) + b"\x80")
                png, text = self.backend.render_link(link)
                pixels = png_pixels(png)
                y = (7 - page) * 8
                for row in range(64):
                    for x in range(128):
                        lit = (x, row) in ((0, y), (127, y + 7))
                        self.assertEqual(pixels[row][x * 3:x * 3 + 3],
                                         b"\xf2\xf2\xf2" if lit else b"\x06\x06\x07")
                self.assertEqual(text.splitlines()[y][0], "#")
                self.assertEqual(text.splitlines()[y + 7][-1], "#")
                self.assertEqual(text.count("#"), 2)
                self.assertEqual(len(text), 64 * 129)
                self.assertTrue(text.endswith("\n"))
                self.assertEqual((png, text), self.legacy_view(link))

    def test_render_uses_lcd_rows_without_mutating_decoder(self):
        class RowsOnly(PanelLink):
            def lcd_rows(self):
                return [[x == y for x in range(128)] for y in range(64)]

        link = RowsOnly()
        link.dirty = True
        link.feed(b"\x2f\x81\x3e\x05\xb7\x7f")
        before = (bytes(link.frame), dict(link.led_rows), dict(link.leds),
                  link.backlight, link.dirty, dict(link.stats))
        png, text = self.backend.render_link(link)
        self.assertEqual(text.splitlines()[63][63], "#")
        self.assertEqual(text.count("#"), 64)
        self.assertEqual(png_pixels(png)[0][:3], b"\xf2\xf2\xf2")
        self.assertEqual(before, (bytes(link.frame), dict(link.led_rows), dict(link.leds),
                                  link.backlight, link.dirty, dict(link.stats)))

    def test_led_payload_preserves_all_32_physical_rows(self):
        leds = self.backend.LedSnapshot(bytes(range(32)), ((255, 15), (5, 0), (16, 3)))
        payload = self.backend.led_payload(leds)
        self.assertEqual(payload, {"bits": bytes(range(32)).hex(),
                                   "ids": {"0x05": 0, "0x10": 3, "0xff": 15}})
        self.assertEqual(list(payload["ids"]), ["0x05", "0x10", "0xff"])
        self.assertEqual(len(bytes.fromhex(payload["bits"])), 32)

    def test_led_payload_preserves_negotiated_row_count_and_optional_backlight(self):
        for count in (0, 17, 32, 64):
            with self.subTest(count=count):
                leds = self.backend.LedSnapshot(bytes(range(count)), ())
                self.assertIsNone(leds.backlight)
                self.assertEqual(self.backend.led_payload(leds),
                                 {"bits": bytes(range(count)).hex(), "ids": {}})
        for value in (0, 255):
            leds = self.backend.LedSnapshot(b"\x80", (), value)
            self.assertEqual(self.backend.led_payload(leds),
                             {"bits": "80", "ids": {}, "backlight": value})

    def test_snapshots_detach_caller_owned_mutable_containers(self):
        bits = bytearray(b"\x01\x80")
        ids = [[9, 2], [1, 15]]
        png = bytearray(b"png")
        capabilities = {"screen", "leds"}
        leds = self.backend.LedSnapshot(bits, ids)
        view = self.backend.ViewSnapshot(png, "display\n", leds, 7, "MKI", capabilities, 12.5, 10.0)
        bits[0] = 0
        ids[0][1] = 0
        ids.clear()
        png.clear()
        capabilities.clear()
        self.assertEqual(leds.bits, b"\x01\x80")
        self.assertEqual(leds.ids, ((1, 15), (9, 2)))
        self.assertEqual(view.png, b"png")
        self.assertEqual(view.capabilities, frozenset(("screen", "leds")))
        self.assertEqual((view.generation, view.model, view.snapshot_at, view.display_changed_at),
                         (7, "MKI", 12.5, 10.0))
        for obj, field, value in ((leds, "bits", b""), (view, "text", "other")):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, field, value)
        hash(leds)
        hash(view)

    def test_payload_is_detached_from_snapshot(self):
        leds = self.backend.LedSnapshot(b"\x01", ((5, 2),))
        payload = self.backend.led_payload(leds)
        payload["ids"]["0x05"] = 0
        payload["bits"] = "00"
        self.assertEqual(self.backend.led_payload(leds), {"bits": "01", "ids": {"0x05": 2}})

    def test_unsupported_optional_state_stays_none(self):
        capabilities = frozenset(("screen",))
        view = self.backend.ViewSnapshot(b"png", "screen", None, 1, None, capabilities, 4.0, 4.0)
        self.assertIsNone(view.leds)
        self.assertIsNone(view.model)
        self.assertEqual(view.capabilities, capabilities)

    def test_view_rejects_mutable_decoder_or_mapping_as_leds(self):
        for invalid in (PanelLink(), {}, []):
            with self.subTest(invalid=type(invalid).__name__), self.assertRaises(TypeError):
                self.backend.ViewSnapshot(b"png", "screen", invalid, 1, None,
                                          frozenset(("screen",)), 4.0, 4.0)

    def test_module_import_has_no_server_or_optional_dependency_requirement(self):
        code = '''
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'panel_server', 'usb', 'numpy', 'sounddevice', 'emu_card', 'toolpath'}:
        raise AssertionError('read-view import requires ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from panel_backend import LedSnapshot, ViewSnapshot, PanelBackend, render_link, led_payload
from panel_link import PanelLink
assert render_link(PanelLink())[0].startswith(b'\\x89PNG')
assert led_payload(LedSnapshot(b'', ())) == {'bits': '', 'ids': {}}
'''
        result = subprocess.run([sys.executable, "-S", "-c", code, str(PANEL)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_clean_package_import_without_panel_directory_on_path(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
from tools.panel.panel_backend import LedSnapshot, ViewSnapshot, PanelBackend, render_link, led_payload
from tools.panel.panel_link import PanelLink
assert render_link(PanelLink())[0].startswith(b'\\x89PNG')
assert led_payload(LedSnapshot(b'', ())) == {'bits': '', 'ids': {}}
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT)],
                                cwd="/tmp", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
