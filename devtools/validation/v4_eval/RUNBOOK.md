# CRS v4 benchmark re-probe: runbook

Follows `v3_eval/RUNBOOK.md`: same bucket, same fleet shape, same habit of reading `--dry-run` first.
What differs: the v4 wave is ONE unit type (`reprobe:pool:I/N`, a hash shard of the pool), the checker
calls no model, and the output is the benchmark's dev / held-out / clean splits, not an arm comparison.

**Status of the commands.** Every `collect.py` command, the `launch_reprobe.py` gate, the launcher
(`v3_eval/launch_fleet.py --profile v4`, Task 8b) and the stager (`stage.py`, Task 8c) are merged; the
launcher flags used below (`--profile v4`, `--only`, `--image-commit`, `--shards-total`, `--zone`) were
checked against its argparse. The one command still unmerged is `preflight.py` (Task 8d), tagged
**[pending 8d]**: re-read it against `--help` when it lands. Nothing here has been run against GCS.

```bash
PY="env PYTHONPATH=$PWD/src .venv/bin/python"
V=devtools/validation/v4_eval
B=gs://dev-crsv3-transfer-224734058693
COMMIT=<40-hex sha of the code to run>      # src/ + devtools/validation go to the VMs
IMAGE=<pipeline image, ...@sha256:...>      # digest-pinned; a floating tag is refused
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

## Why the first shard is measured, and alone

Every per-file minute in the cost model is a guess: **0.5 min/file cheap, 2.5 min/file full**
(`evalspec.CHEAP_MIN_PER_FILE`, `FULL_MIN_PER_FILE`), on 10,887 files with 5,989 reaching the full panel.
At those guesses `launch_reprobe.py` projects **64 c2d VM-hours, 96 worst case**, against a ~600
ceiling. (The gate's own check is on the projected figure and on VMs x `--max-run-hours`, which it
prints as the worst case; the "worst case" figures in the table below are that product, 12 VMs x the cap,
not a rescaled estimate, so the number the gate prints is the one to read.) That is a long way under, which is exactly why it is not safe to trust. Re-running the same
gate (`evalspec.project`, 10,887 files, 96 shards) with the minutes scaled:

| per-file minutes | projected VM-hours | slowest-shard wall | outcome at `--max-run-hours 8` |
|---|---|---|---|
| 1x (the guesses) | 64 | 5.3 h | passes; worst case 96 |
| 2x | 128 | 10.6 h | **gate refuses on wall**: VMs would be deleted mid-shard; needs `--max-run-hours` ~12, worst case ~144 |
| 5x | 319 | 26.6 h | refuses on wall; needs ~28 h cap, worst case ~336, over half the sprint |

So a 2x error already changes the plan (the cap, the worst case, how long money burns), and a 5x error
spends half the sprint ceiling on one wave. One shard is ~113 files: cheap
and fast to measure, and it replaces the guesses before the other 95 shards spend anything.

## 0. Order of operations

pool manifest -> stage inputs -> preflight -> **one measured shard** -> re-project -> full wave ->
collect -> report -> destroy and verify. Do not skip ahead; each step's output is the next one's input.

### 1. Pool manifest

```bash
$PY $V/pool.py build --nist            # ABC + OCCT from committed manifests, NIST fetched. NO --f360.
git diff --stat devtools/validation/corpora/v4/pool.json
```

The committed `pool.json` is already built; rebuild only on purpose. Check:
`$PY $V/launch_reprobe.py --shards 96 --max-run-hours 8` prints `10887 files (5989 on the full panel)`.
A different count means the allow-list or the pool changed: stop and find out why.

### 2. Stage inputs

```bash
$PY $V/stage.py --inputs /data/v4-inputs --scratch /big/disk   # <sha256>/<basename>; every file hash-checked
# upload (not part of stage.py): inputs -> $B/eval/v4-reprobe/inputs/ ; pool.json -> $B/eval/v4-reprobe/pool.json
```

Resumable: rerun after any failure. A hash mismatch is an error, never a warning. If the stager's
output lists an F360 file, stop: that is a licence breach, not a bug to route around.

### 3. Preflight

```bash
$PY $V/launch_reprobe.py --shards 96 --max-run-hours 8                 # the cost gate. exit 1 = REFUSE
$PY $V/preflight.py --image "$IMAGE" --commit $COMMIT                  # [pending 8d: preflight.py is not merged] read-only IAM/image/cache proof
```

Both must pass. The gate refuses over 600 VM-hours (projected or worst case), over 100 vCPU, or a wall
beyond `--max-run-hours` (the VMs would be deleted mid-shard). `--allow-over-budget` waives the VM-hour
ceiling only; do not use it on the guessed numbers.

### 4. One measured shard

```bash
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT \
    --image-commit <sha the image was built from> --only 'reprobe:pool:1/96' --dry-run
# read it, then the same command without --dry-run
$PY $V/collect.py --status $RESULTS --shards 96          # shard-1-of-96 DONE once finished
$PY $V/collect.py --pull $RESULTS --to /tmp/v4-measure --measure --wave-shards 96 --max-run-hours 8
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
    --image-commit <sha> --dry-run
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT --image-commit <sha>
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
easy defects. Look at `excluded_by_reason` / `excluded_by_source` first.
- **Legitimate `--allow-excluded`:** the exclusions are understood and are not the checker silently
  losing a whole kind (e.g. a handful of known >900 s parts, all `file_timeout`), and the owner has
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
$PY devtools/validation/v3_eval/launch_fleet.py --profile v4 --image "$IMAGE" --commit $COMMIT --image-commit <sha> --dry-run
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

See the table under "Why the first shard is measured": 64 projected / 96 worst case at the guesses,
128 projected at 2x, 319 at 5x, against the ~600 ceiling. Record what the shard measured next to them.

Quota: C2D_CPUS limit 100 (this wave takes 96); `PREEMPTIBLE_CPUS` is 0, hence STANDARD VMs.

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
(`evalspec.refuse_heldout_for_tuning`). The harness-side writer of arm rows (`run_arm --mode harness`
emitting `ArmRow`s with the wave provenance) is not built yet; it waits on the search arms (Tasks 17-19).

## If a gate's own plumbing fails instead of the code

Two failure modes have cost real time in this sprint. Both are the harness, not the change.

- **A bind-mounted checkout can make `git cat-file` report commits as missing**, which surfaces as
  3-4 spurious REFUSE failures in `tests/unit/dev/test_v4_fleet_launcher.py`. The cause is unknown:
  `safe.directory` and object-file permissions were both ruled out, and a
  `git clone --no-hardlinks` bind-mounted the same way gives 22 passed, exit 0. Treat a REFUSE under
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
