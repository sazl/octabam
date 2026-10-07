# USB PANEL MIRROR STANDALONE

The mutually exclusive MIDI-only control adapter for
[USB PANEL MIRROR](../usb-panel-mirror/README.md). Select USB MIDI, the
core mirror and this adapter together; selecting any audio output module
is an explicit named conflict. It adds no audio descriptors or processing.

Original implementation: Sami Zeinelabdin. The EP0 descriptor-page fix
follows markandrus/octemu's existing USB audio code and credits that
implementation. Declared detours replay the original stock fallback,
reset and session-end behavior. The adapter calls the core handler only
for exact mirror requests, preserving mass storage and USB MIDI.

This is experimental port-tested logic, not a hardware compatibility
claim. The `usb-panel-standalone` test carrier exercises HS and FS through
the core's selected-image gate. See the core README for the shared DMA,
initialization, lifecycle and physical validation limits.
