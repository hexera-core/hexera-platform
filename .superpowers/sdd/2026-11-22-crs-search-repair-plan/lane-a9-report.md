# Lane A9: audit of the reprobe-800 splits as a measurement instrument, and the merge of sdd/reprobe-800

Read-only. Nothing rebuilt, nothing merged. Splits read at `af3e15b9`
(`devtools/validation/corpora/v4/provisional_v2/`); tip read at `799bcefc`. My numbers come from that branch's own
`probe_records.jsonl` (754), `excluded.jsonl` (46), the two v2 split files and the committed `pool.json`.

## 1. Would I run a benchmark OF RECORD on this? No. As a tuning instrument, yes.

The split cutter is not the problem. `dev_v2`'s failure-kind mix tracks the sample's failing set almost exactly
(panel/geometry 36.0% vs 36.3%, body_overlap 16.7% vs 16.9%, self_intersection 10.7% vs 10.3%), so
`build_splits` stratified faithfully. **Every defect below is in the SAMPLE or in the sealed set, not in the
splitter** - which is also where the cheap fixes are.

### (a) BLOCKER: the sealed held-out set is labelled in the wrong form
The only sealed set is Task 43's `PROVISIONAL_heldout.json`: 25 files, labelled on the ORIGINAL STEP form at an
old commit. `dev_v2` is labelled on the ENGINE form. They therefore measure different quantities, and the
held-out cannot arbitrate a dev result.
How wrong: this branch's own v1r relabel changed **3 of 30 verdicts (10%)** and the coarse kind of 11 more. Applied
to 25 files that is ~2-3 wrong verdicts, unknown which.
Fix before spending it: relabel those 25 in engine form (one ~25-file wave; the 800-file wave cost $4.7, so this is
cents). Cheap, and it is the difference between a sealed set and a stale one.

### (b) 25 files cannot measure the thing v2 exists to measure
Paired sign test on 25 files: the smallest all-one-way discordant count reaching p<0.05 is **5** (2^-5 = 0.031).
So the held-out can only confirm an AI marginal of **>=5 of 25 (20 points)**. v1's measured AI marginal was **0
files**. A realistic 1-3 file marginal is unprovable there, and the set is single-use.
Wilson 95% CI on a clean rate: n=25 is +-23 to 36 points wide (5/25 -> 9%..39%); n=150 is 9 to 16 points
(30/150 -> 14%..27%). The dev split is adequately sized; the sealed set is not.
Fix: draw a fresh held-out in engine form from this sample's 592 failing files (150+ are spare after dev_v2), or
accept the sealed 25 can only refute a large effect and say so in the report.

### (c) The clean control is 25% fragile
10 of 40 `clean_v2` files are near-threshold (a member's min SICN within 3x of the 0.01 floor; closest 0.0123,
0.0125, 0.0138, 0.0151). A clean control exists to catch regressions; if a control file's own label can flip, an
`input_only` "regression" may be measurement noise. Same argument, weaker, for 58 of 150 dev files (39%), and
sharpest for the **10 dev files that are both single-member panel failures and near-threshold** - those produce
phantom repairs and phantom regressions.
Fix: re-measure those 68 files N times on the pinned gmsh and keep only the stable ones, or drop the 10 from the
clean control (clean_v2 -> 30). Lane D's pin should make them reproducible; nobody has shown that it does.

### (d) The exclusion bias is size-shaped, not a flat 5.75%
Exclusion rate by file-size band: **0%** (<100 kB), **1%** (100 kB-1 MB), **11%** (1-10 MB), **38%** (>=10 MB).
So the 46 excluded files are not a random 5.75% haircut: in the largest band more than a third of files produced no
label at all, and `dev_v2` is 3.3% >=10 MB against 4.6% in the sample's own failing set - which was already
depleted. The benchmark therefore understates difficulty on exactly the parts that are hardest to repair.
Fix: quote the >=10 MB rate beside any headline number, and never call a dev clean rate a pool estimate.

### (e) abc_pass is over-represented ~4.9x, not ~2.2x
The report compares `dev_v2`'s 18% abc_pass against "about 8%". That 8% is abc_pass's share of ALL cleared pool
files (887/10887). The right denominator for a FAILING-set split is the pool's FAILING files, and only ~42% of
abc_pass fails against ~99% of abc_fail, so abc_pass is **~3.7%** of the pool's ~10.3k failing files. 18% vs 3.7%
is **4.9x**; nist+occt together are 9.3% of dev_v2 against ~0.7% of the pool failing set.
Consequence, measured: `panel/quality` is **11.3%** of dev_v2 against **3.4%** pool-weighted (3.3x), and every
abc_fail-shaped kind is mildly under-represented (panel/geometry 36.0% vs 41.3%, body_overlap 16.7% vs 19.0%).
Fix: keep the mix (it buys the clean control and panel/quality coverage) but report per-source and per-kind rates
plus one pool-reweighted aggregate, instead of a single dev clean rate.

### (f) Governance: the gate was routed around
`collect.py --report-only` REFUSED at 5.8% excluded (rc 2, no split written), and the splits were then cut by
`partial_reprobe.py splits`. No `--allow-excluded` was used, so nothing was overridden - but the splits of record
did not come through the gated path. They are marked `PROVISIONAL`, which is the right mitigation today; a number
of record must come from the gated path, or the 2% gate means nothing.

### What I would change, in order
1. Relabel the sealed 25 in engine form before spending them (blocker).
2. Decide the held-out question: enlarge it from this sample, or state that it can only confirm a >=5-file marginal.
3. Drop or re-measure the 10 near-threshold clean files; flag the 10 fragile dev files in the report.
4. Quote the >=10 MB exclusion rate (38%) beside every headline number.
5. Report per-source / per-kind rates and a pool-reweighted aggregate, not one dev clean rate.
Run v2 on these splits as a TUNING instrument meanwhile: they are good enough to compare arms against each other,
and not good enough to publish a rate.

## 2. Merging sdd/reprobe-800 (af3e15b9) into 799bcefc

`git merge-tree --write-tree --name-only 799bcefc af3e15b9` conflicts in exactly the four files you named.
`devtools/validation/v3_eval/launch_fleet.py` AUTO-MERGES - see the warning below.

| file | take | why |
|---|---|---|
| `evalspec.py` | **both, union** | Each side only APPENDS `WaveDef`s to `WAVES`: tip `snap_v1_dev/clean` + `snap_v1r_dev/clean`, branch `snap_v2_dev/clean`. Keep all six (order v1, v1r, v2) with both comments. No semantic conflict. |
| `wave.py` | **branch's structure + the tip's one line** | See below: taking the branch wholesale breaks every launch. |
| `test_v4_fleet_launcher.py` | **both, union** | Disjoint: branch adds 3 label tests (`..._renames_the_vms_relabels_them_and_keeps_the_reaper`, `..._derived_label_cannot_be_overridden`, `..._label_overrides_are_v4_only`); tip adds 2 image-label tests. Keep all 5; dedupe identical helpers. |
| `test_v4_wave.py` | **union, but keep the TIP's `_args` helper** | Branch adds 2 tests (labels reach the launcher; preflight checks the wave's own prefix); tip adds 2 (abbreviated `--image-commit` refused; preflight is handed the image commit). The conflict is the shared `_args()`/e2e `WaveArgs`: the tip changed `image_commit=HEAD[:12]` to the full `HEAD`, because a short sha is now refused. KEEP the full sha and ADD the branch's `labels=...`; taking the branch's `HEAD[:12]` makes every wave test hit the new refusal. |

### `wave.py`, precisely
Both sides edit the SAME preflight line.
- Tip: `preflight.py ... --image-commit w.image_commit --pool ...`, plus an `own_refusals` check that
  `--image-commit` is a full 40-hex sha.
- Branch: replaces that inline argv with `preflight_argv(w)`, which adds `--name <wave name>` so preflight checks
  the wave's OWN prefix - and does **not** pass `--image-commit`.

**Branch-side-only is broken**: preflight's `image_commit_label` check (merged as `f6089793`) FAILS when
`--image-commit` is absent, so `wave.py` would refuse every launch, re-probe and snapshot alike.
Resolution: keep the branch's `preflight_argv`, `--label` passthrough, `purpose` property and
`live_units(commit, purpose)`, and put `["--image-commit", w.image_commit]` inside `preflight_argv`. Keep the tip's
`own_refusals` 40-hex check (the branch does not touch it; verify it survives).

### The one-line bug, with the fix
`wave.SHARD_FILES = ("PROVENANCE.json", "rows.json", "EXCLUDED.json", "DONE")` - no `cheap.jsonl`. But
`collect.REQUIRED_FILES` includes it and collect's RE-PROBE path reads it unconditionally
(`collect.py`: `"cheap": [] if parse_rows is not None else rp.read_rows(d / "cheap.jsonl")`), so collect crashes
after a successful watch, which is what happened.
Fix: add `"cheap.jsonl"` to `SHARD_FILES` (identical on both sides, so put it in the merge result).
Safe for ARM waves, which never write that file: `GcsBackend.pull` discards each `cp`'s exit code, so a missing
object just leaves the file absent, and collect with `parse_rows` never reads it; `LocalBackend.pull` already
guards with `if src.exists()`.
Separate latent weakness, NOT for this merge: because that exit code is discarded, a failed `cp` of `rows.json`
is equally silent - collect catches it later with a clear refusal, so it is not urgent, but it is why the bug
reached a live wave.

### After merging, two checks
- `pytest tests/unit/dev -k "v4_wave or v4_fleet_launcher or v4_evalspec"` - the union must pass as a whole.
- `wave.py --kind reprobe ... --dry-run` - proves preflight is still handed `--image-commit` and the wave's own
  `--name`.

### `launch_fleet.py`: auto-merges, verify both halves survive
Both sides edit `V4Profile.extra_metadata` and `wave_preflight`: the tip adds the `v4-image-commit-link` metadata
item and passes `image_commit` to `run_preflight` (recording `args.image_commit_link`); the branch adds the label
overrides, `purpose_of` and the deployment-id VM-name prefix. Git merges them without a conflict, so nothing forces
a human to look - check both are present. They do not interact: `v4-image-commit-link` is METADATA, not a label, so
the branch's `DERIVED_LABELS` refusal does not touch it.
