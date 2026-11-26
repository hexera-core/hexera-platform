# CRS v4 benchmark re-probe: runbook

Follows `v3_eval/RUNBOOK.md`: same bucket, same fleet shape, same habit of reading `--dry-run` first.
What differs: the v4 wave is ONE unit type (`reprobe:pool:I/N`, a hash shard of the pool), the checker
calls no model, and the output is the benchmark's dev / held-out / clean splits, not an arm comparison.

**Status of the commands.** Every command below is merged and was run (lane D, 2026-11-23) up to, and
not including, the VM creates: `launch_reprobe.py`, `stage.py`, `preflight.py`, and `launch_fleet.py
--profile v4 ... --dry-run`. **The re-probe is the FULL verdict on every file** (plan amendment
2026-11-23 19:15): there is no cheap pass, so `cheap.jsonl` is written empty and the collect report's
cheap/full `agreement` compares 0 files.

```bash
PY="env PYTHONPATH=$PWD/src .venv/bin/python"
V=devtools/validation/v4_eval
B=gs://dev-crsv3-transfer-224734058693
COMMIT=<40-hex sha of the code to run>      # src/ + devtools/validation go to the VMs
# IMAGE and IMAGE_COMMIT come from image_build.py (run on an x86 build VM, clean checkout):
#   $PY $V/image_build.py   ->   IMAGE=<repo>@sha256:...   IMAGE_COMMIT=<40-hex>
# The image carries IMAGE_COMMIT as its org.opencontainers.image.revision label; preflight and the launcher read it
# back from the registry and REFUSE a mismatch. The old pin (sha256:633e1c3b..., "built 2026-11-23T02:21Z") has no
# such label, so it is refused: rebuild before launching.
IMAGE=<printed by image_build.py>
IMAGE_COMMIT=<printed by image_build.py>    # full 40-hex; a prefix is refused
C12=${COMMIT:0:12}
RESULTS=$B/eval/reprobe-$C12/pool           # the directory that directly holds shard-I-of-N/
```

`RESULTS` is where the launcher writes shards: `results_root` in `launch_fleet.py` is
`$B/eval/reprobe-<c12>`, and each shard lands in `<kind>/shard-I-of-N/`, i.e. `$B/eval/reprobe-<c12>/pool/shard-I-of-N/`.
Give `collect.py` the directory whose children are `shard-I-of-N` (the value above). The other layout
you may see, `eval/<name>/results/<c12>/`, is 8d's verdict **cache**, not shard output; never point
`collect.py` at it.

## Hard limits (read before anything is created)

| | limit |
|---|---|
| VM | ~**600 c2d VM-hours** for the whole sprint (`evalspec.MAX_VM_HOURS`), at most 100 vCPU (12 x c2d-standard-8 = 96) |
| Model, normal wave | **$50** |
| Model, checkpoint wave | **$300**, at most **3** checkpoint waves |
| Model, held-out | **$250, once** |
| `--model-usd-cap` | **mandatory** on every wave that calls a model (not yet enforced anywhere: see below) |

The re-probe itself (this runbook's wave) calls no model: its cost is VM-hours. The USD caps bind the
repair waves that run on the splits this wave produces. **No code in `v4_eval/` enforces
`--model-usd-cap` today** (grep finds nothing); the repair-wave launcher must refuse to start without it.
Until it does, an operator who launches a model wave without the flag has no guard but this page. The
table above is a statement of policy, not a guard: enforcement will live in the Task 19 model ledger and
the launcher wiring that reads it. This wave calls no model, so it is not exposed to the gap.

**Fusion 360 is NOT cleared.** Its licence (Autodesk: non-commercial research use, no redistribution)
has not been ruled on by the owner. The eval covers **ABC + OCCT + NIST + the five structural injector
kinds** only. `reprobe.ALLOWED_SOURCES` (Task 8) refuses an F360 entry or row before any mesh and
`build_splits` refuses an F360 row; the stager (8c) and launcher (8b) apply the same list. The committed
`pool.json` still LISTS F360; the allowed pool is **10,887 files**, not 53,799. Clearing F360 is an
**owner decision** and a reviewed one-word edit to the allow-lists. Never pass `--f360` to `pool.py
build` for this wave, and never widen the list to get a bigger sample.

**Held-out is never used for tuning or recipe memory.** Not for choosing thresholds, not for the
cheap/full split, not as retrieval material for recipe memory. It runs ONCE, at a frozen commit, in the
single $250 wave. If held-out has been looked at, it is spent. dev is for tuning; clean is for
regression.

## The cost model is measured, not guessed

Measured by lane D on 2026-11-23 at the tip that includes Lane A's 2.6x speed-up (`measure_reprobe.py`, data in
`corpora/v4/measure_lane_d_20261123/`): 59 diagonal-stratified files, 8 shards pinned to 4 cores' hyperthread pairs
(a c2d-standard-8's shape), `--cpus=1`, cold cache.

| | value |
|---|---|
| per file (sample) | median 25.5 s, mean 136 s, p90 620 s, max 904 s |
| file-wall timeouts (at the then-900 s wall) | 3 / 59 = 5.1% (pool-weighted 4.7%) |
| excluded in total (timeouts + the then-300 s `check_valid` wall + one body-pair cap) | 6 / 59 (pool-weighted 8.7%, wide interval) |
| one vCPU per file | holds: cgroup CPU/wall 1.00 median and max, 1.2 s throttled in 8,023 s |
| peak memory per shard | 2.8 GB (8 shards fit a 32 GB c2d-standard-8) |
| pool-wide work (re-weighted to file sizes) | 400 vCPU-h = 132.4 s/file at the 1800 s wall (`evalspec.FULL_MIN_PER_FILE`); it was 336 vCPU-h = 111 s/file at the old 900 s wall |

The table above is the measurement AT THE WALLS IT WAS TAKEN AT (900 s per file, 300 s per `check_valid` call).
Task 55 raised those to **1800 s per file** (`evalspec.DEFAULT_FILE_TIMEOUT_S`) and **900 s per call**
(`meshpipeline.cad.repair_v4.limits.CHECKER_CALL_TIMEOUT_S`) because the old pair excluded ~8.7% of the pool
against collect's 2% limit. Every cost below is re-simulated at the new wall.

Simulated wave (`project_reprobe.py`, 400 reps, +0.25 h boot/stage per VM, $0.377/VM-h = c2d-standard-8 $0.3632 +
100 GB pd-balanced, both from the GCP billing catalog), at the 1800 s wall and (in brackets) at the old 900 s one:

| plan | VMs | wall median (p90) | VM-hours median (p90) | $ median (p90) |
|---|---|---|---|---|
| 96 shards, cost-balanced | 12 | 6.7 h (7.4) | 68.5 (71.6) | $25.8 ($27.0) |
| 96 shards, hash | 12 | 7.1 h (7.8) | 70.6 (73.5) | $26.6 ($27.7) |
| 80 shards, cost-balanced | 10 | 7.7 h (8.5) | 66.5 (69.5) | $25.1 ($26.2) |
| *(at the old 900 s wall: 96 cost-balanced)* | 12 | *5.2 h (5.6)* | *54.8 (56.5)* | *$20.7 ($21.3)* |

**96 shards are now the safe plan, not merely the cheaper one.** At the 1800 s wall the 80-shard simulation puts
the wall at 7.7 h median and 8.5 h p90, past the 8 h `--max-run-hours` default; the 96-shard plan is 6.7 h / 7.4 h.
(`evalspec.check_budget`'s own coarser x1.5-skew model is kinder - 7.5 h for 80 shards, 6.3 h for 96 - and refuses
neither at 8 h, so the simulation is the stricter of the two and is what this note follows.) That makes the C2D
quota note below a launch blocker rather than a preference.

The launcher's own gate uses a coarser x1.5-skew model, not the simulation: at the 1800 s wall and 96 shards it
says 400 vCPU-h, ~6.3 h wall, 75 projected / 144 worst-case VM-hours against the 600 ceiling, cap 12 h - it passes
(it said 63 projected at the old wall). Size predicts cost weakly (R^2 0.21), so balancing by predicted cost buys
~3%, not more.

**The exclusion rate is the risk, not the cost.** At the measured walls (900 s per file, 300 s per `check_valid`)
~8.7% of the pool was excluded, far over collect's 2% limit, so the wave would have ended at collect exit 2 with no
split. Task 55 raised both walls (1800 s / 900 s; the numbers and their derivation are in the constants' own
comments). Task 55 projected 1.3%-4.8%; Lane A job 3 then MEASURED it, re-running every wall-excluded file of Lane D's
and Task 43's samples with the walls lifted (`lane-a3-report.md`): at 1800 s / 900 s, Lane D's sample is still
**~5.6-6.6% excluded, pool-weighted**. The walls rescue the merely-slow files; what remains is one solid whose
self-intersection analysis runs past an hour (no affordable wall), a panel whose single timeout retry is spent
(inconclusive), and the body-pair cap below. Two Task 43 files need ~960-1,090 s per `check_valid` call on c2d, just
over the 900 s call wall. So, as measured, **collect refuses and that refusal is correct**: `--allow-excluded` is not
the answer to it. The remaining known exclusion the walls cannot touch is
`valid_kernel.MAX_OVERLAP_PAIRS` (2000 bbox-overlapping body pairs per call, 1 of 59 sample files, ~1.3%
pool-weighted) - a WORK cap, not a clock, and raising it without measuring the per-pair cost would only convert a
fast exclusion into a slow one. Whatever is left is reported, named, as its own stratum
(`collect.py report()["excluded_stratum"]`, printed on its own line every run), so a reader sees exactly which
files the failing set does not contain.

**C2D quota is 100 vCPU per region, and every other C2D VM counts.** The gate VM
`dev-crsv4-gate-2` (c2d-standard-16) holds 16, leaving 84: a 96-shard wave is REFUSED by the launcher's
live quota check while it exists. Either delete the gate VM first (its owner's call) or launch 80 shards
on 10 VMs. `--shards-total` must then be 80 on every launch and every `collect.py` call of the wave.

## One command: wave.py

`wave.py` chains every step below: preflight (its own refusals, then `preflight.py` and the launcher's gate) -> stage
(code.tgz, split, a cost-balanced shard plan) -> launch -> watch (relaunch only FAILED or VM-less shards, nothing after
launch + `--max-run-hours`) -> collect -> triage -> report, recording each step in `<wave-dir>/steps.jsonl`, mirrored to
`$B/eval/waves/<wave-id>/`. `--dry-run` runs everything up to and including the launcher's `--dry-run`.

```bash
$PY $V/wave.py --kind reprobe --commit $COMMIT --image "$IMAGE" --image-commit $IMAGE_COMMIT \
    --wave-id reprobe-full-1 --shards 96 --max-run-hours 12 --dry-run
```

The numbered steps remain the manual path and the explanation of what each step checks.

## 0. Order of operations

pool manifest -> stage inputs -> preflight -> **one measured shard** -> re-project -> full wave ->
collect -> report -> destroy and verify. Do not skip ahead; each step's output is the next one's input.

### 1. Pool manifest

```bash
$PY $V/pool.py build --nist            # ABC + OCCT from committed manifests, NIST fetched. NO --f360.
git diff --stat devtools/validation/corpora/v4/pool.json
```

The committed `pool.json` is already built; rebuild only on purpose. Check:
`$PY $V/launch_reprobe.py --shards 96 --max-run-hours 12` prints `10887 files (10887 on the full panel)`.
A different count means the allow-list or the pool changed: stop and find out why.

### 2. Stage inputs

```bash
# macOS python: the system CA bundle lacks SSL.com's 2022 root that archive.nyu.edu chains to
# (CERTIFICATE_VERIFY_FAILED). Point Python at certifi's bundle; verification stays ON.
export SSL_CERT_FILE="$($PY -c 'import certifi; print(certifi.where())')"
$PY $V/stage.py --inputs /data/v4-inputs --scratch /big/disk   # <sha256>/<basename>; every file hash-checked
# upload (not part of stage.py). rsync is idempotent; never upload a .partial or a .corrupt
gcloud storage rsync -r -x '.*\.(partial|corrupt)$' /data/v4-inputs $B/eval/v4-reprobe/inputs
gcloud storage cp devtools/validation/corpora/v4/pool.json $B/eval/v4-reprobe/pool.json
# the code the VMs run, ONE per --commit (no v4 tool writes it; v3's stage.py writes the v3 prefix only)
git archive --format=tar.gz -o /tmp/code.tgz $COMMIT src devtools/validation
gcloud storage cp /tmp/code.tgz $B/eval/v4-reprobe/code-$C12/code.tgz
```

The allowed inputs are ~57 GB (abc_fail alone is 56 GB, 9,923 files). A local copy may be used to seed
`<inputs>/<sha256>/<basename>` (e.g. APFS `cp -c`): the stager re-hashes every file already present and
quarantines any mismatch as `*.corrupt`, so a seed is checked, never trusted. Each VM copies the whole
`inputs/` prefix onto its 100 GB boot disk (~57 GB + the ~6 GB image): fine at this pool size.

Resumable: rerun after any failure. A hash mismatch is an error, never a warning. If the stager's
output lists an F360 file, stop: that is a licence breach, not a bug to route around.

### 3. Preflight

```bash
$PY $V/launch_reprobe.py --shards 96 --max-run-hours 12                # the cost gate. exit 1 = REFUSE
$PY $V/preflight.py --image "$IMAGE" --commit $COMMIT --image-commit $IMAGE_COMMIT   # read-only IAM/image label/inputs/cache proof
# once per bucket, before the first live launch (writes only _probe/ objects and the probe record):
$PY $V/marker_probe.py --store gcs:dev-crsv3-transfer-224734058693 --rounds 50 --cleanup
```

Without the marker probe record the launcher refuses every v4 wave (`no probe record for gcs/...`),
dry run included. `--cleanup` removes the run's throwaway `_probe/<run-id>/` keys and keeps the record.
**Run on the real wave bucket 2026-11-23 (task 55): PASSED.** 50 race rounds, 22 with provably overlapping
calls, sequential create/read-back ok. `create` latency on that bucket, 30 uncontended fresh keys: median
138.7 ms, p90 158.0, p95 167.3, max 189.9 (racing median 141.2 ms, one 5.9 s outlier in 100 calls). A shard
pays two creates at start (the wave marker and its own shard marker) before it may spend, so ~0.3 s per shard
start, ~28 s across 96 shards - not a cost driver.

Both must pass. The gate refuses over 600 VM-hours (projected or worst case), over 100 vCPU, or a wall
beyond `--max-run-hours` (the VMs would be deleted mid-shard). `--allow-over-budget` waives the VM-hour
ceiling only; do not use it on the guessed numbers.

### 4. One measured shard

```bash
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT \
    --image-commit $IMAGE_COMMIT --shards-total 96 --max-run-hours 12 --zone us-central1-b \
    --wave-type normal --wave-id reprobe-full-1 --only 'reprobe:pool:1/96' --dry-run
# read it, then the same command without --dry-run
$PY $V/collect.py --status $RESULTS --shards 96          # shard-1-of-96 DONE once finished
$PY $V/collect.py --pull $RESULTS --to /tmp/v4-measure --measure --wave-shards 96 --max-run-hours 12
```

`--measure` is the only mode that accepts fewer shards than planned; it still refuses a failed shard
or mixed commits, and writes no splits. It prints measured min/file, the factor against the estimate,
and a **re-projection of the whole wave** through the same gate. Decide from that:

- projected and worst case comfortably under 600 and wall under `--max-run-hours`: proceed;
- wall over `--max-run-hours`: raise it (VM cap) or add shards, then re-run the gate; the plan changes,
  so say so;
- near or over 600: stop and tell the owner. Do not pass `--allow-over-budget` to make it fit.

Cost context: `--only 'reprobe:pool:1/96'` still starts a whole c2d-standard-8 VM for that single
shard (about 113 files), so you pay a VM's boot, image pull and shutdown for one shard's work. That is
the price of the measurement and it is small, but it is not free.

One shard is one sample. The gate multiplies by `SKEW` 1.5 for the slow tail; if shard 1 happened to be
light, the real tail is worse. When in doubt measure a second shard (`reprobe:pool:2/96`) and
`--measure` both from one `--to`.

Shard 1 is **not** thrown away: the full-wave launch skips shards that already have DONE.

### 5. Full wave

```bash
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT \
    --image-commit $IMAGE_COMMIT --shards-total 96 --max-run-hours 12 --zone us-central1-b \
    --wave-type normal --wave-id reprobe-full-1 --dry-run
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT \
    --image-commit $IMAGE_COMMIT --shards-total 96 --max-run-hours 12 --zone us-central1-b \
    --wave-type normal --wave-id reprobe-full-1
```

Keep `--shards-total` (96) identical on every launch of this wave: a different total is a different
partition and collect refuses it. VMs self-delete when their shards finish; `--max-run-duration` deletes
them regardless. Watch with `collect.py --status` (below). Zone stockouts are common in
`us-central1-a/c/f`: relaunch with `--zone us-central1-b` or `--machine-type c2-standard-8`.

### 6. Collect

```bash
$PY $V/collect.py --status $RESULTS --shards 96          # DONE / FAILED / running / NOT STARTED per shard; exit 0 only when all DONE
$PY $V/collect.py --pull $RESULTS --to /tmp/v4-wave --shards 96 --dry-run    # list + check; fetches nothing
$PY $V/collect.py --pull $RESULTS --to /tmp/v4-wave --shards 96 --report-only # pull + verify + numbers, no splits
$PY $V/collect.py --pull $RESULTS --to /tmp/v4-wave --shards 96               # the same, then writes corpora/v4/{dev,heldout,clean}.json
```

`--pull` fetches only PROVENANCE.json, rows.json, cheap.jsonl, EXCLUDED.json and DONE per shard (logs
stay in the bucket), writes each via a `.partial` rename, then runs the checks below and merges. It is
read-only against the bucket (`gcloud storage ls` / `cp` down only). Use an **empty** `--to`; a
directory holding shards the bucket does not have is refused as stale. `--pool` defaults to the
committed pool.

**Collect REFUSES (exit 1, nothing written) when:**

| refusal | meaning |
|---|---|
| partial wave: `missing shard(s)` | fewer shards than `--shards`; merging would mis-size every stratum |
| shard has `FAILED`, no `DONE`, or a required file missing | unfinished or failed; nothing is downloaded |
| `exit_code` not 0 | the shard ran and crashed |
| `do not share one commit` / `image` | the wave mixed code or toolchain; not comparable (`--measure` too) |
| the plan has N shards but the shards say M | a different partition |
| `partition mismatch` | a file in the wrong shard or in two shards |
| `pool file(s) were never probed` | the wave is incomplete against the pool |
| `row(s) are not in the pool` | shards ran against another pool or an extra source |
| any F360 row | not in the allowed pool: refused as `row(s) are not in the pool` (exit 1). With `--no-pool-check` it still stops, at exit 2, in `build_splits` (`DisallowedSource`) |

**Collect exits 2 (and writes no split) when** the splits are short (`shortfall`), more than 2% of the
pool was EXCLUDED, or a split cannot be cut (too few failing files, or `DisallowedSource`). For the
shortfall and exclusion cases `collect-report.json` is still written to `--to`, with `"accepted": false`
and the reasons. When a split **cannot be cut** the run exits 2 before the report is written: there is
no `collect-report.json` and no stdout report, only the `REFUSED cannot cut the splits` line on stderr.

### 7. Report

`<--to>/collect-report.json` holds commit, image, shard count, total wall, and the report (conclusive /
excluded counts, failing by layer / source / stratum, cheap-vs-full agreement). The splits are
`corpora/v4/{dev,heldout,clean,meta}.json`: commit them. No v4 renderer for papers/PR exists yet (the
v3 `results_to_tex.py` / `render_pr_section.py` read v3 results). Record the measured min/file and
the actual VM-hours (sum of per-shard `wall_seconds` in `collect-report.json` / 3600) beside the
projection; the next wave's gate should use them.

## When things go wrong

### A shard failed
`--status` shows `FAILED`, or `--pull` refuses `exit_code 4`. Read the shard's `run.log` and its
PROVENANCE `error` in the bucket. Infrastructure (OOM, stockout, preempt): relaunch just that shard
(step "Resume"). A code bug: stop, fix, and note that the wave is then at **two commits**, which collect
refuses: relaunch EVERY shard at the new commit. Do not edit a shard's PROVENANCE to make it pass.

### Collect refuses
Believe it. Each refusal above is a real reason; read the message and fix the cause: relaunch the
missing/failed shards, or restart at one commit. Never copy shard directories by hand to make the count
add up, and never delete a shard from `--to` so the check cannot see it.

### Shortfall (exit 2, "splits are short")
The wave is complete but the failing or clean set is smaller than the 300/200/150 asked for.
- **Legitimate `--allow-shortfall`:** the owner has accepted smaller splits in writing (say the actual
  numbers), the shortfall is a property of the real pool (the pool simply has fewer failing parts) and
  the wave is otherwise clean (no exclusions beyond the limit, every refusal above passing). Record the
  shortfall in the commit message.
- **A mistake:** using it to get past a wave with failed/unfinished shards, because an unprobed stratum
  looks like a small stratum; or before checking `by_source`, since a missing source (staging dropped
  NIST) shows up as exactly this; or to reach a held-out split that is too small to mean anything.

### More than 2% excluded (exit 2)
A timeout excludes the most pathological files, so a large exclusion biases the failing set toward
easy defects. Read the EXCLUDED STRATUM first: `report.excluded_stratum` in `collect-report.json` names every
excluded file with its reason and the two wall values that produced it, and collect prints its one-line summary on
every run (`excluded_by_reason` / `excluded_by_source` are the same tallies, kept for older readers). The stratum
is counted as neither a failure nor a pass, so it is never hiding in the failing set - but the files it lists are
the ones the failing set does not contain, which is exactly the bias to judge.
- **Legitimate `--allow-excluded`:** the exclusions are understood and are not the checker silently
  losing a whole kind (e.g. a handful of known parts past the file wall, all `file_timeout`), and the owner has
  agreed the bias is acceptable; say so in the commit.
- **A mistake:** `checker_unavailable` or `inconclusive` dominating (the pool, not the files, was the
  problem: rerun those shards), or one source or family excluded wholesale.

### Resume, don't restart
The fleet is detached; nothing local carries it. After a lost session or laptop restart:
```bash
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v4-eval' \
  --format='table(name,zone.basename(),status,creationTimestamp)'
$PY $V/collect.py --status $RESULTS --shards 96
# relaunch ONLY what is missing; the launcher skips DONE shards and shards a live VM still claims
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT \
    --image-commit $IMAGE_COMMIT --shards-total 96 --max-run-hours 12 --zone us-central1-b \
    --wave-type normal --wave-id reprobe-full-1 --dry-run
$PY devtools/validation/v3_eval/launch_fleet.py ... --only 'reprobe:pool:7/96,reprobe:pool:31/96'
```
Same `--commit`, `--image`, `--shards-total` as the original launch. The verdict cache means a rerun
shard repays only the files it had not finished. `running` in `--status` with no VM listed means the VM
died without its shutdown script: relaunch that shard.

## Destroy, and verify nothing is left

Collect and commit the splits FIRST. Then:
```bash
# 1. VMs: expect 0 items. Each deletes itself when its shards finish; --max-run-duration is the backstop.
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v4-eval'
# 2. disks: expect 0 items (boot disks auto-delete)
gcloud compute disks list --project hexera-dev --filter='name~^dev-crsv3-v4e-'
# 3. anything still running: delete named VMs only (operator action; mutating)
gcloud compute instances delete <name> --project hexera-dev --zone <zone> --quiet
# 4. re-run 1 and 2 until both say 0 items
```
The launcher (`launch_fleet.py`) names VMs `{deployment_id}-{vm_tag}-{launch}-{j}of{m}`; for the v4
profile that is deployment id `dev-crsv3`, label `purpose=v4-eval`, and VM names starting
`dev-crsv3-v4e-`. The deployment id is the **v3** one (v4 reuses the crsv3 slug), so a filter on
`dev-crsv4-...` matches nothing and would pass vacuously: use the prefix above. If 1 and 2
are not both empty you are still being billed; do not stop at "the launcher finished". The transfer
bucket `dev-crsv3-transfer-*` is shared with v3 and is deleted by `make destroy-env SLUG=crsv3`, which
sweeps these v4 VMs too (they carry the `deployment-id=dev-crsv3` label), so that one command is
sufficient to remove them, but it takes your results with it: collect first. Keep the verdict cache prefix until the benchmark is final.

## Estimates (replace with the measured numbers after step 4)

See "The cost model is measured, not guessed": 56 projected / 144 worst case (96 shards, cap 12 h)
against the ~600 ceiling. Record what the wave measured (`collect.py --measure`) next to them.

Quota: C2D_CPUS limit 100, shared with every other C2D VM in us-central1 (see above);
`PREEMPTIBLE_CPUS` is 0, hence STANDARD VMs. All c2d VMs of one launch go to ONE `--zone`; c2d-standard-8
is offered in us-central1-a/b/c/f. us-central1-b is the zone a c2d VM is known to have started in today
(the gate VM); stockouts are only discovered by trying.

## Arm waves and the report (Task 21)

Arms and waves are data in `evalspec.py` (`ARMS`, `WAVES`); the cap of a wave is its TYPE's entry in
`model_ledger.WAVE_CAPS_USD` (normal $50, checkpoint $300 at most three, held_out $250 once). A wave is
launched only through `evalspec.plan_wave`, which claims the once/three-times slot and binds the cap; each
shard opens its slice with `plan.open_ledger`. Fusion 360 is not in any split or report.

    report.py --from <dir/shard-I-of-N> --wave <name> --shards N --split corpora/v4/<split>.json \
        [--frozen-commit <40-hex>]     # required for the held-out wave

Exit 0 = report OK, 2 = report written but UNRELIABLE (read the warnings first), 1 = REFUSED (partial wave,
mixed commit or image, uncleared source, missing frozen commit, a row the checker's own definition
contradicts). An arm with more than 10% inconclusive files is NOT MEASURED and its clean rate is withheld.
The held-out split is for this report only: never for tuning, prompt selection, thresholds or memory
(`evalspec.refuse_heldout_for_tuning`).

**The harness side (Task 24).** `launch_fleet.py --profile v4 --wave <name> --wave-id <id>` claims the wave through
`evalspec.plan_wave` (the wave's type, hence its cap, comes from the definition; `--wave-type` may be omitted and a
contradiction is refused) and hands every VM `v4-wave` and `v4-plan-provenance` metadata; `vm_startup.sh` then runs
`run_arm.py --mode harness --plan-provenance <json>`. Each shard copies that provenance (wave, slot, cap, arms) into its
`PROVENANCE.json`, which is what lets a held-out report tell the once-only slot was claimed; a shard whose own flags
disagree with the plan refuses. Every row is checked against `report.parse_arm_rows` BEFORE it is written, so a row the
report would refuse (a search that measured nothing written as `inconclusive: false`) fails on the shard. A file the
checker could not judge is an excluded file or an `inconclusive: true` row, never a conclusive failure. The
"was this file measured" rule is `repair_v4/measurement.py`, imported by the search driver and the report alike.
`--mode harness` without a plan is still repair_corpus's own run and writes no `ArmRow`s.

The per-file arm dispatch is `run_arm.ARM_RUNNER` (Task 22c's `arms.run_file`, adapted); with none, `--mode harness`
refuses. The end-to-end proof that infrastructure is not a verdict needs the kernel, gmsh and Netgen, so it runs in the
mesh image (it spends nothing and calls no model):

    docker run --rm -v $PWD:/w -w /w meshpipeline-mesh:native bash -lc \
        'PYTHONPATH=/w/src python devtools/validation/v4_eval/check_infra_not_verdict.py --work /tmp/infra-check'

It judges real files with gmsh present (the split's own verdict), runs one shard with gmsh unimportable, and asserts the
dead file reads `inconclusive: true`, is left out of the clean rate, and that `input_only`'s `in_clean` equals the split's
verdict. After 22c lands, pass `--runner pkg.module:factory` to run it against the real dispatch.

## Failure triage: what to fix next

`report.py` says how many files are clean and whether the numbers may be read. `triage.py` says WHY the rest are
not, per arm, from the rows' own `triage` blocks. It covers:

- how each search stopped;
- which site kinds were still open;
- each op's fate: proposed by det or AI, evaluated, declined, failed or invalid, with the top reasons;
- what the deterministic sweep declined to propose;
- which panel members still fail, with their error kinds;
- the deviation budget used;
- the stages kept and rolled back;
- the AI's turns, tool errors and dollars.

It also ranks the largest buckets as "leads". It reads a partial wave and labels it as partial.

```bash
python devtools/validation/v4_eval/triage.py --from <dir with shard-I-of-N/> [--shards N] \
    [--split corpora/v4/<split>.json] --json triage.json --md triage.md
```

Rows written before the triage block existed are counted as "carry NO triage block", and only their stop reason
(the row's `reason`) is used. Re-run the wave on a commit that records triage to get the rest.

## If a gate's own plumbing fails instead of the code

Two failure modes have cost real time in this sprint. Both are the harness, not the change.

- **A bind-mounted checkout can make `git cat-file` report commits as missing**, which surfaces as
  3-4 spurious REFUSE failures in `tests/unit/dev/test_v4_fleet_launcher.py`. The cause is unknown:
  `safe.directory` and object-file permissions were both ruled out, and a
  clone made with `--no-hardlinks` and bind-mounted the same way gives 22 passed, exit 0. Treat a REFUSE under
  a mount as a fault in the setup before debugging the launcher. Workaround: `cp -r` the checkout
  into the container, or clone inside it. It fails closed, so it costs time and cannot turn a
  failing gate into a passing one.
- **A checkout path containing `v4-` used to fail a v3 test** by substring (the assertion scanned the
  whole command, and this repo's directory is `crs-v4-search-repair`). Fixed in task 26 — it now
  parses `--metadata` keys — but it is the reason to prefer a scratch clone path with no `v4-` in it.

The gate invocation that is verified sound:

```bash
git clone --no-hardlinks --quiet --branch <branch> . /tmp/gate    # a path with no "v4-"
docker run --rm -v /tmp/gate:/w -w /w meshpipeline-validation:local \
  bash -lc 'git config --global --add safe.directory /w;
            PYTHONPATH=/w/src python -m pytest <paths> -q'
```

**`PYTHONPATH` must be ABSOLUTE.** A relative `PYTHONPATH=src` works for the test process and then
silently fails for any test that spawns a subprocess with a different cwd - `src` resolves to
nothing there, so Python falls back to the image's INSTALLED `meshpipeline`, which predates the
branch. That produced 16 real-looking launcher failures (`cannot import name ... from
'meshpipeline.cad.repair_v4' (/usr/local/lib/python3.11/site-packages/...)`) which vanished entirely
under `PYTHONPATH=/w/src`: 16 failed -> 143 passed, exit 0. Every one of those tests spawns
`launch_fleet.py` as a subprocess.

This is the third defect found in this gate's own plumbing, and all three shared one shape: a
confident number from an environment that was not the one being measured. The other two were the
`git archive` checkout with no `.git`, and `tail -6` applied to a log before grepping it.

**Never build a gate checkout with `git archive`.** It produces a tree with no `.git`, and every test
that shells out to git then fails before reaching the code — which silently hid ~35 tests for most of
this sprint, including every budget refusal and the Fusion 360 exclusion.

## Event log: what failed, where, and why (CRS v5)

Every unit streams `repair_v5/events.py` events: one JSON line per event, written (and flushed) the moment it happens,
so a file the wall kills or the OOM killer takes still says where it was. Set `CRS_V5_EVENT_LOG=<path>` to turn it on
(snap-v4 `vm_run.sh` sets `<shard out>/events.jsonl` per unit; the launcher's sync loop copies it to the bucket every
`SYNC_EVERY_S`=120 s, next to `rows.partial.jsonl`). Lines carry `run_id/vm/shard/commit`, the file (`file_sha256`,
`file_name`, `part_faces/solids`), `phase` (read, heal, heal_confirm, input_verdict, conformal, diagnose, tournament,
round_panel, fallback, deliver), `round`, `defect_*`, `op`, `tier`, `result`, `reason_key/reason`, `wall_s`, and on
failure `exc_type`, `exc_message`, `traceback` (last 20 lines), kernel call + timeout + worker exit code, mesher
member + `error_kind` + stderr excerpt, loss-guard numbers and RSS. A 30 s `heartbeat` carries the phase, the RSS of
the process tree and its biggest child; `file_end` closes each file (`row`, `excluded`, `file_timeout`, `error`,
`killed` on SIGTERM). vm_run.sh also scrapes kernel OOM kills into `<vm>.oom_events.jsonl`, by container.

```bash
Q=devtools/validation/v4_eval/events_query.py; SRC=gs://dev-crsv3-transfer-224734058693/eval/snap-<label>-<ts>/partial-sync/out
python $Q $SRC summary                                 # all of the below, short
python $Q $SRC failures --by phase,defect_class,op,reason_key
python $Q $SRC timeline --file <sha-prefix|name-part>  # one file, event by event
python $Q $SRC walltime                                # wall per phase, slowest files
python $Q $SRC exceptions --top 10                     # exception types with an example traceback
python $Q $SRC mesher                                  # failing panel members by member/kind; gmsh child stderr
python $Q $SRC killed                                  # files with no file_end (their last heartbeat); OOM kills -> file
python $Q $SRC sql "select event, count(*) from ev group by 1 order by 2 desc"   # DuckDB, if installed
```

The same lines reach Cloud Logging through Docker's `gcplogs` driver (`vm_run.sh` runs every unit with
`--log-driver=gcplogs` and the labels `crs_run_id`, `crs_shard`, `crs_vm`). The driver does not parse the line: the event
is the STRING `jsonPayload.message`, so filter on it with `:` (substring), and on the labels under
`jsonPayload.container.metadata`:

```bash
BASE='logName="projects/hexera-dev/logs/gcplogs-docker-driver" AND jsonPayload.container.metadata.crs_run_id="<wave id>"'
gcloud logging read "$BASE"' AND jsonPayload.message:"\"event\":\"kernel_call_failed\""' --project hexera-dev --freshness 1d
gcloud logging read "$BASE"' AND jsonPayload.message:"\"severity\":\"ERROR\""' --project hexera-dev --freshness 1d
gcloud logging read "$BASE"' AND jsonPayload.container.metadata.crs_shard="dev_v5-3" AND jsonPayload.message:"\"event\":\"heartbeat\""' \
    --project hexera-dev --freshness 1d --limit 5          # where a dead unit was
gcloud logging read "$BASE"' AND jsonPayload.message:"\"file_sha256\":\"<sha>\""' --project hexera-dev \
    --freshness 1d --format 'value(jsonPayload.message)' | python -c 'import sys,json; [print(json.loads(l)["event"]) for l in sys.stdin]'
```

`jsonPayload.container.metadata.crs_vm` selects one VM. Non-JSON lines (the engine's own prints) arrive in the same
log; adding `jsonPayload.message:"\"event\":"` keeps only events.
