| Snapshot serializer in priority-zero task context. Original Sami Zeinelabdin.
| No copied/CRC bytes are processed with interrupts masked. Final metadata
| publication masks only its bounded compare-and-commit, never the body copy.
        .include "remix.inc"
        .text
        .global pm_publish, pm_copy_checkpoint, pm_flags
pm_publish:
        lea     -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        move.w  %sr,%d4
        move.w  #0x2700,%sr
        bsr.w   pm_expire
        move.w  %d4,%sr
        move.l  pm_lease_state,%d0
        cmpi.l  #1,%d0
        bne.w   .Lexit
        tst.l   pm_active
        beq.w   .Lexit
        move.l  pm_lcd_count,%d1
        cmpi.l  #128,%d1
        bne.w   .Lexit
        move.l  0xfc07c00c,%d0
        sub.l   pm_last_publish,%d0
        cmpi.l  #13200000,%d0
        bcs.w   .Lexit
        move.l  pm_generation,%d7
        move.l  pm_token,%d6
        move.l  pm_epoch,%d5
        lea     pm_body,%a2
        lea     pm_lcd,%a3
        moveq   #0,%d2
.Lpage:
        moveq   #0,%d3
.Lblock:
        move.l  %d2,%d0
        ori.l   #0x10,%d0
        move.b  %d0,(%a2)+
        move.b  %d3,(%a2)+
        move.l  (%a3)+,(%a2)
        move.l  (%a3)+,4(%a2)
        addq.l  #8,%a2
        addq.l  #8,%d3
        cmpi.l  #128,%d3
        bcs.s   .Lblock
        addq.l  #1,%d2
        cmpi.l  #8,%d2
        bcs.s   .Lpage
pm_copy_checkpoint:
        lea     pm_row_seen,%a3
        lea     pm_row_values,%a4
        moveq   #0,%d2
.Lrows:
        tst.b   (%a3)+
        beq.s   .Lnextrow
        move.l  %d2,%d0
        addi.l  #0x20,%d0
        cmpi.l  #16,%d2
        bcs.s   .Lrowop
        addi.l  #0x70,%d0
.Lrowop:
        move.b  %d0,(%a2)+
        move.b  (%a4,%d2.l),(%a2)+
.Lnextrow:
        addq.l  #1,%d2
        cmpi.l  #32,%d2
        bcs.s   .Lrows
        lea     pm_level_seen,%a3
        lea     pm_level_values,%a4
        moveq   #0,%d2
.Llevels:
        tst.b   (%a3)+
        beq.s   .Lnextlevel
        moveq   #0,%d0
        move.b  (%a4,%d2.l),%d0
        ori.l   #0x30,%d0
        move.b  %d0,(%a2)+
        move.b  %d2,(%a2)+
.Lnextlevel:
        addq.l  #1,%d2
        cmpi.l  #256,%d2
        bcs.s   .Llevels
        tst.l   pm_backlight_known
        beq.s   .Linputs
        move.l  #0xb7,%d0
        move.b  %d0,(%a2)+
        move.b  pm_backlight,(%a2)+
.Linputs:
        lea     pm_inputs,%a3
        moveq   #OTPM_INPUT_RECORD_SIZE/4-1,%d0
.Linput_copy:
        move.l  (%a3)+,(%a2)+
        subq.l  #1,%d0
        bpl.s   .Linput_copy
        .global pm_input_copy_checkpoint
pm_input_copy_checkpoint:
.Lcrc:
        lea     pm_body,%a0
        move.l  %a2,%d2
        sub.l   %a0,%d2             | serialized length
        move.l  %d2,%d3
        moveq   #-1,%d1
.Lcrcbyte:
        moveq   #0,%d0
        move.b  (%a0)+,%d0
        eor.l   %d0,%d1
        moveq   #7,%d0
.Lcrcbit:
        lsr.l   #1,%d1
        bcc.s   .Lcrcnoxor
        eori.l  #0xedb88320,%d1
.Lcrcnoxor:
        subq.l  #1,%d0
        bpl.s   .Lcrcbit
        subq.l  #1,%d3
        bne.s   .Lcrcbyte
        not.l   %d1
        move.l  %d1,%a5             | CRC survives flags helper
        bsr.w   pm_flags
        move.l  %d0,%a6
        move.w  %sr,%d4
        move.w  #0x2700,%sr
        cmp.l   pm_generation,%d7
        bne.s   .Labandon
        cmp.l   pm_epoch,%d5
        bne.s   .Labandon
        cmp.l   pm_token,%d6
        bne.s   .Labandon
        move.l  pm_lease_state,%d0
        cmpi.l  #1,%d0
        bne.s   .Labandon
        tst.l   pm_active
        beq.s   .Labandon
        move.l  %d7,pm_body_generation
        move.l  pm_output_generation,%d0
        move.l  %d0,pm_body_output_generation
        move.l  %d2,pm_body_length
        move.l  %a5,pm_body_crc
        move.l  %a6,pm_body_flags
        move.l  0xfc07c00c,%d0
        move.l  %d0,pm_last_publish
        moveq   #2,%d0
        move.l  %d0,pm_lease_state   | READY is the final publication store
.Labandon:
        move.w  %d4,%sr
.Lexit:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea     44(%sp),%sp
        rts

| Flags describe implemented families and current known state. d0 result.
pm_flags:
        moveq   #OTPM_CAPABILITY_MASK,%d0
        tst.l   pm_active
        beq.s   1f
        ori.l   #0x100000,%d0
1:      move.l  pm_lcd_count,%d1
        cmpi.l  #128,%d1
        bne.s   2f
        ori.l   #0x10000,%d0
2:      tst.l   pm_row_count
        beq.s   3f
        ori.l   #0x20000,%d0
3:      tst.l   pm_level_count
        beq.s   4f
        ori.l   #0x40000,%d0
4:      tst.l   pm_backlight_known
        beq.s   5f
        ori.l   #0x80000,%d0
5:      rts

| The idle marker is a genuine branch on a unit. The port is configured
| explicitly with --main-park park:resume; no useful work is called idle.
        .global pm_idle_entry, mirror_idle_resume, mirror_idle_park
pm_idle_entry:
        jsr     0x40098a2c          | displaced final init, exactly once
        bsr.w   pm_input_seed
        moveq #1,%d0
        move.l %d0,pm_live
mirror_idle_resume:
        bsr.w   pm_publish
mirror_idle_park:
        bra.w   mirror_idle_resume

        .global pm_expire
| Caller in USB IRQ or priority-zero short service critical section.
pm_expire:
        tst.l pm_lease_state
        beq.s 1f
        move.l 0xfc07c00c,%d0
        sub.l pm_lease_start,%d0
        cmpi.l #132000000,%d0
        bcs.s 1f
        move.l pm_cookie,%d0
        move.l %d0,pm_last_cookie
        clr.l pm_lease_state
        clr.l pm_token
1:      rts

        .data
        .balign 4
        .global pm_epoch, pm_lease_state, pm_token, pm_cookie, pm_last_cookie
        .global pm_last_release, pm_lease_start, pm_last_publish, pm_next_token
        .global pm_body, pm_body_length, pm_body_crc, pm_body_generation, pm_body_flags
pm_epoch: .long 1
pm_lease_state: .long 0
pm_token: .long 0
pm_cookie: .long 0
pm_last_cookie: .long 0
pm_last_release: .long 0
pm_lease_start: .long 0
pm_last_publish: .long -13200000
pm_next_token: .long 0
pm_body_length: .long 0
pm_body_crc: .long 0
.global pm_body_output_generation
pm_body_output_generation: .long 0
pm_body_generation: .long 0
pm_body_flags: .long 0
pm_body: .space OTPM_BODY_STORAGE_CAPACITY,0
