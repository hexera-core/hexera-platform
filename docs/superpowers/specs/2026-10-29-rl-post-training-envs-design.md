# RL environments for post-training open models on CAD repair and meshing

**Date:** 2026-10-29  
**Status:** approved for Phase 0 implementation; lab track  
**Builds on:** CAD repair targeted loop, repair review, LLM planner, catalogue growth, runtime
model routing, dynamic mesh workers, `docs/architecture/agents.md`,
`docs/research/public-benchmarks.md`,
`docs/research/llm-rl-environments-literature-review.md`, `docs/RULES.md`.  
**Sibling:** `docs/superpowers/specs/2026-10-29-defect-cad-rag-design.md` - its fix memory is a
tool inside `crs-repair`.

## Intent

Build RL environments that can post-train open-weight models for two model seats:

- **First:** the CRS repair planner seat, currently exposed as the model-directed `llm_plan`
  route behind `CAD_REPAIR_LLM_PLANNER`.
- **Second:** the mesh graph's builder seat, which authors engine configuration and drives a
  mesh to submission.

The near-term goal is more accuracy per dollar than the current frontier route on the same tasks.
The longer-term goal is better absolute accuracy because the environment gives the model feedback
from verifiers frontier models were not trained against: OCCT re-inspection, caps, rollback,
engine gates, and deliverable checks.

This spec is for the environments and benchmark harnesses. It does not change runtime routing
defaults and does not train on customer data.

## Decisions

Confirmed with the user on 2026-10-29:

- The CRS planner is the first target; `mesh-builder` remains design-only.
- V1 uses no customer data or production `TrainingLogger` records. Verified trajectories generated
  by the current frontier routes may be used for SFT and rejection sampling.
- The first experiment covers every proposed repair scope: OCCT real defects, NIST, generated
  defects, adversarial safe-decline cases, and small-face repair.
- Training uses provenance-tracked public OCCT parts and generated defects. Complete OCCT part
  families and the NIST set are sequestered from training for final evaluation.
- GPT-5.6 Terra is the absolute-accuracy frontier baseline and GPT-5.6 Luna is the cost-efficiency
  baseline. Freeze exact route/model identifiers and decoding settings in each evidence bundle.
- Accuracy is the primary objective. Report as much fully loaded cost as can be measured, but do
  not put dollar cost directly into the policy reward.
- Runtime promotion is not a v1 goal. Human review is not required before trajectories enter SFT
  or RL datasets; automated integrity, safety, and adversarial gates remain mandatory.
- GPU budget and credit availability are unset. Every paid run still needs an explicit ceiling
  before launch.
- Experiment order is verified SFT/rejection sampling, synchronous single-shot GRPO, then
  multi-turn RL only if the earlier results justify it.
- Qwen3 is the default first open-model family. Native Phase 0 measurement precedes the env server.

## Verified Starting Facts

Verified against this checkout on 2026-10-29:

- The top-level `contracts/` path named in the kickoff does not exist here. Contract modules live
  under `src/meshpipeline/contracts/`, including `mesh_machine.py`, `pipeline_state.py`, and
  `decision_record.py`.
- The mesh graph has three model-calling seats: intake, builder, and reviewer. Builder tools are
  `write_file`, `read_file`, `list_directory`, `geometry_report`, `measure_scales`,
  `run_python`, `run_mesh`, `web_search`, and `submit_mesh`; training removes `web_search`.
- `run_python` is OS-sandboxed with no network, and OpenFOAM dictionaries carrying parse-time code
  directives are rejected before meshing.
- `repair_corpus.py` is serial: it walks files in one process and calls `_one` for each file.
- `repair_corpus.py` sets repair env defaults before importing the settings/operations stack,
  because repair settings are read at import.
- CAD repair credit in the corpus runner is by re-inspection of the written output, not by a
  repair operation's own claim.
- `TrainingLogger` is deliberately fail-open: capture failures log a warning and never fail the
  product path.
- `CAD_REPAIR_TIMEOUT_S` defaults to 120 seconds and applies to the repair loop.
- `CAD_REPAIR_FILL_PLANAR_HOLES`, `CAD_REPAIR_FILL_NONPLANAR_HOLES`, and
  `CAD_REPAIR_HEAL_SELF_INTERSECTIONS` default to `false`.
- `CAD_REPAIR_LLM_PLANNER` defaults to `off`; `shadow` records proposals without mutating
  geometry; `live` appends `source="llm-planner"` directives behind the same caps and rollback.
- `src/meshpipeline/contracts/mesh_machine.py` is the runtime authority for mesh placement:
  Cloud Run mesh job memory is 32 GiB; GCE choices are `n2-highmem-8`, `n2-highmem-16`,
  `n2-highmem-32`, `n2-highmem-64`, and `m1-ultramem-40`.
- Current deployment docs describe `hexera-dev` and `hexera-prod` in `us-central1/us-central1-a`,
  Cloud Run API/console/mesh jobs, a worker MIG, Cloud SQL, Memorystore, GCS buckets, and Artifact
  Registry `mesh`.

Native Phase 0 timing could not be measured when this spec was first revised, because the workspace
had no native environment. It has since been measured; see
[Phase 0 Measurement Status](#phase-0-measurement-status).

## Why RL Fits

Both seats have verifier-shaped outcomes:

- CAD repair succeeds only when the written file re-inspects better, stays inside caps, and does
  not damage the part.
- Mesh building succeeds only when engine gates pass and deliverables are complete; quality is
  measured by engine/purpose criteria and verdict rules.

The reward must reuse those verifiers. It must not reimplement the kernel, caps, rollback,
progress rule, paired-rim guard, sandbox, or deliverable checks in trainer-side code.

The literature supports this as a hypothesis, not a forecast. Executable software-agent
environments show that post-training can improve open-weight models, but no reviewed paper trains
on CAD repair or meshing. Phase 1 must therefore compare SFT/rejection sampling with RL at equal
inference and environment cost rather than assuming RL is the winning method.

## Literature-Derived Design Rules

The companion literature review separates actual RL, other post-training, benchmark-only work, and
systems evidence. Its conclusions change this design in six ways:

1. **Hard gates precede optimization.** Harness integrity, artifact validity, no-damage checks,
   caps, rollback enforcement, engine gates, and deliverable completeness cannot be offset by
   quality or cost. A safely rolled-back attempt is an efficiency cost, not a gate failure.
2. **Fresh artifact verification is authoritative.** Evaluate the serialized result in a clean
   process outside the policy's write scope. Learned verifiers may rank already-safe candidates;
   they never accept an artifact.
3. **Exact trajectories are training data.** Preserve token IDs, raw tool observations, action and
   policy versions, artifact hashes, and termination causes. Do not reconstruct training samples
   from display logs.
4. **Environment stability is a data-quality gate.** Native crashes, timeouts, nondeterministic
   transitions, lost trajectories, and verifier failures are separately counted outcomes, not
   ordinary task failures.
5. **Start synchronous.** Asynchronous rollout systems improve throughput, but published work also
   shows policy-staleness regressions. Admit bounded lag only after a synchronous learning baseline.
6. **Build hack-verifiable tasks.** Include intentional openings, stale artifacts, entity
   renumbering, observation reordering, malformed outputs, and verifier-tampering probes whose
   exploitation the environment can detect automatically.

## Environment 1: `crs-repair`

Build this first.

**Task instance:** STEP/STP file, unit interpretation, repair profile, repair flags
(`CAD_REPAIR_FILL_PLANAR_HOLES`, `CAD_REPAIR_FILL_NONPLANAR_HOLES`,
`CAD_REPAIR_HEAL_SELF_INTERSECTIONS`, and later `CAD_REPAIR_FIX_SMALL_FACES` if that operation is
in scope).

**Observation:** inspection report, slice-7 entity context, clipped measurements, fingerprints,
neighbour face types, paired-rim evidence, current run story, operation catalogue, and optional
retrieved fix-memory rows from the sibling RAG track.

**Action space:**

- `inspect`
- `propose(op, defect_fingerprint, targets, confidence)`
- `retrieve_similar_fixes`
- `decline(reason)`
- `finish`

A single-shot variant accepts the same directive list shape emitted by `llm_plan`; use it for SFT
parity and current frontier baseline scoring.

**Execution:** all proposals execute through the real repair session and `DirectedPlanner`, with
machine-source rim guard, caps, rollback, re-inspection, and progress rule unchanged.

**Reward gates and components:**

- For a mutating episode, no positive reward is possible unless a clean process reloads the
  written artifact, required validity checks pass, caps and guards hold, and no forbidden damage
  is observed.
- For a labelled safe-decline episode, no positive reward is possible unless no mutation occurred,
  the source artifact hash is unchanged, and the hidden task label confirms that declining was the
  intended outcome.
- After those gates, positive credit is available per defect removed, counted only after
  re-inspection of the written file.
- Any new warning-or-worse defect, solid/face loss, cap violation, unaccounted area/volume change,
  or rejected promotion artifact makes the episode ineligible for positive quality reward; it is
  not merely a large term in an additive score.
- Small efficiency penalties for rolled-back operations, malformed proposals, unbindable targets,
  and extra steps.
- Positive credit for correct declines on the adversarial-safety track after the decline gate.
- Negative credit for "repairing" intentional openings, vents, paired rims, bolt holes, thin
  walls, or multi-body structures that should be left alone.
- Optional sampled terminal bonus when repaired geometry passes downstream staging/admission that
  the original failed. This is expensive and not in the tight loop.

**Tracks:** OCCT real-defect, NIST, generated-defect scored from `answer.json` only after the
episode, and adversarial safety. Splits are by source part family, never by row.

## Environment 2: `mesh-builder`

Design now; build after `crs-repair`.

**Task instance:** geometry plus approved intent after `node_engine_select` and
`node_geometry_admission`: purpose, dimensionality, selected engine, engine parameters, admission
evidence, and workspace seed.

**Action space:** the builder's real tools except `web_search`: `write_file`, `read_file`,
`list_directory`, `geometry_report`, `measure_scales`, `run_python`, `run_mesh`, and
`submit_mesh`.

**Reward:** no positive reward unless a clean verifier reloads the artifact, gates pass, required
deliverables are present, and no forbidden damage or tampering is observed. Then grade quality
against `quality_criteria`, `openfoam_criteria`, FEA quality, verdict rules, and stated requirements.
Treat rounds, mesher CPU seconds, retry loops, sandbox rejections, and oversized runs as bounded
costs within the feasible set. Reviewer verdict may be a sampled calibration signal, not ground
truth, because reviewer is itself a model.

**Scope for v1:** fixed small machine class, small geometry, cheap engine/purpose pairs first
(`gmsh`, `cfMesh`, `fTetWild` FEA where native path is cheap). Keep snappy multi-region, large
cell budgets, and big-machine routing out until measurements say how to control reward
confounding.

## HTTP Environment Contract

The trainer talks to our environment over HTTP. The logical interface remains small:

```text
reset(task_id, seed, policy_id) -> observation
step(action) -> observation, reward, done, info
```

The deployment separates three planes behind that interface: model/inference workers, CPU-heavy
environment workers, and clean-process verification workers. The environment owns mutable native
state; the verifier owns authoritative artifact acceptance; the trainer sees JSON and text only.

Every trajectory records task id, source hash, exact sampled token IDs, policy/model version,
environment commit, container image digest, OCCT/engine versions, typed actions, raw tool
observations, pre/post state and artifact hashes, per-step verifier outputs, reward components,
wall time, CPU time, RSS high-water mark, worker id, policy lag, termination cause, and final
artifact hashes.

Reward components stay separate so rewards can be audited and recomputed without rerolling the
corpus. Recomputing may change bounded quality/cost terms; it may not turn a hard-gate failure into
a successful episode.

## Scale And Harness Limits

Plan for thousands of defects and hundreds of mesh tasks in flight, with many rollouts per task.
No capacity claim is accepted until Phase 0 measures wall, CPU, and memory per episode.

Known harness issues that must be fixed or routed around:

1. `repair_corpus.py` is serial; `crs-repair` needs a sharded worker pool with N isolated native
   workers per host.
2. OCCT and gmsh can crash the interpreter; episodes run in crash-isolated, recyclable processes.
3. Settings are read at import; per-episode flag variation needs worker pools by flag set or a
   refactor to pass settings explicitly.
4. Long-lived OCCT processes may grow memory; workers need RSS caps and recycle-after-K episodes.
5. Determinism is assumed by caching; Phase 0 must run same input/flags several times and compare
   kernel result rows and output hashes where a repair writes.
6. Product training capture is fail-open; RL trajectories need a durable, idempotent sink with
   counted losses.
7. Mesh reward can be confounded by wall limit and machine class; v1 uses only tasks that fit one
   fixed class.
8. RL pressure will search for sandbox escape hatches; retain OS sandbox, no-network Python, and
   OpenFOAM dictionary code-directive rejection.
9. Random row splits and static public tasks invite contamination. Split by source-part family and
   provenance; reserve fresh private defects and invariant-preserving perturbations for final eval.
10. Synchronous batches will strand GPUs behind high-variance native tools. Measure that loss first,
    then introduce resource-class queues and bounded policy lag with a synchronous control run.

## Phase 0 Measurement Status

Measured on 2026-11-05 on merged `main`, using the 16 OCCT 7.8.0 files the 2026-10-18 corpus run
repaired, three isolated episodes each:

- All 48 episodes ended `repaired`, with verifier results identical across repeats.
- Credited repairs total 430, equal to the prior run on those files.
- Wall time was 3.38 s minimum, 10.34 s median, 49.23 s maximum.
- Child CPU time was 1.78 s minimum, 3.48 s median, 12.27 s maximum.
- Peak child RSS was 338,416 KiB.

These files were selected because they repaired, so the numbers are not capacity figures. Refused
and manual-cleanup files were not measured. Details and limits are in
`devtools/validation/evidence/2026-10-29-crs-repair-phase0/README.md`.

The frontier baseline below has not been run, so there is no `$ / repaired defect` figure and the
plan's Phase 0 gate remains closed.

The local corpus is at `~/cadcorpus/occt/opencascade-dataset-7.8.0/step`. Set `HEXERA_OCCT_CORPUS`
to that path, because the documented default under `/tmp` does not exist on this host.

Phase 0 corpus command:

```bash
.venv/bin/python devtools/validation/repair_corpus.py \
  /path/to/occt-sample --limit 10 --loop --json /tmp/crs-repair-phase0.json
```

Wrap the run with `/usr/bin/time -l` on macOS or `/usr/bin/time -v` on Linux, and run a separate
determinism probe that executes the same one to three inputs three times each with identical flags.

For today's frontier baseline:

```bash
CAD_REPAIR_LLM_PLANNER=live \
.venv/bin/python devtools/validation/repair_corpus.py \
  /path/to/occt-sample --limit 10 --planner live \
  --reply-cache /tmp/llm-plan-cache.json \
  --json /tmp/crs-repair-frontier-baseline.json
```

Baseline metric is `$ / repaired defect`, where model cost is read from inference telemetry or the
provider route's current price table. If no provider key is present, run cache-replay only and
label it plumbing evidence, not a frontier baseline.

## GCP Placement

CPU rollout servers live in `hexera-dev` personal-environment style resources:

- Named `dev-<slug>-rl-env-*`, using labels for spend attribution and teardown.
- Spot CPU MIG, never customer worker fleet and never shared `dev-mesh`.
- Use the existing `mesh` image so the kernel/meshers match production.
- Inputs and trajectories in GCS under `us-central1`, v1 with no customer data.
- Autoscale on rollout queue depth and cap max instances.

GPU training/inference stays portable and can run wherever credits or price win. The HTTP env
contract is the portability boundary.

## Live GPU Cost Sheet

Checked on 2026-10-29. Prices move; recheck before spending.

| Provider | Live source and usable facts | Fit |
|---|---|---|
| GCP Compute Engine | Official GPU pricing lists `us-central1` GPU pricing, Spot discounts, and notes that Spot prices are dynamic. Official GPU location docs show `us-central1-a/b/c/f` GPU availability including G2/L4 and A-series/H100/A100 families. | Best when GCP credits or private networking dominate. Spot needs checkpointing. |
| Lambda Cloud | Official instance page lists per-GPU-hour prices such as H100 SXM around $3.99-$4.29, H100 PCIe $3.29, A100 40GB $1.99, A100 80GB $2.79, A6000 $1.09, plus no egress-fee positioning. | Good stable on-demand baseline; simple VM workflow. |
| RunPod | Public live trackers and RunPod-linked pricing pages show materially lower marketplace floors, with examples around A100 80GB $1.19-$1.79, L40S $0.79-$1.09, H100 PCIe about $2.89. Availability varies by marketplace host. | Cheapest likely option for small experiments if availability/reliability is acceptable. |
| Modal | Pricing page lists GPU billing and third-party 2026 trackers show L4/A10/L40S/A100/H100 bands, generally higher than RunPod but serverless and operationally lighter. | Good for short bursty trainer/inference experiments; less ideal for long saturated RL runs. |

Sources checked:

- GCP GPU pricing: `https://cloud.google.com/products/compute/gpus-pricing?hl=en`
- GCP GPU regions/zones: `https://docs.cloud.google.com/compute/docs/regions-zones/gpu-regions-zones`
- Lambda instances/pricing: `https://lambda.ai/instances`
- Lambda on-demand cloud overview: `https://docs.lambda.ai/on-demand-cloud/overview/`
- RunPod pricing: `https://www.runpod.io/pricing`
- RunPod live tracker cross-check: `https://gpurentwatch.com/providers/runpod/`
- Modal pricing: `https://modal.com/pricing`
- Modal market-price cross-check: `https://nodegrove.io/guides/cloud-gpu`

Rough Phase 1 budget for small-model SFT + GRPO/LoRA:

- **Floor experiment:** 7B/8B model, LoRA/QLoRA, 1x L40S/A100 for 20-40 GPU-hours, plus CPU
  rollouts. Budget $100-$300 on marketplace GPUs, $200-$500 on Lambda/Modal/GCP-style pricing.
- **Serious first run:** 14B model, longer context, 1-2x A100/H100 for 80-160 GPU-hours. Budget
  $500-$1,500 on cheap GPU clouds, $1,000-$3,000 on managed or GCP/Lambda rates.
- **Stop rule:** every run has a fixed dollar ceiling and a held-out eval gate. Do not scale model
  size until the eval curve beats the frontier baseline on `$ / repaired defect`, damage rate, and
  adversarial refusals.

CPU rollout cost cannot be estimated yet. Phase 0 measured only files known to repair, so a
representative wall/CPU/RSS sample that includes refused and manual-cleanup files is still missing.

## Trainer Framework Shortlist

Recommendation: implement the environment contract and trajectory schema independently of the
trainer. Use **Hugging Face TRL `GRPOTrainer`** only for the smallest synchronous, single-shot
proof. Before multi-turn RL, spike the same recorded episode through **Agent Lightning** and
**NeMo Gym** adapters; choose one only if it removes more rollout/harness work than it adds. Move
GPU-side scaling to **veRL** or **SkyRL-Agent** after the environment and synchronous learning
curve are stable.

| Framework | Why consider it | Risk |
|---|---|---|
| TRL `GRPOTrainer` | Official Hugging Face stack, familiar SFT/LoRA/GRPO path, fastest first proof. | Less specialized for remote multi-turn tool environments; may need glue code. |
| verifiers + prime-rl | Built around verifier environments, async GRPO, OpenAI-compatible inference clients, and FSDP-scale training. | Younger stack; lock versions and expect churn. |
| Agent Lightning | Published training-agent disaggregation and call-level credit assignment through an existing agent harness. | Newer stack; the reported tasks do not cover crash-prone CAD kernels. |
| NeMo Gym | Current environment-server, verifier, sandbox, trajectory, and multi-trainer integrations closely match the required separation. | Vendor-led beta stack and no independent systems paper; Python/runtime requirements and operational weight may be high. |
| veRL | Broad RL algorithm support and integrations with FSDP/Megatron/vLLM/SGLang. | More infra to own/debug; use after simple proof. |
| SkyRL-Agent | Published asynchronous multi-turn tool dispatcher with SWE-agent RL results and backend interoperability. | SWE-specific evidence and significant GPU scale; evaluate after synchronous env API stabilizes. |

## Base Model Shortlist

Start with permissive, small, strong tool/reasoning models:

1. **Qwen3 8B or 14B/32B family** - Apache 2.0 open weights, strong tool/reasoning ecosystem.
   Default recommendation for Phase 1.
2. **DeepSeek-R1-Distill-Qwen 7B/14B/32B** - MIT for DeepSeek release, Qwen-derived distilled
   reasoning models. Good comparison if reasoning traces help, but watch verbosity/cost.
3. **Mistral Small / Devstral Small class** - Mistral open models under Apache 2.0 where listed;
   useful if code/tool execution behaviour beats Qwen on builder-style tasks.
4. **Llama 3.1/3.x 8B class** - commercially usable under Meta's community license for most
   companies, but not OSI-style and has acceptable-use and large-user conditions. Keep as a
   benchmark candidate, not first default.

## Evaluation Outputs

Runtime promotion is out of scope for v1. The research report still compares each trained model
with Terra, Luna, deterministic rules, and its own pre-training checkpoint on:

- Repair defect yield on held-out source families and the sequestered NIST set.
- Damage and hard-gate failure rate.
- Adversarial safe-decline accuracy and detected reward-hacking attempts.
- Marginal inference cost and the fullest measurable loaded cost per verified repair.
- Episode wall/CPU/RSS, native crash/timeout rate, and policy-version lag.

No result changes runtime routing defaults.

## Deferred Variables

Before a paid training run, set its GPU dollar ceiling and record any usable credits. Before a
comparative frontier run, resolve the deployed Terra/Luna route versions, decoding settings, retry
policy, token/tool budget, and current price source. These values are run metadata, not blockers for
the Phase 0 harness.
