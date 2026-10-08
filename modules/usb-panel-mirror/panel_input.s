| Accepted physical input reports. Original code Sami Zeinelabdin.
| Parser install (40092674, only called at 4001f948) is after runtime load
| at 4000050c/handoff 40000512. No detour can run from unloaded DRAM.
| Helpers run with interrupts masked; no allocation, OS calls, or bulk copy.
        .include "remix.inc"
        .text
        .global pm_input_seed, pm_input_key, pm_input_encoder, pm_input_fader
        .macro INPUT_SAVE
        lea     -64(%sp),%sp
        movem.l %d0-%d7/%a0-%a6,(%sp)
        move.w  %sr,%d0
        move.l  %d0,60(%sp)
        move.w  #0x2700,%sr
        .endm
        .macro INPUT_RESTORE
        move.l  60(%sp),%d0
        move.w  %d0,%sr
        movem.l (%sp),%d0-%d7/%a0-%a6
        lea     64(%sp),%sp
        .endm

| Final stock initialization has returned. Seed current holds atomically,
| without manufacturing rising edges. Stock last position starts at -1; a
| valid 0..127 value proves a prior calibrated/oriented callback report.
pm_input_seed:
        INPUT_SAVE
        lea     0x46100b18,%a0
        lea     pm_inputs+OTPM_INPUT_OFFSET_KEYS,%a1
        move.l  (%a0)+,(%a1)+
        move.l  (%a0),(%a1)
        moveq   #OTPM_INPUT_KEYS_KNOWN+OTPM_INPUT_ENCODERS_KNOWN,%d0
        move.b  %d0,pm_inputs+OTPM_INPUT_OFFSET_KNOWN
        move.l  0x400d16cc,%d1
        cmpi.l  #128,%d1
        bcc.s   .Linput_seed_ready
        moveq   #127,%d2
        sub.l   %d1,%d2
        move.b  %d2,pm_inputs+OTPM_INPUT_OFFSET_FADER
        ori.l   #OTPM_INPUT_FADER_KNOWN,%d0
        move.b  %d0,pm_inputs+OTPM_INPUT_OFFSET_KNOWN
.Linput_seed_ready:
        bsr.w   .Linput_changed
        INPUT_RESTORE
        rts

| d1 row, d3 byte. At most eight rising-edge increments per accepted row.
pm_input_key:
        move.b  pm_inputs+OTPM_INPUT_OFFSET_KNOWN,%d0
        btst    #0,%d0
        beq.w   .Linput_done
        cmpi.l  #8,%d1
        bcc.w   .Linput_done
        lea     pm_inputs+OTPM_INPUT_OFFSET_KEYS,%a0
        moveq   #0,%d0
        move.b  (%a0,%d1.l),%d0
        cmp.b   %d3,%d0
        beq.w   .Linput_done
        move.b  %d3,(%a0,%d1.l)
        not.l   %d0
        and.l   %d3,%d0            | released-to-held bits only
        move.l  %d1,%d2
        lsl.l   #4,%d2
        lea     pm_inputs+OTPM_INPUT_OFFSET_PRESS_COUNTS,%a0
        adda.l  %d2,%a0
        moveq   #7,%d2
.Linput_edges:
        lsr.l   #1,%d0
        bcc.s   .Linput_nextbit
        moveq   #0,%d4
        move.w  (%a0),%d4
        addq.l  #1,%d4
        move.w  %d4,(%a0)          | modulo 65536
.Linput_nextbit:
        addq.l  #2,%a0
        subq.l  #1,%d2
        bpl.s   .Linput_edges
        bra.w   .Linput_changed

| d1 encoder, d3 signed byte. Extend before negating so -128 is +128.
pm_input_encoder:
        move.b  pm_inputs+OTPM_INPUT_OFFSET_KNOWN,%d0
        btst    #2,%d0
        beq.w   .Linput_done
        cmpi.l  #7,%d1
        bcc.w   .Linput_done
        move.l  %d3,%d0
        extb.l  %d0
        beq.s   .Linput_done
        move.l  %d1,%d2
        lsl.l   #2,%d2
        lea     pm_inputs+OTPM_INPUT_OFFSET_ENCODER_COUNTS,%a0
        adda.l  %d2,%a0
        tst.l   %d0
        bpl.s   .Linput_positive
        neg.l   %d0
        addq.l  #2,%a0
.Linput_positive:
        moveq   #0,%d2
        move.w  (%a0),%d2
        add.l   %d0,%d2
        move.w  %d2,(%a0)
        bra.s   .Linput_changed

| d1 stock calibrated/clamped 0..127, 127=A. Publish browser 0=A.
pm_input_fader:
        cmpi.l  #128,%d1
        bcc.s   .Linput_done
        move.b  pm_inputs+OTPM_INPUT_OFFSET_KNOWN,%d0
        btst    #0,%d0
        beq.s   .Linput_done
        moveq   #127,%d0
        sub.l   %d1,%d0
        move.b  pm_inputs+OTPM_INPUT_OFFSET_KNOWN,%d2
        btst    #1,%d2
        beq.s   .Linput_fader_new
        cmp.b   pm_inputs+OTPM_INPUT_OFFSET_FADER,%d0
        beq.s   .Linput_done
.Linput_fader_new:
        move.b  %d0,pm_inputs+OTPM_INPUT_OFFSET_FADER
        ori.l   #OTPM_INPUT_FADER_KNOWN,%d2
        move.b  %d2,pm_inputs+OTPM_INPUT_OFFSET_KNOWN
.Linput_changed:
        addq.l  #1,pm_generation
        bne.s   .Linput_done
        jmp     pm_invalidate      | same fail-closed generation rule as outputs
.Linput_done:
        rts

| Key detour replays the complete 6-byte stock row load first. Save its
| post-MOVE CCR and all registers so downstream stock sees exact state.
        .global pm_key_tap
pm_key_tap:
        move.l  0x400d16b4,%d1
        INPUT_SAVE
        bsr.w   pm_input_key
        INPUT_RESTORE
        jmp     0x400923c6

| Complete encoder report, before the UI descriptor's enable test or stock
| signed-delta coalescing. Physical turns remain visible for disabled knobs.
| Replay the same whole row load as the key path, preserving its post-MOVE SR.
        .global pm_encoder_tap
pm_encoder_tap:
        move.l  0x400d16b4,%d1
        INPUT_SAVE
        bsr.w   pm_input_encoder
        INPUT_RESTORE
        jmp     0x400924ea

| Both callbacks commit a final stock position: the normal one handles
| MKI UART2 and the calibrated UART1 parser; the other handles MKI's factory
| reversed orientation. Observe their common post-calculation store, including
| repeated reports. Preserve its post-MOVE SR and leave the callback argument
| and saved d2 on the original stack for the stock epilogue.
        .macro FADER_COMMIT name,resume
        .global \name
\name:
        move.l  %d2,0x400d16cc
        INPUT_SAVE
        move.l  %d2,%d1
        bsr.w   pm_input_fader
        INPUT_RESTORE
        jmp     \resume
        .endm
        FADER_COMMIT pm_fader_tap,0x40092fc8
        FADER_COMMIT pm_fader_inverted_tap,0x40092fa8

        .data
        .balign 4
        .global pm_inputs, pm_input_state
pm_inputs:
        .byte OTPM_INPUT_MARKER,OTPM_INPUT_VERSION
pm_input_state:
        .byte 0,0
        .space OTPM_INPUT_RECORD_SIZE-4,0
