# CRS Repair RL Env v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first `crs-repair` post-training environment and rollout harness, starting with
measured native Phase 0 evidence and ending with verified SFT/rejection-sampling and synchronous
GRPO proof adapters.

**Architecture:** Keep native CAD work inside crash-isolated worker processes that call the existing repair stack. Expose a small HTTP `reset`/`step` contract that returns JSON observations and reward components, while durable trajectories capture verifier evidence, timings, versions, and artifacts.

**Tech Stack:** Python 3.11+, existing `meshpipeline.cad.repair` modules, FastAPI or stdlib HTTP test harness following repository conventions, multiprocessing/subprocess isolation, JSONL/GCS-ready trajectory files, pytest, TRL adapter stub.

**Spec:** `docs/superpowers/specs/2026-10-29-rl-post-training-envs-design.md`

## Global Constraints

- Do not change runtime routing defaults.
- Do not train on customer data in v1.
- Verified Terra/Luna trajectories may enter SFT or rejection-sampling datasets only after the
  same automated verifier gates as open-model trajectories.
- Do not weaken caps, rollback, re-inspection, progress rule, paired-rim guard, or repair settings defaults.
- Native Phase 0 measurement must run before throughput, CPU cost, or rollout capacity is claimed.
- Every trajectory records reward components separately from total reward.
- Accuracy is the primary experiment objective. Capture marginal and fully loaded costs as
  telemetry; do not optimize a dollar-cost reward.
- V1 covers OCCT real defects, NIST, generated defects, adversarial safe-decline, and small-face
  repair. Track and source-family provenance are required fields.
- Runtime promotion and mandatory human trajectory review are out of scope.
- Qwen3 is the default first open-model family; GPU spend remains blocked until a run ceiling is set.
- Environment worker processes are recyclable and crash-isolated; a native crash is a terminal episode record, not a lost rollout.

## Review Focus

- Same input and flags repeated across workers should produce the same verifier result or record nondeterminism explicitly.
- Native crash or timeout should emit exactly one terminal trajectory with crash/timeout status and no partial success.
- Per-episode repair flags should not bleed across episodes despite import-time settings.
- A malformed or unbindable model proposal should be penalized and logged, never guessed into a different target.
- Trajectory sink failures should be counted and visible; product-style fail-open capture is not sufficient for training evidence.

## Phase 0 Gate

Tasks 2-7 must not start until Task 1 has produced successful native evidence for wall time, CPU
time, RSS, determinism, and frontier/cache-replay baseline classification, unless the user
explicitly overrides that gate. A blocked Task 1 is an accepted plan outcome only when the evidence
README records the missing native prerequisite and the exact command that failed.

All `.venv/bin/python` commands below mean "run inside a native-capable repository environment or
container with `meshpipeline`, OCP/OpenCASCADE, and gmsh importable."

**Outcome on 2026-11-05: gate closed, recorded as blocked. Tasks 2-7 have not started.** Wall time,
CPU time, RSS, and determinism have native evidence from 48 episodes. Frontier/cache-replay baseline
classification does not: the baseline was not attempted and needs the user's decision on Terra/Luna
access. Because nothing was attempted, there is no failed command to record, so this block differs
from the blocked case described above. See
`devtools/validation/evidence/2026-10-29-crs-repair-phase0/README.md`.

---

### Task 1: Native Phase 0 Measurement

**Files:**
- Create: `devtools/validation/crs_repair_phase0.py`
- Test: `tests/unit/devtools/test_crs_repair_phase0.py`
- Output evidence: `devtools/validation/evidence/2026-10-29-crs-repair-phase0/README.md`

**Interfaces:**
- Consumes: `devtools/validation/repair_corpus.py` row shape; existing repair settings.
- Produces: `measure_inputs(paths: list[Path], repeats: int, planner: str) -> dict` with `rows`, `wall_seconds`, `cpu_seconds`, `max_rss_kb`, `determinism`.

- [x] **Step 1: Write failing tests for result aggregation**

Test that repeated synthetic row sets produce per-file wall/CPU summaries, detect mismatched `located_after`/`repaired_defects`, and preserve raw row paths.

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/devtools/test_crs_repair_phase0.py -q`  
Expected: FAIL because `devtools.validation.crs_repair_phase0` does not exist.

- [x] **Step 3: Implement `measure_inputs(paths: list[Path], repeats: int = 3, planner: str = "off") -> dict`**

Use subprocess execution around `repair_corpus.py` so native faults do not kill the controller.
Run one subprocess per file/repeat, or snapshot `RUSAGE_CHILDREN` before and after each
file/repeat, so wall/CPU/RSS can be reported per episode rather than only as a whole-run aggregate.
Parse rows JSON, collect process resource data from `resource.getrusage`, and compare
deterministic fields across repeats.

- [x] **Step 4: Add evidence README template**

Document exact command, interpreter, commit, OCP/gmsh availability, corpus path, and why a host without `.venv`/OCP/gmsh cannot satisfy Phase 0.

- [x] **Step 5: Run native measurement**

Run: `.venv/bin/python devtools/validation/crs_repair_phase0.py /path/to/occt-sample --limit 10 --repeats 3 --planner off --out devtools/validation/evidence/2026-10-29-crs-repair-phase0/off.json`  
Expected: JSON with wall/CPU/RSS and determinism; if native deps are absent, README records the blocked command and import failure.

### Task 2: Environment Data Contracts And Track Manifest

**Files:**
- Create: `src/meshpipeline/cad/repair/rl_env_contracts.py`
- Test: `tests/unit/cad/test_repair_rl_env_contracts.py`

**Interfaces:**
- Produces: `RepairEnvTrack`, `RepairEnvTask`, `RepairEnvObservation`, `RepairEnvAction`,
  `RewardComponents`, `TrajectoryRecord` dataclasses with `to_dict()`/`from_dict()` helpers.
- Consumes: repair report/entity dictionaries from the existing inspection path.

- [ ] **Step 1: Write failing serialization tests**

Assert round-trip JSON for every v1 track, a task with source-family/provenance fields, an action,
reward components, and a trajectory record containing wall/CPU/RSS and version metadata.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_env_contracts.py -q`

- [ ] **Step 3: Implement dataclasses in `rl_env_contracts.py`**

Keep them pure, import no OCP, and use only JSON-compatible field types.

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_env_contracts.py -q`

### Task 3: Crash-Isolated Episode Worker

**Files:**
- Create: `src/meshpipeline/cad/repair/rl_episode_worker.py`
- Test: `tests/unit/cad/test_repair_rl_episode_worker.py`

**Interfaces:**
- Consumes: `RepairEnvTask`, `RepairEnvAction`.
- Produces: `run_episode(task: RepairEnvTask, actions: Iterable[RepairEnvAction]) -> TrajectoryRecord`.

- [ ] **Step 1: Write failing tests with fake repair seams**

Cover successful finish, malformed proposal penalty, timeout terminal record, and worker exception terminal record.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_episode_worker.py -q`

- [ ] **Step 3: Implement worker controller with injectable repair seam**

The unit path uses fakes. Native imports happen only inside the real seam, not at module import.

- [ ] **Step 4: Add a native smoke test only if OCP is available**

Test a single existing fixture or generated loose-face sample behind `pytest.importorskip("OCP")`.

- [ ] **Step 5: Run unit and native-targeted tests**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_episode_worker.py -q`  
Run: `.venv/bin/python -m pytest tests/native -k "repair_rl_env or native_repair" -q`

### Task 4: Durable Trajectory Sink

**Files:**
- Create: `src/meshpipeline/cad/repair/rl_trajectory_sink.py`
- Test: `tests/unit/cad/test_repair_rl_trajectory_sink.py`

**Interfaces:**
- Produces: `TrajectorySink.write(record: TrajectoryRecord) -> WriteResult`.
- Consumes: `TrajectoryRecord`.

- [ ] **Step 1: Write failing tests for idempotent JSONL writes**

Assert duplicate `(task_id, episode_id, env_commit)` writes are idempotent, conflicting writes are refused, and write failures increment a visible loss counter.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_trajectory_sink.py -q`

- [ ] **Step 3: Implement local filesystem JSONL sink**

Keep GCS as a later adapter; v1 writes local files that can be uploaded by deployment glue.

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_trajectory_sink.py -q`

### Task 5: HTTP Env Server

**Files:**
- Create: `src/meshpipeline/cad/repair/rl_env_server.py`
- Test: `tests/unit/cad/test_repair_rl_env_server.py`

**Interfaces:**
- Exposes: `POST /reset`, `POST /step`, `GET /healthz`.
- Consumes: contracts and worker controller from earlier tasks.

- [ ] **Step 1: Write failing API tests**

Assert reset creates an episode, step returns observation/reward/done/info, invalid action returns 400, and worker crash returns `done=true` with crash metadata.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_env_server.py -q`

- [ ] **Step 3: Implement server using existing project web conventions**

If adding FastAPI dependencies is unnecessary because the project already has FastAPI, reuse it; otherwise keep the server behind existing runtime dependencies.

- [ ] **Step 4: Run API tests**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_env_server.py -q`

### Task 6: SFT, Rejection-Sampling, And GRPO Adapter Proof

**Files:**
- Create: `labs/rl_training/crs_repair_trl_adapter.py`
- Create: `labs/rl_training/README.md`
- Test: `tests/unit/labs/test_crs_repair_trl_adapter.py`

**Interfaces:**
- Consumes: HTTP env contract.
- Produces: verified trajectory export for SFT/rejection sampling and a minimal synchronous
  TRL-compatible prompt/reward adapter; no training run required in repository tests.

- [ ] **Step 1: Write failing adapter tests**

Use recorded trajectory fixtures and a fake HTTP env to assert hard-gate filtering, SFT export,
rejection-sampling selection, prompt construction, action parsing, reward extraction, and episode
termination.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/labs/test_crs_repair_trl_adapter.py -q`

- [ ] **Step 3: Implement adapter without importing TRL at module import**

Keep optional trainer imports inside CLI/runtime functions so unit tests do not require GPU/trainer deps.

- [ ] **Step 4: Document first-run command**

README includes Qwen3 default, LoRA/QLoRA posture, mandatory per-run cost ceiling placeholder,
accuracy-first evaluation, and the SFT -> rejection sampling -> synchronous GRPO order.

- [ ] **Step 5: Run adapter tests**

Run: `.venv/bin/python -m pytest tests/unit/labs/test_crs_repair_trl_adapter.py -q`

### Task 7: Documentation And Verification

**Files:**
- Modify: `docs/superpowers/specs/2026-10-29-rl-post-training-envs-design.md`
- Create: `docs/research/crs-repair-rl-env.md`

**Interfaces:**
- Consumes: evidence from Tasks 1-6.
- Produces: operator/developer documentation for running the benchmark and reading trajectories.

- [ ] **Step 1: Update docs with measured Phase 0 numbers**

Replace blocked measurement notes with actual wall/CPU/RSS/determinism results only after native evidence exists.

- [ ] **Step 2: Add env-server operation doc**

Document task source, no-customer-data rule, HTTP endpoints, trajectory schema, and failure interpretation.

- [ ] **Step 3: Run focused tests**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_rl_env_contracts.py tests/unit/cad/test_repair_rl_episode_worker.py tests/unit/cad/test_repair_rl_trajectory_sink.py tests/unit/cad/test_repair_rl_env_server.py tests/unit/labs/test_crs_repair_trl_adapter.py -q`

- [ ] **Step 4: Run host gates before any deploy**

Run: `make lint`  
Run: `make typecheck`  
Run: `make test-fast`  
Run: `make check-fast`

- [ ] **Step 5: Commit**

```bash
git add devtools/validation src/meshpipeline/cad/repair tests/unit/cad labs/rl_training tests/unit/labs docs/research docs/superpowers
git commit -m "feat: add crs repair rl environment"
```

## Self-Review Notes

Spec coverage: the plan covers Phase 0 measurement, all five v1 repair scopes, environment
contracts, crash isolation, trajectory durability, HTTP env contract, SFT/rejection-sampling and
synchronous GRPO proofs, and documentation. It intentionally does not implement `mesh-builder`,
customer data ingestion, runtime routing promotion, mandatory human review, or GPU infrastructure.

Open gap: the paid Phase 1 run remains blocked until its GPU ceiling and available credits are
recorded. That does not block repository-only adapters or CPU environment work.
