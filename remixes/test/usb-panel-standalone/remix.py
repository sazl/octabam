"""Experimental MIDI-only USB panel mirror with the standalone adapter."""
from remix.schema import Proof,Remix
REMIX=Remix(name='usb-panel-standalone',family='probes',proof=Proof.CHECK,proof_note='Experimental; see local verification evidence.',
    doc='USB panel mirror and USB MIDI standalone test carrier, without audio.',
    modules=('USB MIDI','USB PANEL MIRROR STANDALONE','USB PANEL MIRROR'),fallback='NONE')
