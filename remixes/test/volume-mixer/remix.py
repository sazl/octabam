"""Module-only fixture for the VOLUME MIXER's ColdFire port gates."""
from remix.schema import Proof, Remix

REMIX = Remix(
    name="volume-mixer", family="probes", proof=Proof.PORT,
    proof_note="verify_volume_mixer on the MKI/MKII port; 24-bit audio and input holds, no hardware proof",
    doc="module-only VOLUME MIXER fixture for peaks, key lifecycle, LCD and refresh gates.",
    modules=("VOLUME MIXER",), fallback="NONE",
)
