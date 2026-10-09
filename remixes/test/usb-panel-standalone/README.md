# usb-panel-standalone

Experimental MIDI-only USB PANEL MIRROR test carrier. Selects only the core and
USB MIDI; the composer adds the MIDI-only dispatch adapter automatically. There are no
audio interfaces. See [the module](../../../modules/usb-panel-mirror/README.md)
for initialization, DMA and physical validation limits.

Run `make check REMIX=usb-panel-standalone`; use `TESTS=1` in acceptance
planning so this test carrier is included.
