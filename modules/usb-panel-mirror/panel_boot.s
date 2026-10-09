| Accepted bytes may precede the platform loader. This small image-resident
| FIFO is initialized with the OS image; it never jumps to unloaded DRAM.
| Full state is handed to the DRAM observer at the verified post-loader call.
        .text
        .global pm_boot_dispatch, pm_boot_count, pm_boot_overflow, pm_boot_bytes
        .macro BOOT_TAP name,resume
        .global \name
\name:
        move.l %d0,0x400b96c8
        lea -64(%sp),%sp
        movem.l %d0-%d7/%a0-%a6,(%sp)
        move.w %sr,%d0
        move.l %d0,60(%sp)
        bsr.w pm_boot_accept
        move.l 60(%sp),%d0
        move.w %d0,%sr
        movem.l (%sp),%d0-%d7/%a0-%a6
        lea 64(%sp),%sp
        jmp \resume
        .endm
        BOOT_TAP pm_boot_byte_tap,0x40010af0
        BOOT_TAP pm_boot_ring_tap,0x40010b70
pm_boot_accept:
        move.l pm_boot_dispatch,%a0
        tst.l %a0
        beq.s .Learly
        jmp (%a0)
.Learly:
        move.l pm_boot_count,%d0
        cmpi.l #3072,%d0
        bcc.s .Loverflow
        lea pm_boot_bytes,%a0
        move.b %d2,(%a0,%d0.l)
        addq.l #1,pm_boot_count
        rts
.Loverflow:
        moveq #1,%d0
        move.l %d0,pm_boot_overflow
        rts
        .balign 4
pm_boot_dispatch: .long 0
pm_boot_count: .long 0
pm_boot_overflow: .long 0
pm_boot_bytes: .space 3072,0

| Direct text/programming helpers bypass the accepted ring. Before publishing
| begins they belong to boot, whose final screen is independently checked.
| In the supported runtime, any entry makes the observer unavailable.
        .global pm_boot_invalidate
pm_boot_invalidate: .long 0
        .macro BYPASS name,reg,resume
        .global \name
\name:
        lea -64(%sp),%sp
        movem.l %d0-%d7/%a0-%a6,(%sp)
        move.w %sr,%d0
        move.l %d0,60(%sp)
        move.w #0x2700,%sr
        move.l pm_boot_invalidate,%a0
        tst.l %a0
        beq.s 1f
        jsr (%a0)
1:      move.l 60(%sp),%d0
        move.w %d0,%sr
        movem.l (%sp),%d0-%d7/%a0-%a6
        lea 64(%sp),%sp
        move.b \reg,0xfc06400c
        jmp \resume
        .endm
        BYPASS pm_bypass_program_word,%d6,0x4001f42c
        BYPASS pm_bypass_program,%d1,0x4001f504
        BYPASS pm_bypass_byte,%d1,0x40033d8c
        BYPASS pm_bypass_position,%d0,0x40033dac
        BYPASS pm_bypass_control,%d0,0x40033dd6
        BYPASS pm_bypass_text,%d0,0x40033e04
        BYPASS pm_bypass_exception,%d0,0x4003afb2
