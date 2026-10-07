| USB PANEL MIRROR: accepted-byte observer, original code Sami Zeinelabdin.
        .text
        .global pm_accept
| pm_accept: d2.b accepted byte. Caller serializes with the stock SR mask.
| Commits only complete messages. Clobbers d0/d3/d4/a0/a1, never calls OS.
pm_accept:
        tst.l   pm_active
        beq.w   .Ldone
        moveq   #0,%d0
        move.b  pm_pending,%d0
        lea     pm_message,%a0
        move.b  %d2,(%a0,%d0.l)
        tst.l   %d0
        bne.s   .Lpayload
        moveq   #0,%d3
        move.b  %d2,%d3
        lea     pm_lengths,%a1
        move.b  (%a1,%d3.l),%d4
        beq.w   pm_invalidate
        move.b  %d4,pm_needed
.Lpayload:
        addq.l  #1,%d0
        move.b  %d0,pm_pending
        cmp.b   pm_needed,%d0
        bne.w   .Ldone
        clr.b   pm_pending
        addq.l  #1,pm_messages
        moveq   #0,%d3
        move.b  (%a0),%d3
        moveq   #0,%d4
        move.b  1(%a0),%d4
        cmpi.l  #0x18,%d3
        bcs.s   .Llcd
        cmpi.l  #0x30,%d3
        bcs.w   .Lrowlow
        cmpi.l  #0x40,%d3
        bcs.w   .Llevel
        cmpi.l  #0xa0,%d3
        bcs.w   .Ldone
        cmpi.l  #0xb0,%d3
        bcs.s   .Lrowhigh
        cmpi.l  #0xb7,%d3
        bne.w   .Ldone
        move.b  %d4,pm_backlight
        moveq   #1,%d0
        move.l  %d0,pm_backlight_known
        bra.w   .Lcommit
.Llcd:
        move.l  %d4,%d0
        andi.l  #0x87,%d0
        bne.w   pm_invalidate
        andi.l  #7,%d3
        lsl.l   #7,%d3
        add.l   %d4,%d3
        lea     pm_lcd,%a1
        adda.l  %d3,%a1
        move.l  2(%a0),(%a1)
        move.l  6(%a0),4(%a1)
        lsr.l   #3,%d3
        lea     pm_lcd_seen,%a1
        tst.b   (%a1,%d3.l)
        bne.s   .Lcommit
        moveq   #1,%d0
        move.b  %d0,(%a1,%d3.l)
        addq.l  #1,pm_lcd_count
        bra.s   .Lcommit
.Lrowhigh:
        subi.l  #0x90,%d3
        bra.s   .Lrow
.Lrowlow:
        subi.l  #0x20,%d3
.Lrow:
        lea     pm_row_values,%a1
        move.b  %d4,(%a1,%d3.l)
        lea     pm_row_seen,%a1
        tst.b   (%a1,%d3.l)
        bne.s   .Lcommit
        moveq   #1,%d0
        move.b  %d0,(%a1,%d3.l)
        addq.l  #1,pm_row_count
        bra.s   .Lcommit
.Llevel:
        andi.l  #15,%d3
        lea     pm_level_values,%a1
        move.b  %d3,(%a1,%d4.l)
        lea     pm_level_seen,%a1
        tst.b   (%a1,%d4.l)
        bne.s   .Lcommit
        moveq   #1,%d0
        move.b  %d0,(%a1,%d4.l)
        addq.l  #1,pm_level_count
.Lcommit:
        addq.l  #1,pm_generation
        beq.s   pm_invalidate       | fail closed on generation exhaustion
.Ldone:
        rts
        .global pm_invalidate
pm_invalidate:
        clr.l   pm_active
        clr.l   pm_lcd_count
        addq.l  #1,pm_faults
        rts

| Both sites replay the committed producer index before observation.
| SR is still 0x2700 here. Preserve every general register and post-store CCR.
        .macro TAP name,resume
        .global \name
\name:
        move.l  %d0,0x400b96c8
        lea     -64(%sp),%sp
        movem.l %d0-%d7/%a0-%a6,(%sp)
        move.w  %sr,%d0
        move.l  %d0,60(%sp)
        bsr.w   pm_accept
        move.l  60(%sp),%d0
        move.w  %d0,%sr
        movem.l (%sp),%d0-%d7/%a0-%a6
        lea     64(%sp),%sp
        jmp     \resume
        .endm
        TAP pm_byte_tap,0x40010af0
        TAP pm_ring_tap,0x40010b70

        .data
        .balign 4
        .global pm_lcd, pm_lcd_seen, pm_lcd_count, pm_generation
        .global pm_row_values, pm_row_seen, pm_level_values, pm_level_seen
        .global pm_backlight_known, pm_backlight, pm_active, pm_faults, pm_messages
        .global pm_row_count, pm_level_count
pm_row_count: .long 0
pm_level_count: .long 0
pm_generation: .long 0
pm_lcd_count: .long 0
pm_active: .long 1
pm_faults: .long 0
pm_messages: .long 0
pm_backlight_known: .long 0
pm_backlight: .byte 0
pm_pending: .byte 0
pm_needed: .byte 0
pm_message: .space 10,0
        .balign 4
pm_lcd: .space 1024,0
pm_lcd_seen: .space 128,0
pm_row_values: .space 32,0
pm_row_seen: .space 32,0
pm_level_values: .space 256,0
pm_level_seen: .space 256,0

pm_lengths:
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 10,10,10,10,10,10,10,10,0,0,0,0,0,0,0,0
        .byte 2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2
        .byte 2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2
        .byte 1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 2,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 2,0,0,0,2,0,0,0,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2
        .byte 0,0,0,0,0,6,0,2,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
        .byte 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0

        .text
        .global pm_after_loader
pm_after_loader:
        jsr     0x4000f938          | displaced SRAM setup before normal boot
        lea     -64(%sp),%sp
        movem.l %d0-%d7/%a0-%a6,(%sp)
        move.w  %sr,%d0
        move.l  %d0,60(%sp)
        move.w  #0x2700,%sr
        move.l  pm_boot_count,%d5
        lea     pm_boot_bytes,%a2
        tst.l   pm_boot_overflow
        bne.s   .Lbootfault
        tst.l   %d5
        beq.s   .Lbootready
.Lbootbyte:
        move.b  (%a2)+,%d2
        bsr.w   pm_accept
        subq.l  #1,%d5
        bne.s   .Lbootbyte
        bra.s   .Lbootready
.Lbootfault:
        bsr.w   pm_invalidate
.Lbootready:
        move.l  #pm_bypass,%d0
        move.l  %d0,pm_boot_invalidate
        move.l  #pm_accept,%d0
        move.l  %d0,pm_boot_dispatch
        move.l  60(%sp),%d0
        move.w  %d0,%sr
        movem.l (%sp),%d0-%d7/%a0-%a6
        lea     64(%sp),%sp
        jmp     0x40000518

        .global pm_bypass
pm_bypass:
        tst.l pm_live
        beq.s 1f
        bsr.w pm_invalidate
1:      rts
        .data
        .balign 4
        .global pm_live
pm_live: .long 0
