| MIDI-only mirror dispatch. Original Sami Zeinelabdin.
| The EP0 page fix follows markandrus/octemu's USB audio implementation.
        .text
        .global pm_adapter_ctrl, pm_adapter_reset, pm_adapter_session, pm_adapter_page
pm_adapter_ctrl:
        bsr pm_ctrl
        tst.l %d0
        bne.s 1f
        move.l 0xfc0b01c0,%d0
        jmp 0x4001de6a
1:      jmp 0x4001de74
pm_adapter_reset:
        jsr 0x4001d6b8
        bsr pm_transport_reset
        moveq #64,%d0
        jmp 0x4001e922
pm_adapter_session:
        bsr pm_transport_reset
        move.l 0xfc0b0140,%d0
        jmp 0x4001e958
pm_adapter_page:
        move.l (%a0),%d0
        move.l %d0,0x4ec95028
        andi.l #0xfffff000,%d0
        addi.l #0x1000,%d0
        move.l %d0,0x4ec9502c
        jmp 0x4001d4b8
