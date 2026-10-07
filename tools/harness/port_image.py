"""Image-bound launch options for the ColdFire port (no guest code changes).

The USB panel publisher replaces main's stock spin. Configure the port's
generic --main-park instrument only after checking the selected image's hook,
its complete linked loader, the loader's actual runtime table/payload, and the
runtime's entry/call/branch instructions. Symbol names alone confer no idle
semantics. Stock and other idle owners keep the port's existing behavior.

Call launch_args on the FINAL argv, including user options. The default ELF is
this checkout's latest runtime, never a guess based on an image filename. An
external image can use its matching runtime.elf and sibling ../loader.elf;
pass runtime_elf explicitly for strict association. Default discovery leaves
unknown images unchanged when metadata is unavailable or unrelated. This
proves image association and the small scheduling adapter,
not publisher correctness, loader execution, or hardware timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import struct
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from remix import pack

ROOT = Path(__file__).resolve().parents[2]
IMAGE_BASE = 0x40000400
IDLE_HOOK = 0x4001fc96
UNCACHED = 0x08000000


def _roll(data):
    h = 0
    for byte in data:
        h = (h * 33 + byte) & 0xffffffff
    return h


def _slice(data, offset, size):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError('truncated image/ELF metadata')
    return data[offset:offset + size]


@dataclass
class _Elf:
    base: int
    raw: bytes
    symbols: dict[str, int]
    executable: tuple[tuple[int, int], ...]

    def code(self, addr, size):
        if not any(lo <= addr and addr + size <= hi for lo, hi in self.executable):
            raise ValueError('park adapter instruction is outside executable ELF sections')
        return _slice(self.raw, addr - self.base, size)


def _elf(path):
    """Read the linked GNU ELF32/MSB/68K subset, without a host toolchain.

    Objcopy's binary consists of allocated PROGBITS at their load addresses,
    with zero-filled gaps. Refuse differing physical/virtual load addresses:
    the platform linker does not use them and guessing would misbind markers.
    """
    data = path.read_bytes()
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x02\x01':
        raise ValueError(f'{path}: expected ELF32 big-endian metadata')
    kind, machine, version, _, phoff, shoff, _, _, phsize, phnum, shsize, shnum, _ = struct.unpack_from('>HHIIIIIHHHHHH', data, 16)
    if (kind, machine, version, shsize) != (2, 4, 1, 40) or not shnum:
        raise ValueError(f'{path}: unsupported linked ColdFire ELF metadata')
    if phnum and phsize != 32:
        raise ValueError('unsupported ELF program header')
    for i in range(phnum):
        typ, _, virt, phys, _, _, _, _ = struct.unpack('>8I', _slice(data, phoff + i * phsize, phsize))
        if typ == 1 and virt != phys:
            raise ValueError('ELF load addresses differ from runtime addresses')
    sections = [struct.unpack('>10I', _slice(data, shoff + i * shsize, shsize)) for i in range(shnum)]
    loaded = [s for s in sections if s[1] == 1 and s[2] & 2 and s[5]]
    if not loaded:
        raise ValueError('ELF has no allocated contents')
    base = min(s[3] for s in loaded)
    end = max(s[3] + s[5] for s in loaded)
    if end - base > 16 * 1024 * 1024 or end > 0x100000000:
        raise ValueError('ELF runtime extent is outside platform limits')
    raw = bytearray(end - base)
    spans = []
    executable = []
    for s in loaded:
        addr, size = s[3], s[5]
        if any(lo < addr + size and addr < hi for lo, hi in spans):
            raise ValueError('overlapping ELF contents')
        spans.append((addr, addr + size))
        raw[addr - base:addr - base + size] = _slice(data, s[4], size)
        if s[2] & 4:
            executable.append((addr, addr + size))
    symbols = {}
    for s in sections:
        if s[1] != 2:
            continue
        if s[9] != 16 or s[5] % 16 or s[6] >= len(sections):
            raise ValueError('invalid ELF symbol table')
        st = sections[s[6]]
        strings = _slice(data, st[4], st[5])
        for off in range(s[4], s[4] + s[5], 16):
            name, value, _, info, _, section = struct.unpack('>IIIBBH', _slice(data, off, 16))
            if not name or not section:
                continue
            if name >= len(strings) or b'\0' not in strings[name:]:
                raise ValueError('invalid ELF symbol name')
            label = strings[name:strings.index(b'\0', name)].decode('ascii')
            # Repeated translation-unit-local names are legal. Only the
            # loader's local table is part of this adapter's metadata API.
            if info >> 4 == 0 and label != 'table':
                continue
            if label in symbols and symbols[label] != value:
                raise ValueError(f'ambiguous ELF symbol {label}')
            symbols[label] = value
    return _Elf(base, bytes(raw), symbols, tuple(executable))


def _associated_runtime(image, runtime_elf, *, required=True):
    try:
        loader = _elf(runtime_elf.parent.parent / 'loader.elf')
    except OSError as exc:
        if not required:
            return None
        raise ValueError(f'DRAM idle hook needs matching runtime/loader ELF metadata: {exc}. '
                         'Rebuild this image or pass its runtime_elf to launch_args.') from exc
    offset = loader.base - IMAGE_BASE
    if offset < 0 or offset + len(loader.raw) > len(image) or image[offset:offset + len(loader.raw)] != loader.raw:
        if not required:
            return None
        raise ValueError('linked loader metadata does not match selected image; rebuild/select matching ELFs')
    try:
        runtime = _elf(runtime_elf)
    except OSError as exc:
        if not required:
            return None
        raise ValueError(f'DRAM idle hook needs matching runtime ELF metadata: {exc}') from exc
    # The metadata now describes the selected loader, not a different build.
    # Its binding and adapter checks are strict: never swallow these errors
    # and launch a known candidate with guessed or missing park semantics.
    syms = loader.symbols
    if not {'table', 'octabam_bootstrap'} <= syms.keys():
        raise ValueError('loader ELF metadata lacks platform table/entry')
    boot = b'\x4e\xb9' + syms['octabam_bootstrap'].to_bytes(4, 'big')
    if _slice(image, 0x4000050c - IMAGE_BASE, 6) != boot:
        raise ValueError('image boot hook does not select this linked loader')
    table = syms['table'] - loader.base
    count = int.from_bytes(_slice(loader.raw, table, 4), 'big')
    if not 1 <= count <= 256:
        raise ValueError('invalid loader payload table count')
    blob = b'OCTAGKA3' + len(runtime.raw).to_bytes(4, 'big') + pack.pack(runtime.raw, 4096)
    matches = 0
    for i in range(count):
        src, size, phash, stage, dst, rawlen, rhash, backup = struct.unpack('>8I', _slice(loader.raw, table + 4 + i * 32, 32))
        if dst != runtime.base + UNCACHED:
            continue
        actual = _slice(loader.raw, src - loader.base, size)
        if (actual != blob or rawlen != len(runtime.raw) or rhash != _roll(runtime.raw)
                or phash != _roll(blob[4:]) or backup != 0
                or stage != ((runtime.base + len(runtime.raw) + 0xfff) & ~0xfff) + UNCACHED):
            raise ValueError('linked runtime does not match loader runtime payload/mapping')
        matches += 1
    if matches != 1:
        raise ValueError('linked runtime has no unique loader runtime payload')
    return runtime


def _one_option(argv, option):
    if any(str(arg).startswith(option + '=') for arg in argv):
        raise ValueError(f'{option} requires a separate value (port CLI syntax)')
    indices = [i for i, arg in enumerate(argv) if arg == option]
    if len(indices) > 1 or (indices and indices[0] + 1 == len(argv)):
        raise ValueError(f'{option} must have exactly one value')
    return argv[indices[0] + 1] if indices else None


def launch_args(argv, *, runtime_elf=None):
    """Return validated port argv; leave explicit matching markers untouched.

    A known marker conflict fails before spawning. Feature-absent/stock
    images receive no new arguments. Default metadata discovery is best
    effort for unknown images; an explicit runtime_elf requires association.
    No inferred park is supplied without a fully validated mirror adapter.
    """
    args = list(argv)
    image_path = _one_option(args, '--image')
    if image_path is None:
        raise ValueError('port launch requires an explicit --image')
    explicit = _one_option(args, '--main-park')
    pair = None
    if explicit is not None:
        try:
            fields = explicit.split(':')
            if len(fields) != 2:
                raise ValueError()
            if not all(re.fullmatch(r'(?:[0-9]+|0[xX][0-9a-fA-F]+)', s) for s in fields):
                raise ValueError()
            pair = tuple(int(s, 16 if s.lower().startswith('0x') else 10) for s in fields)
            if any(v <= 0 or v > 0xffffffff or v & 1 for v in pair) or pair[0] == pair[1]:
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise ValueError('invalid --main-park PARK:RESUME') from exc
    image = Path(image_path).read_bytes()
    hook = _slice(image, IDLE_HOOK - IMAGE_BASE, 6)
    if hook[:2] != b'\x4e\xf9':
        return args
    target = int.from_bytes(hook[2:], 'big')
    if IMAGE_BASE <= target < IMAGE_BASE + len(image):
        return args
    runtime = _associated_runtime(image, Path(runtime_elf) if runtime_elf is not None else ROOT / 'out/platform/runtime/runtime.elf',
                                  required=runtime_elf is not None)
    if runtime is None:
        return args
    names = {'pm_idle_entry', 'mirror_idle_park', 'mirror_idle_resume', 'pm_publish', 'pm_live'}
    present = names & runtime.symbols.keys()
    if not present:
        return args
    if present != names:
        raise ValueError('incomplete panel idle marker metadata')
    s = runtime.symbols
    park, resume, entry = s['mirror_idle_park'], s['mirror_idle_resume'], s['pm_idle_entry']
    if target != entry:
        raise ValueError('image idle hook does not select linked panel idle entry')
    expected = b'\x4e\xb9\x40\x09\x8a\x2c\x70\x01\x23\xc0' + s['pm_live'].to_bytes(4, 'big')
    if resume != entry + len(expected) or runtime.code(entry, len(expected)) != expected:
        raise ValueError('panel idle entry instruction mismatch')
    if park != resume + 4 or runtime.code(resume, 2) != b'\x61\x00':
        raise ValueError('panel resume instruction is not the publisher BSR.w')
    if resume + 2 + int.from_bytes(runtime.code(resume + 2, 2), 'big', signed=True) != s['pm_publish']:
        raise ValueError('panel resume branch does not call linked publisher')
    if runtime.code(park, 2) != b'\x60\x00' or park + 2 + int.from_bytes(runtime.code(park + 2, 2), 'big', signed=True) != resume:
        raise ValueError('panel park branch does not resume real publisher work')
    if pair is not None:
        if pair != (park, resume):
            raise ValueError('explicit --main-park conflicts with selected image idle markers')
        return args
    return args + ['--main-park', f'{park:#x}:{resume:#x}']


def main():
    """Shell callers: python3 tools/harness/port_image.py PORT --image IMAGE ..."""
    import os
    import sys
    args = launch_args(sys.argv[1:])
    os.execv(args[0], args)


if __name__ == '__main__':
    main()
