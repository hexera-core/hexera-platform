# CRS v3 verdict runs (T19, T29): runbook

Everything below runs from a checkout at the run's commit with the venv:

```bash
PY="env PYTHONPATH=$PWD/src .venv/bin/python"
V=devtools/validation/v3_eval
COMMIT=$(git rev-parse HEAD)              # the full sha; the image must be built from it
C12=${COMMIT:0:12}
B=gs://dev-crsv3-transfer-224734058693
```

Every launcher/stager/collector command has `--dry-run`; run it first and read it.

## 0. Blockers to clear before any launch (as of 812ae022)

`launch_fleet.py` (host) and `run_arm.py` (in the container) both run a settings preflight and
**refuse** an arm whose flags the build does not read. At 812ae022 it refuses:

| flag | why | effect if accepted anyway |
|---|---|---|
| `CAD_REPAIR_TOURNAMENT=live`, `CAD_REPAIR_TOURNAMENT_K=3` | no row in `settings/cad_repair.py`; `tournament.posture()` reads `getattr(rcfg, ..., "off")` | `v3-full` == `v3-no-tournament`; **T19 is meaningless** |
| `CAD_REPAIR_MEMORY=live` | no settings row, and nothing in `cad/repair/` imports `memory.py` | none (memory is unwired) |

Fix: land the three settings rows (+ inventory + configuration.md, the plan's three-place rule),
rebuild/redeploy `crsv3`, then use THAT commit. `--accept-inert CAD_REPAIR_MEMORY` is a defensible
override (it is recorded in every PROVENANCE and bundle); accepting the tournament flags is not.

Image: use the **mesh** image (`.../mesh/mesh@sha256:...`). The `pipeline`/`app` image has no
fTetWild (Dockerfile: only `mesh-toolchain` copies `/opt/ftetwild`), so tier 2b would read
`unavailable`; `run_arm.py` refuses that unless `--allow-no-ftetwild`. Get the digest after the
redeploy:

```bash
IMAGE=$(gcloud run jobs describe dev-crsv3-mesh --region us-central1 --project hexera-dev \
  --format='value(spec.template.spec.template.spec.containers[0].image)')
gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.tags)'  # must list $C12
```

## 1. Stage (once per sample; code once per commit)

```bash
$PY $V/stage.py --commit $COMMIT --dry-run         # resolves the 120 locally, writes sample.json
$PY $V/stage.py --commit $COMMIT                   # uploads 120 parts (~700 MB), sample, clean manifest, code.tgz
$PY $V/clean_control.py fetch --work /tmp/v3-clean --gcs $B/eval/v3-verdict/inputs/clean --dry-run
$PY $V/clean_control.py fetch --work /tmp/v3-clean --gcs $B/eval/v3-verdict/inputs/clean
```

The clean fetch downloads ten ABC chunk archives (~870 MB each, ~8.7 GB total) from NYU, checks
each archive's sha256 against the track manifest, extracts only the ~10 drawn members per chunk,
checks each part's sha256, uploads 100 parts (~37 MB), and deletes the archive.

## 2. T19 (wave 1: 80 c2d vCPUs of 100)

```bash
for ARM in v3-no-tournament v3-full; do
  $PY $V/launch_fleet.py --arm $ARM --kind failing --image "$IMAGE" --commit $COMMIT --shards 10 --dry-run
done
# then the same without --dry-run
```

## 3. T29 (wave 2, after wave 1 has gone: 80 c2d vCPUs)

`v3-full/failing` from wave 1 IS T29's v3 arm (same sample, commit, env); it is not re-run.

```bash
$PY $V/launch_fleet.py --arm deterministic --kind failing --image "$IMAGE" --commit $COMMIT --shards 10
$PY $V/launch_fleet.py --arm deterministic --kind clean   --image "$IMAGE" --commit $COMMIT --shards 5 --max-run-hours 2
$PY $V/launch_fleet.py --arm v3-full       --kind clean   --image "$IMAGE" --commit $COMMIT --shards 5 --max-run-hours 2
```

(Optional, for T19's clean control: `--arm v3-no-tournament --kind clean`.)

Results land, per arm, under `$B/eval/<arm>-$C12/<kind>/shard-I-of-N/`:
`bundle/rows.jsonl` (one row per file, synced every 4 min while running), `rows.json`,
`PROVENANCE.json`, `model-calls.jsonl`, `transcripts.jsonl` (v3 arms), `run.log`, then `DONE`
(or `FAILED` / `INCOMPLETE` + `run.partial.log`).

## 4. Collect and seal (any session, any machine: needs GCS only)

```bash
$PY $V/collect.py --commit $COMMIT --status                # per shard: DONE / FAILED / running
$PY $V/collect.py --commit $COMMIT --pull --dry-run        # pulls, checks, prints the verdict
$PY $V/collect.py --commit $COMMIT --pull                  # seals bundles + fills the README
for d in devtools/validation/evidence/2026-11-20-crs-v3-verdict/bundle-*; do
  $PY devtools/validation/repair_corpus.py --verify "$d"; done
```

It refuses: a missing/failed/unfinished shard, mixed commits or images, a sample sha256 that
differs between arms, a row count that is not 120 (failing) / 100 (clean), a file in two shards.
Collect BEFORE `make destroy-env SLUG=crsv3`: destroy-env deletes the transfer bucket.

## 5. Resume (after a laptop restart, or a failed shard)

The fleet is detached: VMs run and self-delete with nothing local. To pick up:

```bash
# what is still running
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v3-eval' \
  --format='table(name,zone.basename(),status,labels.arm,labels.kind,creationTimestamp)'
# which shards have results
$PY $V/collect.py --commit $COMMIT --status
# relaunch only what is missing: the launcher skips every shard with DONE and every VM that exists
$PY $V/launch_fleet.py --arm <arm> --kind <kind> --image "$IMAGE" --commit $COMMIT --shards <same N> --dry-run
$PY $V/launch_fleet.py --arm <arm> --kind <kind> --image "$IMAGE" --commit $COMMIT --shards <same N>
# or name them explicitly
$PY $V/launch_fleet.py ... --shards 10 --only-shards 3,7
```

Use the SAME `--shards N` as the original launch (the shard is the sorted sample sliced
`[I-1::N]`; a different N is a different partition and collect refuses mixed N). A rerun VM clears
its shard's FAILED/INCOMPLETE markers before starting. A shard still shown `running` with no VM
listed died without its shutdown script: relaunch it.

## 6. Verify the VMs are gone

```bash
gcloud compute instances list --project hexera-dev --filter='labels.purpose=v3-eval'   # expect: 0 items
gcloud compute disks list --project hexera-dev --filter='name~^dev-crsv3-v3e-'          # expect: 0 items
```

Every VM deletes itself at the end of its shard; `--max-run-duration` (4 h default, 2 h for clean)
with `--instance-termination-action=DELETE` deletes it regardless. Boot disks auto-delete with the
instance. Last resort:
`gcloud compute instances delete $(gcloud compute instances list --project hexera-dev --filter='labels.purpose=v3-eval' --format='value(name)') --zone us-central1-a --project hexera-dev`.

## Estimates

Basis: T6 at 0bf78625 (same sampler, same 120, 3 of 4 shards ran on the laptop): 97 s mean repair
per file, ~132 s per file of shard wall including the two probes. `--goal benchmark` adds a
tier-2/2b verdict (≤ 60 + 30 s); model arms add review/investigation calls inside the same
`CAD_REPAIR_TIMEOUT_S` budget. VM overhead: ~5-10 min (docker install, image pull).

| wave | arms | VMs × type | per-shard files | est. wall | VM $ | model $ |
|---|---|---|---|---|---|---|
| 1 (T19) | v3-no-tournament, v3-full (failing) | 20 × c2d-highcpu-4 | 12 | ~50-70 min | ~$3 | ~$5-35 (luna $0.20/$1.20 per M tok; investigator ≤ 48 calls/file; K=3 multiplies) |
| 2 (T29) | deterministic (failing), 2 × clean | 10 + 5 + 5 × c2d-highcpu-4 | 12 / 20 | ~40-50 min / ~20 min | ~$1.5 | ~$1-5 (reviewer only, plus v3-full on clean parts) |

c2d-highcpu-4 lists at ~$0.150/h on demand in us-central1. Whole T19 + T29: about 2 h wall in two
waves, under ~$50 all-in at the upper model estimate. `collect.py` prints the measured figures
(model spend from per-call telemetry, VM spend from shard wall).

Quota (us-central1, read 2026-11-19): C2D_CPUS 0/100, C3_CPUS 0/24, C2_CPUS 0/100, N2_CPUS 16/200
(not used: shared with the product fleets), CPUS 76/200, PREEMPTIBLE_CPUS 0/0 (hence STANDARD VMs,
not Spot).
