#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR MIT
"""Compile actual G17 admission, queue-ID, and unused-graph eligibility methods.

Run with Python and rustc; temporary host binaries are removed after the checks.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

DRIVER = Path(__file__).resolve().parents[2] / 'drivers/gpu/drm/asahi'

def admission_tests(root, source):
    s=(source/'g17/queue/admission.rs').read_text();cfg=(source/'hw/t8140.rs').read_text()
    def item(start):
     at=s.index(start);begin=s.index('{',at);depth=1;end=begin+1
     while depth:
      depth+=(s[end]=='{')-(s[end]=='}');end+=1
     return s[at:end]
    constants='\n'.join(re.search(r'pub\(crate\) const '+name+r': [^;]+;',cfg).group(0) for name in ['RENDER_SLOTS','RENDER_PAIRS_PER_OWNER','RENDER_DEPTH'])
    mmu=(source/'mmu.rs').read_text()
    limits='\n'.join(re.search(r'const '+name+r': [^;]+;',mmu).group(0) for name in ['EXECUTION_CONTEXT_MASK','MAX_EXECUTION_CONTEXTS'])
    header='''#![allow(dead_code, unused_macros)]
    type Result<T=()> = std::result::Result<T,i32>;
    const ENOSPC:i32=-28; const EIO:i32=-5;
    macro_rules! resource_exhausted { ($s:literal)=>{ENOSPC}; }
    type KVec<T> = Vec<T>; struct InFlight;
    mod cfg {\n'''+constants+'}\n'+limits+'\n'
    methods=['const fn new','fn exhausted','fn refusal','fn advance','fn take_idle','fn acquire','fn claim_sibling','fn reassign_unused_spare','fn warmable_sibling','fn borrow_warm_sibling','fn cancel_empty_slot','fn release']
    code=header+item('struct State')+'\nimpl State {\n'+re.search(r'    const MASK: u64 = .*;',s).group(0)+'\n'+'\n'.join(item(n) for n in methods)+'\n}\n'+'''
    fn main(){
     let mut state=State::new();
     for owner in 1..=32 {
      assert!(state.refusal(owner).is_ok());
      let slot=state.acquire(owner).expect("existing owner fits");
      state.release(slot);
     }
     assert_eq!(state.active,0,"all submitted work retired");
     let retained=state.owners.iter().filter(|o|**o!=0).count();
     assert_eq!(retained,63);
     assert_eq!(state.refusal(33),Err(ENOSPC));
     assert!(state.acquire(33).is_none());
     println!("confirmed: 32 open idle owners retain all 63 usable pairs; owner33 receives ENOSPC");
     // Yield one unused spare for each additional logical owner. The runtime
     // separately proves that these physical graphs have never been published.
     for owner in 33..=u64::from(MAX_EXECUTION_CONTEXTS) {
      let slot=(1..cfg::RENDER_SLOTS).find(|slot|state.reassign_unused_spare(*slot as u8,None,Some(owner))).unwrap();
      assert_eq!(state.acquire(owner),Some(slot as u8));state.release(slot as u8);
     }
     for owner in 1..=u64::from(MAX_EXECUTION_CONTEXTS) {let slot=state.acquire(owner).unwrap();state.release(slot);}
     assert!(state.owners.iter().all(|owner|*owner<=u64::from(MAX_EXECUTION_CONTEXTS)));
     println!("fixed: all{} logical owners render and resume without reducing slot count or depth",MAX_EXECUTION_CONTEXTS);
     let mut guarded=State::new();let first=guarded.acquire(1).unwrap();guarded.release(first);
     let spare=guarded.owners.iter().enumerate().find(|(s,o)|**o==1&&*s!=first as usize).unwrap().0;
     guarded.take_idle(spare,1);
     assert!(!guarded.reassign_unused_spare(spare as u8,Some(1),Some(2)),"warm borrower excludes reclamation");
     guarded.release(spare as u8);
     assert!(!guarded.reassign_unused_spare(spare as u8,Some(3),Some(2)),"owner mismatch");
     guarded.quarantined |= 1u64<<first;
     assert!(!guarded.reassign_unused_spare(spare as u8,Some(1),Some(2)),"keep another usable pair");
     guarded.quarantined=0;
     assert!(guarded.reassign_unused_spare(spare as u8,Some(1),Some(2)));
     assert!(!guarded.reassign_unused_spare(first,None,Some(2)),"never take an owner's last pair");
     assert!(!guarded.reassign_unused_spare(0,None,Some(3)));
     assert!(!guarded.reassign_unused_spare(64,None,Some(3)));
     guarded.released |= 1u64<<first;
     assert!(guarded.reassign_unused_spare(first,Some(1),None),"closed owner's unused last slot can be freed for compute");
     let second=guarded.acquire(3).unwrap();guarded.release(second);
     let spare3=guarded.owners.iter().enumerate().find(|(s,o)|**o==3&&*s!=second as usize).unwrap().0;
     assert!(guarded.reassign_unused_spare(spare3 as u8,Some(3),Some(2)));
     let fourth=guarded.acquire(4).unwrap();guarded.release(fourth);
     let spare4=guarded.owners.iter().enumerate().find(|(s,o)|**o==4&&*s!=fourth as usize).unwrap().0;
     assert!(!guarded.reassign_unused_spare(spare4 as u8,Some(4),Some(2)),"destination retains at most two channels");
     let mut recency=State::new();recency.owners[9]=1;recency.owners[2]=1;
     recency.take_idle(9,1);let epoch=recency.epochs[9];
     assert_eq!(recency.borrow_warm_sibling(1,9),Some(2));
     assert_eq!(recency.epochs[2],0);assert_eq!(recency.epochs[9],epoch);
     recency.release(2);recency.release(9);
     for _ in 0..32 {let slot=recency.acquire(1).unwrap();assert_eq!(slot,9);recency.release(slot);}
     let a=recency.acquire(1).unwrap();let b=recency.acquire(1).unwrap();assert_ne!(a,b);
     recency.release(b);recency.released|=1u64<<b;
     assert_eq!(recency.borrow_warm_sibling(1,a),None,"never reopen a closed sibling while warming");
     recency.release(a);
     let mut empty=State::new();let slot=empty.acquire(1).unwrap();
     assert!(!empty.cancel_empty_slot(slot,2));
     empty.refs[slot as usize]+=1;assert!(!empty.cancel_empty_slot(slot,1));empty.release(slot);
     assert!(empty.cancel_empty_slot(slot,1));empty.release(slot);
     assert_eq!(empty.owners[slot as usize],0);assert_eq!(empty.active&(1u64<<slot),0);
     println!("recency: serial work keeps its used pair; overlapping work retains both channels; warm close and empty-reservation races protected");
     println!("guards: live warming, expected owner, remaining usable pair, existing destination, and reserved slots");
     let mut state=State::new();
     for owner in 1..=32 {let slot=state.acquire(owner).unwrap();state.release(slot);}
     // Every existing owner still resumes, so this is held capacity, not live GPU work.
     for owner in 1..=32 {let slot=state.acquire(owner).unwrap();state.release(slot);}
     // Logical close makes the retained pair eligible; no firmware behavior modeled.
     let slot=state.owners.iter().position(|o|*o==1).unwrap();
     state.released |= 1u64<<slot;
     assert!(state.refusal(33).is_ok());
     assert_eq!(state.acquire(33),Some(slot as u8));
     println!("control: explicitly released idle ownership admits a new owner");
    }
    '''
    fixture=root/'admission-capacity-fixed-controls.rs';fixture.write_text(code)
    subprocess.run([os.environ.get('RUSTC', 'rustc'),'--edition=2021',str(fixture),'-o',str(root/'admission-capacity-fixed-controls')],check=True)
    subprocess.run([str(root/'admission-capacity-fixed-controls')],check=True)

def queue_tests(root, source):
    source = source / "g17"
    def item(s,start):
     at=s.index(start);begin=s.index('{',at);depth=1;end=begin+1
     while depth:
      depth+=(s[end]=='{')-(s[end]=='}');end+=1
     return s[at:end]
    s=(source/'kick.rs').read_text()
    fw=(source/'fw/kick.rs').read_text()
    constants=re.search(r'const QID_MAX: [^;]+;',fw).group(0)+'\n'+'\n'.join(re.search(r'const '+name+r': [^;]+;',s).group(0) for name in ['QID_COUNT','COMPUTE_RENDER_RESERVE'])
    header='''#![allow(dead_code, unused_macros)]
    type Result<T=()> = std::result::Result<T,i32>;
    const ENOSPC:i32=-28; const EINVAL:i32=-22; const EOVERFLOW:i32=-75;
    macro_rules! resource_exhausted { ($s:literal)=>{ENOSPC}; }
    #[derive(Clone,Copy,PartialEq,Eq,Debug)] enum DataMaster {Compute,Tiling,Fragment}
    #[derive(Clone,Copy,PartialEq,Eq,Debug)] struct Id {qid:u8,owner:u64,engine:DataMaster}
    #[derive(Clone,Copy)] struct Identity {id:Id,published:bool}
    '''+constants+'\n'+item(s,'pub(crate) struct QueueIds')+'\nimpl QueueIds {\n'+''.join(item(s,'pub(crate) fn '+name+'(')+'\n' for name in ['reserve_initial_render','reserve','publish','can_cancel_unpublished','cancel_unpublished'])+'}\n'
    test='''
    fn fresh()->QueueIds {QueueIds {entries:[None;QID_COUNT],initial_render:[None;2],compute_publication:0,leased:0}}
    fn main(){
     for render in [0,2,16,32,112,126] {
      let mut q=fresh();
      for i in 0..render {q.reserve(i as u64+1,if i%2==0 {DataMaster::Tiling}else{DataMaster::Fragment},None).unwrap();}
      let mut compute=0;
      while q.reserve(1000+compute,DataMaster::Compute,None).is_ok(){compute+=1;}
      assert_eq!(compute as usize,QID_COUNT-render.max(COMPUTE_RENDER_RESERVE));
     }
     let mut q=fresh();let first=q.reserve(1,DataMaster::Tiling,None).unwrap();
     assert!(q.can_cancel_unpublished(first));q.publish(first).unwrap();
     assert!(!q.can_cancel_unpublished(first));assert_eq!(q.cancel_unpublished(first),Err(EINVAL));assert_eq!(q.leased,1);
     let unused=q.reserve(2,DataMaster::Fragment,None).unwrap();q.cancel_unpublished(unused).unwrap();assert_eq!(q.leased,1);
     let next=q.reserve(3,DataMaster::Fragment,None).unwrap();assert_eq!(next.qid,unused.qid);
     assert!(!q.can_cancel_unpublished(unused));assert_eq!(q.cancel_unpublished(unused),Err(EINVAL));
     println!("queue IDs: six render/compute capacity boundaries; compute-only render reserve retained; published and stale identities protected");
     // Reproduce the hardware snapshot: 124 installed render identities, two
     // installed compute identities, and two initial reservations fill128 IDs.
     let mut q=fresh();q.reserve_initial_render().unwrap();
     assert_eq!(q.reserve_initial_render(),Err(EINVAL));
     let initial=q.initial_render;
     for owner in 1000..1002 {let id=q.reserve(owner,DataMaster::Compute,Some(4)).unwrap();q.publish(id).unwrap();}
     let mut installed=Vec::new();
     for owner in 3..127 {let id=q.reserve(owner,if owner%2==0 {DataMaster::Tiling}else{DataMaster::Fragment},None).unwrap();q.publish(id).unwrap();installed.push(id);}
     assert_eq!(q.leased,QID_COUNT);
     for (n,old) in initial.into_iter().enumerate() {
      let old=old.unwrap();let id=q.reserve(2000+n as u64,DataMaster::Compute,None).unwrap();
      assert_eq!(id.qid,old.qid);assert_eq!(q.leased,QID_COUNT);
      assert!(!q.can_cancel_unpublished(old));assert_eq!(q.cancel_unpublished(old),Err(EINVAL));
      q.publish(id).unwrap();
     }
     assert_eq!(q.reserve(3000,DataMaster::Compute,None),Err(ENOSPC));
     for id in installed {assert!(!q.can_cancel_unpublished(id));assert_eq!(q.entries[id.qid as usize].unwrap().id,id);}
     // A bootstrap token that became published is never replaced.
     let mut q=fresh();q.reserve_initial_render().unwrap();let protected=q.initial_render[0].unwrap();q.publish(protected).unwrap();
     for owner in 3..129 {q.reserve(owner,DataMaster::Tiling,None).unwrap();}
     assert_eq!(q.leased,QID_COUNT);
     assert_eq!(q.reserve(1000,DataMaster::Compute,None).unwrap().qid,1);
     assert_eq!(q.reserve(1001,DataMaster::Compute,None),Err(ENOSPC));
     assert_eq!(q.entries[0].unwrap().id,protected);
     // Replacement must not consume the protected render minimum.
     let mut q=fresh();q.reserve_initial_render().unwrap();
     for owner in 3..17 {q.reserve(owner,DataMaster::Tiling,None).unwrap();}
     for owner in 1000..1112 {q.reserve(owner,DataMaster::Compute,None).unwrap();}
     assert_eq!(q.leased,QID_COUNT);assert_eq!(q.reserve(2000,DataMaster::Compute,None),Err(ENOSPC));
     assert!(q.initial_render.iter().all(Option::is_some));
     // Ordinary free capacity leaves the initialization reservations untouched.
     let mut q=fresh();q.reserve_initial_render().unwrap();let initial=q.initial_render;
     let id=q.reserve(1000,DataMaster::Compute,Some(4)).unwrap();assert_eq!(id.qid,4);assert_eq!(q.initial_render,initial);
     println!("initial reservations: full-namespace compute admission restored; physical count unchanged; published and stale identities plus render minimum protected");

    }
    '''
    fixture=root/'queue-id-capacity-controls.rs';fixture.write_text(header+test)
    subprocess.run([os.environ.get('RUSTC', 'rustc'),'--edition=2021',str(fixture),'-o',str(root/'queue-id-capacity-controls')],check=True)
    subprocess.run([str(root/'queue-id-capacity-controls')],check=True)
    s=(source/'queue/render/parked.rs').read_text();guard=item(s,'pub(crate) fn can_yield_unused')
    fixture=root/'unused-render-guard-controls.rs';fixture.write_text('''#![allow(dead_code, unused_macros)]
    type Result<T> = std::result::Result<T,i32>;
    struct Context;
    struct Pair {owner:Option<u64>,ordinal:u64,registered:Option<u8>,outer_started:u8,previous:Option<()>,graph_started:bool,backing:Result<bool>}
    impl Pair {
     fn context(&self)->&Context {&Context}
     fn can_park_backing(&self,_:&Context)->Result<bool>{self.backing}
    '''+guard+'''
    }
    fn fresh()->Pair {Pair {owner:Some(1),ordinal:0,registered:None,outer_started:0,previous:None,graph_started:false,backing:Ok(true)}}
    fn main(){
     assert_eq!(fresh().can_yield_unused(),Ok(true));
     let mut p=fresh();p.owner=None;assert_eq!(p.can_yield_unused(),Ok(true));
     let mut p=fresh();p.ordinal=1;assert_eq!(p.can_yield_unused(),Ok(true));
     let mut p=fresh();p.registered=Some(2);assert_eq!(p.can_yield_unused(),Ok(true));
     let mut p=fresh();p.outer_started=4;assert_eq!(p.can_yield_unused(),Ok(true));
     let mut p=fresh();p.graph_started=true;assert_eq!(p.can_yield_unused(),Ok(false));
     let mut p=fresh();p.backing=Ok(false);assert_eq!(p.can_yield_unused(),Ok(false));
     let mut p=fresh();p.backing=Err(-5);assert_eq!(p.can_yield_unused(),Err(-5));
     println!("unused graph: eight eligibility/error controls protect every publication boundary");
    }
    ''')
    subprocess.run([os.environ.get('RUSTC', 'rustc'),'--edition=2021',str(fixture),'-o',str(root/'unused-render-guard-controls')],check=True)
    subprocess.run([str(root/'unused-render-guard-controls')],check=True)

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='asahi-resource-tests-') as temporary:
        admission_tests(Path(temporary), DRIVER)
        queue_tests(Path(temporary), DRIVER)
