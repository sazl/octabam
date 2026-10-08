"""Automatically selected USB MIDI-only dispatcher for USB PANEL MIRROR."""
from remix.schema import Category, Detour, Kind, Linked, Module, Proof
OUTPUTS=tuple('USB AUDIO OUT '+s for s in ('MAIN','MAIN CUE','MASTER','TRACKS','TRACKS MAIN CUE'))
MODULE=Module(name='usb-panel-mirror-standalone',key='USB PANEL MIRROR STANDALONE',kind=Kind.CF_PATCH,
    category=Category.MIDI_USB,author='Sami Zeinelabdin',author_url='https://github.com/sazl',
    proof=Proof.CHECK,proof_note='Experimental adapter; no hardware proof.',doc='Automatic USB MIDI-only EP0 adapter for the panel mirror; omitted with audio output.',
    requires=('USB MIDI','USB PANEL MIRROR'),
    linked=(Linked('panel_adapter','modules/usb-panel-mirror-standalone/adapter.s',cpu='5475',dram=True),),
    detours=(
        Detour(0x4001de64,bytes.fromhex('2039fc0b01c0'),'panel_adapter','pm_adapter_ctrl','mirror requests; original fallback for every other control'),
        Detour(0x4001e91c,bytes.fromhex('4ebaed9a7040'),'panel_adapter','pm_adapter_reset','bus reset retires transport lease'),
        Detour(0x4001e952,bytes.fromhex('2039fc0b0140'),'panel_adapter','pm_adapter_session','session end retires transport lease'),
        Detour(0x4001d4b2,bytes.fromhex('23d04ec95028'),'panel_adapter','pm_adapter_page','EP0 descriptor page fix following USB audio'),
    ),
    conflicts=tuple((key,'audio output owns mirror dispatch for this selection') for key in OUTPUTS))
