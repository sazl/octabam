| Read-only vendor EP0 snapshots. Original Sami Zeinelabdin.
        .include "remix.inc"
        .text
        .global pm_ctrl, pm_transport_reset, pm_expire, pm_reply
| Returns d0=0 for unrelated requests; d0=1 when answered or stalled.
| Callee-saved registers and d2 (stock wLength) remain intact.
pm_ctrl:
        moveq #0,%d0
        move.b 0x46c8ce08,%d0
        cmpi.l #OTPM_BM_REQUEST_TYPE,%d0
        bne.w .Lunhandled
        move.b 0x46c8ce09,%d0
        subi.l #OTPM_REQUEST_INFO,%d0
        cmpi.l #3,%d0
        bhi.w .Lunhandled
        lea -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        move.l %d0,%d6
        addq.l #1,%d6
        moveq #0,%d7
        move.b 0x46c8ce0f,%d7
        lsl.l #8,%d7
        move.b 0x46c8ce0e,%d7
        cmpi.l #OTPM_HEADER_SIZE,%d7
        bcs.w .Lstall
| Single nonblocking EP0-IN flush/check. A failed quiescence check leaves
| reply bytes and leases untouched. No EP0 OUT/audio/MIDI register changes.
        move.l #0x10000,%d0
        move.l %d0,0xfc0b01b4
        move.l 0xfc0b01b4,%d0
        move.l 0xfc0b01b0,%d1
        or.l %d1,%d0
        move.l 0xfc0b01b8,%d1
        or.l %d1,%d0
        btst #16,%d0
        bne.w .Lstall
        lea pm_reply+0x08000000,%a2
        move.l %a2,%a0
        moveq #15,%d0
.Lclear:
        clr.l (%a0)+
        subq.l #1,%d0
        bpl.s .Lclear
        move.l #0x4f54504d,(%a2)
        move.b #OTPM_PROTOCOL_MAJOR,4(%a2)
        move.b #OTPM_PROTOCOL_MINOR,5(%a2)
        move.b %d6,7(%a2)
        move.l pm_epoch,%d0
        move.l %d0,8(%a2)
        bsr.w pm_flags
        move.l %d0,28(%a2)
        moveq #0,%d5
        move.b 0x46c8ce0b,%d5
        lsl.l #8,%d5
        move.b 0x46c8ce0a,%d5
        moveq #0,%d4
        move.b 0x46c8ce0d,%d4
        lsl.l #8,%d4
        move.b 0x46c8ce0c,%d4
        cmpi.l #OTPM_BOOTSTRAP_RESPONSE_SIZE,%d7
        bhi.w .Lbad
        cmpi.l #OTPM_KIND_INFO,%d6
        beq.w .Linfo
        cmpi.l #OTPM_KIND_BEGIN,%d6
        beq.w .Lbegin
        tst.l %d5
        beq.w .Lbad
        cmpi.l #OTPM_KIND_RELEASE,%d6
        beq.w .Lrelease
| READ validates a live token before disclosing any frozen descriptor.
        bsr.w pm_expire
        tst.l pm_active
        beq.w .Lnotready
        cmp.l pm_token,%d5
        bne.w .Lstale
        tst.l pm_lease_state
        beq.w .Lstale
        move.l pm_lease_state,%d0
        cmpi.l #1,%d0
        beq.w .Lpending
        cmp.l pm_body_length,%d4
        bhi.w .Lbad
        bsr.w .Lready_header
        move.w %d4,20(%a2)
        move.l pm_body_length,%d0
        sub.l %d4,%d0
        move.l %d7,%d1
        subi.l #OTPM_HEADER_SIZE,%d1
        cmp.l %d1,%d0
        bls.s .Lreadlength
        move.l %d1,%d0
.Lreadlength:
        tst.l %d0
        bne.s .Lreadcopy
        cmp.l pm_body_length,%d4
        bne.w .Lbad_clean
        bra.w .Lsend
.Lreadcopy:
        move.w %d0,22(%a2)
        lea pm_body,%a0
        adda.l %d4,%a0
        lea 32(%a2),%a1
.Lcopybyte:
        move.b (%a0)+,(%a1)+
        subq.l #1,%d0
        bne.s .Lcopybyte
        bra.w .Lsend
.Linfo:
        move.l %d4,%d0
        or.l %d5,%d0
        bne.w .Lbad
        cmpi.l #OTPM_BOOTSTRAP_RESPONSE_SIZE,%d7
        bne.w .Lbad
| Valid INFO describes the current shadow, not a frozen leased body.
        move.l pm_generation,%d0
        move.l %d0,12(%a2)
        move.w #OTPM_INFO_SIZE,22(%a2)
        move.b #OTPM_BODY_SCHEMA,32(%a2)
        move.b #OTPM_MODEL_UNKNOWN,33(%a2)
        move.w #OTPM_LCD_WIDTH,34(%a2)
        move.w #OTPM_LCD_HEIGHT,36(%a2)
        move.b #OTPM_LCD_PAGES,38(%a2)
        move.b #OTPM_LCD_BLOCK_COLUMNS,39(%a2)
        move.w #OTPM_BOOTSTRAP_RESPONSE_SIZE,40(%a2)
        move.w #OTPM_MAX_SCHEMA_BODY_SIZE,42(%a2)
        move.w #OTPM_DEFAULT_MIN_POLL_MS,44(%a2)
        move.l pm_lcd_count,%d0
        move.w %d0,46(%a2)
        move.w #OTPM_PROPOSED_LEASE_MS,48(%a2)
        move.w #OTPM_PROPOSED_MAX_PUBLICATIONS_HZ,50(%a2)
        bra.w .Lsend
.Lbegin:
        move.l %d4,%d3
        swap %d3
        or.l %d5,%d3
        beq.w .Lbad
        bsr.w pm_expire
        tst.l pm_active
        beq.w .Lnotready
        move.l pm_lcd_count,%d0
        cmpi.l #OTPM_LCD_BLOCK_COUNT,%d0
        bne.w .Lnotready
        tst.l pm_lease_state
        beq.s .Lnewlease
        cmp.l pm_cookie,%d3
        bne.w .Lbusy
        move.l pm_lease_state,%d0
        cmpi.l #1,%d0
        beq.w .Lpending
        bsr.w .Lready_header
        bra.w .Lsend
.Lnewlease:
        cmp.l pm_last_cookie,%d3
        beq.w .Lstale
        addq.l #1,pm_next_token
        move.l pm_next_token,%d0
        cmpi.l #0xffff,%d0
        bls.s .Ltoken
        bsr.w pm_transport_reset
        moveq #1,%d0
        move.l %d0,pm_next_token
.Ltoken:
        move.l %d0,pm_token
        move.l pm_epoch,%d0
        move.l %d0,8(%a2)
        move.l %d3,pm_cookie
        move.l 0xfc07c00c,%d0
        move.l %d0,pm_lease_start
        moveq #1,%d0
        move.l %d0,pm_lease_state
.Lpending:
        move.b #OTPM_STATUS_PENDING,6(%a2)
        move.l pm_token,%d0
        move.w %d0,16(%a2)
        bra.w .Lsend
.Lrelease:
        tst.l %d4
        bne.w .Lbad
        bsr.w pm_expire
        cmp.l pm_last_release,%d5
        beq.s .Lreleased
        cmp.l pm_token,%d5
        bne.w .Lstale
        tst.l pm_lease_state
        beq.w .Lstale
        clr.l pm_lease_state
        move.l pm_cookie,%d0
        move.l %d0,pm_last_cookie
        move.l %d5,pm_last_release
        clr.l pm_token
.Lreleased:
        move.w %d5,16(%a2)
        bra.s .Lsend
.Lready_header:
        move.l pm_body_generation,%d0
        move.l %d0,12(%a2)
        move.l pm_token,%d0
        move.w %d0,16(%a2)
        move.l pm_body_length,%d0
        move.w %d0,18(%a2)
        move.l pm_body_crc,%d0
        move.l %d0,24(%a2)
        move.l pm_body_flags,%d0
        move.l %d0,28(%a2)
        rts
.Lbad_clean:
        clr.l 12(%a2)
        clr.l 16(%a2)
        clr.l 20(%a2)
        clr.l 24(%a2)
.Lbad:
        move.b #OTPM_STATUS_BAD_REQUEST,6(%a2)
        bra.s .Lsend
.Lstale:
        move.b #OTPM_STATUS_STALE,6(%a2)
        bra.s .Lsend
.Lnotready:
        move.b #OTPM_STATUS_NOT_READY,6(%a2)
        bra.s .Lsend
.Lbusy:
        move.b #OTPM_STATUS_BUSY,6(%a2)
.Lsend:
        moveq #0,%d0
        move.w 22(%a2),%d0
        addi.l #OTPM_HEADER_SIZE,%d0
        move.l %a2,-(%sp)
        move.l %d0,-(%sp)
        jsr 0x4001d498
        addq.l #8,%sp
        bra.s .Lhandled
.Lstall:
        move.l 0xfc0b01c0,%d0
        bset #16,%d0
        move.l %d0,0xfc0b01c0
.Lhandled:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea 44(%sp),%sp
        moveq #1,%d0
        rts
.Lunhandled:
        moveq #0,%d0
        rts

| USB reset/session end retire transport only, never reset physical state.
| Preserve all registers and CCR so both adapter and audio shims can use it.
pm_transport_reset:
        lea -12(%sp),%sp
        movem.l %d0-%d1,(%sp)
        move.w %sr,%d0
        move.l %d0,8(%sp)
        clr.l pm_lease_state
        clr.l pm_token
        clr.l pm_last_release
        clr.l pm_last_cookie
        addq.l #1,pm_epoch
        bne.s 1f
        addq.l #1,pm_epoch
1:      move.l 8(%sp),%d0
        move.w %d0,%sr
        movem.l (%sp),%d0-%d1
        lea 12(%sp),%sp
        rts
        .data
        .balign 4096
pm_reply: .space OTPM_BOOTSTRAP_RESPONSE_SIZE,0
        .global pm_test_scratch
pm_test_scratch: .space 32,0
