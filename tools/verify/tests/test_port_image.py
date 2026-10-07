"""Small invented ColdFire images and ELF files; no firmware assets/toolchain."""
import pathlib
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import port_image
from remix import pack


def elf(base, raw, symbols):
    """ELF32/MSB executable with one allocatable section and a symbol table."""
    strings = b'\0'
    syms = bytes(16)
    entries = [(k, v, 0x10) for k, v in symbols.items()] if isinstance(symbols, dict) else symbols
    for name, addr, info in entries:
        syms += struct.pack('>IIIBBH', len(strings), addr, 0, info, 0, 1)
        strings += name.encode() + b'\0'
    data = raw + strings + syms
    off = 52 + len(data)
    header = b'\x7fELF\x01\x02\x01' + bytes(9)
    header += struct.pack('>HHIIIIIHHHHHH', 2, 4, 1, base, 0, off, 0, 52, 0, 0, 40, 4, 0)
    sections = bytes(40)
    sections += struct.pack('>10I', 0, 1, 6, base, 52, len(raw), 0, 0, 2, 0)
    sections += struct.pack('>10I', 0, 3, 0, 0, 52 + len(raw), len(strings), 0, 0, 1, 0)
    sections += struct.pack('>10I', 0, 2, 0, 0, 52 + len(raw) + len(strings), len(syms), 2, 1, 4, 16)
    return header + data + sections


class PortImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.runtime = self.root / 'runtime' / 'runtime.elf'
        self.runtime.parent.mkdir()
        self.image = self.root / 'arbitrary-name.bin'
        self.base = 0x47000000
        self.symbols = dict(pm_idle_entry=self.base, mirror_idle_resume=self.base + 14,
                            mirror_idle_park=self.base + 18, pm_publish=self.base + 24,
                            pm_live=self.base + 28)
        self.raw = bytes.fromhex('4eb940098a2c700123c04700001c610000086000fffa4e714e750000000000000000')
        self.write_fixture()
        self.argv = ['ot_emu', '--image', str(self.image), '--interactive']

    def write_fixture(self):
        self.runtime.write_bytes(elf(self.base, self.raw, self.symbols))
        blob = b'OCTAGKA3' + len(self.raw).to_bytes(4, 'big') + pack.pack(self.raw, 4096)
        loader_base = 0x4010fdf0
        table = loader_base + 8
        blob_addr = table + 36
        entry = struct.pack('>8I', blob_addr, len(blob), port_image._roll(blob[4:]),
                            self.base + 0x08001000, self.base + 0x08000000,
                            len(self.raw), port_image._roll(self.raw), 0)
        loader = bytes.fromhex('4e714e714e714e71') + struct.pack('>I', 1) + entry + blob
        (self.root / 'loader.elf').write_bytes(elf(loader_base, loader, {'table': table, 'octabam_bootstrap': loader_base}))
        image = bytearray(loader_base - 0x40000400)
        image[0x10c:0x112] = bytes.fromhex('4eb9') + loader_base.to_bytes(4, 'big')
        image[0x1f896:0x1f89c] = bytes.fromhex('4ef9') + self.base.to_bytes(4, 'big')
        self.image.write_bytes(image + loader)

    def launch(self, args=None):
        return port_image.launch_args(self.argv if args is None else args, runtime_elf=self.runtime)

    def test_matching_image_gets_exact_branch_markers_without_mutating_argv(self):
        result = self.launch()
        self.assertEqual(result, self.argv + ['--main-park', '0x47000012:0x4700000e'])
        self.assertNotIn('--main-park', self.argv)

    def test_explicit_equal_markers_preserved_and_mismatch_rejected(self):
        args = self.argv + ['--main-park', '1191182354:1191182350']
        self.assertEqual(self.launch(args), args)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.launch(self.argv + ['--main-park', '0x47000012:0x47000000'])

    def test_stock_needs_no_metadata_and_keeps_explicit_user_option(self):
        image = bytearray(self.image.read_bytes())
        image[0x1f896:0x1f89c] = bytes.fromhex('4eb940098a2c')
        self.image.write_bytes(image)
        self.runtime.unlink()
        self.assertEqual(self.launch(), self.argv)
        args = self.argv + ['--main-park', '0x40001000:0x40001004']
        self.assertEqual(self.launch(args), args)

    def test_alternate_image_resident_idle_target_unchanged(self):
        image = bytearray(self.image.read_bytes())
        image[0x1f898:0x1f89c] = (0x40010000).to_bytes(4, 'big')
        self.image.write_bytes(image)
        self.runtime.unlink()
        self.assertEqual(self.launch(), self.argv)

    def test_missing_or_stale_metadata_refused(self):
        self.runtime.unlink()
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.launch()
        self.write_fixture()
        image = bytearray(self.image.read_bytes())
        image[-1] ^= 1
        self.image.write_bytes(image)
        with self.assertRaisesRegex(ValueError, 'loader.*image'):
            self.launch()

    def test_symbols_do_not_authorize_skipping_other_code(self):
        for offset in (0, 14, 18, 20):
            with self.subTest(offset=offset):
                original = self.raw
                raw = bytearray(original)
                raw[offset] ^= 1
                self.raw = bytes(raw)
                self.write_fixture()
                with self.assertRaisesRegex(ValueError, 'instruction|branch'):
                    self.launch()
                self.raw = original

    def test_wrong_idle_hook_rejected_even_with_matching_runtime(self):
        image = bytearray(self.image.read_bytes())
        image[0x1f898:0x1f89c] = (self.base + 2).to_bytes(4, 'big')
        self.image.write_bytes(image)
        with self.assertRaisesRegex(ValueError, 'idle hook'):
            self.launch()

    def test_runtime_bytes_must_match_loader_payload(self):
        self.runtime.write_bytes(elf(self.base, self.raw + b'\x01', self.symbols))
        with self.assertRaisesRegex(ValueError, 'runtime.*payload'):
            self.launch()

    def test_nonmirror_runtime_preserves_existing_idle_semantics(self):
        self.symbols = {'another_idle': self.base}
        self.write_fixture()
        self.assertEqual(self.launch(), self.argv)

    def test_malformed_duplicate_options_refused(self):
        for extra in (['--image', str(self.image)], ['--main-park'],
                      ['--main-park', '1:2'], ['--main-park', '2:2'],
                      ['--main-park=2:4'], ['--image=other.bin'],
                      ['--main-park', '2:4', '--main-park', '2:4']):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.launch(self.argv + extra)

    def test_invalid_elf_is_diagnostic(self):
        self.runtime.write_bytes(b'not an elf')
        with self.assertRaisesRegex(ValueError, 'ELF'):
            self.launch()

    def test_local_symbol_reuse_is_legal_but_conflicting_global_markers_are_not(self):
        symbols = [(k, v, 0x10) for k, v in self.symbols.items()]
        symbols += [('local_loop', self.base, 0), ('local_loop', self.base + 2, 0)]
        self.runtime.write_bytes(elf(self.base, self.raw, symbols))
        self.assertIn('--main-park', self.launch())
        symbols += [('mirror_idle_park', self.base + 8, 0x10)]
        self.runtime.write_bytes(elf(self.base, self.raw, symbols))
        with self.assertRaisesRegex(ValueError, 'ambiguous ELF symbol mirror_idle_park'):
            self.launch()

    def test_existing_usb_verifier_passes_validated_markers_to_process(self):
        # Exercise the real existing main() through its actual Popen boundary.
        # The process is the sole stub: no firmware/socket/guest time required.
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        import verify_usb
        import port_image as bare_helper
        selected = self.root / 'out/platform/runtime/runtime.elf'
        selected.parent.mkdir(parents=True)
        selected.write_bytes(self.runtime.read_bytes())
        (selected.parent.parent / 'loader.elf').write_bytes((self.root / 'loader.elf').read_bytes())
        class Spawned(Exception):
            pass
        with patch.object(verify_usb, 'ROOT', self.root), \
             patch.object(verify_usb, 'IMAGE', self.image), \
             patch.object(verify_usb, 'EMU', self.image), \
             patch.object(bare_helper, 'ROOT', self.root), \
             patch.object(verify_usb.registry, 'remix', return_value=types.SimpleNamespace(modules=[])), \
             patch.object(verify_usb.subprocess, 'Popen', side_effect=Spawned) as spawn:
            with self.assertRaises(Spawned):
                verify_usb.main()
        self.assertEqual(spawn.call_args.args[0][-2:], ['--main-park', '0x47000012:0x4700000e'])


if __name__ == '__main__':
    unittest.main()
