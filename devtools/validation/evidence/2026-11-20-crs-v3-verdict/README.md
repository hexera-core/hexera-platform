# CRS v3 verdict: T19 (tournament off vs on) and T29 (deterministic vs v3) — TEMPLATE

> Placeholders `{{...}}` are filled by `devtools/validation/v3_eval/collect.py` from the sealed
> bundles. Anything still in braces after a collect is for a human to write. Until then this file
> states the method and the acceptance rule, and claims no result.

## What this does not show (read first)

- **Sample size.** 120 files, a stratified draw (seed 20261120) of the 9,956 real mesh failures.
  A per-family difference of one or two files is noise; per-stratum deltas are reported, not
  concluded on.
- **The clean control is 100 parts.** Zero breaks in 100 bounds the break rate at about **3.0%**
  (rule of three, 95%), and about **1.4%** only when both arms' 100 + the 120 failing files are
  pooled (3 / 220). "newly_broken == 0" is a bound, not a proof of safety.
- **Probe vs product mesher.** `meshes_before/after` is the coarse gmsh probe (tier 2,
  `repair_corpus --mesh-probe`, one mesh size = the input's diagonal), the same verdict T6 used.
  The product mesher (escalation, big machines, FEA ladder) is not run; a part the probe meshes
  can still fail the product, and the reverse.
- **{{FLAGS_INERT_NOTE}}** (which posture flags the build did not read, if any were accepted with
  `--accept-inert`; see each bundle's `settings.json` `accepted_inert`).
- **Model nondeterminism.** One run per arm. A re-run of a model arm can differ.

## Method

| | |
|---|---|
| commit | `{{COMMIT}}` |
| image | `{{IMAGE}}` |
| failing sample | `sample.json` (name + sha256, sorted), sha256 `{{SAMPLE_SHA256}}` |
| clean control | `devtools/validation/corpora/clean-control-100.json`, sha256 `{{CLEAN_SHA256}}` |
| harness | `repair_corpus.py --manifest ... --sample 120 --seed 20261120 --shard I/N --mesh-probe --force-repair --goal benchmark` (clean: directory mode, same flags) |
| where | one GCE VM per shard (c2d-highcpu-4, hexera-dev/us-central1), labelled `deployment-id=dev-crsv3,purpose=v3-eval`, self-deleting |
| model | INTAKE route (`openai` / `gpt-5.6-luna`), key from Secret Manager `openai-api-key` |

Arms (env in `devtools/validation/v3_eval/evalspec.py`, frozen into each bundle's settings):

- `deterministic` — everything-on, investigator off, tournament off (T6's posture, Linux image).
- `v3-no-tournament` — everything-on, investigator live, tournament off.
- `v3-full` — everything-on, investigator live, tournament live, K=3. Serves as T19's "on" arm
  **and** T29's v3 arm (same sample, commit and env).

Every collect refuses mixed commits, mixed images, a sample whose sha256 differs between arms, a
missing/failed shard, or a row count that is not the sample's.

## Per arm (failing sample, 120 files)

| | deterministic | v3-no-tournament | v3-full |
|---|---|---|---|
| meshes before → after | {{DETERMINISTIC.FAILING.MESHED_BEFORE}} → {{DETERMINISTIC.FAILING.MESHED_AFTER}} | {{V3_NO_TOURNAMENT.FAILING.MESHED_BEFORE}} → {{V3_NO_TOURNAMENT.FAILING.MESHED_AFTER}} | {{V3_FULL.FAILING.MESHED_BEFORE}} → {{V3_FULL.FAILING.MESHED_AFTER}} |
| newly_meshed | {{DETERMINISTIC.FAILING.NEWLY_MESHED}} | {{V3_NO_TOURNAMENT.FAILING.NEWLY_MESHED}} | {{V3_FULL.FAILING.NEWLY_MESHED}} |
| newly_broken | {{DETERMINISTIC.FAILING.NEWLY_BROKEN}} | {{V3_NO_TOURNAMENT.FAILING.NEWLY_BROKEN}} | {{V3_FULL.FAILING.NEWLY_BROKEN}} |
| model calls (all / investigator) | {{DETERMINISTIC.FAILING.MODEL_CALLS}} / {{DETERMINISTIC.FAILING.INVESTIGATOR_CALLS}} | {{V3_NO_TOURNAMENT.FAILING.MODEL_CALLS}} / {{V3_NO_TOURNAMENT.FAILING.INVESTIGATOR_CALLS}} | {{V3_FULL.FAILING.MODEL_CALLS}} / {{V3_FULL.FAILING.INVESTIGATOR_CALLS}} |
| tokens | {{DETERMINISTIC.FAILING.TOKENS}} | {{V3_NO_TOURNAMENT.FAILING.TOKENS}} | {{V3_FULL.FAILING.TOKENS}} |
| model $ | {{DETERMINISTIC.FAILING.MODEL_USD}} | {{V3_NO_TOURNAMENT.FAILING.MODEL_USD}} | {{V3_FULL.FAILING.MODEL_USD}} |
| VM $ (container wall, list price) | {{DETERMINISTIC.FAILING.VM_USD}} | {{V3_NO_TOURNAMENT.FAILING.VM_USD}} | {{V3_FULL.FAILING.VM_USD}} |
| shard-seconds (longest shard) | {{DETERMINISTIC.FAILING.WALL_S}} ({{DETERMINISTIC.FAILING.LONGEST_SHARD_S}}) | {{V3_NO_TOURNAMENT.FAILING.WALL_S}} ({{V3_NO_TOURNAMENT.FAILING.LONGEST_SHARD_S}}) | {{V3_FULL.FAILING.WALL_S}} ({{V3_FULL.FAILING.LONGEST_SHARD_S}}) |
| $ per fixed part | {{DETERMINISTIC.FAILING.USD_PER_FIXED}} | {{V3_NO_TOURNAMENT.FAILING.USD_PER_FIXED}} | {{V3_FULL.FAILING.USD_PER_FIXED}} |
| shard-seconds per fixed part | {{DETERMINISTIC.FAILING.WALL_PER_FIXED_S}} | {{V3_NO_TOURNAMENT.FAILING.WALL_PER_FIXED_S}} | {{V3_FULL.FAILING.WALL_PER_FIXED_S}} |

Endings:

- deterministic: {{DETERMINISTIC.FAILING.ENDINGS}}
- v3-no-tournament: {{V3_NO_TOURNAMENT.FAILING.ENDINGS}}
- v3-full: {{V3_FULL.FAILING.ENDINGS}}

Operations kept / rolled back, by op:

- deterministic: {{DETERMINISTIC.FAILING.OPS}}
- v3-no-tournament: {{V3_NO_TOURNAMENT.FAILING.OPS}}
- v3-full: {{V3_FULL.FAILING.OPS}}

Still failing, by kind:

- deterministic: {{DETERMINISTIC.FAILING.STILL_FAILING}}
- v3-no-tournament: {{V3_NO_TOURNAMENT.FAILING.STILL_FAILING}}
- v3-full: {{V3_FULL.FAILING.STILL_FAILING}}

## Clean control (100 parts)

| | deterministic | v3-full |
|---|---|---|
| meshes before → after | {{DETERMINISTIC.CLEAN.MESHED_BEFORE}} → {{DETERMINISTIC.CLEAN.MESHED_AFTER}} | {{V3_FULL.CLEAN.MESHED_BEFORE}} → {{V3_FULL.CLEAN.MESHED_AFTER}} |
| newly_broken | {{DETERMINISTIC.CLEAN.NEWLY_BROKEN}} | {{V3_FULL.CLEAN.NEWLY_BROKEN}} |
| untouched (no kept op on any part, nothing newly broken) | {{DETERMINISTIC.CLEAN.UNTOUCHED}} | {{V3_FULL.CLEAN.UNTOUCHED}} |

"Untouched" is checked per part: no operation was kept, so the geometry delivered is the input's.
(A `no_change` run still writes a re-serialised STEP, so a raw byte comparison of written files is
not the test; the kept-operation count is.)

## T19: tournament off vs on (same 120)

| | off (`v3-no-tournament`) | on (`v3-full`, K=3) |
|---|---|---|
| newly_meshed | {{T19.OFF_NEWLY_MESHED}} | {{T19.ON_NEWLY_MESHED}} |
| newly_broken | {{T19.OFF_NEWLY_BROKEN}} | {{T19.ON_NEWLY_BROKEN}} |
| $ per fixed part | {{T19.OFF_USD_PER_FIXED}} | {{T19.ON_USD_PER_FIXED}} |
| fixed by on, not off / by off, not on | {{T19_DIFF_FILES}} | |
| winners by source, rehearsal seconds | {{T19_TOURNAMENT_DETAIL}} | |

If "on" fixes no more than "off" at a higher cost, that is the result, stated plainly.

## T29 verdict

**Acceptance rule.** v3 `newly_meshed` ≥ 4 × deterministic `newly_meshed` on the same 120,
`newly_broken == 0` in both arms, the clean control untouched in both arms, and the cost per fixed
part printed.

**Fallback.** If the deterministic arm newly meshes 0, the 4× ratio is degenerate (anything ≥ 0):
the verdict reads "4x goal: undefined (deterministic ceiling 0)" and is judged on v3's absolute
`newly_meshed`, with its count stated plainly. The goal is never claimed as met in that case.

| | |
|---|---|
| deterministic newly_meshed | {{T29.DETERMINISTIC_NEWLY_MESHED}} |
| v3 newly_meshed | {{T29.V3_NEWLY_MESHED}} |
| ratio | {{T29.RATIO}} — {{T29.RATIO_RULE}} |
| newly_broken (both arms) | {{T29.NEWLY_BROKEN_TOTAL}} |
| clean control untouched (both arms) | {{T29.CLEAN_UNTOUCHED}} |
| v3 $ per fixed part | {{T29.COST_PER_FIXED_PART_USD}} |
| **accepted** | **{{T29.ACCEPTED}}** |

If `newly_broken > 0` anywhere: stop, list the files, and propose turning `crs_v3` off in shared
dev (a one-line revert for a human to approve) before anything else.

## Caveats

- {{CAVEAT_SAMPLE}} (120 of 9,956; per-arm Wilson 95% intervals on newly_meshed/120 go here).
- {{CAVEAT_CLEAN}} (100 clean parts: zero breaks bounds the rate at ~3% per arm, ~1.4% pooled).
- {{CAVEAT_PROBE}} (tier-2 probe vs product mesher divergence; T6's own clean reference saw the
  probe fail on 6 of 40 clean parts).
- {{CAVEAT_TIER2B}} (`ftetwild` in each PROVENANCE: tier 2b reads `unavailable` without it).

## Files

`sample.json`, `verdict.json`, `rows-<arm>-<kind>.json`, `bundle-<arm>-<kind>/` (sealed; verify
with `repair_corpus.py --verify <dir>`). Raw shard outputs (rows, PROVENANCE, run logs, model-call
telemetry, investigator transcripts) stay in
`gs://dev-crsv3-transfer-224734058693/eval/<arm>-<commit12>/` and are not committed.
