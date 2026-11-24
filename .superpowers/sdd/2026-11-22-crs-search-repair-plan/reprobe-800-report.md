# reprobe-800 report: partial engine-form re-probe, provisional dev_v2 / clean_v2, v1 relabel

Branch `sdd/reprobe-800` (worktree `sdd-worktrees/reprobe-800`), not pushed, not merged. No model called, nothing deployed.
Labels come from wave `reprobe-800-2` at commit **`c8f5235f`**. That commit is the feature tip `2f70c734` (Lane D's
engine_form labels, gmsh pin, wall-stop fix; Task 55's 1800 s file wall / 900 s check_valid wall) merged into this
branch. Image `crsv4-mesh@sha256:633e1c3bfd8e2c0fb97b166fa28ff834d0433b84ca8e77c24eb4b00c83ece361`
(image commit 78880908155a; `src/` runs from the code archive; the launcher's drift check found no toolchain change).
**PROVISIONAL**: one run, one partial sample. The real splits come from the full re-probe.

## Deliverables (`devtools/validation/corpora/v4/provisional_v2/`)
| file | what |
|---|---|
| `PROVISIONAL_dev_v2.json` | 150 failing files, stratified by `reprobe.build_splits(dev=150, heldout=0, clean=40, seed=20261122)` |
| `PROVISIONAL_clean_v2.json` | 40 full-tier certified-clean files |
| `PROVISIONAL_dev_v1r.json`, `PROVISIONAL_clean_v1r.json` | Task 43's 30 + 8 files, every row replaced by the engine-form row; membership kept; changes listed in the header (`label_changes`) |
| `sample_manifest.json`, `pool_subset.json` | the sample: seed, quotas, size bands, strata, held-out exclusion, every file |
| `probe_records.jsonl` (754) / `excluded.jsonl` (46) | every engine-form row (plus size band, outcome, kind, failing members, near-threshold flag, v2 split); every exclusion |
| `SUMMARY.json`, `HELDOUT_CHECK.txt` | counts below; the held-out proof |
| `origform_partial_rows_09f172ef.jsonl` | 82 ORIGINAL-form rows from the stopped first wave, kept as form-gap evidence only |

The split files have the same schema as `PROVISIONAL_dev.json` (`split`, `seed`, `strata`, `files` of ProbeRow, plus a
`PROVISIONAL` field). `run_arm.split_entries` loads all four against the committed pool: 150 / 40 / 30 / 8 files.
`evalspec.WAVES` has two new normal waves, `snap_v2_dev` and `snap_v2_clean`, with arms input_only, staged,
search_det and search_ai.

## Held-out
- **Definition.** The only sealed held-out set in the repo is Task 43's
  `provisional_sample_task43/PROVISIONAL_heldout.json`: 25 files plus their part groups `nist-ctc-02` and
  `nist-stc-10`. evalspec has no hash rule: `build_splits` draws the full corpus's held-out from the full re-probe's
  failing set.
- **Exclusion.** The sampler removed those 25 files and the 3 group siblings (28 pool files) before drawing.
- **Proof.** The script output is in `HELDOUT_CHECK.txt`:
  - held-out sha256 overlap with the 800 probed files (rows plus excluded) is **0**;
  - held-out group members in the subset pool: **0**;
  - held-out overlap (sha and group) with dev_v2, clean_v2, dev_v1r and clean_v1r: **0** each;
  - dev_v2 ∩ clean_v2 = 0; v2 ∩ v1 = 0, so v2 is additive to v1.
- **Caveat.** The FULL wave's `build_splits` must exclude this branch's dev_v2/clean_v2 (and v1) files from its
  held-out draw. Otherwise a file tuned on here could be sealed later. Nothing enforces this yet.

## Sample design
- **Stratifier.** Source x file-size band, using bands <100 kB, 100 kB-1 MB, 1-10 MB and >=10 MB. File size is the
  only pre-probe feature available for every file; evalspec's cost model uses it for the same reason. The diagonal
  bands Task 43 and Lane D used would need a CAD read of every candidate.
- **Allocation.** Within a source, files are allocated over bands with reprobe's largest-remainder `_apportion`, then
  drawn with reprobe's seeded `_shuffled`. Seed **20261124**, recorded in the manifest.
- **Weights.** nist and occt are taken whole (39 and 31 after held-out). abc_pass gets 240 random files plus 12 from
  v1; abc_fail gets the rest, 459 random plus 19 from v1. Sampling fractions: abc_fail 4.8%, abc_pass 28.4%,
  nist 89%, occt 94%.
- **Why abc_pass is oversampled about 6x.** The prior is measured: Task 43 and Lane D certified abc_pass clean 12 of
  29 times and abc_fail 0 of 82. A pool-proportional 800 would yield about 25 clean files. No source was oversampled
  toward failing: abc_fail already fails about 100% of the time.
- **v1 files.** The 38 v1 files (Task 43's dev and clean) were INCLUDED and marked `v1`. They are excluded from the v2
  draw and relabelled into v1r.

## Results (754 conclusive + 46 excluded = 800)
Overall: failing 592, clean 152, no_solids 6, unbounded 4 (conclusive, all out of scope), excluded 46.

The pool-weighted failing rate among failing+clean files is **94.6%**. In the raw sample it is 79.6%, because
abc_pass is oversampled.

| source | failing | clean | other | excluded | fail rate (failing / failing+clean) |
|---|---|---|---|---|---|
| abc_fail | 429 | 3 | no_solids 3, unbounded 4 | 39 | 99.3% |
| abc_pass | 106 | 144 | - | 2 | 42.4% |
| nist | 34 | 4 | - | 1 | 89.5% |
| occt | 23 | 1 | no_solids 3 | 4 | 95.8% |

Per stratum (failing / clean / excluded; other outcomes in brackets):

| stratum | failing | clean | excluded | other |
|---|---|---|---|---|
| abc_fail <100k | 67 | 3 | 0 | 1 no_solids |
| abc_fail 100k-1M | 188 | 0 | 2 | - |
| abc_fail 1-10M | 148 | 0 | 20 | 2 unbounded |
| abc_fail >=10M | 26 | 0 | 17 | 2 no_solids, 2 unbounded |
| abc_pass <100k | 34 | 118 | 0 | - |
| abc_pass 100k-1M | 54 | 26 | 2 | - |
| abc_pass 1-10M | 17 | 0 | 0 | - |
| abc_pass >=10M | 1 | 0 | 0 | - |
| nist <100k | 0 | 2 | 0 | - |
| nist 100k-1M | 20 | 1 | 0 | - |
| nist 1-10M | 14 | 1 | 1 | - |
| occt <100k | 0 | 1 | 0 | 2 no_solids |
| occt 100k-1M | 20 | 0 | 0 | 1 no_solids |
| occt 1-10M | 3 | 0 | 2 | - |
| occt >=10M | 0 | 0 | 2 | - |

**Failure kinds** (layer/coarse kind, 592 failing):

| kind | files |
|---|---|
| panel/geometry | 215 |
| valid/body_overlap | 100 |
| panel/quality | 66 |
| valid/self_intersection | 61 |
| valid/valid_other | 55 |
| panel/mesher | 49 |
| valid/open_shell | 44 |
| faithful | 2 |

Fine families: overlapping_facets 117, body_overlap 100, low_quality 66, self_intersection 61, brep_invalid 55,
edge_through_face 55, open_shell 44, no_volume 32, hxt_3d_failed 30, timeout_crash 8, other 6.

**Panel members failing** on panel-layer failures: 1: 51, 2: 70, 3: 157, 4: 52.

**dev_v2 (150):**
- by source: abc_fail 109, abc_pass 27, nist 8, occt 6;
- largest strata: abc_fail panel/geometry 46, abc_fail body_overlap 21, abc_fail self_intersection 14, abc_fail
  valid_other 12, abc_pass panel/quality 11;
- strata are proportional to the SAMPLE's failing set, so abc_pass is over-represented relative to the pool
  (18% vs about 8%).

**clean_v2 (40):** abc_pass 38, abc_fail 1, nist 1.

## Excluded stratum (46 = 5.75%, over collect's 2% limit)
- **By reason:** file_timeout at 1800 s: 39; checker_unavailable: 6 (crashed check_valid 2, check_valid 900 s
  timeout 2, read_step_xcaf failure 1, "6802 body pairs" 1); inconclusive panel: 1.
- **By source:** abc_fail 39, occt 4, abc_pass 2, nist 1.
- **Concentration:** almost all are files >= 1 MB; 37 of the 46 are abc_fail 1-10 MB or >= 10 MB.
- **Every file is named** in `excluded.jsonl`.
- **`collect.py --report-only` (rc 2) REFUSED** at 5.8% excluded and wrote no split. The provisional splits were cut
  by `partial_reprobe.py splits` (build_splits over the conclusive rows), so no `--allow-excluded` override was needed
  or used. The bias stands: the failing set under-represents files the checker cannot finish in 1800 s.
- **Unbounded, misclassified as excluded (Lane D's known D4 issue):** 0 identifiable from the available records. None
  of the 46 is in the 82 original-form rows, in Task 43's records or in Lane D's diagonal sample, so there is no
  unbounded label or absurd diagonal to match against. Lane D's example `bfc8d0b5...` is not in this sample. The 39
  file_timeouts are therefore UNVERIFIED for unboundedness; a diagonal-only pass over them would settle it. None of
  them is in dev_v2/clean_v2: excluded files have no row, and the 4 conclusive `unbounded` rows are kept out by
  build_splits.

## The 38 v1 files relabelled (v1r)
**clean_v1r:** all 8 identical: still clean, same panel.

**dev_v1r (30):**

| change | files |
|---|---|
| verdict changed | **3** |
| panel member status changed, same layer and hard code | 4 |
| coarse-kind label changed only, identical panel statuses | 11 |
| unchanged | 12 |

The three verdict changes:
1. `abc/.../00611852_..._step_008`: panel/mesher -> **CLEAN**. gmsh_alt fail -> pass; this is now a clean file
   inside dev_v1r.
2. `00573880_..._step_005`: valid/self_intersection -> panel/mesher. Layer 1 now passes.
3. `00528484_..._step_003`: valid/open_shell -> valid/valid_other. Hard code changed; still layer 1.

The four panel-only changes (still failing in the same layer):
- 00533660: crashed <-> fail on 2 members;
- 00519727, occt `bug32314_corrupted`, 00606843: gmsh_alt fail -> crashed.

The 11 kind-only changes are panel/mesher -> geometry or quality. They come from today's family classifier
(HXT/ranked families) re-labelling Task 43's old `other` rows. The panel statuses are identical, so these are not
verdict flips.

Every change is listed in `label_changes` in the v1r headers. Membership is v1's own, so the lead decides whether
00611852 stays in dev.

## Form gap: original-form vs engine-form
The 82 original-form rows from the stopped wave (all abc_fail) compared with the same files' engine-form rows:
- identical layer and panel: 61;
- same layer, a member's status changed: 19;
- layer/clean changed: 2.
  - `1c9153fac870`: valid -> panel;
  - `e26f6abcb88a`: panel -> **clean** (gmsh_prod fail -> pass).

Mixed causes: the form, plus the gmsh pin. Evidence only, not split labels.

## Near-threshold and fragile labels
- **Near-threshold rule:** any panel member's min SICN within x3 of the 0.01 floor (0.0033-0.03). It applies to 233
  of 754 rows: 58 of dev_v2 and **10 of clean_v2**. Lane D's gmsh pin should make these reproducible run to run on
  one form, but this was not re-measured here.
  - The 10 clean_v2 files: 07088e489198, 3c6b613a6cbf, 5111e67e2347, 72e4e1b22d08, 7bbad431704b, 8dbb7ea338a2,
    983a5a2b23bf, a7b4cc2fc62b, df145cf6cd4e, fa9d0150c4f8.
  - Closest to the floor: 0.0123, 0.0125, 0.0138, 0.0151.
- **Single-member panel failures:** 51 rows, 14 in dev_v2. Ten of those 14 are also near-threshold: 2a387cc52f65,
  321517229eff, 3596803faa49, 71777c28da76, 7b9283b4288d, d2cb9938a2d7, d92ecb192f7f, dd9e77d1867e, ddd86130e55e,
  f3819472cf82. These are the dev files most likely to flip.

## Compute, wall, $
**First wave `reprobe-800-1`** at `09f172ef` (original form):
- stopped at about 10% on the lead's instruction (form fix);
- 6 c2d-standard-8 VMs from 23:48 to 00:02Z, about 1.4 VM-h, about **$0.53**;
- VMs deleted by me; the bucket rows were kept as evidence.

**Second wave `reprobe-800-2`** at `c8f5235f`:
- 48 cost-balanced shards (`evalspec.build_shard_plan`, plan digest 8a9af77e...) on 6 c2d-standard-8 VMs in
  us-central1-b;
- labels `deployment-id=dev-crsv4,purpose=v4-reprobe-800,owner=lead-2`, VM names `dev-crsv4-v4e-11240009-{1..6}of6`;
- `--max-run-duration=4h --instance-termination-action=DELETE` on every VM.

Timeline:
- created 00:09Z; first shard started 00:13Z; last shard ended 02:14:58Z;
- **wall: 2.1 h from launch to the last VM self-delete**;
- slowest shard 7,295 s (2.03 h); fastest shard 864 s.

Cost:
- **11.1 VM-h, about $4.17** at $0.377/VM-h (c2d-standard-8 plus a 100 GB pd-balanced disk; VM life = create to its
  slowest shard's end);
- **total about $4.7**, against the $15 cap.

Work and projection:
- the shards' `wall_seconds` sum to **57.4 vCPU-h**, i.e. 258 s per file;
- that is 2.3x the 25 vCPU-h projection from Lane D's 136 s/file, which predates engine_form and the 1800 s wall;
- the tail of 1800 s files dominates. Re-measure before costing the full wave.

**VMs gone:**
- Audit logs show each VM's self-delete from 01:38 to 02:16Z.
- `gcloud compute instances list --filter='labels.purpose=v4-reprobe-800'` returns nothing.
- `gcloud compute disks list --filter='name~^dev-crsv4-v4e-'` returns nothing.
- Quota was checked before launch (C2D 16-24 of 100 used by others); I touched no VM I did not create.

## Code changes (each with tests)
- **`launch_fleet.py`:** `--label KEY=VALUE` (v4 only) replaces deployment-id/purpose or adds owner. deployment-id
  also prefixes the VM name. Derived labels are refused. The live-unit and resume filters use the effective purpose.
  The existing reaper is unchanged and now asserted in a test.
- **`wave.py`:**
  - `--label` passthrough; the watch filters live VMs on the purpose label;
  - `preflight.py` is pointed at the wave's own prefix (`--name`).
  - **Known bug, not fixed:** `wave.py`'s collect pull omits `cheap.jsonl`, so its collect step crashed
    (`wave.py` rc 1 after the watch succeeded). I pulled the file and ran collect by hand. This is a one-line fix to
    `SHARD_FILES`.
- **`partial_reprobe.py`:** the sampler, the v2/v1r split cutter and the summary.
- **`evalspec.py`:** `snap_v2_dev` / `snap_v2_clean`.

Gates:
- `ruff` is clean on the changed files. 2 pre-existing findings in `local_reprobe_sample.py` (Task 43's file) were
  left alone.
- v4 unit tests: 709 passed after the merge.
- mypy: only the repo's existing `run_arm` module-attribute pattern errors.

## Caveats
1. One run, one commit; labels near a threshold can still move (see above).
2. The 5.75% excluded set biases the failing set toward files that finish in 1800 s; the unbounded misclassification
   (D4) is unverified for the 39 timeouts.
3. dev_v2 strata are sample-proportional, so abc_pass is over-represented relative to the pool.
4. The full wave's held-out draw must exclude v1/v2 dev/clean files.
5. dev_v1r contains one file that is now clean (00611852 ... step_008).
6. The verdict cache at `eval/v4-reprobe-800/verdict-cache` holds this wave's engine-form verdicts; the first wave's
   cache (83 original-form entries) was deleted.
