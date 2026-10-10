# `volume-mixer`

Module-only port fixture for **VOLUME MIXER**, with fallback **NONE**.
It exposes the three live volume pages through MIDI/REC1/CUE + MIXER;
see [the module](../../../modules/volume-mixer/README.md) for signal stages,
paired gains, REC1 release behavior, hook ownership and measured costs.
It has no stock-effect chooser entries. The audio gate derives a THRU
fixture with FX NONE in both slots and compares exact 24-bit MAIN/CUE
against stock handlers after an identical composed boot.

```bash
OT_PROJECT=<saved-project-copy> make check REMIX=volume-mixer
```

Proof is `PORT`: the registered gate tests all available arithmetic,
source, key, actual firmware LCD, idle/playing timing and DSP audio cases on MKI/MKII modes
using the composed DRAM runtime. A saved project is required and is copied
to disposable cards. Real input pulses verify independent clip holds; live and persisted
controls remain unchanged. No hardware proof is claimed.
