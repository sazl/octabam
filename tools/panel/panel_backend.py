"""Immutable read views shared by emulator and physical panel backends.

Backends publish completed snapshots under a short lock. Reading a snapshot
does not render, poll a device, run emulator commands, or write HTTP. Optional
state is None until the source provides it; capabilities describe the source,
not the browser skin.
"""
from dataclasses import dataclass
import struct
from typing import Protocol
import zlib

if __package__:
    from .panel_link import PanelLink
else:
    from panel_link import PanelLink


@dataclass(frozen=True)
class LedSnapshot:
    bits: bytes
    ids: tuple[tuple[int, int], ...]
    backlight: int | None = None

    def __post_init__(self):
        # Copy containers at publication; retain every negotiated row byte.
        object.__setattr__(self, "bits", bytes(self.bits))
        object.__setattr__(self, "ids", tuple(sorted((led, level) for led, level in self.ids)))


@dataclass(frozen=True)
class InputSnapshot:
    known: int
    fader: int | None
    keys: tuple[int, ...]
    press_counts: tuple[int, ...]
    encoder_counts: tuple[tuple[int, int], ...]

    def __post_init__(self):
        object.__setattr__(self, "keys", tuple(self.keys))
        object.__setattr__(self, "press_counts", tuple(self.press_counts))
        object.__setattr__(self, "encoder_counts", tuple(tuple(pair) for pair in self.encoder_counts))


@dataclass(frozen=True)
class ViewSnapshot:
    png: bytes
    text: str
    leds: LedSnapshot | None
    generation: int
    model: str | None
    capabilities: frozenset[str]
    snapshot_at: float
    display_changed_at: float
    inputs: InputSnapshot | None = None

    def __post_init__(self):
        if self.leds is not None and not isinstance(self.leds, LedSnapshot):
            raise TypeError("leds must be an immutable LedSnapshot or None")
        if self.inputs is not None and not isinstance(self.inputs, InputSnapshot):
            raise TypeError("inputs must be an immutable InputSnapshot or None")
        object.__setattr__(self, "png", bytes(self.png))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


class PanelBackend(Protocol):
    source: str
    capabilities: frozenset[str]

    def snapshot(self) -> ViewSnapshot | None:
        """Return the cached immutable view, or None before a verified frame."""
        ...

    def status(self) -> dict[str, object]:
        """Return a detached JSON-ready copy of source-specific status."""
        ...

    def supports(self, capability: str) -> bool:
        """Check capability membership without probing the source."""
        ...

    def close(self) -> None:
        """Idempotently close with bounded worker teardown."""
        ...


def render_link(link: PanelLink) -> tuple[bytes, str]:
    """Render decoded rows with the existing panel's MKII RGB colors and text.

    PanelLink owns LCD geometry. This function only serializes its rows and
    leaves the decoder, including its dirty flag, untouched.
    """
    lcd = link.lcd_rows()
    on, off = b"\xf2\xf2\xf2", b"\x06\x06\x07"
    rows = [b"".join(on if px else off for px in row) for row in lcd]

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 128, 64, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    text = "\n".join("".join("#" if px else "." for px in row) for row in lcd) + "\n"
    return png, text


def led_payload(leds: LedSnapshot) -> dict[str, object]:
    """Return existing LED JSON keys with all row bytes and numeric ID order."""
    payload = {"bits": leds.bits.hex(),
               "ids": {f"{led:#04x}": level for led, level in leds.ids}}
    if leds.backlight is not None:
        payload["backlight"] = leds.backlight
    return payload


def input_payload(inputs: InputSnapshot) -> dict[str, object]:
    """Detach the cached observation for JSON readers; unknown fader is null."""
    return {"known": inputs.known, "fader": inputs.fader,
            "keys": list(inputs.keys), "press_counts": list(inputs.press_counts),
            "encoder_counts": [list(pair) for pair in inputs.encoder_counts]}
