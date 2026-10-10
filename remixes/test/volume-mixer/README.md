# `volume-mixer`

Module-only port fixture for **VOLUME MIXER**, with fallback **NONE**.
It exposes the three live volume pages through MIDI/REC1/CUE + MIXER;
see [the module](../../../modules/volume-mixer/README.md) for signal stages,
paired gains, REC1 release behavior, hook ownership and measured costs.
It has no stock-effect chooser entries; stock-versus-remixed playing-audio
comparison belongs to the separate integration gate.

```bash
OT_PROJECT=<saved-project-copy> make check REMIX=volume-mixer
```

Proof is `PORT`: the registered gate tests all available arithmetic,
source, key, actual firmware LCD and idle timing cases on MKI/MKII modes
using the composed DRAM runtime. A saved project is required and is copied
to disposable cards. No hardware or playing-DSP proof is claimed.
