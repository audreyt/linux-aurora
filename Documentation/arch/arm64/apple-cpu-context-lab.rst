.. SPDX-License-Identifier: GPL-2.0-only

J314s secondary CPU context-loss diagnostic
=========================================

This is a lab experiment, not a suspend or idle implementation. It can leave a
CPU stopped and the kernel blocked until an external reset. It must never be
enabled in a distributed kernel configuration or invoked on the controller Mac.

Build APPLE_CPU_CONTEXT_LAB=y and use apple_cpu_context=1 only with the matching
AURA v0.1.0-smp-79d1c20c loader on the J314s M1 Pro. The diagnostic creates
root-only debugfs trigger and result files. Probe does not execute the test.
The trigger accepts exactly ``cpu1-context-loss`` followed by a newline, once
per boot, after the external harness has verified the immutable kernel, loader,
device tree, boot identity, no active VMs, serial identity and reset recovery.

All ten online CPUs enter stop_machine callbacks with interrupts disabled.
CPU0 remains active in CPU1's E cluster; the other eight CPUs cannot initiate
waited IPIs or TLB shootdowns. CPU1 enters the timer broadcast and CPU-PM notifier
paths, then generic cpu_suspend. A CPU-PM veto has already been unwound by
CPU_PM_ENTER_FAILED and does not receive an unmatched CPU_PM_EXIT.

The finisher publishes an idmap trampoline through CPU1's existing AURA release
field. It saves the generic suspend context and local state, programs only the
local timer/CYC registers, sweeps only local L1, requests one CPU stop and waits
without returning. CPU0 requires a nonzero status before the attempt and an
observed OFF transition within 50 ms before issuing the two CPU-start writes.
There is no RVBAR, ACC_OVRD, DCP, PMP, shared-L2 or DRAM power write.

AURA's existing per-MPIDR reset path calls the physical trampoline, which enters
cpu_resume. Only the generic saved-context return counts as a successful
cpu_suspend. The delivery field, CYC and timer snapshots must be restored before
the other CPUs are released. The observer can cancel an uncommitted finisher,
but leaves the delivery field to CPU1 once a stop is committed and never retries
a start. Observer errors do not overwrite CPU1's result. Fast-IPI pending state
is acknowledged after a real saved-context return; a pre-existing pending IPI
aborts before poweroff and is left for the normal interrupt handler.
An unrecovered CPU prevents stop_machine
from completing: that is an expected terminal failure requiring the separately
reviewed external reset, not a bounded software recovery or successful return.

M1 EHID4 disables DC maintenance by VA and set/way operations on L2. The final
leaf uses L1 set/way only, with shared L2 kept active by CPU0. The explicit PoC
requests for the context, stash and release field do not establish hardware
visibility by themselves. Whether this return contract works on the M1 is what
the single reviewed hardware experiment must determine.

A successful result proves one secondary's context-loss return only. Whole-system
entry/wake, boot-CPU handoff, device restoration, repeated cycles and measured
sleep power remain separate acceptance requirements.
