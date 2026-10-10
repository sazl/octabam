"""Module-only fixture for the VOLUME MIXER's ColdFire port gates."""
from remix.schema import Proof, Remix

REMIX = Remix(
    name="volume-mixer", family="probes", proof=Proof.PORT,
    proof_note="verify_volume_mixer on the MKI/MKII port; no hardware or playing-DSP proof",
    doc="module-only VOLUME MIXER fixture for peaks, key lifecycle, LCD and refresh gates.",
    modules=("VOLUME MIXER",), fallback="NONE",
)
