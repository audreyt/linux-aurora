// SPDX-License-Identifier: GPL-2.0-only OR MIT

//! Physical kick-ring backing, queue identities and entry publication.
//!
//! The device mutex serializes identities and publication. Installed identities
//! and their ring backing stay owned until both firmware processors stop;
//! command completion alone does not detach a hardware queue.

use core::sync::atomic::{fence, Ordering};
use kernel::prelude::*;

use super::{
    fw::{kick::*, queue::DataMaster},
    object::{Allocator, CpuMap, KernelObject},
};
use crate::{hw::t8140, mem, mmu};

pub(super) const QID_COUNT: usize = QID_MAX as usize + 1;

/// Minimum render-channel capacity protected from compute-only allocation.
const COMPUTE_RENDER_RESERVE: usize = 16;

/// Stable physical-channel identity. The owner must not be reused within a
/// device lifetime, including after an unpublished allocation is cancelled.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub(crate) struct Id {
    qid: u8,
    owner: u64,
    engine: DataMaster,
}

impl Id {
    pub(crate) fn qid(self) -> u8 {
        self.qid
    }
    pub(crate) fn engine(self) -> DataMaster {
        self.engine
    }
}

#[derive(Copy, Clone)]
struct Identity {
    id: Id,
    published: bool,
}

/// One shared namespace for tiling, fragment and compute queues.
pub(crate) struct QueueIds {
    entries: [Option<Identity>; QID_COUNT],
    initial_render: [Option<Id>; 2],
    compute_publication: u64,
    leased: usize,
}

impl QueueIds {
    pub(crate) fn new() -> impl Init<Self, Error> {
        kernel::try_init!(Self {
            entries <- pin_init::init_array_from_fn(|_| None),
            initial_render: [None; 2],
            compute_publication: 0,
            leased: 0,
        })
    }

    /// Keep the initial render reservations until another engine needs their
    /// capacity. No graph receives these tokens, so an unpublished reservation
    /// can be replaced without changing an installed physical channel.
    pub(crate) fn reserve_initial_render(&mut self) -> Result {
        if self.leased != 0 {
            return Err(EINVAL);
        }
        for (qid, engine) in [DataMaster::Tiling, DataMaster::Fragment]
            .into_iter()
            .enumerate()
        {
            let id = self.reserve(qid as u64 + 1, engine, Some(qid as u8))?;
            self.initial_render[qid] = Some(id);
        }
        Ok(())
    }

    /// Reuses only an exact owner/engine match. Preferred placement is for
    /// bootstrapping; it does not reserve a partition for an engine.
    pub(crate) fn reserve(
        &mut self,
        owner: u64,
        engine: DataMaster,
        preferred: Option<u8>,
    ) -> Result<Id> {
        if owner == 0 {
            return Err(EINVAL);
        }
        if let Some(entry) = self
            .entries
            .iter()
            .flatten()
            .find(|entry| entry.id.owner == owner)
        {
            return if entry.id.engine == engine {
                Ok(entry.id)
            } else {
                Err(EINVAL)
            };
        }
        let initial = if engine == DataMaster::Compute && self.leased == QID_COUNT {
            self.initial_render
                .iter()
                .copied()
                .enumerate()
                .find_map(|(slot, id)| {
                    id.filter(|id| self.can_cancel_unpublished(*id))
                        .map(|id| (slot, id))
                })
        } else {
            None
        };
        // Existing render identities already satisfy this reservation. Counting
        // them twice can refuse compute even while physical QIDs remain free.
        // Published identities always retain their original lifetime rules.
        if engine == DataMaster::Compute {
            let render = self
                .entries
                .iter()
                .flatten()
                .filter(|entry| {
                    matches!(entry.id.engine, DataMaster::Tiling | DataMaster::Fragment)
                })
                .count();
            // Replacing an initial reservation consumes one render identity.
            // Include that transition when protecting render's minimum, and
            // count the replacement as available compute capacity.
            let replacement = usize::from(initial.is_some());
            if QID_COUNT - self.leased + replacement
                <= COMPUTE_RENDER_RESERVE.saturating_sub(render - replacement)
            {
                return Err(ENOSPC);
            }
        }
        let index = preferred
            .map(usize::from)
            .filter(|index| self.entries.get(*index).is_some_and(Option::is_none))
            .or_else(|| self.entries.iter().position(Option::is_none))
            .or_else(|| initial.map(|(_, id)| usize::from(id.qid)))
            .ok_or(ENOSPC)?;
        let id = Id {
            qid: index as u8,
            owner,
            engine,
        };
        self.entries[index] = Some(Identity {
            id,
            published: false,
        });
        if let Some((slot, _)) = initial {
            self.initial_render[slot] = None;
        } else {
            self.leased += 1;
        }
        Ok(id)
    }

    /// Ordered under the device mutex; no atomic operation on the submission path.
    pub(crate) fn next_compute_publication(&mut self) -> Result<u64> {
        self.compute_publication = self.compute_publication.checked_add(1).ok_or(EOVERFLOW)?;
        Ok(self.compute_publication)
    }

    /// Call after preflighting the complete installation, before any address
    /// becomes firmware-visible. A failed or partial installation retains it.
    pub(crate) fn publish(&mut self, id: Id) -> Result {
        let entry = self
            .entries
            .get_mut(usize::from(id.qid))
            .and_then(Option::as_mut)
            .ok_or(EINVAL)?;
        if entry.id != id {
            return Err(EINVAL);
        }
        entry.published = true;
        Ok(())
    }

    pub(crate) fn can_cancel_unpublished(&self, id: Id) -> bool {
        self.entries
            .get(usize::from(id.qid))
            .and_then(Option::as_ref)
            .is_some_and(|entry| entry.id == id && !entry.published)
    }

    pub(crate) fn cancel_unpublished(&mut self, id: Id) -> Result {
        let slot = self.entries.get_mut(usize::from(id.qid)).ok_or(EINVAL)?;
        match slot {
            Some(entry) if entry.id == id && !entry.published => {
                *slot = None;
                self.leased -= 1;
                Ok(())
            }
            _ => Err(EINVAL),
        }
    }
}

// Aliases drop before their backing. A compute queue is allocated low first;
// a render queue is allocated high first.
enum Backing {
    Compute {
        high: mmu::KernelMapping,
        low: KernelObject,
    },
    Render {
        low: mmu::KernelMapping,
        high: KernelObject,
    },
}

/// An installed queue's permanent ring. Client aliases are held by the bound
/// context graph and must be dropped before this object.
pub(crate) struct Queue {
    backing: Backing,
    id: Id,
    timestamp: KickTimestamp,
    previous: Option<KickTimestamp>,
}

/// A checked entry and destination held without changing the shared ring.
/// Discarding it leaves both the slot and the next timestamp unchanged.
pub(crate) struct PreparedKick<'a> {
    queue: &'a mut Queue,
    timestamp: KickTimestamp,
    entry: KickEntry,
    destination: *mut KickEntry,
}

impl PreparedKick<'_> {
    /// Publish translation updates after the queue configuration pointer and before its entry.
    pub(crate) fn prepare_compute_install(&self) -> Result {
        self.queue.prepare_compute_install()
    }

    /// Writes and fences the complete entry, then advances host state. The
    /// caller invokes this between the queue configuration and announcement.
    pub(crate) fn commit(self) -> KickTimestamp {
        // SAFETY: prepare_entry checked the complete destination range and
        // this token exclusively borrows the queue and retains its mapping.
        // The caller only prepares retired ring slots; no firmware access to
        // the new entry starts before the outer command is published.
        unsafe { self.destination.write_unaligned(self.entry) };
        fence(Ordering::SeqCst);
        self.queue.previous = Some(self.timestamp);
        self.queue.timestamp = self.timestamp.next();
        self.timestamp
    }
}

impl Queue {
    pub(crate) fn compute(alloc: &Allocator<'_>, id: Id) -> Result<Self> {
        if id.engine != DataMaster::Compute {
            return Err(EINVAL);
        }
        let low = alloc.lower(
            KICK_RING_SIZE,
            mmu::UAT_PGSZ as u64,
            mmu::PROT_GPU_SHARED_RW,
            CpuMap::WriteCombined,
        )?;
        let mut range = alloc.uat.geometry().kernel_range();
        range.start += t8140::dynamic::KERNEL_OFFSET;
        let high = low.alias_in(
            alloc.uat.kernel_vm(),
            range,
            mmu::UAT_PGSZ as u64,
            mmu::PROT_FW_SHARED_RW,
        )?;
        fence(Ordering::SeqCst);
        Ok(Self {
            backing: Backing::Compute { high, low },
            id,
            timestamp: KickTimestamp::FIRST,
            previous: None,
        })
    }

    pub(crate) fn render(
        alloc: &Allocator<'_>,
        id: Id,
        client: &mmu::Vm,
    ) -> Result<(Self, mmu::KernelMapping)> {
        if id.engine == DataMaster::Compute {
            return Err(EINVAL);
        }
        let high = alloc.kernel(
            KICK_RING_SIZE,
            mmu::UAT_PGSZ as u64,
            mmu::PROT_GPU_FW_SHARED_RW,
            CpuMap::WriteCombined,
        )?;
        let mut range = t8140::dynamic::LOWER;
        let (low, client) = loop {
            let low = high.alias_in(
                alloc.uat.kernel_lower_vm(),
                range.clone(),
                mmu::UAT_PGSZ as u64,
                mmu::PROT_GPU_SHARED_RW,
            )?;
            let end = low
                .iova()
                .checked_add(KICK_RING_SIZE as u64)
                .ok_or(EOVERFLOW)?;
            match high.map_alias(client, low.iova(), mmu::PROT_GPU_SHARED_RW) {
                Ok(client) => break (low, client),
                Err(error) if error == ENOSPC && end < range.end => {
                    drop(low);
                    range.start = end;
                }
                Err(error) => return Err(error),
            }
        };
        Ok((
            Self {
                backing: Backing::Render { low, high },
                id,
                timestamp: KickTimestamp::FIRST,
                previous: None,
            },
            client,
        ))
    }

    pub(crate) fn id(&self) -> Id {
        self.id
    }
    pub(crate) fn low_va(&self) -> u64 {
        match &self.backing {
            Backing::Compute { low, .. } => low.gpu_va(),
            Backing::Render { low, .. } => low.iova(),
        }
    }
    pub(crate) fn firmware_va(&self) -> u64 {
        match &self.backing {
            Backing::Compute { high, .. } => high.iova(),
            Backing::Render { high, .. } => high.gpu_va(),
        }
    }
    fn object(&mut self) -> &mut KernelObject {
        match &mut self.backing {
            Backing::Compute { low, .. } => low,
            Backing::Render { high, .. } => high,
        }
    }
    pub(crate) fn map_client(&mut self, vm: &mmu::Vm) -> Result<mmu::KernelMapping> {
        let address = self.low_va();
        self.object()
            .map_alias(vm, address, mmu::PROT_GPU_SHARED_RW)
    }

    /// Reuse a permanent client view only when every page names this ring.
    pub(crate) fn prepare_client_alias(&self, vm: &mmu::Vm) -> Result<Option<mmu::KernelMapping>> {
        let object = match &self.backing {
            Backing::Compute { low, .. } => low,
            Backing::Render { high, .. } => high,
        };
        object.prepare_alias(vm, self.low_va(), mmu::PROT_GPU_SHARED_RW, true)
    }

    /// Both aliases are owned mappings of the same full ring. Publish their
    /// page-table updates to each live translation regime before installation.
    pub(crate) fn prepare_compute_install(&self) -> Result {
        if self.id.engine != DataMaster::Compute {
            return Err(EINVAL);
        }
        mem::sync();
        for asid in 0..4 {
            mem::tlbi_range_or_asid(asid, self.low_va() as usize, KICK_RING_SIZE);
            mem::tlbi_range_or_asid(asid, self.firmware_va() as usize, KICK_RING_SIZE);
        }
        mem::sync();
        Ok(())
    }

    /// Detaches the implicit dependency after firmware recovery, retaining
    /// the monotonically advancing 40-bit producer.
    pub(crate) fn clear_parent_after_recovery(&mut self) {
        self.previous = None;
    }

    pub(crate) fn timestamp(&self) -> KickTimestamp {
        self.timestamp
    }
    pub(crate) fn parent(&self) -> KickTimestamp {
        self.previous.unwrap_or_else(|| match self.id.engine {
            DataMaster::Compute => KickTimestamp::ZERO,
            _ => self.timestamp.prev(),
        })
    }
    pub(crate) fn prepare_entry(&mut self, args: &KickArgs<'_>) -> Result<PreparedKick<'_>> {
        if args.qid != self.id.qid
            || args.timestamp != self.timestamp
            || args.parent != self.parent()
        {
            return Err(EINVAL);
        }
        let entry = KickEntry::new(args)?;
        let destination = self
            .object()
            .pointer(args.timestamp.entry_offset(), size_of::<KickEntry>())?
            .cast();
        let timestamp = self.timestamp;
        Ok(PreparedKick {
            queue: self,
            timestamp,
            entry,
            destination,
        })
    }
}
