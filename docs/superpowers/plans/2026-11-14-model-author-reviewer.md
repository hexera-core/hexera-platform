# Model Author and Semantic Reviewer Implementation Plan (Plan 3 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A model authors the region packages (choosing the operation, stating the claim, or declaring "the CAD is sound — the mesher is the problem"), and a second, separate model reviews every committed package on measured evidence — the owner's replacement for any numeric "barely moved" gate.

**Architecture:** The author plugs into the region loop's existing seam — `author(region, defects, attempts) -> TargetPackage | None` (`region_loop.py:501`, shape set by `rules_author.py:253`) — so the loop, caps, snapshot/revert and predicate machinery are untouched. Model calls go through the bounded wire already used by repair review (`agents/repair_review/_wire.ask(system, user, *, job_id, timeout_s) -> dict | None`, with `configured()` failing closed). The reviewer is a separate call with its own prompt that sees measured evidence only; reject reverts the package exactly as a failed predicate does. The deterministic `rules_author` stays the benchmark baseline (spec decision 6).

**Tech Stack:** Python 3.12, the existing model-inference router (`contracts/model_inference`), `cad/repair/` package machinery, pytest. No new model SDK.

**Spec:** `docs/superpowers/specs/2026-11-14-mesh-failure-repair-design.md` ("C. The model author", "Owner Answers" §2 — the semantic review design) and its parent. Owner decisions binding here: no numeric deviation gate; reviewer separate from author; unsure = reject; every verdict recorded; shadow before live; "mesher's problem" scores unresolved until the mesher-side plan exists; reserved judgement codes stay untouched (mesh codes are not reserved); benchmark reproducibility via recorded-reply cache.

## Global Constraints

- Two new postures, fail closed, registered like every `CAD_REPAIR_*` setting: `CAD_REPAIR_LLM_AUTHOR` (`off`/`shadow`/`live`) and `CAD_REPAIR_PACKAGE_REVIEW` (`off`/`live`). Unknown values read `off` with a warning log. `CAD_REPAIR_PACKAGE_REVIEW=live` requires the mesh probe live to have meshability evidence; it must not crash without it (evidence rows simply lack mesh fields).
- Off is byte-for-byte today: no model call, no prompt import, every existing test green (known pre-existing failures: 9 unit/cad environmental, 3 native fill_nonplanar timing).
- A model reply is DATA, never code or a cap waiver: schema-validate hard; any parse failure, timeout, unconfigured router, or over-budget call is `None`/reject. The author's plan may only name operations in the active catalogue; `answered_held` and reserved codes are unreachable from a model reply (the Plan-1 branch removed that field from plan shape — keep it removed).
- Unit tests run with the router unconfigured and no network; model calls are faked at the `ask` seam. Corpus runs use the runner's recorded-reply cache (`--reply-cache`, `CachingRouter`) so a sealed benchmark replays byte-identically.
- Prompts live in the module as named constants; every call and reply (clipped) is recorded in the repair report for audit — the report is the training log.
- One commit per task, targeted `git add`. Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- venv `.venv`; gates per commit: `make lint`, `make check-fast`, `make typecheck` (pre-existing `fea_joins` error), `pytest tests/unit/cad -q`, native repair tier for flag-off identity.

## Review Focus

1. **A reply claiming a fingerprint nobody reported, or an operation not in the catalogue.** Expected: the package is refused before anything runs (the phantom-reclassification lesson). Pinned in Task 2 (`test_reply_naming_unknown_targets_is_refused`).
2. **The reviewer fed the author's self-assessment as evidence.** Expected: the reviewer's evidence dict is built from measurements only; the author's text appears solely under an `intent` key labelled as a claim. Pinned in Task 4 (`test_reviewer_evidence_is_measured_only`).
3. **A reviewer timeout, parse failure, or unconfigured router on a committed package.** Expected: reject → revert; never a silent accept. Pinned in Task 4 (`test_reviewer_no_answer_rejects`).
4. **"Mesher's problem" as a dodge on a hard region.** Expected: verdict recorded, region's defects stay undischarged, run cannot end `resolved` because of it. Pinned in Task 5 (`test_mesher_problem_discharges_nothing`).
5. **Shadow mode touching geometry.** Expected: in `shadow`, the rules author drives; model proposals are recorded beside the run and execute nothing. Pinned in Task 2 (`test_shadow_records_and_executes_nothing`).

---

### Task 1: The observation builder

**Files:**
- Create: `src/meshpipeline/cad/repair/observe.py`
- Test: `tests/unit/cad/test_repair_observe.py`

**Interfaces:**
- Produces: `observation_of(region: Region, defects: tuple, attempts: tuple[PackageVerdict, ...], shape, *, original: Measure) -> dict` — JSON-safe, size-bounded (reuse `_wire.clip_text`/`clip_measurements` patterns): region bbox; per defect: code, fingerprint, severity, measurements (incl. gmsh `message` and `stage`); per target face: surface type (`BRepAdaptor_Surface().GetType()` name), area, bbox, neighbour count; per prior attempt: ops with status and reason, predicate failure text; the caps that bind a package (max area/volume deltas, op and time budgets).

- [ ] **Step 1: Failing tests** — `test_observation_is_json_and_bounded` (json.dumps round-trips; byte length under 16_000 for a 30-defect region), `test_attempt_failures_are_carried` (a reverted attempt's reason appears), `test_no_kernel_needed_for_located_fields` (faces summarised via an injectable face-reader so the test is hermetic). RED.
- [ ] **Step 2: Implement; PASS; gates; commit** `feat(repair): the observation a model author reads`.

### Task 2: The model author

**Files:**
- Create: `src/meshpipeline/cad/repair/model_author.py`
- Modify: `src/meshpipeline/cad/repair/conservative.py` (driver selection where `rules_author` is wired, ~line 876), settings files
- Test: `tests/unit/cad/test_repair_model_author.py`

**Interfaces:**
- Consumes: Task 1's `observation_of`; `_wire.ask` (via a sync bridge: `asyncio.run` in a worker thread, the way the repair-review pre-pass invokes it — follow `agents/repair_review/planner.py`); `rules_author.rules_author` for shadow.
- Produces: `model_author(shape, *, original: Measure, reserved: frozenset, job_id: str) -> Callable[[Region, tuple, tuple[PackageVerdict, ...]], TargetPackage | None]`, `LABEL = "regions-model-v1"`. Reply schema (one JSON object): either `{"package": {"intent": str, "plan": [{"op": str, "fingerprint": str}], "cleared": [str], "unchanged": [str], "max_area_delta_mm2": float, "max_volume_delta_mm3": float}}` or `{"verdict": {"mesher_problem": [fingerprints], "reason": str}}` or `{"pass": true}` (nothing to propose → author returns None, which ends the region's attempts — region_loop.py:503). Budget: at most `CAD_REPAIR_REGION_ATTEMPTS` calls per region, `CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S` (default 20.0) each.
- Validation refuses: ops not in the active catalogue, fingerprints not among the region's defects (for `cleared`) or the part's entities (for `unchanged`), area/volume claims above the run caps, an empty plan with no verdict.

- [ ] **Step 1: Failing tests** (fake `ask`) — `test_valid_reply_becomes_a_package`, `test_reply_naming_unknown_targets_is_refused`, `test_timeout_or_garbage_is_none`, `test_shadow_records_and_executes_nothing` (shadow: the returned author delegates to `rules_author`'s author and appends the model's proposal to a `shadow_proposals` list on the report), `test_second_attempt_sees_the_first_failure` (the observation passed on call 2 contains attempt 1's revert reason), `test_call_budget_is_respected`. RED.
- [ ] **Step 2: Implement; PASS.** Driver selection: `CAD_REPAIR_LLM_AUTHOR=live` + router configured → model author; `shadow` → rules drives, model records; else rules author exactly as today.
- [ ] **Step 3: Report plumbing** — each call's observation hash, reply (clipped), and validation outcome land on the repair report (`model_author` section). Test: `test_report_carries_calls`.
- [ ] **Step 4: Flag-off identity, gates, commit.**

### Task 3: Deviation evidence

**Files:**
- Create: `src/meshpipeline/cad/repair/deviation.py`
- Modify: `src/meshpipeline/cad/repair/session.py` (attach to PackageVerdict evidence), `src/meshpipeline/pipeline/repair_receipt.py` (surface it)
- Test: `tests/unit/cad/test_repair_deviation.py`, extend `tests/native/test_native_repair_region_loop.py`

**Interfaces:**
- Produces: `surface_deviation_mm(before_shape, after_shape, faces: tuple[str, ...] = (), *, samples: int = 32, budget_s: float = 10.0) -> float | None` — sampled max distance from points on the changed/added faces of `after_shape` to `before_shape` (`BRepExtrema_DistShapeShape`); `None` on any failure or empty sample (measured evidence, never a gate — spec Owner Answer 2). Recorded per committed package as `deviation_mm` beside the declared area/volume.

- [ ] **Step 1: Failing unit tests** (injectable distance seam) — `test_deviation_is_max_of_samples`, `test_failure_is_none_not_raise`, `test_budget_bounds_sampling`. RED.
- [ ] **Step 2: Implement; PASS.** Native: a knee/blade fill run records a plausible (>0, < part diagonal) deviation.
- [ ] **Step 3: Gates, commit.**

### Task 4: The semantic reviewer

**Files:**
- Create: `src/meshpipeline/agents/package_review/__init__.py`, `reviewer.py` (reusing `repair_review/_wire` helpers)
- Modify: `src/meshpipeline/cad/repair/region_loop.py` (verdict before a package's commit becomes durable), `session.py` (revert path reuse), settings files
- Test: `tests/unit/agents/test_package_reviewer.py`, `tests/unit/cad/test_repair_region_loop.py`

**Interfaces:**
- Produces: `review_package(evidence: dict, *, job_id: str, timeout_s: float) -> tuple[str, str]` returning `("accept"| "reject", reason)`; evidence dict built by a pure `evidence_of(package, verdict, deviation_mm, probe_before, probe_after) -> dict` containing ONLY: op names with kept/rolled-back status, declared vs measured area/volume, faces/solids counts before/after, `deviation_mm`, mesh verdicts before/after (kinds), the defect codes cleared, and the author's `intent` under a key named `author_claim`. No author self-assessment, no raw reply text.
- Region-loop wiring: with `CAD_REPAIR_PACKAGE_REVIEW=live`, a package that passed its predicate is handed to the reviewer before commit; reject → the package's snapshot revert (same path as a failed predicate) with the reason recorded on the PackageVerdict (`reviewed="reject"`, `review_reason`); accept → commit with `reviewed="accept"`. Any no-answer → reject. Verdicts and evidence land in the report and reuse the `repair_review` persistence tables where the pipeline already writes them.

- [ ] **Step 1: Failing tests** — `test_reviewer_evidence_is_measured_only` (evidence_of output keys are exactly the list above), `test_reject_reverts_the_package`, `test_reviewer_no_answer_rejects`, `test_accept_commits_with_verdict_recorded`, `test_review_off_is_byte_identical`. RED.
- [ ] **Step 2: Implement; PASS.** Prompt: instructs the reviewer it is judging whether the edit preserved the part's design while making it meshable; reject when unsure; answer one JSON object `{"verdict": "accept"|"reject", "reason": str}`.
- [ ] **Step 3: Flag-off identity, gates, commit.**

### Task 5: The "mesher's problem" verdict path

**Files:**
- Modify: `src/meshpipeline/cad/repair/region_loop.py`, `pipeline/repair_receipt.py`
- Test: `tests/unit/cad/test_repair_region_loop.py`

**Interfaces:**
- Produces: when the author returns the verdict form, the region stops authoring, a `mesher_problem` record (fingerprints, reason, author label) is added to the run report, the region's defects stay undischarged, and the receipt renders it ("the repair judged this geometry sound; the mesher could not handle it at: ..."). `_terminal` is untouched: such a run still ends `unmeshable`/fallback by the existing rules, scoring unresolved (spec: verified only when the mesher-side plan exists).

- [ ] **Step 1: Failing tests** — `test_mesher_problem_discharges_nothing` (same stop reason as without the verdict), `test_verdict_is_recorded_with_reason`, `test_receipt_renders_the_verdict`. RED.
- [ ] **Step 2: Implement; PASS; gates; commit.**

### Task 6: Evaluate on the corpus

**Files:**
- Create: `devtools/validation/evidence/2026-11-15-model-author/README.md` + sealed bundles + reply cache JSON (model outputs; stored under evidence, no CAD)

- [ ] **Step 1:** Three sequential runs of `repair_corpus.py --mesh-probe` on the 80-file track with the Plan-2 flags on and probe live: (a) rules baseline, (b) model author live + reviewer live via `--reply-cache` recording, (c) the 40-clean no-op control with the model live. Plus the NIST 33 as a regression arm (rules vs model).
- [ ] **Step 2:** Evidence README: newly_meshed model vs rules (the model must beat the baseline for this plan to claim value — say so plainly either way), newly_broken (**0 required**), reviewer accept/reject counts with reasons, deviation distribution of accepted packages (the owner's watching-glass for the no-numeric-gate decision), mesher-problem verdicts (count, and that they scored unresolved), clean-control mutations (**0 required**), model calls and cost. "What this does NOT show": reviewer accuracy is unvalidated against ground truth; gmsh ≠ product mesher; one platform.
- [ ] **Step 3: Commit** `Evidence: model author and semantic reviewer on the real-world track`.

---

**Blocked-on note for the executor:** Tasks 2, 4 and 6 need the model router configured in the environment (`agents/repair_review/_wire.configured()`); without it, unit tests still pass (faked seam) but Task 6's recording run cannot be made — stop and report rather than fabricating a cache. Plan 4 (production route: the FEA ladder's MESH-2 refusal hands faces back to repair) is a separate document and comes after this plan proves the loop on the benchmark.
