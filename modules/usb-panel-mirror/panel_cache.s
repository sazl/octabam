| Post-loader handoff must synchronize instructions from OS-resident code.
| MKI PDBG6/7 hung executing a freshly unpacked DRAM stub; the same stub
| in OS space (PDBG8) booted. IC-only CPUSHL restored DRAM execution in
| PDBG9 and the complete panel feature in PDBG10. The port has no IC model.
| Preserve stock CACR/ACRs, data cache, registers, SR and entry stack.
        .text
        .global pm_cache_handoff
pm_cache_handoff:
        lea -24(%sp),%sp
        movem.l %d0-%d1/%a0-%a1,(%sp)
        move.w %sr,%d0
        move.l %d0,16(%sp)
        move.w #0x2700,%sr
| Cover 256 sets / four ways if CACR selects set/way addressing.
        moveq #0,%d0
.Lsets:
        move.l %d0,%a0
        cpushl %ic,(%a0)
        addq.l #1,%a0
        cpushl %ic,(%a0)
        addq.l #1,%a0
        cpushl %ic,(%a0)
        addq.l #1,%a0
        cpushl %ic,(%a0)
        addi.l #16,%d0
        cmpi.l #4096,%d0
        bne.s .Lsets
| Cover the panel's linked range and later runtime units if physical
| address search is selected. Include both SDRAM address windows.
        moveq #1,%d1
        move.l pm_cache_first,%d0
        andi.l #0xfffffff0,%d0
        move.l %d0,%a0
        move.l pm_cache_end,%a1
.Lrange:
        cpushl %ic,(%a0)
        lea 16(%a0),%a0
        cmpa.l %a1,%a0
        bcs.s .Lrange
        subq.l #1,%d1
        bmi.s .Ldone
        move.l pm_cache_first,%d0
        andi.l #0xfffffff0,%d0
        addi.l #0x08000000,%d0
        move.l %d0,%a0
        adda.l #0x08000000,%a1
        bra.s .Lrange
.Ldone:
| A return-style tail transfer consumes the private target slot and leaves
| SP identical to the original JMP entry, without borrowing a register.
        move.l pm_cache_target,%d0
        move.l %d0,20(%sp)
        move.l 16(%sp),%d0
        move.w %d0,%sr
        movem.l (%sp),%d0-%d1/%a0-%a1
        lea 20(%sp),%sp
        rts
| Fixed bridge slots let the existing SymbolRef machinery resolve DRAM
| addresses after the runtime link. No hard-coded DRAM entry or size.
        .org 0x100
        .global pm_cache_first, pm_cache_end, pm_cache_target
pm_cache_first: .long 0
pm_cache_end: .long 0
pm_cache_target: .long 0
