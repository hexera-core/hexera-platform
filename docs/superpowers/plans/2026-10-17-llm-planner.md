# LLM Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The model takes the repair planner's seat — shadow (logged, geometry untouched) and live (model-first directives under every slice-6 cap) behind one setting — validated by a measured corpus run, never by assertion.

**Architecture:** An async plan-ahead pre-pass in the repair node emits directive dicts (`source="llm-planner"`, stamped `directive_id`/`planner_confidence`) executed through the existing `DirectedPlanner` → session caps path; one shadow-row writer covers every node exit; the corpus tool grows `--planner` with a reply cache.

**Tech Stack:** The slice-6/7 repair stack; the `call_repair_review_model` seam; `devtools/validation/repair_corpus.py`.

**Spec:** `docs/superpowers/specs/2026-10-17-llm-planner-design.md` — read it INCLUDING the "Builder-check answers" section; those rulings bind this plan.

## Global Constraints

- `CAD_REPAIR_LLM_PLANNER=off` is byte-for-byte today's behaviour, pinned by test.
- Every model path fails open to the rules planner; a model failure is never a run's failure.
- The pre-pass is bounded (≤2 calls × `TIMEOUT_S`, outside the kernel timeout) and the second call is only a retry on malformed/timeout.
- LLM directives are machine-sourced: the DirectedPlanner rim guard applies; only single-target unpaired fills can ever play.
- Settings via the inventory + docs regen; config-doc test run DIRECTLY.
- Host gates: `make lint && make typecheck`; `pytest tests/unit/pipeline tests/unit/agents tests/unit/cad tests/unit/api tests/unit/application -q`; `pytest tests/native -k "repair or fixture or scout or review or spotlight or directives or minting or planner" -q`. Known pre-existing failures stand (bore_staging, config_parity, docker/venv families, builder run_python ×2).
- Commits: declarative behaviour sentences + the environment's attribution trailer. Never `git add -A` (two incidents on this branch); add named paths only.

## Review Focus

1. **A model reply that mixes valid and invalid proposals** — the valid ones must play, each invalid one logged as its own decline, never the whole reply discarded silently. (Task 2 test.)
2. **Shadow mode on a run that dies early** (timeout/unavailable) — the shadow row still lands with `not_executed`; no path loses the proposals. (Task 3 test.)
3. **Live with an empty/declining model** — the run is indistinguishable from `off` apart from the decline log. (Task 3 test.)
4. **A confidence exactly at the threshold** — `>= MIN_CONFIDENCE` plays; below declines; missing/non-numeric declines. (Task 2 test.)
5. **The corpus cache serving a stale prompt** — the cache key must cover the full prompt content so a changed report never replays the wrong reply. (Task 4 test.)

---

### Task 1: Settings and the workflow posture input

**Files:**
- Modify: `src/meshpipeline/settings/cad_repair.py`, `src/meshpipeline/settings/inventory.py`, regenerate `docs/reference/configuration.md`; `.github/workflows/deploy.yml`, `deploy/gcp/scripts/create-api-service.sh`, `deploy/gcp/scripts/create-worker-fleet.sh`, `deploy/gcp/worker/startup.sh`
- Test: `tests/unit/settings/test_llm_planner_settings.py`

**Interfaces:**
- Produces `rcfg.CAD_REPAIR_LLM_PLANNER: str` ("off"|"shadow"|"live", default "off"; any other value reads as "off" — fail closed), `rcfg.CAD_REPAIR_LLM_PLANNER_CALLS_MAX: int` (2), `rcfg.CAD_REPAIR_LLM_PLANNER_TIMEOUT_S: float` (20.0), `rcfg.CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE: float` (0.5).
- Workflow: a `llm_planner` choice input (off/shadow/live, default off) riding the `cad_repair` plumbing exactly (env pair when not "off", template metadata `cad-repair-llm-planner`, startup.sh injection beside its siblings).

- [ ] **Step 1: Failing test** — `test_llm_planner_settings_exist_with_dormant_defaults` (four names, defaults, inventory declaration; posture note must say off is byte-for-byte prior behaviour) and `test_an_unknown_posture_reads_as_off`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement settings + inventory + docs regen (config-doc test directly) + the four deploy files (`bash -n` the scripts; YAML parses).**
- [ ] **Step 4: Run the settings test file + the deploy-reading unit test files touched by the workflow change. PASS.**
- [ ] **Step 5: Commit.**

### Task 2: The planner agent — `llm_plan`

**Files:**
- Create: `src/meshpipeline/agents/repair_review/planner.py`
- Modify: `src/meshpipeline/agents/repair_review/_wire.py` only for shared clipping helpers
- Test: `tests/unit/agents/test_llm_planner_agent.py` (seam stubbed via `set_model_router`, the proposer tests' harness)

**Interfaces:**
- Produces `async llm_plan(report: dict, *, story: dict, job_id: str = "") -> PlanResult` — dataclass: `directives: list[dict]` (each the slice-7 wire dict + `directive_id: f"llm:{n}"`, `source: "llm-planner"`, `planner_confidence: float`, `defect_code` derived from the scan entry matching `defect_fingerprint`), `declines: list[dict]` (reason per dropped proposal), `called: int`, `model_reply_cached: bool` (False here; the corpus sets it). TOTAL: no router ⇒ empty result; one whole-part call (report clipped ≤40 defects via `_wire`), one retry on malformed/timeout; order preserved from the model.
- Validation drops (each a logged decline, `TrainingLogger("repair_review", op_id="review:plan")`): unknown op; fingerprint not in the scan; targets spanning different codes; confidence missing/non-numeric/`< MIN_CONFIDENCE` (exactly-at plays); fill with multiple targets.

- [ ] **Step 1: Failing tests** — `test_a_mixed_reply_plays_the_valid_and_declines_each_invalid_alone`, `test_confidence_at_the_threshold_plays_and_below_declines` (incl. missing/非numeric), `test_no_router_returns_empty_without_raising`, `test_one_retry_then_fail_open` (first call malformed, second good ⇒ played; both bad ⇒ empty + declines), `test_the_model_order_is_preserved_and_ids_are_stamped`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (prompt as module constants, product voice; context: clipped defect entries, tried, story counts + stopped_because).
- [ ] **Step 4: Run + `pytest tests/unit/agents -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 3: The node — postures, directive merge, one shadow writer

**Files:**
- Modify: `src/meshpipeline/pipeline/repair_attempt.py`, `src/meshpipeline/cad/repair/directives.py` (stamp pass-through for `directive_id`/`planner_confidence` onto the Candidate→op record, mirroring item_id/source), `src/meshpipeline/contracts/pipeline_state.py` + `src/meshpipeline/pipeline/data_contract.py` (any new state key), `src/meshpipeline/application/repair_review_minting.py` only if the outcome join needs the `directive_id` branch
- Test: `tests/unit/pipeline/test_llm_planner_node.py`, extend `tests/unit/pipeline/test_review_outcome_capture.py`

**Interfaces:**
- `off`: pinned byte-for-byte (a scripted-double test asserts the model seam is never consulted and the state out equals today's).
- `shadow`: `llm_plan` runs (own `wait_for` per call, outside the kernel timeout); ONE writer `_shadow_row(...)` logs `TrainingLogger("planner_shadow", op_id="review:shadow")` with proposals+declines, the rules run's ops+verdicts, `stopped_because`/`resolved` — called from success, refusal, AND the early-decline paths (there with outcome `not_executed`). Geometry path untouched.
- `live`: directives passed to `repair_step_file` = engineer/replay (state, FIRST, unchanged) + llm directives; outcomes join via the stamped `directive_id` (the outcome machinery keeps directives with `item_id` OR `directive_id`; the review-item writer ignores non-UUID ids); the training row's `directives` entries carry `planner_confidence`.

- [ ] **Step 1: Failing tests** — `test_off_is_byte_for_byte_and_never_consults_the_model`, `test_shadow_logs_one_row_on_success_refusal_and_early_decline` (parametrised over the three exits; early decline carries not_executed), `test_live_merges_engineer_first_then_llm_and_outcomes_join_by_directive_id`, `test_live_with_a_declining_model_matches_off_except_the_decline_log`, `test_a_rolled_back_llm_directive_never_touches_review_items` (non-UUID id ignored by the item writer).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run + `pytest tests/unit/pipeline tests/unit/cad -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 4: The corpus instrument — `--planner` with a reply cache

**Files:**
- Modify: `devtools/validation/repair_corpus.py`
- Test: `tests/unit/dev/test_repair_corpus_planner.py` (hermetic: scripted model via `set_model_router`; the tool's existing unit-test pattern if one exists, else a thin invocation test)

**Interfaces:**
- `--planner off|shadow|live` (default off) and `--reply-cache PATH` (JSON, key = sha256 of the full prompt content, value = raw reply). The tool drives `llm_plan` itself (it calls the loop directly, so it owns the async edge: `asyncio.run` around the pre-pass per file). Summary additions: shadow — agreement with the rules planner (same op+target on the first proposal), coverage (parts where the model proposed anything), decline counts; live — per-op kept/rolled-back for `llm-planner`-sourced directives, aggregate repaired-defect count beside the rules-only figure from the same invocation (run both when `--planner live`: rules-only first, then live, so the never-worse aggregate is measured in one report).
- Cache behaviour pinned: a hit skips the model call; a changed prompt misses (Review Focus 5).

- [ ] **Step 1: Failing tests** — `test_the_cache_key_covers_the_prompt_content`, `test_live_reports_aggregate_against_rules_only`, `test_shadow_touches_no_geometry` (output files byte-identical to an off run on the same fixture).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run + the tool's existing tests. PASS.**
- [ ] **Step 5: Commit.**

### Task 5: Native proof — a stalled part repaired by the model's choice

**Files:**
- Create: `tests/native/test_native_llm_planner.py`
- Test: itself.

**Interfaces:** a scripted router (native tier may use the seam stub) whose reply proposes `fix_shape_global` (or the sequence that works) for a fixture the RULES planner stalls on — build the fixture from the slice-6 set (the open-shell box the rules loop cannot close is a candidate; the implementer verifies which fixture genuinely stalls rules-only and is repaired live, and says so in the report; if NO fixture satisfies both, the test instead pins: live-with-stub plays the model's directive first, the caps judge it, and the outcome rows carry `llm-planner` provenance — honesty over theatre).

- [ ] **Step 1: Write the test red-first** (stub router absent ⇒ provenance assertions fail).
- [ ] **Step 2–3: Implement fixture choice + assertions; run `pytest tests/native -k planner -q`. PASS.**
- [ ] **Step 4: Commit.**

### Task 6: Evidence — corpus runs, deploy, PR

No source changes; evidence only, by the controller or one subagent with the exact commands.

- [ ] **Step 1: Host gates** (Global Constraints list), outputs captured.
- [ ] **Step 2: Corpus SHADOW run** over `/tmp/cadcorpus/occt/opencascade-dataset-7.8.0/step` with the real model if a provider key is in the host env (check `DEEPINFRA_API_KEY`/`DEEPSEEK_API_KEY`/`OPENAI_API_KEY`), else the scripted stub — the report says WHICH, verbatim. Reply cache committed beside the demo evidence (not the repo).
- [ ] **Step 3: Corpus LIVE run** (same key rule): aggregate repaired-defect count ≥ the rules-only figure from the same invocation. If the real-model run is unavailable, the stub live run proves the plumbing and the deployed shadow rows are the real-model evidence — stated honestly.
- [ ] **Step 4: Deploy** `gh workflow run deploy.yml --ref <branch> -f slug=pranav -f app=true -f console=true -f fleet=true -f cad_repair=true -f llm_planner=shadow`; after the worker converges, drive one defective upload (the slice-7 recorder path) and verify the run's capture carries `planner_shadow` rows — via the training capture the corpus/API expose, not gcloud.
- [ ] **Step 5: Update PR #201** (slice 8 section + the measured numbers), post the corpus tables as a PR comment.

---

## Self-review (at writing time)

- Spec coverage: postures (T1/T3), bounded pre-pass (T2/T3), one shadow writer incl. early exits (T3), machine rim guard + stamps (T3 via directives.py), corpus instrument + cache + aggregate bound (T4), native proof (T5), deploy posture input (T1) and evidence (T6). Non-goals respected: no new ops, no review changes.
- Wire consistency: directive dict extensions (`directive_id`, `planner_confidence`) defined in T2, consumed T3/T4; `PlanResult` defined T2 consumed T3/T4.
- Review Focus lines own tests in T2/T3/T4.
- Proportion: plan ≈ spec; signatures and test names only.
