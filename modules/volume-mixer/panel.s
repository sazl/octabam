/* VOLUME MIXER: measured stock key/window contracts, OS 1.40C.
 * Drawing and snapshot normalization run in the UI task, never the ISR.
 * ONE/ONE2 router presses defer until release so MIXER can consume both
 * edges. HOLD routes natively and disables the recording-sensitive shortcut.
 */
.text
.globl vm_mixer,vm_midi,vm_rec_router,vm_open,vm_close,vm_evicted
.globl vm_left,vm_right,vm_draw,vm_tick,vm_refresh,VM_LAYER
.set VM_ROWS,0x46100b18
.set VM_PERIOD,5280000

vm_mixer:
 moveq #0,%d0
 moveb VM_ROWS+5,%d0
 btst #5,%d0
 bne.w vm_mixer_stock
 tstl vm_window
 bne.w vm_close
 moveb VM_ROWS+6,%d0
 btst #5,%d0
 bne.s vm_mixer_midi
 moveb VM_ROWS+5,%d0
 btst #3,%d0
 bne.s vm_mixer_rec
 btst #2,%d0
 bne.s vm_mixer_cue
vm_mixer_stock:
 tstl 0x460e7424
 jmp 0x4007d46e
vm_mixer_midi:
 movel vm_pending_releases,%d0
 oril #1,%d0
 movel %d0,vm_pending_releases
 moveq #0,%d0
 bra.w vm_open
vm_mixer_rec:
 tstl vm_rec_hold
 bne.w vm_mixer_stock
 tstl vm_rec_pending
 beq.s 1f
 clrl vm_rec_pending
 movel vm_pending_releases,%d0
 oril #2,%d0
 movel %d0,vm_pending_releases
1: moveq #1,%d0
 bra.w vm_open
vm_mixer_cue:
 moveq #2,%d0
 bra.w vm_open

vm_midi:
 tstl %sp@(8)
 bne.s vm_midi_stock
 movel vm_pending_releases,%d0
 btst #0,%d0
 beq.s vm_midi_stock
 movel vm_pending_releases,%d0
 andil #-2,%d0
 movel %d0,vm_pending_releases
 rts
vm_midi_stock:
 movel %d3,%sp@-
 movel %d2,%sp@-
 movel %sp@(12),%d3
 jmp 0x40055680

/* This replaces a JSR, not a function-entry JMP. The original five args
 * remain owned by the stock caller. Tail-rewritten router arguments must
 * NEVER alias the saved press or the still-pending release arguments.
 */
vm_rec_router:
 movel %sp@(12),%d0
 cmpil #0x2b,%d0
 bne.w vm_rec_stock
 tstl %sp@(16)
 beq.w vm_rec_release
 clrl vm_rec_pending
 movel vm_pending_releases,%d0
 andil #-3,%d0
 movel %d0,vm_pending_releases
 movel %sp@(8),%d0
 cmpil #2,%d0
 bne.s 2f
 moveq #1,%d0
 movel %d0,vm_rec_hold
 bra.w vm_rec_stock
2: clrl vm_rec_hold
 lea %sp@(4),%a0
 lea vm_rec_args,%a1
 moveq #5,%d0
1: movel %a0@+,%a1@+
 subql #1,%d0
 bne.s 1b
 moveq #1,%d0
 movel %d0,vm_rec_pending
 moveb 0x100b14cf,%d0
 moveb %d0,vm_rec_part
 movel 0x46c82456,%d0
 movel %d0,vm_rec_bank
 moveal %d0,%a0
 moveq #0,%d0
 moveb vm_rec_part,%d0
 movel #6322,%d1
 mulul %d1,%d0
 addal %d0,%a0
 addal #0x8eda2,%a0
 movel vm_rec_args,%d0
 moveb %a0@(0,%d0:l),%d0
 moveb %d0,vm_rec_machine
 movel 0x460d1736,%d0
 movel %d0,vm_rec_setup
 movel 0x460d5db4,%d0
 movel %d0,vm_rec_setup_page
 movel 0x460fab40,%d0
 movel %d0,vm_rec_grid
 movel 0x80000012,%d0
 movel %d0,vm_rec_midi
 rts
vm_rec_release:
 movel %d2,%sp@-
 bsr.w vm_rec_cleanup
 movel %sp@+,%d2
 tstl vm_rec_hold
 beq.s 2f
 clrl vm_rec_hold
 clrl vm_rec_pending
 bra.w vm_rec_stock
2: movel vm_pending_releases,%d0
 btst #1,%d0
 beq.s 1f
 movel vm_pending_releases,%d0
 andil #-3,%d0
 movel %d0,vm_pending_releases
 clrl vm_rec_pending
 rts
1: tstl vm_rec_pending
 beq.w vm_rec_stock
 clrl vm_rec_pending
 movel %sp@(4),%d0
 cmpl vm_rec_args,%d0
 bne.w vm_rec_stock
 movel %sp@(8),%d0
 cmpl vm_rec_args+4,%d0
 bne.w vm_rec_stock
 lea vm_rec_args,%a0
 movel %a0@(16),%sp@-
 movel %a0@(12),%sp@-
 movel %a0@(8),%sp@-
 movel %a0@(4),%sp@-
 movel %a0@,%sp@-
 jsr 0x40097924
 lea %sp@(20),%sp
vm_rec_stock:
 jmp 0x40097924

vm_open:
 movel %d0,vm_page
 tstl vm_window
 bne.w vm_redraw_now
 bsr.w vm_reset
 pea vm_evicted
 pea 5
 clrl %sp@-
 pea -1
 pea 64
 pea 128
 jsr 0x4005829c
 lea %sp@(24),%sp
 movel %d0,vm_window
 beq.s 1f
 pea VM_LAYER
 jsr 0x40031494
 addql #4,%sp
 moveq #1,%d0
 movel %d0,vm_layer_live
 bra.w vm_redraw_now
1: rts

/* WDESTROY zeros the handle; the layer guard makes explicit close and
 * stock eviction idempotent, including a nested closed callback.
 * Pending consumed releases deliberately survive both paths.
 */
vm_close:
vm_evicted:
 tstl vm_window
 beq.s 1f
 pea vm_window
 jsr 0x40055db4
 addql #4,%sp
1: tstl vm_layer_live
 beq.s 2f
 clrl vm_layer_live
 pea VM_LAYER
 jsr 0x4003146c
 addql #4,%sp
2: rts

vm_left:
 tstl vm_page
 beq.s 1f
 subql #1,vm_page
 bra.w vm_redraw_now
1: rts
vm_right:
 movel vm_page,%d0
 cmpil #2,%d0
 bge.s 1f
 addql #1,vm_page
 bra.w vm_redraw_now
1: rts
vm_redraw_now:
 movel 0xfc07c00c,%d0
 movel %d0,vm_last_draw_time
 bsr.w vm_snapshot
 bra.w vm_draw

vm_tick:
 lea %sp@(-60),%sp
 movem.l %d0-%d7/%a0-%a6,%sp@
 bsr.w vm_rec_cleanup
 movel 0xfc07c00c,%d0
 bsr.w vm_refresh
 movem.l %sp@,%d0-%d7/%a0-%a6
 lea %sp@(60),%sp
 tstl 0x46104ca8
 jmp 0x40056c98
/* Called on the stock UI timer event. A routing-context change cancels
 * deferred ONE/ONE2 presses. Raw-up alone cannot cancel them: the queued
 * release callback may not have run yet. A fresh routed press retires a
 * consumed-release marker left by a release routed elsewhere. HOLD shortcut
 * eligibility is latched for the physical hold.
 */
vm_rec_cleanup:
 moveb VM_ROWS+5,%d0
 btst #3,%d0
 bne.s 1f
 clrl vm_rec_hold
1: tstl vm_rec_pending
 beq.w 3f
 movel 0x460d1736,%d0
 cmpl vm_rec_setup,%d0
 bne.w 2f
 movel 0x460d5db4,%d0
 cmpl vm_rec_setup_page,%d0
 bne.w 2f
 movel 0x460fab40,%d0
 cmpl vm_rec_grid,%d0
 bne.w 2f
 movel 0x80000012,%d0
 cmpl vm_rec_midi,%d0
 bne.w 2f

 moveq #0,%d0
 moveb 0x100b14cc,%d0
 cmpl vm_rec_args,%d0
 bne.s 2f
 moveb 0x100b14cf,%d1
 cmpb vm_rec_part,%d1
 bne.s 2f
 movel 0x46c82456,%d1
 cmpl vm_rec_bank,%d1
 bne.s 2f
 moveal %d1,%a0
 moveq #0,%d1
 moveb vm_rec_part,%d1
 movel #6322,%d2
 mulul %d2,%d1
 addal %d1,%a0
 addal #0x8eda2,%a0
 moveb %a0@(0,%d0:l),%d1
 cmpb vm_rec_machine,%d1
 bne.s 2f
 moveq #12,%d2
 mulul %d2,%d0
 moveq #0,%d1
 addal #0x5e3,%a0
 moveb %a0@(0,%d0:l),%d1
 cmpl vm_rec_args+4,%d1
 beq.s 3f
2: clrl vm_rec_pending
3: rts

/* Callable scheduling helper: d0 = wrapping 132 MHz DTCN3 count.
 * Maintain the absolute 40 ms grid over 60 Hz UI wakeups. At >=2 periods
 * late restart from now once, without subsequent catch-up bursts.
 */
vm_refresh:
 tstl vm_window
 beq.s 2f
 movel %d0,%d1
 subl vm_last_draw_time,%d1
 cmpil #VM_PERIOD,%d1
 bcs.s 2f
 cmpil #VM_PERIOD*2,%d1
 bcc.s 1f
 movel vm_last_draw_time,%d0
 addil #VM_PERIOD,%d0
 movel %d0,vm_last_draw_time
 bra.w vm_refresh_draw
1: movel %d0,vm_last_draw_time
vm_refresh_draw:
 bsr.w vm_snapshot
 bra.w vm_draw
2: rts

/* All geometry uses the approved top-origin pixel coordinates. Stock
 * surfaces use bottom-origin y; helpers convert here. Stock font only.
 * vm_box: d0=x1,d1=y1,d2=x2,d3=y2,d4=mode; a5=surface.
 */
vm_box:
 movel %d4,%sp@-
 moveq #63,%d4
 subl %d1,%d4
 movel %d4,%sp@-
 movel %d2,%sp@-
 moveq #63,%d4
 subl %d3,%d4
 movel %d4,%sp@-
 movel %d0,%sp@-
 movel %a5,%sp@-
 jsr 0x40012254
 lea %sp@(24),%sp
 rts
/* Text at top-origin y; a0 string, d0 center, d1 top. Stock FONT
 * has height 6 and origin offset 1; baseline 60-y keeps the bottom
 * footer inside TEXT's checked surface bounds (baseline must be >=1). */
vm_center:
 movel %d0,%sp@-
 movel %d1,%sp@-
 movel %a0,%sp@-
 movel %a0,%sp@-
 pea -1
 pea 0x400ba876
 jsr 0x40012f30
 lea %sp@(12),%sp
 movel %sp@+,%a0
 movel %sp@+,%d1
 movel %sp@+,%d2
 lsrl #1,%d0
 subl %d0,%d2
 movel %d2,%d0
vm_text:
 movel %a0,%sp@-
 pea -1
 moveq #60,%d2
 subl %d1,%d2
 movel %d2,%sp@-
 movel %d0,%sp@-
 movel %a5,%sp@-
 pea 0x400ba876
 jsr 0x40012bd8
 lea %sp@(24),%sp
 rts
/* format unsigned setting in a6's local buffer; d0=value */
vm_number:
 movel %d0,%sp@-
 pea vm_numfmt
 movel %a6,%sp@-
 jsr 0x40013a08
 lea %sp@(12),%sp
 moveal %a6,%a0
 rts

/* Rail descriptor in a4: x,width,first snapshot channel,channel count.
 * d7 = volume. Preserve the caller's loop registers. */
vm_rail:
 lea %sp@(-24),%sp
 movem.l %d5-%d7/%a2-%a4,%sp@
 movel %a4@,%d5
 movel %a4@(4),%d6
 /* outline as filled box with clear interior */
 movel %d5,%d0
 moveq #19,%d1
 movel %d5,%d2
 addl %d6,%d2
 subql #1,%d2
 moveq #46,%d3
 moveq #1,%d4
 bsr.w vm_box
 movel %d5,%d0
 addql #1,%d0
 moveq #20,%d1
 movel %d5,%d2
 addl %d6,%d2
 subql #2,%d2
 moveq #45,%d3
 moveq #0,%d4
 bsr.w vm_box
 moveal %a4@(8),%a2
 lea vm_snapshot_peaks,%a3
 lea %a3@(0,%a2:l:4),%a3
 moveq #0,%d7
vm_rail_signal:
 movel %a3@+,%d0
 moveq #26,%d1
 mulul %d1,%d0
 addil #63,%d0
 moveq #127,%d1
 divul %d1,%d0
 tstl %d0
 beq.s vm_rail_next
 moveq #46,%d1
 subl %d0,%d1
 movel %d5,%d0
 addql #1,%d0
 movel %d6,%d2
 subql #2,%d2
 movel %a4@(12),%d4
 cmpil #1,%d4
 beq.s 1f
 lsrl #1,%d2
 tstl %d7
 beq.s 2f
 addl %d2,%d0
2: subql #1,%d2
1: addl %d0,%d2
 subql #1,%d2
 moveq #45,%d3
 moveq #1,%d4
 bsr.w vm_box
vm_rail_next:
 addql #1,%d7
 cmpl %a4@(12),%d7
 blt.s vm_rail_signal
 /* The fixed 100 line is y=25: dark interior, lit edge ticks. */
 movel %d5,%d0
 subql #1,%d0
 moveq #25,%d1
 movel %d5,%d2
 addl %d6,%d2
 moveq #25,%d3
 moveq #1,%d4
 bsr.w vm_box
 movel %d5,%d0
 addql #1,%d0
 moveq #25,%d1
 movel %d5,%d2
 addl %d6,%d2
 subql #2,%d2
 moveq #25,%d3
 moveq #0,%d4
 bsr.w vm_box
 movel %sp@(8),%d7
 movel %d7,%d0
 moveq #27,%d1
 mulul %d1,%d0
 addil #63,%d0
 moveq #127,%d1
 divul %d1,%d0
 moveq #46,%d7
 subl %d0,%d7
 movel %d5,%d0
 addl %d6,%d0
 addql #1,%d0
 movel %d7,%d1
 movel %d0,%d2
 addql #1,%d2
 movel %d7,%d3
 moveq #1,%d4
 bsr.w vm_box
 movel %d5,%d0
 addl %d6,%d0
 addql #2,%d0
 movel %d7,%d1
 subql #1,%d1
 movel %d0,%d2
 movel %d7,%d3
 addql #1,%d3
 moveq #1,%d4
 bsr.w vm_box
 movem.l %sp@,%d5-%d7/%a2-%a4
 lea %sp@(24),%sp
 rts

vm_draw:
 lea %sp@(-76),%sp
 movem.l %d2-%d7/%a2-%a6,%sp@
 lea %sp@(44),%a6
 movel vm_window,%d0
 beq.w vm_draw_exit
 moveal %d0,%a5
 lea %a5@(36),%a5
 movel %a5,%sp@-
 jsr 0x40035624
 addql #4,%sp
 movel vm_page,%d0
 lea vm_titles,%a0
 moveal %a0@(0,%d0:l:4),%a0
 moveq #3,%d0
 moveq #3,%d1
 bsr.w vm_text
 movel vm_page,%d0
 addql #1,%d0
 movel %d0,%sp@-
 pea vm_pagefmt
 movel %a6,%sp@-
 jsr 0x40013a08
 lea %sp@(12),%sp
 moveal %a6,%a0
 moveq #111,%d0
 moveq #3,%d1
 bsr.w vm_text
 moveq #0,%d0
 moveq #0,%d1
 moveq #127,%d2
 moveq #8,%d3
 moveq #-1,%d4
 bsr.w vm_box
 movel vm_page,%d0
 beq.w vm_draw_tracks
 cmpil #1,%d0
 beq.w vm_draw_inputs
 bra.w vm_draw_outputs
vm_draw_tracks:
 lea vm_track_rails,%a4
 moveq #0,%d5
vm_track_loop:
 movel %d5,%d0
 addql #1,%d0
 bsr.w vm_number
 movel %d5,%d0
 lsll #4,%d0
 addql #8,%d0
 moveq #11,%d1
 bsr.w vm_center
 lea 0x80000c50,%a0
 moveq #0,%d7
 moveb %a0@(0,%d5:l:2),%d7
 bsr.w vm_rail
 movel %d7,%d0
 bsr.w vm_number
 movel %d5,%d0
 lsll #4,%d0
 addql #8,%d0
 moveq #49,%d1
 bsr.w vm_center
 lea %a4@(16),%a4
 addql #1,%d5
 cmpil #8,%d5
 blt.s vm_track_loop
 bra.w vm_draw_footer
vm_draw_inputs:
 lea vm_input_rails,%a4
 moveq #0,%d5
vm_input_loop:
 lea vm_input_labels,%a0
 lea %a0@(0,%d5:l:4),%a0
 lea vm_snapshot_holds,%a1
 tstw %a1@(0,%d5:l:2)
 beq.s 1f
 lea vm_clip_labels,%a0
 lea %a0@(0,%d5:l:4),%a0
1: movel %d5,%d0
 moveq #30,%d1
 mulul %d1,%d0
 addil #18,%d0
 moveq #11,%d1
 bsr.w vm_center
 lea vm_snapshot_holds,%a1
 tstw %a1@(0,%d5:l:2)
 beq.s 2f
 movel %d5,%d0
 moveq #30,%d1
 mulul %d1,%d0
 addil #13,%d0
 moveq #10,%d1
 movel %d0,%d2
 addil #10,%d2
 moveq #16,%d3
 moveq #-1,%d4
 bsr.w vm_box
2: moveq #0,%d7
 moveb 0x8000002f,%d7
 cmpil #2,%d5
 blt.s 3f
 moveb 0x8000002e,%d7
3: bsr.w vm_rail
 movel %d7,%d0
 bsr.w vm_number
 movel %d5,%d0
 moveq #30,%d1
 mulul %d1,%d0
 addil #18,%d0
 moveq #49,%d1
 bsr.w vm_center
 lea %a4@(16),%a4
 addql #1,%d5
 cmpil #4,%d5
 blt.w vm_input_loop
 bra.w vm_draw_footer
vm_draw_outputs:
 lea vm_output_rails,%a4
 moveq #0,%d5
vm_output_loop:
 lea vm_main,%a0
 tstl %d5
 beq.s 1f
 lea vm_cue,%a0
1: movel %d5,%d0
 lsll #6,%d0
 addil #28,%d0
 moveq #11,%d1
 bsr.w vm_center
 lea 0x80000035,%a0
 moveq #0,%d7
 moveb %a0@(0,%d5:l),%d7
 bsr.w vm_rail
 lea %a4@(16),%a4
 bsr.w vm_rail
 lea %a4@(16),%a4
 movel %d7,%d0
 bsr.w vm_number
 movel %d5,%d0
 lsll #6,%d0
 addil #28,%d0
 moveq #49,%d1
 bsr.w vm_center
 lea vm_l,%a0
 movel %d5,%d0
 lsll #6,%d0
 addql #8,%d0
 moveq #31,%d1
 bsr.w vm_text
 lea vm_r,%a0
 movel %d5,%d0
 lsll #6,%d0
 addil #47,%d0
 moveq #31,%d1
 bsr.w vm_text
 addql #1,%d5
 cmpil #2,%d5
 blt.w vm_output_loop
vm_draw_footer:
 moveq #0,%d0
 moveq #56,%d1
 moveq #127,%d2
 moveq #56,%d3
 moveq #1,%d4
 bsr.w vm_box
 lea vm_footer,%a0
 moveq #64,%d0
 moveq #59,%d1
 bsr.w vm_center
 lea vm_lt,%a0
 moveq #2,%d0
 moveq #59,%d1
 bsr.w vm_text
 lea vm_gt,%a0
 moveq #122,%d0
 moveq #59,%d1
 bsr.w vm_text
 moveq #1,%d0
 movel %d0,0x46c7c72c
vm_draw_exit:
 movem.l %sp@,%d2-%d7/%a2-%a6
 lea %sp@(76),%sp
 rts

.balign 4
VM_LAYER: .long 0,VM_KEYS,VM_ENCS,0,0,-1,-1
VM_KEYS:
 .byte 0x30,0
 .long vm_close,0,0,0,0
 .word 0,0
 .byte 0x32,0
 .long vm_close,0,0,0,0
 .word 0,0
 .byte 0x34,0
 .long vm_left,0,0,0,0
 .word 0,0
 .byte 0x21,0
 .long vm_right,0,0,0,0
 .word 0,0
 .byte 0xff,0
 .long 0,0,0,0,0
 .word 0,0
VM_ENCS:
 .byte 0xff,0
 .long 0,0,0,0,0
.balign 4
vm_layer_live: .long 0
vm_rec_pending: .long 0
vm_rec_hold: .long 0
vm_rec_bank: .long 0
vm_rec_part: .long 0
vm_rec_machine: .long 0
vm_rec_setup: .long 0
vm_rec_setup_page: .long 0
vm_rec_grid: .long 0
vm_rec_midi: .long 0
vm_rec_args: .space 20
vm_track_rails:
 .long 2,11,0,2,18,11,2,2,34,11,4,2,50,11,6,2
 .long 66,11,8,2,82,11,10,2,98,11,12,2,114,11,14,2
vm_input_rails:
 .long 12,13,16,1,42,13,17,1,72,13,18,1,102,13,19,1
vm_output_rails:
 .long 14,10,20,1,33,10,21,1,78,10,22,1,97,10,23,1
vm_titles: .long vm_tracks,vm_inputs,vm_outputs
vm_tracks: .asciz "INTERNAL TRACKS"
vm_inputs: .asciz "EXTERNAL INPUTS"
vm_outputs: .asciz "MAIN / CUE"
vm_pagefmt: .asciz "%d/3"
vm_numfmt: .asciz "%d"
vm_input_labels: .byte 65,0,0,0,66,0,0,0,67,0,0,0,68,0,0,0
vm_clip_labels: .byte 65,33,0,0,66,33,0,0,67,33,0,0,68,33,0,0
vm_main: .asciz "MAIN"
vm_cue: .asciz "CUE"
vm_l: .asciz "L"
vm_r: .asciz "R"
vm_footer: .asciz "0-127  -100"
vm_lt: .asciz "<"
vm_gt: .asciz ">"
.balign 4
