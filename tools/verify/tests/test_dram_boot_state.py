"""Firmware-free rejection tests for boot-time runtime mutation accounting."""
import unittest

from tools.verify.dram_boot_state import compare_runtime


class BootRuntimeState(unittest.TestCase):
    def setUp(self):
        self.base = 0x100000
        self.raw = bytes(4096)
        self.symbols = {'pm_accept': (self.base + 8, 'T')}
        self.fields = {
            'pm_row_count': 4, 'pm_level_count': 4, 'pm_generation': 4,
            'pm_lcd_count': 4, 'pm_active': 4, 'pm_faults': 4,
            'pm_messages': 4, 'pm_backlight_known': 4,
            'pm_backlight': 1, 'pm_pending': 1, 'pm_needed': 1, 'pm_message': 10,
            'pm_lcd': 1024, 'pm_lcd_seen': 128, 'pm_row_values': 32,
            'pm_row_seen': 32, 'pm_level_values': 256, 'pm_level_seen': 256,
            'pm_live': 4,
        }
        offset = 128
        for name, size in self.fields.items():
            self.symbols[name] = (self.base + offset, 'd' if name in ('pm_pending', 'pm_needed', 'pm_message') else 'D')
            offset += size + 1  # padding must remain immutable, too
        self.symbols['pm_lengths'] = (self.base + offset, 'd')
        self.symbols['pm_body'] = (self.base + offset + 256, 'D')

    def compare(self, got, *, modules=('USB MIDI', 'USB PANEL MIRROR'), symbols=None):
        return compare_runtime(self.raw, got, base=self.base, module_keys=modules,
                               symbols=self.symbols if symbols is None else symbols)

    def test_initialized_capture_state_may_change_beyond_old_sixteen_byte_limit(self):
        got = bytearray(self.raw)
        for name, size in self.fields.items():
            offset = self.symbols[name][0] - self.base
            got[offset:offset + size] = b'\x81' * size
        result = self.compare(got)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.differences), 1777)
        self.assertEqual(sum(count for _name, _start, _size, count in result.permitted), 1777)
        self.assertEqual(result.unexpected, ())

    def test_even_one_code_constant_snapshot_or_adjacent_byte_change_is_rejected(self):
        offsets = [8, self.symbols['pm_lengths'][0] - self.base,
                   self.symbols['pm_body'][0] - self.base]
        offsets += [self.symbols[name][0] - self.base + size for name, size in self.fields.items()]
        for offset in offsets:
            with self.subTest(offset=offset):
                got = bytearray(self.raw); got[offset] = 1
                result = self.compare(got)
                self.assertFalse(result.ok)
                self.assertEqual(result.unexpected, (offset,))

    def test_permitted_changes_do_not_hide_one_unexpected_code_byte(self):
        got = bytearray(self.raw)
        at = self.symbols['pm_lcd'][0] - self.base
        got[at:at + 1024] = b'\xff' * 1024
        got[8] = 1
        result = self.compare(got)
        self.assertFalse(result.ok)
        self.assertEqual(result.unexpected, (8,))

    def test_missing_wrong_kind_out_of_bounds_and_overlapping_symbols_fail_closed(self):
        mutations = [None, (self.base + 128, 'T'), (self.base + 128, 'A'),
                     (self.base - 1, 'D'), (self.base + len(self.raw) - 1023, 'D'),
                     self.symbols['pm_row_count']]
        for changed in mutations:
            with self.subTest(metadata=changed):
                symbols = dict(self.symbols)
                if changed is None: del symbols['pm_lcd']
                else: symbols['pm_lcd'] = changed
                with self.assertRaises(ValueError): self.compare(self.raw, symbols=symbols)

    def test_exact_length_is_required_even_if_no_surviving_bytes_differ(self):
        for got in (self.raw[:-1], self.raw + b'\0', b''):
            with self.subTest(length=len(got)):
                self.assertFalse(self.compare(got).ok)

    def test_nonmirror_legacy_threshold_is_not_expanded_by_mirror_symbols(self):
        for count in (0, 16, 17, 1024):
            with self.subTest(count=count):
                got = bytes([1]) * count + self.raw[count:]
                result = self.compare(got, modules=('USB MIDI',))
                self.assertEqual(result.ok, count <= 16)
                self.assertEqual(result.permitted, ())
