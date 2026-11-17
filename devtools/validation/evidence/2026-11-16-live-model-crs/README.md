# Live-model evaluation of mesh-failure repair, through the product, on dev-crs (2026-11-16)

This is the first run in which the model author and the semantic package reviewer worked against a
real model (`gpt-5.6-luna`). Every file went through the deployed product the way a customer's
file would: upload, intake chat, approval, a job on the worker fleet, then the product's own mesher.

**Verdict.** The model author made no difference to what meshed. The product mesher meshed
**8 of 25** files under the rules (arm A) and **7 of 25** with the model author and reviewer live
(arm B). The one file that differs (`occt/bm2_ec_leverrod-A.stp`) skipped repair in both arms
(`mesh_as_is`), and both arms sent the mesher the same bytes. So the difference is
timing variance at the 40-minute wall, not the model. The model wrote **34 packages; none was
committed**. The reviewer was reached **once**, and it **rejected** that package. With the model on,
**0 files** had a repair kept, against **2** under the rules. Repair broke no file that meshed
before (**0**, see the definition below). The clean control was **untouched**: no operation kept and
no repaired geometry sent to the mesher.

## Headline (the frozen 25-file cut, `cut-25.json`)

| | arm A: rules | arm B: model author + reviewer |
|---|---|---|
| **meshed by the product mesher** (`final_result.executor_success` and `outcome_code=success`) | **8 / 25** | **7 / 25** |
| meshed from repaired geometry (`repair_lineage` set) | 1 (`trj7_k1-id-214`, kept `sew_faces`) | 0 |
| files with any operation kept | 2 (`trj7_k1-id-214`; `trj3_s1-tc-214`: `fragment_bodies` + 9 small-edge/face fixes) | **0** |
| **newly broken by repair** | **0** | **0** |
| cancelled at the 40-min job wall | 14 | 15 |
| failed / blocked before a job | 2 failed, 1 blocked | 2 failed, 1 blocked |
| repair route: mesh_as_is / conservative_repair / manual_cleanup / abstain or none | 6 / 13 / 4 / 2 | 6 / 14 / 4 / 1 |
| packages: committed / reverted | 11 / 231 | 0 / 34 |
| median repair-loop seconds (max) | 83.6 (249.9) | 65.3 (268.8) |

**Per file:** `arm-comparison.json`.
- **Meshed in A but not in B:** `occt/bm2_ec_leverrod-A.stp`. It was `mesh_as_is` in both arms,
  so the mesher got identical bytes. It meshed in about 6 minutes during the smoke run and in
  arm A, and hit the 40-minute wall in arm B. This is variance in the mesher or the
  infrastructure, not repair.
- **Meshed in B but not in A:** none.

**How "newly broken" is defined here.** A file is newly broken when repair sent the mesher changed
geometry and the mesh then failed, on a file whose unrepaired bytes meshed.
- **Arm A** sent repaired geometry twice.
  - `trj7_k1-id-214` meshed.
  - `trj3_s1-tc-214` hit the wall. Its unrepaired bytes, in arm B, hit the wall too, so this case
    is inconclusive rather than broken.
- **Arm B** sent no repaired geometry, so it cannot have broken anything.

There was no repair-off arm. "Meshes before" therefore comes from the identical-bytes rows (the
`mesh_as_is` and refused rows, where the original is what was meshed). It does not come from a
separate baseline run.

## The model author (arm B: 25 files, 99 call records)

| outcome | count |
|---|---|
| `package` (a proposal the loop then tried) | 34 |
| `pass` (the model declined the region) | 24 |
| `refused` (proposal rejected before trying: "plan fills edge:41a9… while the part has a paired rim") | 1 |
| `skipped`: "the run's model calls are spent (CAD_REPAIR_LLM_AUTHOR_CALLS_MAX)" | 40 |
| `verdict` (mesher-problem verdicts) | 0 |

So there were 59 real author calls: 34 + 24 + 1.

- **Package pass rate: 0 / 34.** Why each failed:
  - 31: "no operation was kept, so nothing was repaired"
  - 2: "cleared defects still present"
  - 1: rejected by the reviewer
- **What the model proposed** (plan steps; `arm-b-results.json` → `model_proposals`):
  `fix_small_edges` 63, `rebuild_face` 6, `fragment_bodies` 6, `heal_self_intersections` 5,
  `fix_small_faces` 3, `split_closed_faces` 2, `fix_shape_global` 2.
  - `heal_self_intersections` was not enabled in this posture. The model proposed it 5 times anyway.
- **The call cap was the binding limit on 5 of the 13 files where the author was called**
  (`CAD_REPAIR_LLM_AUTHOR_CALLS_MAX` = 6, the default; all 40 skips).
- **`trj3_s1-tc-214` is the one file where the model did worse than the rules.** The rules kept
  `fragment_bodies` plus 9 small fixes. The model's 3 packages there were reverted, one of them by
  the reviewer.
- **`trj7_k1-id-214`:** the rules kept `sew_faces` and meshed from repaired geometry. The model's 2
  packages kept nothing, and the file meshed from the original instead.

## The reviewer (arm B)

Only one package passed its predicate, so the reviewer was consulted once. The verdict
distribution is **accept 0 / reject 1**.

The rejected package was on `trj3_s1-tc-214`, region 1:
- **Deviation:** `deviation_mm = 0.0` (36 samples on 7 faces).
- **Review time:** 4.3 s.
- **Reason:** "The repair cleared no defects—the four small_edge defects remain—and the mesh already
  had no failures before the edit, while measured area and volume changed by -1.68 mm² and
  -73.71 mm³."

No package was accepted, so there are no accepted-package deviations to report.

## Clean control (arm B posture, 10 parts)

The parts are `/tmp/corpus/cad-samples`, round-robin: the `_001` file of the first 10 families
alphabetically. Raw rows are in `raw-rows-clean-control.json`.

- **Kept operations: 0. Repaired geometry delivered: 0. Untouched (met).**
- 9 of the 10 meshed.
- `blade_row_rotor_001` took the `mesh_as_is` route, so repair did not touch it, and it hit the
  40-minute wall.
- 9 files went `mesh_as_is`.
- `diffuser_plenum_001` entered a session: the author made 6 calls (3 packages, 3 passes) and 6 were
  skipped at the cap. All 3 packages were reverted. The repair was refused and the file meshed from
  the original.

## What this does NOT show

- **It is not the 80-file track.** By owner decision (17:50) the run was cut to 25 files, chosen by
  failure family and frozen before arm B (`cut-25.json` gives the name, sha256, family and reason
  for each). Seven files were named as required.
  - Arm A had already finished 14 files outside the 25. Their rows are kept in
    `raw-rows-arm-a-all.json.gz` and under `excluded_outside_cut` in `arm-a-results.json`.
  - Those 14 meshed 0 times and were all cancelled at the wall. 8 of them had operations kept,
    including `fragment_bodies` 6 times.
  - So the cut is not neutral about where the rules kept things.
  - 6 more arm-A jobs were in flight at the cut and were cancelled. They have no rows.
- **There was no repair-off arm**, so this run has no "meshes before" baseline of its own. A and B
  are compared with each other.
- **One pass per arm.** Nondeterminism was not measured, and the `bm2_ec_leverrod-A` flip shows it
  is real at the wall.
- **The 40-minute wall decides more than half the verdicts.** It is a measurement rule, not product
  behaviour.
  - On these parts the product's FEA plan routes meshing to `mesh-big-*` Compute Engine VMs
    (n2-highmem-8 up to 64, and m1-ultramem-40) with a 4-hour mesh timeout.
  - A "cancelled" row means "not meshed within 40 minutes of job creation". It does not mean
    "the mesher refused it".
  - The wall was introduced at 16:50, after N2 quota exhaustion (below), and applied to every row
    in the headline.
  - **Host sleep stretched the wall for some rows.** The local driver host slept, so 4 arm-A jobs
    were cut at 45 to 55 minutes rather than 40. `caffeinate` was used from then on.
- **Reports for cancelled jobs come from checkpoints.** A cancelled job writes no `repair_report`
  artifact. Those rows carry the job's checkpointed `repair_attempt` instead (`_source: checkpoint`,
  read with `ckpt_repair.py` inside a worker container). This is the same record, read from
  LangGraph state.
- **Model cost was not metered.**
  - Counted: 59 author calls and 1 review call in arm B, plus 6 author calls in the control, all on
    gpt-5.6-luna.
  - Each file also had about 5 intake turns, and gpt-5.6-terra was called for the builder's FEA
    decision.
  - Token spend was not collected.
- **The intake path is not deterministic.** Every job went through the LLM intake. The prompt asked
  for FEA static stress, and every file got the gmsh FEA route. Only STEP files are repaired.

## Infrastructure, and what was changed to get here (all on slug `crs` only)

1. **The PR branch could not deploy a fleet.**
   - Cause: it is 24 commits behind `main` and lacks #419. Run 43969893501 failed at MIG create with
     "Spot VMs with termination action DELETE cannot be used with MIGs".
   - Fix: deployed `eval/crs-live-fleetfix` = PR tip `67799856` plus a cherry-pick of #419, which
     touches only the deploy scripts.
   - The engine code is byte-identical to the PR tip.
   - **The PR branch needs `main` merged before it can deploy a personal environment.**
2. **The workers could not read `worker.env`.**
   - Cause: no `storage.objectViewer` binding for the compute account on
     `gs://dev-crs-transfer-…`. On `main` that grant comes from #474.
   - Fix: applied the same binding by hand.
3. **The worker template carries no `database-url`.**
   - Effect: workers dialled `localhost:5432`, and the smoke job went to the DLQ.
   - Fix: added `POSTGRES_HOST`/`POSTGRES_DB` to `worker.env`, the same hand-written wiring dev-areen
     uses.
4. **API environment on `dev-crs-api`:**
   - `CREDIT_GATE_ENABLED=false`
   - `MAX_JOBS_PER_OWNER=40`
   - `MAX_CONCURRENT_JOBS=40`
5. **Fleet sizing:** the floor and ceiling went 8/10, then 16, then 10. They are reset to 0/5.
6. **Quota:** at concurrency 16 the `mesh-big` VMs took `N2_CPUS` to **200/200**. That quota is
   shared by the whole project. The run was throttled to 8 concurrent jobs and a fleet of 10.
7. **No mesh VMs left behind.**
   - Checked with `gcloud compute instances list --filter=name~mesh-big` after arm A (18:50) and
     after the control (22:25).
   - No `dev-crs-mesh` VM remains.
   - The one VM still listed runs as `dev-areen-mesh` and is not ours.
   - The `mesh-big` VMs carry no `deployment-id` label, so `make destroy-env` would not find one.
8. **Other product findings:**
   - `occt/bug30790.step`, both arms: the inspection timed out at 60 s in arm A. The builder in arm A,
     and the repair in arm B, then crashed with `Standard_OutOfRange`. The result was
     `internal_pipeline_failure`, with no `repair_report` artifact.
   - `occt/bug25013_25013face.stp`: an open surface, refused by the FEA check before any job, so
     repair never sees it.
   - `occt/bug33100_window.step`: the FEA driver cannot measure the staged solid (`gate_failed`).

## Files

| file | contents |
|---|---|
| `cut-25.json` | the frozen list |
| `worker-env-arm-a.env`, `worker-env-arm-b.env` | the posture appended to the worker env object, plus the deployment wiring (the clean control used B) |
| `arm-a-results.json`, `arm-b-results.json`, `clean-control-results.json` | engine commit, image digest, summary, timings and compact per-file rows: job id, status, mesh verdict, route, kept ops, packages with review and deviation, author calls |
| `arm-comparison.json` | per-file A vs B |
| `raw-rows-*.json.gz` | full job documents and repair reports; signed URLs and the account address are redacted |
| `ckpt_repair.py` | the read-only checkpoint reader |

- **Driver:** `devtools/validation/live_repair_eval.py` (`login`, `run`, `summarize`).
- **Engine:** `67799856758408ec3a526196ce3dc1c78c24ff2d`.
- **Image:** `us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:f09ed556a23bc4f3fa4c0fdd629ce5b42c5008fdc80fea2d9f1647555b8a491d`.
