# CRS v3 evaluation runs (T19 + T29): runbook

There is no pass/fail bar. The headline per arm is **files fixed**: failing parts whose
CAD-repaired STEP export newly meshes in the gmsh probe. The clean control counts a part **good**
when it meshed before, still meshes after repair and gained no new defect; the count of clean parts
repair **changed** (a kept operation or a crash) is information, not a failure. The tolerant mesher
(fTetWild) is never a fix and never a product fallback; it is an opt-in diagnostic only (see
"Tolerant-mesher diagnostic" below). The `v3-verdict` / `crs-v3-verdict` path names are historical.

One wave launches everything: three arms on the 120-file sample and the 100-part clean control
under two of them. Run from a checkout that contains `devtools/validation/v3_eval/`:

```bash
PY="env PYTHONPATH=$PWD/src .venv/bin/python"
V=devtools/validation/v3_eval
B=gs://dev-crsv3-transfer-224734058693
COMMIT=<full sha of the code to run>    # its src/ + devtools/validation go to the VMs (code.tgz)
IMAGE_COMMIT=<sha the image was built from>
IMAGE=<pipeline image, ...@sha256:...>
```

Every stager, launcher and collector command has `--dry-run`. Run it first and read it.

## What runs where

| | |
|---|---|
| code | `git archive $COMMIT src devtools/validation` → `$B/eval/v3-verdict/code-<c12>/code.tgz`, extracted on each VM; every container runs with `PYTHONPATH=/eval/code/src`, so the image's installed wheel is shadowed. `run_arm.py` refuses if `meshpipeline` imports from anywhere else, and records the path. |
| image | toolchain only (OCP, gmsh, fTetWild, Python deps). Built from `$IMAGE_COMMIT`. A tag mismatch is a WARNING. A change under `requirements/`, `pyproject.toml`, `Dockerfile` or `deploy/docker/` between `$IMAGE_COMMIT` and `$COMMIT` is REFUSED (`--allow-dep-drift` to override): the image would not carry it. |
| PROVENANCE | `commit` (code) and `image_commit` (image), `image` digest, `meshpipeline_from`, arm env, `known_noops` (memory flag inert: no consumer wired), `ftetwild`, sample sha256, start/end, model calls and $ |
| arms | `t29_deterministic` (investigator + tournament off; reviewer live, so it calls the model as a reviewer only), `v3_full` (investigator live, tournament live K=3: T29's v3 arm AND T19's "on" arm, run once), `t19_tournament_off` (`v3_full` with tournament off) |
| clean control | 100 parts under `v3_full` and under `t29_deterministic` |
| shards | 96, one per vCPU (`docker --cpus=1`, `OMP_NUM_THREADS=1`), 8 per c2d-standard-8 (4 GB RAM per shard), 12 VMs = 96 vCPU ≤ 100. Shard counts per unit come from `evalspec.shard_plan` (estimated minutes per file × files), so every shard takes about the same wall. |
| results | `$B/eval/<arm>-<c12>/<kind>/shard-I-of-N/`: `bundle/rows.jsonl` streamed per file and synced every 4 min, then `rows.json`, `PROVENANCE.json`, `model-calls.jsonl`, `transcripts.jsonl` (investigator arms), `run.log`, `DONE` (written only after `rows.json` and `PROVENANCE.json` uploaded, with retries; else `FAILED`) or `INCOMPLETE` + `run.partial.log` |

The plan for `--shards-total 96`:

| unit | shards | files/shard | est. min/file | est. shard wall |
|---|---|---|---|---|
| t29_deterministic / failing | 21 | ≤ 6 | 3.5 | ~21 min |
| v3_full / failing | 32 | ≤ 4 | 5.5 | ~22 min |
| t19_tournament_off / failing | 29 | ≤ 5 | 5.0 | ~25 min |
| v3_full / clean | 7 | ≤ 15 | 1.5 | ~23 min |
| t29_deterministic / clean | 7 | ≤ 15 | 1.5 | ~23 min |

## 1. Stage (no VM, uploads only)

```bash
$PY $V/stage.py --commit $COMMIT --dry-run       # resolves the 120 locally via repair_corpus, prints uploads
$PY $V/stage.py --commit $COMMIT                 # 120 parts (~700 MB) once, sample.json, clean manifest, code.tgz
$PY $V/clean_control.py fetch --work /tmp/v3-clean --gcs $B/eval/v3-verdict/inputs/clean --dry-run
$PY $V/clean_control.py fetch --work /tmp/v3-clean --gcs $B/eval/v3-verdict/inputs/clean
```

The inputs are commit-independent and are staged once. Re-running `stage.py` at a newer `$COMMIT`
re-uploads only `code.tgz` (the parts use `--no-clobber`). The clean fetch downloads ten ABC chunk
archives (~870 MB each) from NYU, checks each archive's sha256, extracts only the drawn members,
checks each part's sha256, uploads 100 parts (~37 MB), and deletes each archive.

## 2. Launch everything (ONE command)

```bash
$PY $V/launch_fleet.py --image "$IMAGE" --commit $COMMIT --image-commit $IMAGE_COMMIT --dry-run
$PY $V/launch_fleet.py --image "$IMAGE" --commit $COMMIT --image-commit $IMAGE_COMMIT
```

Before it creates anything, a live launch checks:
- the settings preflight for each arm against this checkout;
- toolchain drift between the two commits;
- the image tag (a warning only);
- that `code.tgz`, `sample.json`, the clean manifest and both input sets are staged;
- the C2D quota;
- which shards already have `DONE` or are claimed by a running VM.

Each container repeats the preflight against the archived `src/`:
- environment drift;
- flags the build does not read (`CAD_REPAIR_MEMORY` is accepted as a known no-op);
- fTetWild present;
- model key present;
- the staged sample sha256 equals the sampler's;
- `meshpipeline` imports from the archive.

## 3. Collect, seal, emit results.json (any session, GCS only)

```bash
$PY $V/collect.py --commit $COMMIT --status               # per shard: DONE / FAILED / running
$PY $V/collect.py --commit $COMMIT --pull --t6 --dry-run  # pulls, checks, prints the per-arm counts
$PY $V/collect.py --commit $COMMIT --pull --t6            # seals bundles, writes results.json + README
for d in devtools/validation/evidence/2026-11-20-crs-v3-verdict/bundle-*; do
  $PY devtools/validation/repair_corpus.py --verify "$d"; done
```

`results.json` follows the evidence lane's `devtools/validation/v3_eval/results.schema.json`
(schema_version 2; no verdict, ratio or acceptance field):
- `t29_v3` and `t19_tournament_on` are both filled from `v3_full`, and the latter has
  `same_run_as: t29_v3`.
- `t6_rules_only_local` is filled from `$B/eval/t6-rules-only-0bf78625/` (90 files, shards 1–3).
- each arm's `newly_meshed` is its files fixed (the repaired STEP's gmsh probe only; never the
  tolerant mesher).
- `clean_control.still_meshes_no_new_defects` is the minimum, over the arms covered, of the clean
  parts that meshed before, still mesh after and gained no new defect (a crash is never good).
- `clean_control.changed` is the maximum, over the arms covered, of clean parts with a kept
  operation or a crash. Information, not a failure.
- `delivered_via_tolerant_mesher` is present only for a diagnostic run; otherwise null.
- Anything unmeasured is null.

Collect refuses any of the following:
- a missing, failed or unfinished shard;
- mixed code commits, image commits or images;
- a sample sha256 that differs between arms;
- a row count other than 120 (failing) or 100 (clean);
- a file that appears in two shards.

### 3b. Numbers into the papers, the PR and the PDFs

`collect.py` already writes a schema-v2 `results.json`; `render` can read it directly. The
`from-collect` adapter is the same mapping (it adds `--t6-rows`), and both take the clean
control's `newly_broken` as the worst arm.

```bash
$PY $V/results_to_tex.py render --results <collect out>/results.json   # -> overleaf/crs-v3-results.tex
$PY $V/render_pr_section.py --results <collect out>/results.json      # -> docs/superpowers/pr-494-evidence-section.md
(cd docs/whitepapers/overleaf && latexmk -pdf main.tex full-paper.tex customer-brief.tex && latexmk -c)
uv run --no-project --with reportlab python docs/whitepapers/build_crs_technical_overview.py
```

Not generated, edit by hand once numbers land: the reportlab overview's section 5 ("not yet
measured"), the hand-typed T6 "0 of its first 90 files" sentence in `main.tex` / `full-paper.tex`
and `render_pr_section.py`, and the evidence README's `CAVEAT_*` / `T19_DIFF_FILES` /
`T19_TOURNAMENT_DETAIL` placeholders.

Collect BEFORE `make destroy-env SLUG=crsv3`: destroy-env deletes the transfer bucket.

### 3c. Tolerant-mesher diagnostic (opt-in, off by default)

No default run takes the tolerant route. To measure it, ask for it explicitly:
- locally: add `--tolerant-delivery` to `repair_corpus.py` (failing kind only);
- on the fleet: `evalspec.harness_argv(..., tolerant_diagnostic=True)`. `run_arm.py` does not pass
  it today, so this is a code change committed into `$COMMIT` for the diagnostic run.

A file gmsh still refuses after repair whose tier-2b reading is `tolerant_rung_eats_it` then gets
one fTetWild pass on what would be delivered. The count lands in `delivered_via_tolerant_mesher`
and renders only in `\vThreeDiagnosticTable`, the PR's diagnostic appendix and the evidence
README's diagnostic table, labelled "tolerant-mesher meshable (not counted as CAD repair)". It is
never in a headline and never summed into files fixed.

## 4. Resume (after a laptop restart, a lost session, or a failed shard)

The fleet is detached: VMs run and self-delete with nothing local.

```bash
# eval VMs still running, and which shards each one carries
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v3-eval' \
  --format='table(name,zone.basename(),status,labels.shards,labels.arms,creationTimestamp)'
# which shards have results
$PY $V/collect.py --commit $COMMIT --status
# relaunch ONLY the missing shards: the same command skips every shard with DONE and every shard
# claimed by a VM that still exists (read off its v3-units metadata)
$PY $V/launch_fleet.py --image "$IMAGE" --commit $COMMIT --image-commit $IMAGE_COMMIT --dry-run
$PY $V/launch_fleet.py --image "$IMAGE" --commit $COMMIT --image-commit $IMAGE_COMMIT
# or name them
$PY $V/launch_fleet.py ... --only 'v3_full:failing:7/32,t29_deterministic:clean:3/7'
```

Keep `--shards-total` the same as the original launch. The plan is recomputed from it, and a
different total is a different partition, which collect refuses. A rerun shard clears its old
FAILED/INCOMPLETE marker before starting. If `--status` shows a shard as `running` but no VM
carries it, the VM died without its shutdown script: relaunch the shard.

## 5. Verify the VMs are gone

```bash
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v3-eval'   # expect: 0 items
gcloud compute disks list --project hexera-dev --filter='name~^dev-crsv3-v3e-'          # expect: 0 items
```

Every VM deletes itself once all its shards finish. `--max-run-duration=3h` with
`--instance-termination-action=DELETE` deletes it regardless. Boot disks auto-delete.

## Estimates

**Per-file times.** T6 (0bf78625) measured ~3.5 min per file rules-only, including both probes and
the goal profile's finishing check. The model arms are estimated at 5.0–5.5 min per file, and the clean parts at
1.5 min per file because most mesh before repair.

**Wall clock.** About 25 minutes for the slowest shard, plus 5–10 minutes per VM for boot, docker
install and image pull: **about 35–40 minutes end to end**. If parts run twice as slow as
estimated, it is about 60 minutes, still inside the 3 h backstop.

**Cost.**

| | Estimate |
|---|---|
| VMs | 12 × c2d-standard-8 at ~$0.36/h × ~0.7 h ≈ **$3** |
| Model, investigator arms | ~$5–25 each (240 files) |
| Model, `t29_deterministic` | ~$1–3 (reviewer only) |
| **All-in** | **well under ~$60** |

Model prices are gpt-5.6-luna list: $0.20 per million input tokens, $1.20 per million output
tokens. `collect.py` reports the measured figures: model spend from per-call telemetry, VM spend
from shard wall time, each shard charged 1/shards-per-VM (one per vCPU) of its VM's hourly price.

**RAM.** 4 GB per shard. If a shard dies of OOM on a >100k-entity part, relaunch with
`--machine-type c2d-highmem-8` (8 GB per shard, ~$0.49/h). The plan and the 96 vCPU total stay
the same.

**Quota (us-central1, read 2026-11-19).**

| Metric | Used / limit |
|---|---|
| C2D_CPUS | 0/100 (this launch takes 96) |
| C2_CPUS | 0/100 |
| C3_CPUS | 0/24 |
| CPUS | 76/200 |
| N2_CPUS | 16/200 (never used) |
| PREEMPTIBLE_CPUS | 0/0 (hence STANDARD VMs) |
