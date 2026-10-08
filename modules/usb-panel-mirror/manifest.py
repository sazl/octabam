"""USB PANEL MIRROR -- bounded accepted-panel observer and EP0 snapshots."""
from pathlib import Path
from remix.schema import Category, Detour, Gate, Kind, Linked, Module, Proof, SymbolRef, Poke

OUTPUTS = tuple('USB AUDIO OUT '+s for s in ('MAIN','MAIN CUE','MASTER','TRACKS','TRACKS MAIN CUE'))
ADAPTER = 'USB PANEL MIRROR STANDALONE'

def mirror_inc(modules):
    if 'USB MIDI' not in modules:
        raise ValueError('USB PANEL MIRROR requires USB MIDI')
    owners = [key for key in (*OUTPUTS, ADAPTER) if key in modules]
    if len(owners) != 1:
        raise ValueError('USB PANEL MIRROR requires exactly one dispatch owner: audio output or standalone adapter')
    return (Path(__file__).parent / 'protocol.inc').read_text()

MODULE = Module(
    name='usb-panel-mirror', key='USB PANEL MIRROR', kind=Kind.CF_PATCH,
    category=Category.MIDI_USB, author='Sami Zeinelabdin', author_url='https://github.com/sazl',
    proof=Proof.CHECK, proof_note='Experimental: MKI/MKII accepted-UART capture and EP0 snapshots verified under the port; pre-ISR abort timing and physical coexistence remain unmeasured.',
    doc='Experimental read-only LCD/LED snapshots from accepted panel bytes over vendor EP0.',
    requires=('USB MIDI',),
    conflicts=(('CF METER IDLE','both own the priority-zero main service loop'),),
    gates=(Gate('tools/verify/verify_usb_panel.py', remix_arg=True, venv=True, stage='image'),),
    linked=(Linked('panel_boot','modules/usb-panel-mirror/panel_boot.s',cpu='5475'), Linked('panel_cache','modules/usb-panel-mirror/panel_cache.s',cpu='5475',cave_addr=0x400d7a00), Linked('panel_capture','modules/usb-panel-mirror/panel_capture.s',cpu='5475',dram=True,include=mirror_inc,defsyms=tuple((s,0) for s in ('pm_boot_dispatch','pm_boot_count','pm_boot_bytes','pm_boot_overflow','pm_boot_invalidate'))), Linked('panel_input','modules/usb-panel-mirror/panel_input.s',cpu='5475',dram=True,include=mirror_inc), Linked('panel_snapshot','modules/usb-panel-mirror/panel_snapshot.s',cpu='5475',dram=True,include=mirror_inc), Linked('usb_panel','modules/usb-panel-mirror/usb_panel.s',cpu='5475',dram=True,include=mirror_inc)),
    # Stock initializes the EP0 IN dQH while its controller is stopped.
    # Exact-wLength 64-byte panel replies must not leave an extra IN ZLP.
    pokes=(Poke(0x4001d658,bytes.fromhex('00400000'),bytes.fromhex('20400000'),
                'EP0 IN: retire full-size data without automatic ZLP'),),
    symbol_refs=(
        SymbolRef(0x400d7b00,0,'panel_capture','pm_accept','first panel runtime instruction'),
        SymbolRef(0x400d7b04,0,'panel_capture','_edata','end of loaded runtime'),
        SymbolRef(0x400d7b08,0,'panel_capture','pm_after_loader','post-cache panel handoff'),
    ),
    detours=tuple(Detour(addr, bytes.fromhex(op+'fc06400c'), 'panel_boot', symbol,
                        'invalidate mirror on direct runtime panel programming/text')
                  for addr,op,symbol in (
                      (0x4001f426,'13c6','pm_bypass_program_word'),
                      (0x4001f4fe,'13c1','pm_bypass_program'),
                      (0x40033d86,'13c1','pm_bypass_byte'),
                      (0x40033da6,'13c0','pm_bypass_position'),
                      (0x40033dd0,'13c0','pm_bypass_control'),
                      (0x40033dfe,'13c0','pm_bypass_text'),
                      (0x4003afac,'13c0','pm_bypass_exception'),
                  )) + (
        Detour(0x400923c0,bytes.fromhex('2239400d16b4'),'panel_input','pm_key_tap','complete accepted key row before stock edge dispatch'),
        Detour(0x400924e4,bytes.fromhex('2239400d16b4'),'panel_input','pm_encoder_tap','complete accepted encoder before UI descriptor filtering/coalescing'),
        Detour(0x40092fc2,bytes.fromhex('23c2400d16cc'),'panel_input','pm_fader_tap','normal fader callback commits final stock position: MKI UART2 and UART1 parser'),
        Detour(0x40092fa2,bytes.fromhex('23c2400d16cc'),'panel_input','pm_fader_inverted_tap','factory-inverted MKI UART2 fader callback commits final stock position'),
        Detour(0x4001fc96,bytes.fromhex('4eb940098a2c'),'panel_snapshot','pm_idle_entry','priority-zero bounded snapshot service after final init'),
        Detour(0x40000512,bytes.fromhex('4eb94000f938'),'panel_cache','pm_cache_handoff','synchronize instructions before handing accepted preloader bytes to DRAM'),
        Detour(0x40010aea,bytes.fromhex('23c0400b96c8'),'panel_boot','pm_boot_byte_tap','accepted single-byte panel ring commit'),
        Detour(0x40010b6a,bytes.fromhex('23c0400b96c8'),'panel_boot','pm_boot_ring_tap','accepted bulk panel ring commit'),
    ),
)
