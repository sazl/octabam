/* VOLUME MIXER: integer-only frame peaks, no EMAC or DSP writes.
 * Channel order: T1 L/R..T8 L/R, A/B/C/D, MAIN L/R, CUE L/R.
 * vm_peak_accum holds absolute 24-bit units; vm_snapshot_peaks holds 0..127.
 * Helpers use register arguments and preserve d2-d7/a2-a6. d0/d1/a0/a1
 * and CCR are caller-saved. vm_sample_block(a0=16-frame stereo, d0=even
 * first channel in 0..22) is low-level; vm_collect gates it on vm_window.
 * Input holds age once when their stereo block is sampled, then any rail
 * reloads that channel to 1379 blocks (>500 ms at 44100/16 blocks/sec).
 */
.text
.globl vm_frame,vm_collect,vm_sample_block,vm_snapshot,vm_normalize,vm_reset

/* Hook at 0x4000d98e. The ISR restores its own general registers later,
 * but preserve all locally rather than depending on other detour users.
 * Recreate the displaced load last. Subsequent stock arithmetic owns CCR.
 */
vm_frame:
 lea %sp@(-60),%sp
 movem.l %d0-%d7/%a0-%a6,%sp@
 bsr.w vm_collect
 movem.l %sp@,%d0-%d7/%a0-%a6
 lea %sp@(60),%sp
 movel 0x80004800,%d0
 jmp 0x4000d994

/* Callable whole-frame producer; completed source selectors are read-only.
 * Uses exactly the arenas proven equivalent by volume_mixer_probe.py.
 */
vm_collect:
 tstl vm_window
 beq.w vm_collect_done
 lea %sp@(-12),%sp
 movem.l %d2/%a2-%a3,%sp@
 addql #1,vm_frame_count
 movel 0x800000e4,%d0
 andil #1,%d0
 lsll #8,%d0
 lsll #2,%d0
 addil #0x80003190,%d0
 moveal %d0,%a2
 moveq #0,%d2
vm_collect_tracks:
 moveal %a2,%a0
 movel %d2,%d0
 bsr.w vm_sample_block
 lea %a2@(128),%a2
 addql #2,%d2
 cmpl #16,%d2
 bne.s vm_collect_tracks
 movel 0x46104d00,%d0
 andil #7,%d0
 lsll #8,%d0
 addil #0x80005660,%d0
 moveal %d0,%a3
 lea %a3@(128),%a0
 moveq #16,%d0
 bsr.w vm_sample_block
 moveal %a3,%a0
 moveq #18,%d0
 bsr.w vm_sample_block
 lea 0x80005e60,%a0
 moveq #20,%d0
 bsr.w vm_sample_block
 lea 0x80005ee0,%a0
 moveq #22,%d0
 bsr.w vm_sample_block
 movem.l %sp@,%d2/%a2-%a3
 lea %sp@(12),%sp
vm_collect_done:
 rts

/* All 32 words are shifted as signed values before abs; -0x800000 is
 * safely representable as +0x800000 in a longword. Test both rails before
 * abs or display rounding, which also maps nearby non-rails to 127.
 */
vm_sample_block:
 lea %sp@(-24),%sp
 movem.l %d2-%d6/%a2,%sp@
 movel %d0,%d3
 lsll #2,%d0
 lea vm_peak_accum,%a1
 addal %d0,%a1
 moveq #0,%d4
 cmpl #16,%d3
 blo.s vm_sample_ready
 cmpl #20,%d3
 bhs.s vm_sample_ready
 moveq #1,%d4
 subil #16,%d3
 addl %d3,%d3
 lea vm_clip_hold,%a2
 addal %d3,%a2
 moveq #0,%d1
 movew %a2@,%d1
 beq.s vm_age_right
 subql #1,%d1
 movew %d1,%a2@
vm_age_right:
 moveq #0,%d1
 movew %a2@(2),%d1
 beq.s vm_sample_ready
 subql #1,%d1
 movew %d1,%a2@(2)
vm_sample_ready:
 moveq #32,%d2
 moveq #0,%d6
vm_sample_word:
 movel %a0@+,%d0
 asrl #8,%d0
 tstl %d4
 beq.s vm_sample_magnitude
 cmpl #0x7fffff,%d0
 beq.s vm_sample_clipped
 cmpl #-0x800000,%d0
 bne.s vm_sample_magnitude
vm_sample_clipped:
 movel #1379,%d1
 movew %d1,%a2@(0,%d6:l:2)
vm_sample_magnitude:
 tstl %d0
 bpl.s vm_sample_positive
 negl %d0
vm_sample_positive:
 cmpl %a1@(0,%d6:l:4),%d0
 bls.s vm_sample_next
 movel %d0,%a1@(0,%d6:l:4)
vm_sample_next:
 eoril #1,%d6
 subql #1,%d2
 bne.s vm_sample_word
 movem.l %sp@,%d2-%d6/%a2
 lea %sp@(24),%sp
 rts

/* d0=magnitude in 24-bit units, d0=min(127,(m*127+0x400000)>>23).
 * m <= 0x800000, so the integer product + rounding fits in 32 bits.
 */
vm_normalize:
 movel %d0,%d1
 lsll #7,%d0
 subl %d1,%d0
 addil #0x400000,%d0
 lsrl #8,%d0
 lsrl #8,%d0
 lsrl #7,%d0
 cmpl #127,%d0
 bls.s vm_normalize_done
 moveq #127,%d0
vm_normalize_done:
 rts

/* Brief exchange only under IPL7. Normalization follows SR restoration;
 * drawing is the UI's responsibility. Clip countdowns are never cleared
 * by a snapshot, so a clip between redraws persists for its full hold.
 * Named mask boundaries support instruction-cost measurements in gate.
 */
vm_snapshot:
 lea %sp@(-8),%sp
 movem.l %d2/%a2,%sp@
 movew %sr,%d1
 movel %d1,%d0
 oril #0x700,%d0
 movew %d0,%sr
vm_snapshot_masked:
 lea vm_peak_accum,%a0
 lea vm_snapshot_peaks,%a1
 moveq #24,%d2
vm_snapshot_copy_peak:
 movel %a0@,%d0
 clrl %a0@+
 movel %d0,%a1@+
 subql #1,%d2
 bne.s vm_snapshot_copy_peak
 lea vm_clip_hold,%a0
 lea vm_snapshot_holds,%a1
 moveq #4,%d2
vm_snapshot_copy_hold:
 movew %a0@+,%d0
 movew %d0,%a1@+
 subql #1,%d2
 bne.s vm_snapshot_copy_hold
 movew %d1,%sr
vm_snapshot_unmasked:
 lea vm_snapshot_peaks,%a2
 moveq #24,%d2
vm_snapshot_normalize:
 movel %a2@,%d0
 bsr.w vm_normalize
 movel %d0,%a2@+
 subql #1,%d2
 bne.s vm_snapshot_normalize
 movem.l %sp@,%d2/%a2
 lea %sp@(8),%sp
 rts

/* UI calls before publishing a new nonzero vm_window. Clear all audio
 * state including stale snapshots; leave lifecycle/page/modifier state
 * alone. Safe even if the previous window was still collecting.
 */
vm_reset:
 movew %sr,%d1
 movel %d1,%d0
 oril #0x700,%d0
 movew %d0,%sr
vm_reset_masked:
 lea vm_peak_accum,%a0
 moveq #52,%d0
vm_reset_audio:
 clrl %a0@+
 subql #1,%d0
 bne.s vm_reset_audio
 clrl vm_frame_count
 movew %d1,%sr
vm_reset_unmasked:
 rts

.balign 4
.globl vm_window,vm_page,vm_pending_releases,vm_frame_count,vm_last_draw_time
.globl vm_peak_accum,vm_snapshot_peaks,vm_clip_hold,vm_snapshot_holds
vm_window: .long 0
vm_page: .long 0
vm_pending_releases: .long 0
vm_frame_count: .long 0
vm_last_draw_time: .long 0
/* Keep this audio exchange region contiguous: vm_reset clears 208 bytes. */
vm_peak_accum: .space 96
vm_snapshot_peaks: .space 96
vm_clip_hold: .space 8
vm_snapshot_holds: .space 8
