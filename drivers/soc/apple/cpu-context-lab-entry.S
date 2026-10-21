/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/linkage.h>
#include <asm/assembler.h>

	.text
/*
 * x0: CPU stop register, x1: target bit. CPU0 holds the shared L2 awake.
 * Sweep only the local L1. No stack access or C call follows this sweep.
 * M1 disables set/way L2 operations in EHID4, not L1 operations.
 */
SYM_FUNC_START(apple_context_lab_poweroff)
	dsb	sy
	msr	csselr_el1, xzr
	isb
	mrs	x2, ccsidr_el1
	and	x3, x2, #7
	add	x3, x3, #4
	mrs	x4, id_aa64mmfr2_el1
	ubfx	x4, x4, #20, #4
	cbnz	x4, 1f
	ubfx	x4, x2, #3, #10
	ubfx	x5, x2, #13, #15
	b	2f
1:
	ubfx	x4, x2, #3, #21
	ubfx	x5, x2, #32, #24
2:
	clz	w6, w4
	mov	x7, x4
3:
	mov	x8, x5
4:
	lsl	x9, x7, x6
	lsl	x10, x8, x3
	orr	x9, x9, x10
	dc	cisw, x9
	subs	x8, x8, #1
	b.ge	4b
	subs	x7, x7, #1
	b.ge	3b
	dsb	sy
	str	w1, [x0]
	dsb	sy
5:
	isb
	wfi
	b	5b
SYM_FUNC_END(apple_context_lab_poweroff)

	.pushsection ".idmap.text", "ax"
/* AURA calls this physical address with MMU off; no return to AURA. */
SYM_CODE_START(apple_context_lab_resume)
	b	cpu_resume
SYM_CODE_END(apple_context_lab_resume)
	.popsection
