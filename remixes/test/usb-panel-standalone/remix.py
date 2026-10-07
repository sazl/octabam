"""Experimental USB panel mirror beside the two-channel MAIN output."""
from remix.schema import Proof,Remix
REMIX=Remix(name='usb-panel-standalone',family='probes',proof=Proof.CHECK,proof_note='Experimental; see local verification evidence.',
    doc='USB panel mirror, USB MIDI and MAIN output test carrier.',
    modules=('USB MIDI','USB PANEL MIRROR STANDALONE','USB PANEL MIRROR'),fallback='NONE')
