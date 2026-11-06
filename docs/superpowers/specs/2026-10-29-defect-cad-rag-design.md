# Defect-driven 3D CAD RAG: isolate the defect, embed its geometry, retrieve the fix

**Date:** 2026-10-29 · **Status:** draft, lab track — facts verified against this workspace on
2026-10-29, awaiting the user's answers before implementation planning.

**Builds on:** the CAD repair system (`2026-09-22-cad-repair-system-design.md`), the targeted loop
(`2026-10-14-cad-repair-targeted-loop-design.md`), repair review
(`2026-10-17-repair-review-design.md`), the LLM planner
(`2026-10-17-llm-planner-design.md`) and catalogue growth
(`2026-10-18-catalogue-growth-design.md`).

**Sibling:** `2026-10-29-rl-post-training-envs-design.md` — the fix memory built here becomes the
`retrieve_similar_fixes` tool in the repair environment, and RL rollouts become a future source of
memory rows.

**Research basis:** `docs/research/defect-cad-rag-literature-review.md` reviews isolation,
representation, supervision, canonicalization, retrieval use, storage and evaluation, and records
the iteration direction adopted below.

## Intent

The CRS planner — rules or model — still decides each repair from scratch. It sees one defect code,
entity context, measurements and the run story, then chooses an operation from the catalogue. It has
no lookup of fixes that were kept on similar local geometry.

This track turns repair outcomes into a scoped memory:

- **key:** an embedding of the local 3D geometry around one located defect, plus symbolic facets
  (`defect_code`, region, entity type, scale band, operation catalogue version);
- **value:** the fix that was tried: operation, targets expressed relative to the patch, parameters,
  and outcome evidence (`kept`, `rolled_back`, `declined`, cap/progress reason, after-scan delta).

At repair time: isolate each located defect, embed its patch, retrieve nearest kept fixes, and hand
them to the planner as directives or context. The safety argument does not change: **retrieval
proposes, the kernel executes, the caps judge.**

This is a lab track. Nothing ships to the customer path until the offline and shadow evidence says
retrieval improves defect yield without increasing damage or refusal misses.

## Verified repository facts

- Repair code lives in `src/meshpipeline/cad/repair/` with `localize.py`, `identity.py`,
  `operations.py`, `plan.py`, `directives.py`, `session.py`, `caps.py`, `loop.py`,
  `spotlight.py`, `triage.py` and support modules.
- The current catalogue is eight operations in declaration order: `fix_small_edges`,
  `fix_face_boundary`, `fix_small_faces`, `sew_faces`, `fill_planar_hole`,
  `fill_nonplanar_hole`, `heal_self_intersections`, `fix_shape_global`.
- Relevant settings are read at import in `src/meshpipeline/settings/cad_repair.py`: timeout
  `CAD_REPAIR_TIMEOUT_S=120`, max ops `CAD_REPAIR_MAX_OPS=6`, stall limit
  `CAD_REPAIR_STALL_LIMIT=2`, LLM planner posture `off|shadow|live`, and default-off geometry
  flags for planar fill, non-planar fill, self-intersection heal and small-face fixing.
- There is no `CAD_REPAIR_RAG` setting today. This track adds one only after offline evidence:
  `off | shadow | live`, default `off`, fail-open to the existing planner.
- `TrainingLogger` in `src/meshpipeline/capture/logger.py` is fail-open by design: it writes via
  `capture.trace.add_event` and logs a warning on any exception. It is useful evidence, not a
  durable source of truth for a fix memory.
- `devtools/validation/repair_corpus.py` is serial and in-process: `run_files()` loops `for path in
  files`, `_one()` imports and executes OCCT in the same interpreter, and planner calls use
  `asyncio.run()` per file. Rows are per file, not per defect.
- Current cloud shape is shared-dev/personal-env style in `hexera-dev`, region `us-central1` with
  worker MIGs in `us-central1-a`; personal envs get per-slug Cloud Run services/jobs, a worker MIG,
  buckets and a database named `meshpipeline_<slug>` on the shared `hexera-dev-pg` Cloud SQL
  instance. `docs/deployment/environments-and-delivery.md` records dev Cloud SQL as `db-g1-small`
  and prod as `db-custom-2-7680`; `deploy/gcp/scripts/create-data-tier.sh` defaults new instances
  to PostgreSQL 16, Enterprise, zonal, `db-g1-small`, 20 GB.

## Phase-0 data audit, 2026-10-29

### OCCT committed evidence

The committed catalogue-growth evidence (`devtools/validation/evidence/2026-10-18-catalogue-growth/`)
records the current best OCCT headline: 331 rows from the OCCT 7.8.0 corpus, 430 defects repaired
by re-inspection, split as 414 `open_shell` and 16 `small_edge`. Kept operation records in the log
are sparse: 14 kept `sew_faces` aimed at `open_shell`, and 2 kept `fix_small_edges` aimed at
`small_edge`. The old spec's "430 credited repairs" is true as a defect-yield headline, but not as
430 independent kept operation labels.

### Defectgen rows

`~/cadbench/defectgen-rows.json` contains 50 `repair_corpus.py` rows. The engine saw 263 located
`small_face` defects across 42 files and 156 located `small_edge` defects across 10 files. Outcomes:
33 `refused`, 8 `nothing_to_repair`, 5 `repaired`, 4 `not_attempted:manual_cleanup`.

Usable kept-operation labels from these rows are much narrower: 5 kept `fix_small_faces` operations
aimed at `small_face`. Re-inspection credited 23 `small_face` and 40 `small_edge` defects removed,
but the `small_edge` removals were collateral in files whose kept operation was aimed at
`small_face`; they should not be treated as clean `small_edge -> fix_small_edges` labels.

### Defectgen answer corpus

`~/cadcorpus/defectgen/repo/answers/c_20261016T102018Z_cdae3abe40` contains 50 answer cases with
60 labelled generated defects across 18 families. Causal operator counts:

| causal operator | count |
|---|---:|
| `thin_wall` | 13 |
| `extreme_thin_ligament` | 13 |
| `isolated_debris` | 9 |
| `residual_boss` | 6 |
| `short_edge_sliver` | 5 |
| `micro_slot` | 4 |
| `pin_hole` | 4 |
| `residual_flash` | 3 |
| `internal_void` | 2 |
| `fluid_obstruction` | 1 |

These are strong labelled *ground truth* records but not yet CRS operation labels. Most generated
defects surface through current CRS localization as `small_face` / `small_edge` or manual-cleanup
residue, so they are useful for isolation and evaluation before they are useful as kept-fix memory.

### Current label gaps

| CRS defect code | usable kept-fix labels today | gap |
|---|---:|---|
| `open_shell` | 14 kept op records / 414 credited defects from OCCT | Need per-defect patch rows, not per-file credit. |
| `small_edge` | 2 kept op records / 16 OCCT credited defects | Too few direct labels; defectgen collateral fixes are not clean labels. |
| `small_face` | 5 kept `fix_small_faces` rows from defectgen | Early but small; needs split by family and more campaigns. |
| `self_intersection` | 0 kept | Current heal attempts decline in OCCT evidence. |
| `wire_gap`, `curve_inconsistency`, `degenerate_edge`, `invalid_brep`, `bad_orientation`, surface defects | 0 kept | Retrieval cannot help until review/corpus rows create labels. |

Phase 0's first deliverable is therefore a durable per-defect record format and a label report, not
an encoder.

## Phase-0 timing, 2026-10-29

Measured command:

```bash
PYTHONPATH=src \
CAD_REPAIR_ENABLED=true \
CAD_REPAIR_AUTONOMOUS=true \
CAD_REPAIR_FILL_PLANAR_HOLES=true \
CAD_REPAIR_FILL_NONPLANAR_HOLES=false \
CAD_REPAIR_HEAL_SELF_INTERSECTIONS=false \
.venv/bin/python devtools/validation/repair_corpus.py ~/cadcorpus/occt \
  --limit 25 --json /tmp/defect-cad-rag-occt-sample-25.json
```

Environment note: the repository had no `.venv`; the base Python lacked `OCP`. A pinned `.venv`
was created from `requirements/runtime.txt` before this run. Docker was unavailable on this host.

Result: 25 sorted OCCT files completed in 14.67 summed row-seconds. Outcomes: 13 `refused`,
11 `nothing_to_repair`, 1 `not_attempted:manual_cleanup`, 0 repaired in this first sorted sample.

| metric | n | min | p50 | mean | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| seconds per file | 25 | 0.01 | 0.07 | 0.5868 | 0.38 | 11.79 |
| located defects per file | 25 | 0 | 1 | 23.36 | 13 | 476 |
| seconds per located defect proxy | 14 files with defects | 0.0008 | 0.0206 | 0.0609 | 0.2358 | 0.28 |

The weighted proxy over files with located defects was 0.023887 s per located defect, dominated by
584 located defects total and one slow 11.79 s file. This is **not true patch timing** because no
patch isolator exists yet; it is the current inspection/repair row time divided by located-defect
count. Phase 1 must measure real `isolate_patch()` and descriptor time separately.

Phase-0 acceptance number for this track: the harness must record p50, p90 and max for file time,
patch isolation time and descriptor time on at least 100 OCCT files or the full corpus, and
retrieval/embedding in shadow must reserve at least 90% of `CAD_REPAIR_TIMEOUT_S` for the existing
repair loop unless the user explicitly raises the budget.

## Literature-backed direction

1. **Isolation:** retain three nested patch levels: operation target closure, incident topology and
   one additional face-adjacency ring clipped by local feature size. Use sampled oriented geometry
   only when invalid topology prevents traversal. Test stability under rigid transforms and STEP
   write/read before testing retrieval.
2. **Representation:** start with a 64-128 dimensional descriptor combining topology, analytic
   geometry, dimensionless measures and existing defect evidence. Keep absolute units and kernel
   tolerances as facets. Neural B-rep, point and multi-view encoders are later comparisons, not the
   baseline.
3. **Supervision:** historical kept fixes are observational because the existing planner selected
   the attempted operation. In the lab, independently try every applicable operation against the
   same cloned pre-repair state. The unchanged caps and re-inspection path label each attempt kept,
   rolled back, declined or failed. Untried operations remain unknown.
4. **Canonicalisation:** prefer invariant scalars and pairwise relations. A patch-local frame is an
   optional encoder input with symmetry/axis-ambiguity flags, not the source of invariance. Keep
   normalized and absolute measurements because repair tolerances are scale-sensitive.
5. **Use:** first log retrieval evidence, then shadow a reranker over operations deterministic rules
   already consider applicable, then evaluate relative-target directives. This separates operation
   selection from target rebinding. Live execution begins with the reranker, not direct replay.
   *Revised by experiment 1 (`docs/research/defect-cad-rag-experiment-1.md`):* the action sweep
   showed the oracle top-1 kept rate barely exceeds the rules baseline — the catalogue rarely
   offers two viable operations per defect, so a reranker has almost no headroom today. The first
   shadow target is instead **outcome prediction**: "will the rules-chosen operation be kept?"
   (held-out-family AUC: knn 0.72 open_shell, 0.75–0.85 small_face vs a degenerate within-code
   prior), and the first live behaviour is ordering or skipping attempts inside the existing
   budget. Reranking becomes relevant when the catalogue widens.
6. **Abstention:** retrieve rolled-back and declined cases as contraindications. Abstain when
   neighbors disagree, target binding is ambiguous or distance exceeds a threshold calibrated only
   on training families.

## Architecture

```text
inspect -> localize (EntityDefect + fingerprint)
            |
            v
   isolate_patch(shape, defect) -> Patch(sub-shape/mesh + frame + facets)      [new, pure]
            |
            v
   describe/embed(Patch) -> vector + descriptor + encoder_version              [new, versioned]
            |
            v
   FixMemory.query(facets, vector, k, budget) -> [FixRecord]                   [new store]
            |
            v
   to_directives(records, patch) -> slice-7 directive format, source="fix-memory"
            |
            v
   DirectedPlanner -> RepairSession caps/rollback/re-inspection/progress rule  [unchanged]
            |
            v
   outcome -> durable attempt + repair-effect write (kept, rolled back, declined, failed)
```

`FixRecord` should be keyed per defect, not per file:

```text
{
  patch_id, repair_episode_id, file_sha256, defect_fingerprint, encoder_version,
  defect_code, entity_type, region, scale_band, op, relative_targets, params,
  outcome: kept|rolled_back|declined|failed,
  reason, cap_evidence, removed_defects, retained_defects, introduced_defects,
  before_after_delta, source, part_family, tenant_scope,
  engine_commit, occt_version
}
```

Rows from one source file or repair episode are correlated and stay in the same split. One kept
operation that removes many defects remains one operation attempt plus one effect set; it is not
expanded into many independent positive examples.

Relative targets are the hard part. A stored fix must name target roles in a patch ("boundary loop",
"shortest edge of defect face", "target face plus crossing partners"), then `to_directives()`
re-binds those roles to fingerprints in the new part. Unbindable records are dropped and counted.
They are never guessed.

## Store choice

Use exact in-process search for the first offline baseline, then Postgres + `pgvector` when durable
shared access is needed, not a separate ANN service. Google Cloud documentation currently
lists `pgvector` as a supported Cloud SQL for PostgreSQL extension, and Cloud SQL can store and
query vectors with that extension. The implementation plan should still include a live probe in a
personal environment (`CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion ...`) because
extension availability and version are properties of the actual instance.

Sizing assumptions for the first design target:

- Descriptor baseline: fixed-size float vector, start at 128 dimensions or less plus JSON facets.
- Neural encoder bake-off: allow 384, 768 and 1024 dimensions in the schema.
- Storage estimate: one million 768-dim `float4` vectors is roughly 3 GB for raw vector values
  before table, JSON, btree and HNSW index overhead. A `db-g1-small` shared dev instance is not a
  vector workload target; use a personal env only for functional probes and small samples.
- Query shape: facet prefilter by `encoder_version`, `defect_code`, tenant scope and scale band,
  then vector top-k. Batch queries per file; never one round trip per defect.
- Do not add HNSW until exact query latency misses the measured budget. Filtered ANN can return too
  few rows, so every ANN configuration is compared with exact top-k recall and tests iterative scans
  or partitioned/partial indexes on the actual extension version.

Move to AlloyDB or a dedicated ANN service only when measured query latency or maintenance cost says
Cloud SQL is the bottleneck.

## Harness fixes

These are design requirements, not optional cleanup:

1. **Sharded runner:** supplement the single in-process `for path in files` path for lab runs with a
   process-pool runner locally and a Cloud Run job / personal-env worker mode on GCP. Keep the
   existing serial tool as a small-debug path.
2. **Crash isolation:** run each file or shard in a subprocess. OCCT can crash the interpreter; a
   crash must become a row with input hash, signal/exit code and phase, not a lost run.
3. **Durable per-defect records:** `TrainingLogger` remains fail-open, but the fix memory writer is
   idempotent and durable with a loss counter. Key writes by `(run_id, file_sha256,
   defect_fingerprint, op_attempt_index)`.
4. **Per-defect rows:** one file with 476 located defects is not one training example. The memory
   record is the patch/defect, with file-level metadata duplicated or normalized.
5. **Split by part family:** no patches from the same part family, source part or defectgen
   reference can sit on both sides of train/eval.
6. **Settings/runtime stamp:** every row carries the repair settings, operation catalogue version,
   engine commit, OCP/OCCT version and host/container class.
7. **Independent action sweep:** for lab inputs only, clone the pre-repair state and run each
   applicable operation independently. Never sequence exploratory operations onto a state changed
   by another candidate.

## GCP placement

- **Phase 0-2 batch:** run only in a personal env or local host. On GCP use `hexera-dev`,
  `us-central1`, personal-env resources named `dev-<slug>-*`, and task-owned buckets. Never use the
  shared mesh job or customer worker fleet.
- **Store probe:** use the personal-env database first. Do not load millions of vectors into the
  shared `hexera-dev-pg`.
- **Online phase 4 only:** repair workers call the store over private networking; any neural encoder
  that is too slow on CPU becomes a separate service. No GPU dependency is added to the repair
  worker without a separate design.

## Data and safety

- First phases use OCCT, defectgen and engineer-labelled rows only. Production/customer trace data
  is out of scope until the user defines tenant policy.
- Customer geometry is tenant data. Any production-sourced record must be tenant-scoped and never
  retrieved cross-tenant unless a later policy explicitly permits it.
- Retrieval never weakens caps, rollback, the progress rule, paired-rim protections,
  `MAX_HEAL_FACES`, or default-off geometry flags.
- `CAD_REPAIR_RAG=off` must add no work to the hot path.

## Phases

0. **Data audit + harness.** Build sharded crash-isolated corpus runs, durable per-defect records,
   family split manifests, and timing reports. Acceptance: verified label counts by defect code and
   op, at least 100-file timing or full OCCT timing, and a store-extension probe in a personal DB.
1. **Isolation + compatibility baseline.** Implement the three-level `isolate_patch()` and
   descriptor, metamorphic stability tests, the independent applicable-operation sweep and exact
   kNN. Acceptance: true isolation/descriptor timing, grouped retrieval metrics, action-outcome
   counts and a report naming insufficient codes.
   *Status 2026-10-29: run in the lab — `labs/defect_cad_rag/`, results and acceptance numbers in
   `docs/research/defect-cad-rag-experiment-1.md`. 184 independent kept labels (~9× the prior
   evidence) from existing corpora; isolate+describe ≈1 ms per defect; crash isolation proved
   necessary (11/331 OCCT files fault the interpreter); 8 OCCT files arrive over the absolute
   tolerance cap and are excluded from labels; the localiser's AABB-diagonal small-entity
   thresholds are not pose-invariant. Insufficient codes: small_edge (18 keeps),
   self_intersection and invalid_brep (0 — need operation work, not retrieval work).*
2. **Encoder bake-off.** Begin only after each evaluated operation has at least 200 independent
   attempts across at least 30 part families with both positive and negative outcomes. Compare a
   pretrained/self-supervised B-rep graph encoder, a rotation-invariant point encoder and then
   multi-view against the descriptor. Acceptance: held-out-family retrieval value plus CPU cost per
   patch on current worker hardware; no GPU assumption without data.
3. **Shadow.** Add `CAD_REPAIR_RAG=shadow` in corpus tooling and one personal env. Shadow retrieval
   evidence and a candidate reranker separately from relative-target directives. Geometry is
   untouched.
   *Refined by experiment 2 (`docs/research/defect-cad-rag-experiment-2.md`): shadow logs
   predicted P(kept) per proposed attempt; the offline bar is 49% of attempts / 63% of
   attempt-seconds skippable at 0.946 held-out keep recall, and first-round keeps 67→84 under the
   real budgets from ordering alone. Live behaviour 1 is attempt ordering; live behaviour 2 is
   skipping with a per-code guard: no skipping in a code with fewer than ~30 training keeps unless
   the code has zero keeps anywhere (the contraindication case).*
   *Status 2026-11-05: implemented. `CAD_REPAIR_RAG=off|shadow` (fail-closed parse, no `live`
   value exists yet) in `settings/cad_repair.py`; the descriptor promoted to
   `cad/repair/patchdesc.py`; `cad/repair/fixmemory.py` predicts P(kept) per planner-proposed
   attempt from a baked snapshot (`cad/repair/data/fixmemory-hand-v2.json.gz`, exported by
   `labs/defect_cad_rag/export_memory.py`) under a 10%-of-timeout shadow budget, fail-open
   throughout; rows join verdicts in `RepairRun.shadow` → `measurements["rag_shadow"]` → corpus
   rows and the training trace. Deploy: `cad_repair_rag` workflow input, API env and worker
   metadata, same convention as `llm_planner`. The in-process snapshot IS the spec's "exact
   in-process search first" store choice; pgvector remains the durable-shared-access step.*
4. **Live in corpus, then gated.** Execute retrieval directives through `DirectedPlanner` only after
   the reranker beats the rules baseline on held-out family splits without increasing
   damage/refusal miss rates and target rebinding has its own measured playability bar.

## Evaluation

Use `docs/research/public-benchmarks.md` CAD-repair tracks: OCCT, NIST, generated-defect and
adversarial safety. Report:

- patch extraction success and descriptor drift under rigid transforms, tessellation changes and
  STEP write/read;
- recall@k / MRR for a kept op, per defect code;
- calibrated top-1 operation compatibility and abstention coverage;
- top-1 playable rate after target rebinding;
- defect yield, repair yield, damage rate and refusal quality against rules-only and LLM-planner
  baselines;
- cost per repaired defect and wall time per file/patch;
- family-held-out performance, not random patch split performance.

Report confidence intervals over source families, not over correlated patch or operation-attempt
rows. Retrieval metrics are diagnostics; promotion depends on kernel-judged repair value.

Promotion proposal for the user to approve: the reranker's lower confidence bound must beat
rules-only top-1 operation compatibility on at least one adequately supported defect code; live
retrieval must then beat rules-only on defect yield, match it on damage and adversarial refusals,
and cost less per repaired defect than `llm_plan`.

## Open for the user before implementation planning

Research supports these defaults for approval:

1. Keep production `TrainingLogger`, customer geometry and cross-tenant retrieval out of Phases 0-2.
2. Require held-out-family shadow improvement before any live corpus experiment; live customer use
   remains outside this design.
3. Put lab-only code in `labs/defect_cad_rag/`; promote only stable, dependency-light interfaces into
   `src/meshpipeline/cad/repair/` later.
4. Prioritize direct `open_shell` and `small_edge` action-outcome labels. Use generated `small_face`
   design-intent cases as abstention and damage controls until a safe operation is demonstrated.

One input still needs the user's decision: the Phase-0 ceiling for additional defectgen campaigns,
expressed as either cases, wall time or cloud/model spend.
