# USB PANEL MIRROR STANDALONE

The automatic MIDI-only control adapter for
[USB PANEL MIRROR](../usb-panel-mirror/README.md). Select USB MIDI and the
core mirror; the composer adds this adapter only without a USB audio output.
With audio, the audio module provides dispatch instead. Legacy explicit
adapter selections remain supported and are omitted when audio is selected.
It adds no audio descriptors or processing and is hidden from the module picker.

Original implementation: Sami Zeinelabdin. The EP0 descriptor-page fix
follows markandrus/octemu's existing USB audio code and credits that
implementation. Declared detours replay the original stock fallback,
reset and session-end behavior. The adapter calls the core handler only
for exact mirror requests, preserving mass storage and USB MIDI.

This is experimental port-tested logic, not a hardware compatibility
claim. The `usb-panel-standalone` test carrier exercises HS and FS through
the core's selected-image gate. See the core README for the shared DMA,
initialization, lifecycle and physical validation limits.
