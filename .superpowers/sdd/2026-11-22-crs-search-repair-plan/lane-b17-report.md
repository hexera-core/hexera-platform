# Lane B17: the two-region, multi-VM, per-arm launcher, as code

B14 was a diff in a report. This is the merged thing. It is a SEPARATE launcher (`snap-v4/`), so the single-VM
`snap-v1r/launch.sh` keeps running v3c untouched: **no file the snapshot runner is executing was changed, and no
shared eval code was changed either** — not `evalspec.py`, not `report.py`, not `collect.py`.

## What was added

| file | what it does |
|---|---|
| `snap-v4/prep.py` | per-arm shard groups, per-arm costs, the throttle cap, the VM/region packing, caps, manifest |
| `snap-v4/vm_run.sh` | runs only the `(group, shard)` units `vm_plan.json` gives THIS VM, each its own container |
| `snap-v4/launch.sh` | multi-region VM creation with per-region quota gates, parallel run, partial sync, collect |
| `snap-v4/regroup.py` | presents the groups as ONE wave per split, so report.py's gates apply unchanged |
| `snap-v4/post.py` | regroup, then report.py and triage.py on the merged view, with v1r's sensitivity behaviour |
| `snap-v4/profiles/v4.env` | the shape: regions, vCPU per region, target minutes, prior rows = **v2**, not v1 |
| `snap-v4/test_snap_v4.py` | 10 hermetic tests, no VM and no model |

## The dry run, on v2's real measured walls

```
OK profile=v4 groups=dev_ai:35s/49.8min dev_det:35s/48.5min dev_staged:13s/48.0min dev_input:8s/48.0min clean:1s/17.6min
   92 shard slots on 5 VMs / 184 vCPU in ['us-central1', 'us-east1']; run 49.8 min + boot 9.0 min -> 64.8 min total
   198.7 vCPU-h ~ $9.34; model caps $49.5 of $49.95; ai shards 35 (ceiling 37)
```

B14 projected 92 shards, 184 vCPU, ~49 min, ~$9.50, zero quota asks. The code lands on 92 shards, 184 vCPU,
49.8 min of run and $9.34, and every VM's packed load per slot is 46–49 minutes, so nothing trails at the end:

| VM | machine | slots | predicted / slot |
|---|---|---|---|
| us-central1-1 | c2d-standard-56 | 28 | 47.1 min |
| us-central1-2 | c2d-standard-32 | 16 | 47.9 min |
| us-east1-1 | c2d-standard-56 | 28 | 46.1 min |
| us-east1-2 | c2d-standard-32 | 16 | 47.9 min |
| us-east1-3 | c2d-standard-8 | 4 | 48.8 min |

`dev_ai` wanted 35 shards and the ceiling is 37, so the throttle does not bind at this file count; `prep.py` caps it
anyway and records `capped_by_throttle` when it does. 121 of the 150 dev files had no `search_det` prior row and were
sized by the size model scaled by that arm's measured share; 29 came from v2's own measured walls.

## The cross-region numbers you asked for, measured

One `c2d-standard-4` per region, 25 sequential requests against the real bucket from each, then both deleted.

| | us-central1 | us-east1 | delta |
|---|---|---|---|
| verdict-cache `exists` (median) | 51.4 ms | 92.6 ms | +41 ms |
| verdict-cache `download` (median) | 58.2 ms | 252.7 ms | +195 ms |
| **one cache GET** (`GcsStore.get` does both) | **~110 ms** | **~345 ms** | **+235 ms** |
| input staging, 20 files / 71 MB | 5 s | 6 s | +1 s |
| `apt-get install docker.io` | 13 s | 13 s | — |
| image pull, 4.34 GB, digest-pinned | **50 s** | **50 s** | **none** |

Three things follow.

1. **The image pull is not a cross-region penalty at all.** 50 s in both. The registry repo is 187 GB but this image
   is 4.34 GB, and the bottleneck is local decompression, not the pull. The 9-minute boot figure in the manifest is
   therefore conservative; VM create plus `--max-run-duration` metadata plus docker install plus pull measured about
   2 minutes of real work, and the rest is the create/ssh-ready wait.
2. **Staging is free.** 241 MB is about 20 s cross-region.
3. **The cache get is the only real cross-region cost, and it is small against this workload.** A dev file's search
   arm does on the order of 35 verdict gets (D15: ~20 candidates per file plus confirms), so an east shard pays
   about **8 s per file**, against a mean 1714 s of four-arm work: **~1%**, or about +40 s on a 48-minute shard.
   48 of the 92 slots are in us-east1, so the wave's wall moves by well under a minute. **The 49-minute figure
   survives the measurement.**

What would overturn that: a shard doing thousands of gets. It does not — but `prov["verdict_cache"]` (lane B15) now
records hits, misses and errors per shard, so the first v4 wave will say the real get count instead of my estimate,
and if an east shard's counts come back an order of magnitude higher than a central one's, the two-region shape
should be reconsidered on that evidence.

## The part that bit, and how it was handled without weakening a gate

`post.py` and `analyze.py` assume one wave id per split, as you warned. The deeper problem is underneath them:

- `collect.check_partition` refuses a file that appears in two shard directories of one wave. With per-arm groups a
  dev file's four arms live in four group directories, so a naive merge is exactly the thing that check forbids.
- `report.load_wave` requires every shard's provenance to name the wave's four arms, so a group shard that ran only
  `search_ai` would be refused.

Both checks are right: they catch a wave cut from two plans, and an arm that silently did not run. So I did not
touch them. `regroup.py` rewrites the groups into one directory per split whose partition is a single plan and whose
shards carry all four arms, and every gate then applies to the merged view unchanged. Nothing is invented: rows are
copied byte for byte; `commit`, `image`, `wave`, `split` and `file_timeout_s` must AGREE across groups or it
refuses; the merged `exit_code` is the worst of the contributing shards, so **an unfinished or failed group is
refused rather than reported as a whole wave**; and each merged shard's provenance keeps every group's own
`n_shards`, `shard_plan`, `model_throttle` and ledger path under `groups`, so the regrouping is auditable.

Tests cover: the merge (four arms per merged shard, no file in two shards, the plan written), a failed group
(refused, exit code carried), and groups that disagree on the commit (refused).

## What the runner has to decide, and what I did not do

- **`REGIONS` is a profile variable now**, `<region>:<vcpu>:<zones>`. The vCPU figure is what the LIVE quota allows,
  and `launch.sh` re-checks C2D_CPUS **and** the region-wide CPUS against the plan before creating anything, per
  region, and refuses rather than half-creating.
- **`OPENAI_API_KEY` reaches each VM** the way v1r does it, over the ssh channel into `~/snap/secret.env`, shredded
  at the end of `vm_run.sh`; every VM is deleted in `launch.sh`'s EXIT trap, and the surviving list is written to
  `vm_surviving.txt`.
- **`analyze.py` is untouched and still reads one wave id per split** — it now reads the MERGED directory, which is
  one wave id per split, so it needs no change. If it is pointed at a group directory instead, it will see one arm
  and say so.
- I have **not launched anything**. v3c is running on the single-VM launcher and I did not go near it. The next
  wave is the runner's to launch; `--dry-run` does everything up to the first VM.
