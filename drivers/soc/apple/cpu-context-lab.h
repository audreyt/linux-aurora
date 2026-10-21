/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef APPLE_CPU_CONTEXT_LAB_H
#define APPLE_CPU_CONTEXT_LAB_H

#include <linux/types.h>

void secondary_holding_pen(void);
void __noreturn apple_context_lab_poweroff(void __iomem *stop, u32 bit);
void apple_context_lab_resume(void);

#endif
