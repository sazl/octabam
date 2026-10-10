# Mixer display prototype

This previews three mixer panels on the Octatrack emulator's 128 × 64
framebuffer before firmware implementation. The prototype supplies the
display and handles its shortcuts from the host; it does not install a
firmware module or prove that the shortcuts or meters work on hardware.

Run from the repository root:

```bash
.venv/bin/python3 tools/prototypes/mixer_display.py --capture --model mkii
.venv/bin/python3 tools/prototypes/mixer_display.py --interactive --model mkii
```

The panels share one view:

| Panel | Open with | Channels |
| --- | --- | --- |
| Internal tracks | Hold MIDI, then press MIXER | T1–T8, each with L/R signal bars |
| External inputs | Hold REC1, then press MIXER | A, B, C, D |
| Outputs | Hold CUE, then press MIXER | MAIN and CUE, each with L/R signal bars |

LEFT and RIGHT move through internal tracks → external inputs → outputs.
Navigation stops at either end of that order.

Use `--model mki` to boot the emulator as an MKI instead. Both previews use
the same 128 × 64 display layout and mixer shortcuts. Their shared keymap
entries are documented in `tools/panel/key_map.json`; model-specific keys
such as the MKII's dedicated PROJ key are outside this prototype.

Filled vertical bars show synthetic signal activity. The separate LEVEL
markers and numerical values show example volume settings from 0 to 127.
A horizontal reference marker indicates volume 100; it is not a clipping
threshold or a claim that 100 is unity gain. External-input clipping indicators light only
when a simulated signal reaches or exceeds digital full scale, independently
of its volume setting; the demonstration clips input B and inverts its label
as `B!`. Within each stereo meter, the left bar is L and the right bar is R.
Signal activity
and volume position are different: a channel can have a high volume setting
while its signal is silent. For internal tracks, LEVEL means the stock
post-FX track level, rather than the AMP page's pre-FX VOL control. External
and output values are display examples pending verification of their stock
control semantics and signal taps.

All activity and control values are demonstration data. This preview does
not establish calibrated meters, working gain controls, persisted settings,
or hardware behavior. The prototype contains no copied stock firmware bytes;
the emulator uses the user's local stock image.
