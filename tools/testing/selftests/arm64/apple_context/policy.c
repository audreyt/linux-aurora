// SPDX-License-Identifier: GPL-2.0-only
/* Host-only test of the real CPU restart admission gate. No hardware access. */
#include <assert.h>
#include <stdio.h>
#include "cpu-context-lab-policy.h"

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

static void check_case(unsigned int before, unsigned int after,
		       unsigned long long elapsed, int mode)
{
	int stopping = mode & 1;
	int started = !!(mode & 2);
	int allowed = apple_context_lab_may_start(before, after, stopping, started, elapsed);

	if (allowed) {
		assert(before & 0xff);
		assert(!(after & 0xff));
		assert(stopping && !started && elapsed < 50000);
	} else if (before == 1 && after == 0 && stopping && !started && elapsed < 50000) {
		assert(!"valid OFF transition rejected");
	}
}

int main(void)
{
	const unsigned int status[] = { 0, 1, 0xff, 0x100, 0x101, 0xffffff00 };
	const unsigned long long elapsed[] = { 0, 1, 49999, 50000, 50001, ~0ULL };
	unsigned int checks = 0;

	for (unsigned int a = 0; a < ARRAY_SIZE(status); a++)
		for (unsigned int b = 0; b < ARRAY_SIZE(status); b++)
			for (unsigned int t = 0; t < ARRAY_SIZE(elapsed); t++)
				for (int mode = 0; mode < 4; mode++) {
					check_case(status[a], status[b], elapsed[t], mode);
					checks++;
				}
	printf("PASS: %u CPU-start admission cases; no hardware operations\n", checks);
	return 0;
}
