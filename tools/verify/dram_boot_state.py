"""Compare a loaded runtime with its linked bytes at the boot handoff."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeComparison:
    ok: bool
    differences: tuple[int, ...]
    permitted: tuple[tuple[str, int, int, int], ...]
    unexpected: tuple[int, ...]


# panel_capture.s initializes these objects in .data. pm_after_loader drains
# accepted preloader bytes, then the normal taps update the shadow; pm_idle_entry
# sets pm_live and seeds the input baseline before the port reports HANDOFF.
# pm_input_state excludes the immutable input marker/version bytes. Sizes are their assembly storage
# extents, not a whole .data exemption: padding, pm_lengths, snapshot buffers,
# USB replies and every other module's data remain compared byte-for-byte.
MIRROR_BOOT_FIELDS = (
    ("pm_row_count", 4), ("pm_level_count", 4), ("pm_generation", 4),
    ("pm_lcd_count", 4), ("pm_active", 4), ("pm_faults", 4),
    ("pm_messages", 4), ("pm_backlight_known", 4),
    ("pm_backlight", 1), ("pm_pending", 1), ("pm_needed", 1), ("pm_message", 10),
    ("pm_lcd", 1024), ("pm_lcd_seen", 128), ("pm_row_values", 32),
    ("pm_row_seen", 32), ("pm_level_values", 256), ("pm_level_seen", 256),
    ("pm_live", 4), ("pm_output_generation", 4), ("pm_input_state", 166),
)


def compare_runtime(raw, got, *, base, module_keys, symbols):
    differences = tuple(i for i, (expected, actual) in enumerate(zip(raw, got)) if expected != actual)
    if "USB PANEL MIRROR" not in module_keys:
        # Preserve the existing nonmirror compatibility policy (e.g. midisc's
        # boot state). It is NOT an extra allowance outside the mirror ranges.
        return RuntimeComparison(len(got) == len(raw) and len(differences) <= 16,
                                 differences, (), differences)
    allowed = set()
    permitted = []
    for name, size in MIRROR_BOOT_FIELDS:
        if name not in symbols:
            raise ValueError(f"missing mirror boot-state symbol {name}")
        address, kind = symbols[name]
        if kind not in ("d", "D"):
            raise ValueError(f"mirror boot-state symbol {name} is {kind}, not initialized data")
        start = address - base
        if not 0 <= start < start + size <= len(raw):
            raise ValueError(f"mirror boot-state symbol {name} lies outside the linked runtime")
        extent = set(range(start, start + size))
        if allowed & extent:
            raise ValueError(f"mirror boot-state symbol {name} overlaps another permitted object")
        allowed.update(extent)
        permitted.append((name, address, size, sum(start <= i < start + size for i in differences)))
    unexpected = tuple(i for i in differences if i not in allowed)
    return RuntimeComparison(len(got) == len(raw) and not unexpected,
                             differences, tuple(permitted), unexpected)
