# usb-panel-main

Experimental test carrier: USB MIDI, two-channel USB AUDIO OUT MAIN and
USB PANEL MIRROR. Uses no replacement DSP effects and changes no shipped
selection. See [the module](../../../modules/usb-panel-mirror/README.md)
for its protocol, checks, and unmeasured hardware limitations.

Run `make check REMIX=usb-panel-main`; include `TESTS=1` when planning
acceptance shards because this carrier lives under `remixes/test/`.
