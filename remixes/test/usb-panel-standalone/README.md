# usb-panel-standalone

Experimental MIDI-only USB PANEL MIRROR test carrier. Selects the core,
USB MIDI and its mutually exclusive standalone adapter. There are no
audio interfaces. See [the module](../../../modules/usb-panel-mirror/README.md)
for initialization, DMA and physical validation limits.

Run `make check REMIX=usb-panel-standalone`; use `TESTS=1` in acceptance
planning so this test carrier is included.
