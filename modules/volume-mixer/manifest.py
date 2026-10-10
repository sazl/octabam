"""VOLUME MIXER -- read-only live track, input and output volume pages."""
from remix.schema import Category, Detour, Gate, Kind, Linked, Module, Proof

MODULE = Module(
    name="volume-mixer", key="VOLUME MIXER", kind=Kind.CF_PATCH,
    category=Category.MACHINES, author="repeat98", author_url="https://github.com/repeat98",
    proof=Proof.PORT,
    proof_note="verify_volume_mixer: integer peaks, key lifecycle, actual LCD, playing refresh, 24-bit audio invariance and input holds on MKI/MKII port; no hardware proof",
    doc="MIDI/REC1/CUE + MIXER: live track, input and output peaks beside existing volume settings.",
    linked=(Linked("volume-meter", "modules/volume-mixer/meter.s", dram=True),
            Linked("volume-panel", "modules/volume-mixer/panel.s", dram=True)),
    detours=(
        Detour(0x4000d98e, bytes.fromhex("203980004800"), "volume-meter", "vm_frame",
               "completed frame producer; replay counter load"),
        Detour(0x4007d468, bytes.fromhex("4ab9460e7424"), "volume-panel", "vm_mixer",
               "MIXER chords and stock fallback"),
        Detour(0x40055678, bytes.fromhex("2f032f02262f000c"), "volume-panel", "vm_midi",
               "consume chord MIDI release; replay eight-byte prologue", pad_to=8),
        Detour(0x40030ffc, bytes.fromhex("4eb940097924"), "volume-panel", "vm_rec_router",
               "defer REC1 recording press until release or chord cancellation", kind="jsr"),
        Detour(0x40056c92, bytes.fromhex("4ab946104ca8"), "volume-panel", "vm_tick",
               "UI refresh; replay stock UI test"),
    ),
    gates=(Gate("tools/verify/verify_volume_mixer.py", venv=True),),
)
