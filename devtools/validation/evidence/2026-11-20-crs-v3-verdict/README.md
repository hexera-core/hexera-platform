# CRS v3 measurement: T19 (tournament off vs on) and T29 (deterministic vs v3) — TEMPLATE

> Placeholders `{{...}}` are filled by `devtools/validation/v3_eval/collect.py` from the sealed
> bundles. Anything still in braces after a collect is for a human to write. Until then this file
> states the method and claims no result. There is no pass/fail bar: the numbers are reported as
> measured. (The directory name `crs-v3-verdict` is historical; nothing here is a verdict.)

## What this does not show (read first)

- **Sample size.** 120 files, a stratified draw (seed 20261120) of the 9,956 real mesh failures.
  A per-family difference of one or two files is noise; per-stratum deltas are reported, not
  concluded on.
- **The clean control is 100 parts.** Zero breaks in 100 bounds the break rate at about **3.0%**
  (rule of three, 95%), and about **1.4%** only when both arms' 100 + the 120 failing files are
  pooled (3 / 220). "newly_broken == 0" is a bound, not a proof of safety.
- **Probe vs product mesher.** `meshes_before/after` is the coarse gmsh probe (tier 2,
  `repair_corpus --mesh-probe`, one mesh size = the input's diagonal), the same probe T6 used.
  The product mesher (escalation, big machines, FEA ladder) is not run; a part the probe meshes
  can still fail the product, and the reverse.
- **Memory flag inert: no consumer wired.** `CAD_REPAIR_MEMORY=live` is set in every arm and has
  a settings row, but nothing under `cad/repair/` imports `memory.py`, so it changes nothing.
- **The deterministic arm calls a model.** `PACKAGE_REVIEW=live` is the shipping posture and stays
  on, so `t29_deterministic` makes reviewer calls (never authoring calls). T6 (0bf78625, laptop)
  ran with no model router at all, so its reviewer had no model: T6 and `t29_deterministic` are
  not the same arm.
- **Model nondeterminism.** One run per arm. A re-run of a model arm can differ.

## Method

| | |
|---|---|
| code commit (runs from the archive, `PYTHONPATH=src`) | `{{COMMIT}}` |
| image (toolchain only: OCP, gmsh, fTetWild, Python deps) | `{{IMAGE}}`, built from `{{IMAGE_COMMIT}}` |
| failing sample | `sample.json` (name + sha256, sorted), sha256 `{{SAMPLE_SHA256}}` |
| clean control | `devtools/validation/corpora/clean-control-100.json`, sha256 `{{CLEAN_SHA256}}` |
| harness | `repair_corpus.py --manifest ... --sample 120 --seed 20261120 --shard I/N --mesh-probe --force-repair --goal benchmark` (clean: directory mode, same flags) |
| where | one wave: 96 shards, one per vCPU (`--cpus=1`, `OMP_NUM_THREADS=1`), 12 × c2d-standard-8 in hexera-dev/us-central1, labelled `deployment-id=dev-crsv3,purpose=v3-eval`, self-deleting |
| model | INTAKE route (`openai` / `gpt-5.6-luna`), key from Secret Manager `openai-api-key` |

Arms (env in `devtools/validation/v3_eval/evalspec.py`, frozen into each bundle's settings):

- `t29_deterministic` — everything-on, investigator off, tournament off; reviewer live (reviewer
  model calls only).
- `t19_tournament_off` — `v3_full` with `CAD_REPAIR_TOURNAMENT=off`.
- `v3_full` — everything-on, investigator live, tournament live, K=3. It is **both** T29's v3
  arm and T19's "tournament on" arm: one run, referenced twice (`results.json`:
  `t19_tournament_on.same_run_as = t29_v3`).

The 100-part clean control runs under `v3_full` and under `t29_deterministic`.

Every collect refuses mixed commits, mixed images, a sample whose sha256 differs between arms, a
missing/failed shard, or a row count that is not the sample's.

## Per arm (failing sample, 120 files)

| | t29_deterministic | t19_tournament_off | v3_full |
|---|---|---|---|
| meshes before → after | {{T29_DETERMINISTIC.FAILING.MESHED_BEFORE}} → {{T29_DETERMINISTIC.FAILING.MESHED_AFTER}} | {{T19_TOURNAMENT_OFF.FAILING.MESHED_BEFORE}} → {{T19_TOURNAMENT_OFF.FAILING.MESHED_AFTER}} | {{V3_FULL.FAILING.MESHED_BEFORE}} → {{V3_FULL.FAILING.MESHED_AFTER}} |
| **files fixed** (CAD-repaired STEP newly meshes in gmsh) | {{T29_DETERMINISTIC.FAILING.FILES_FIXED}} | {{T19_TOURNAMENT_OFF.FAILING.FILES_FIXED}} | {{V3_FULL.FAILING.FILES_FIXED}} |
| newly_broken | {{T29_DETERMINISTIC.FAILING.NEWLY_BROKEN}} | {{T19_TOURNAMENT_OFF.FAILING.NEWLY_BROKEN}} | {{V3_FULL.FAILING.NEWLY_BROKEN}} |
| model calls (all / investigator) | {{T29_DETERMINISTIC.FAILING.MODEL_CALLS}} / {{T29_DETERMINISTIC.FAILING.INVESTIGATOR_CALLS}} | {{T19_TOURNAMENT_OFF.FAILING.MODEL_CALLS}} / {{T19_TOURNAMENT_OFF.FAILING.INVESTIGATOR_CALLS}} | {{V3_FULL.FAILING.MODEL_CALLS}} / {{V3_FULL.FAILING.INVESTIGATOR_CALLS}} |
| tokens | {{T29_DETERMINISTIC.FAILING.TOKENS}} | {{T19_TOURNAMENT_OFF.FAILING.TOKENS}} | {{V3_FULL.FAILING.TOKENS}} |
| model $ | {{T29_DETERMINISTIC.FAILING.MODEL_USD}} | {{T19_TOURNAMENT_OFF.FAILING.MODEL_USD}} | {{V3_FULL.FAILING.MODEL_USD}} |
| VM $ (container wall, list price) | {{T29_DETERMINISTIC.FAILING.VM_USD}} | {{T19_TOURNAMENT_OFF.FAILING.VM_USD}} | {{V3_FULL.FAILING.VM_USD}} |
| shard-seconds (longest shard) | {{T29_DETERMINISTIC.FAILING.WALL_S}} ({{T29_DETERMINISTIC.FAILING.LONGEST_SHARD_S}}) | {{T19_TOURNAMENT_OFF.FAILING.WALL_S}} ({{T19_TOURNAMENT_OFF.FAILING.LONGEST_SHARD_S}}) | {{V3_FULL.FAILING.WALL_S}} ({{V3_FULL.FAILING.LONGEST_SHARD_S}}) |
| $ per fixed part | {{T29_DETERMINISTIC.FAILING.USD_PER_FIXED}} | {{T19_TOURNAMENT_OFF.FAILING.USD_PER_FIXED}} | {{V3_FULL.FAILING.USD_PER_FIXED}} |
| shard-seconds per fixed part | {{T29_DETERMINISTIC.FAILING.WALL_PER_FIXED_S}} | {{T19_TOURNAMENT_OFF.FAILING.WALL_PER_FIXED_S}} | {{V3_FULL.FAILING.WALL_PER_FIXED_S}} |

Endings:

- t29_deterministic: {{T29_DETERMINISTIC.FAILING.ENDINGS}}
- t19_tournament_off: {{T19_TOURNAMENT_OFF.FAILING.ENDINGS}}
- v3_full: {{V3_FULL.FAILING.ENDINGS}}

Operations kept / rolled back, by op:

- t29_deterministic: {{T29_DETERMINISTIC.FAILING.OPS}}
- t19_tournament_off: {{T19_TOURNAMENT_OFF.FAILING.OPS}}
- v3_full: {{V3_FULL.FAILING.OPS}}

Still failing, by kind:

- t29_deterministic: {{T29_DETERMINISTIC.FAILING.STILL_FAILING}}
- t19_tournament_off: {{T19_TOURNAMENT_OFF.FAILING.STILL_FAILING}}
- v3_full: {{V3_FULL.FAILING.STILL_FAILING}}

## Clean control (100 parts)

| | t29_deterministic | v3_full |
|---|---|---|
| meshes before → after | {{T29_DETERMINISTIC.CLEAN.MESHED_BEFORE}} → {{T29_DETERMINISTIC.CLEAN.MESHED_AFTER}} | {{V3_FULL.CLEAN.MESHED_BEFORE}} → {{V3_FULL.CLEAN.MESHED_AFTER}} |
| newly_broken | {{T29_DETERMINISTIC.CLEAN.NEWLY_BROKEN}} | {{V3_FULL.CLEAN.NEWLY_BROKEN}} |
| **still good** (meshed before, still meshes, no new defect) | {{T29_DETERMINISTIC.CLEAN.STILL_GOOD}} | {{V3_FULL.CLEAN.STILL_GOOD}} |
| changed (a kept operation or a crash) — information | {{T29_DETERMINISTIC.CLEAN.CHANGED}} | {{V3_FULL.CLEAN.CHANGED}} |

A clean part is good when it meshed before, still meshes after repair, and gained no new defect
(its located-defect count did not rise); a crashed part is never good. "Changed" counts clean parts
where repair kept an operation or crashed. It is information about how often repair acts on a
healthy part, not a failure.

## T19: tournament off vs on (same 120)

| | off (`t19_tournament_off`) | on (`v3_full`, K=3) |
|---|---|---|
| files fixed | {{T19.OFF_NEWLY_MESHED}} | {{T19.ON_NEWLY_MESHED}} |
| newly_broken | {{T19.OFF_NEWLY_BROKEN}} | {{T19.ON_NEWLY_BROKEN}} |
| $ per fixed part | {{T19.OFF_USD_PER_FIXED}} | {{T19.ON_USD_PER_FIXED}} |
| fixed by on, not off / by off, not on | {{T19_DIFF_FILES}} | |
| winners by source, rehearsal seconds | {{T19_TOURNAMENT_DETAIL}} | |

If "on" fixes no more than "off" at a higher cost, that is the result, stated plainly.

## T29: deterministic vs v3 (same 120)

No acceptance rule and no ratio: the two arms' counts are reported side by side.

| | t29_deterministic | v3_full |
|---|---|---|
| **files fixed** | {{T29_DETERMINISTIC.FAILING.FILES_FIXED}} | {{V3_FULL.FAILING.FILES_FIXED}} |
| newly_broken | {{T29_DETERMINISTIC.FAILING.NEWLY_BROKEN}} | {{V3_FULL.FAILING.NEWLY_BROKEN}} |
| clean control still good | {{T29_DETERMINISTIC.CLEAN.STILL_GOOD}} | {{V3_FULL.CLEAN.STILL_GOOD}} |
| $ per fixed part | {{T29_DETERMINISTIC.FAILING.USD_PER_FIXED}} | {{V3_FULL.FAILING.USD_PER_FIXED}} |

If `newly_broken > 0` anywhere: stop, list the files, and propose turning `crs_v3` off in shared
dev (a one-line revert for a human to approve) before anything else.

## Diagnostic only: tolerant mesher (not counted as CAD repair)

Filled only when the runs were launched with the opt-in `--tolerant-delivery` diagnostic; otherwise
`n/a`. A part here is one gmsh still refuses after repair that fTetWild meshes as delivered. It is
not a fix, not a product fallback, and never part of files fixed.

| | t29_deterministic | t19_tournament_off | v3_full |
|---|---|---|---|
| tolerant-mesher meshable (not counted as CAD repair) | {{T29_DETERMINISTIC.FAILING.DIAG_TOLERANT_MESHER}} | {{T19_TOURNAMENT_OFF.FAILING.DIAG_TOLERANT_MESHER}} | {{V3_FULL.FAILING.DIAG_TOLERANT_MESHER}} |

## Caveats

- {{CAVEAT_SAMPLE}} (120 of 9,956; per-arm Wilson 95% intervals on newly_meshed/120 go here).
- {{CAVEAT_CLEAN}} (100 clean parts: zero breaks bounds the rate at ~3% per arm, ~1.4% pooled).
- {{CAVEAT_PROBE}} (tier-2 probe vs product mesher divergence; T6's own clean reference saw the
  probe fail on 6 of 40 clean parts).
- {{CAVEAT_TIER2B}} (`ftetwild` in each PROVENANCE: tier 2b reads `unavailable` without it).

## Files

`sample.json`, `results.json` (the evidence lane's `results.schema.json`, schema_version 2), `verdict.json` (name historical; it holds the checked inputs and per-arm reports, no verdict), `rows-<arm>-<kind>.json`, `bundle-<arm>-<kind>/` (sealed; verify
with `repair_corpus.py --verify <dir>`). Raw shard outputs (rows, PROVENANCE, run logs, model-call
telemetry, investigator transcripts) stay in
`gs://dev-crsv3-transfer-224734058693/eval/<arm>-<commit12>/` and are not committed.
