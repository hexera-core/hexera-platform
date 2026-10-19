# Repair Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The unsure residue of the autonomous CAD repair becomes an in-console review the meshing engineer does — one defect at a time on the geometry, grouped with sample-then-apply-all, LLM proposing and interpreting — with runs meshing first and parking in `pending_review` only when the mesh fails.

**Architecture:** Per-defect triage replaces the job-level gate so repairables fix silently while judgement defects mint `ReviewItem`s; parks persist items plus the kept-op sequence; the re-run replays kept ops and the engineer's directives through a `DirectedPlanner` that wraps the slice-6 machinery (caps/rollback/progress rule untouched); a new customer API and a full-screen console surface drive it; every decision is a training row.

**Tech Stack:** Python/FastAPI/SQLAlchemy/alembic, OpenCASCADE (OCP) via the slice-6 repair package, the legacy `ui/js` console bundle (vanilla ES modules + the existing vtk.js geometry stage), model calls through `contracts/model_inference.py`.

**Spec:** `docs/superpowers/specs/2026-10-17-repair-review-design.md` (read it first; it carries the user's verbatim decisions).

## Global Constraints

- Everything new is behind `CAD_REPAIR_REVIEW_ENABLED` (default `false`); with it off, nothing is minted, no route exists, no run parks.
- The slice-6 safety machinery is never weakened: review-approved operations execute only through `RepairSession` under every cap; an approval is a planner directive, not a cap waiver.
- The upload is immutable; every re-run derives from it. Parked runs hold no workspace.
- Nothing ever replaces a delivered mesh. Terminal states are never overwritten except `pending_review → pending` (re-run) which this plan legalises.
- Apply-all never crosses a rim pairing; paired-rim items are individual-review only.
- New settings go through `src/meshpipeline/settings/inventory.py` and the docs roster regen; verify with the configuration-documentation test directly (NOT `make check-fast`, which skips it — slice-6 lesson).
- Host gates before any deploy: `make lint && make typecheck`, `pytest tests/unit/cad tests/unit/pipeline tests/unit/api tests/unit/application -q`, `pytest tests/native -k "repair or fixture or scout or review" -q`, `CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" pytest tests/ui -q`.
- Known pre-existing failure: `tests/unit/cad/test_bore_staging.py::test_a_flanged_tube_stages_its_bore_alone`. Any OTHER failure is yours.
- Native tests assert on `localize_scan` output, never on `measure().valid` and never on counts inferred from in-memory construction (slice-6 kernel facts: `IsValid()` is True for an open shell; STEP readers wrap loose faces in per-face shells; `BRepCheck` never attributes `BadOrientation`).
- Copy rules: product voice is lowercase, explanatory, no jargon dumps; errors say what to change. Commit messages are declarative sentences about behaviour (read `git log`), ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Review Focus

Spec-implied failure modes no happy-path test covers; each line's test is added to the owning task:

1. **Apply-all onto a vanished entity** — a group member whose fingerprint no longer matches at re-run must come back as "could not be done safely", never silently skipped or misapplied. (Task 4, directive `target_absent` test.)
2. **A re-run that parks again** — newly discovered unsure items make a second park; prior decisions survive as history and nothing auto-loops without an explicit Run-it. (Task 6 test.)
3. **Review actions against the wrong job or wrong owner** — a decision POST on a job that is not `pending_review`-parked (or not yours) is refused with 409/404 and records nothing. (Task 8 test.)
4. **A park whose every proposal failed open** — LLM unreachable and no rules proposal: items render with leave-it/redirect only, and Run-it still works. (Task 8 API test + Task 9 UI test.)
5. **Cancel while parked** — owner cancels a `pending_review` job: the park closes, the review API answers 409, no orphan review remains actionable. (Task 6 test.)

---

### Task 1: Settings

**Files:**
- Modify: `src/meshpipeline/settings/cad_repair.py`, `src/meshpipeline/settings/inventory.py` (the existing "CAD repair" group)
- Regenerate: `docs/reference/configuration.md` (the inventory's roster regen path)
- Test: `tests/unit/settings/test_cad_repair_review_settings.py`

**Interfaces:**
- Produces: `rcfg.CAD_REPAIR_REVIEW_ENABLED: bool` (default False), `rcfg.CAD_REPAIR_REVIEW_SAMPLE_SIZE: int` (default 3), `rcfg.CAD_REPAIR_REVIEW_LLM_CALLS_MAX: int` (default 4), `rcfg.CAD_REPAIR_REVIEW_LLM_TIMEOUT_S: float` (default 20.0) — all read once at import like their neighbours.

- [ ] **Step 1: Write the failing test** — `test_review_settings_exist_with_dormant_defaults`: import `meshpipeline.settings.cad_repair as rcfg`; assert the four names above with exactly those defaults; assert `CAD_REPAIR_REVIEW_ENABLED` is declared in the inventory group "CAD repair" with `kind="bool"` and default `"false"`.
- [ ] **Step 2: Run it** — expect FAIL (AttributeError).
- [ ] **Step 3: Add the four vars** to `cad_repair.py` (same `bool_env`/`optional_env` idiom as neighbours) and declare all four in `inventory.py` with one-line notes in the inventory's voice (`REVIEW_ENABLED` note must say review mints nothing and no run parks while false).
- [ ] **Step 4: Regenerate the docs roster** the way the inventory's header says; run the configuration-documentation test DIRECTLY and the new test. Expect PASS.
- [ ] **Step 5: Commit.**

### Task 2: Persistence — parks and items

**Files:**
- Create: `alembic/versions/0015_repair_review.py`, `src/meshpipeline/persistence/repositories/repair_review_repository.py`
- Modify: `src/meshpipeline/persistence/models.py`
- Test: `tests/unit/persistence/test_repair_review_repository.py` (sqlite in-memory, same harness as neighbouring repo tests)

**Interfaces:**
- Produces models `RepairReviewPark` (table `repair_review_parks`: `id` uuid pk, `job_id` FK jobs, `created_at`, `status` in {`open`,`closed`}, `closed_at` nullable, `closed_by` in {`run`,`cancel`} nullable, `kept_operations` JSON, `source_sha256` str, `geometry_session_id` str) and `RepairReviewItem` (table `repair_review_items`: `id` uuid pk, `park_id` FK nullable — **nullable because items are also minted on delivered runs that never park**, `job_id` FK, `group_key` str, `defect_code` str, `severity` str, `entity_type` str, `entity_index` int, `fingerprint` str, `measurements` JSON, `message` str, `origin` in {`triage`,`stall`,`refusal`,`survivor`}, `pairing_key` str default `""`, `proposal` JSON nullable (`{"op","targets","rationale","provenance"}`), `camera` JSON (`{"centroid_mm","normal","extent_mm"}`), `state` in {`open`,`accepted`,`left`,`redirected`,`unreviewed`}, `decision` JSON nullable, `decided_at` nullable).
- Produces `RepairReviewRepository` with: `create_park(job_id, kept_operations, source_sha256, geometry_session_id) -> RepairReviewPark`; `add_items(job_id, park_id|None, items: list[dict]) -> list[RepairReviewItem]`; `open_park(job_id) -> RepairReviewPark|None`; `items_for(job_id, park_id|None) -> list[RepairReviewItem]`; `record_decision(item_id, state, decision: dict) -> RepairReviewItem` (refuses when park closed); `record_group_decision(job_id, group_key, state, decision) -> int` (count updated; MUST refuse any item whose `pairing_key` is non-empty); `close_park(park_id, closed_by) -> None`.

- [ ] **Step 1: Failing tests** — `test_a_park_holds_its_items_and_closes_once`, `test_group_decision_never_touches_a_paired_item` (two items same `group_key`, one with `pairing_key="rims:abc"`; group decision updates 1, paired item stays `open`), `test_decisions_refuse_on_a_closed_park`.
- [ ] **Step 2: Run — FAIL** (no module).
- [ ] **Step 3: Migration + models + repository.** Migration numbering/style follows `0014_repaired_cad_artifact.py`.
- [ ] **Step 4: Run new tests + `pytest tests/unit/persistence -q`. PASS; alembic upgrade/downgrade round-trips on the test harness the neighbouring migration tests use.**
- [ ] **Step 5: Commit.**

### Task 3: Spotlight — camera payload per defect

**Files:**
- Create: `src/meshpipeline/cad/repair/spotlight.py`
- Test: `tests/native/test_native_repair_spotlight.py`

**Interfaces:**
- Consumes `sub_shape(shape, entity_type, entity_index)` and `box_of(sub)` from `cad/repair/operations.py`; `EntityDefect` from `localize.py`.
- Produces `camera_for(shape, defect: EntityDefect) -> dict` returning `{"centroid_mm":[x,y,z], "normal":[x,y,z] unit, "extent_mm": float}` — centroid and extent from the entity's bounding box; normal: the owning/adjacent face's outward normal at the entity (faces: own normal; edges/wires: the first adjacent face's), falling back to the vector from the shape's centre through the entity's centroid when no face answers. Never raises: an entity it cannot resolve yields the shape-centre fallback with `extent_mm` of the shape's diagonal/20.

- [ ] **Step 1: Failing native tests** — on `write_loose_faces_step` + `localize_scan`: `test_every_located_defect_gets_a_finite_camera` (all six items: finite centroid within the part's bbox, unit normal, 0 < extent ≤ diagonal); on `write_missing_bore_wall_step`: `test_a_rim_camera_points_along_the_bore_axis` (normal within 15° of the bore axis — the fixture's known axis).
- [ ] **Step 2: Run — FAIL.**  `pytest tests/native/test_native_repair_spotlight.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run native file + `pytest tests/native -k "repair or fixture" -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 4: Directives — DirectedPlanner and the loop

**Files:**
- Create: `src/meshpipeline/cad/repair/directives.py`
- Modify: `src/meshpipeline/cad/repair/conservative.py` (`repair_step_file` gains `directives: tuple[dict,...] = ()`), `src/meshpipeline/cad/repair/plan.py` only if a shared helper is needed
- Test: `tests/unit/cad/test_repair_directives.py` (scripted planner/session doubles, same style as the loop's unit tests), `tests/native/test_native_repair_directives.py`

**Interfaces:**
- A directive dict (the wire format the API and dispatch payload carry): `{"op": str, "targets": [fingerprint,...], "defect_code": str, "defect_fingerprint": str, "source": "replay"|"engineer", "item_id": str|""}`.
- Produces `DirectedPlanner(directives, fallback)` with `label = "directed-v1"` and `propose(self, defects, history: RunHistory, *, shape) -> Candidate | None`: consumes directives in order — each resolved against the CURRENT defect list/fingerprints to a `Candidate` (op looked up in the slice-6 catalogue by name; targets resolved by fingerprint). A directive whose op name is unknown or whose every target fingerprint is absent is NOT proposed; it is recorded on the planner as `self.unplayable: list[dict]` (directive + reason `"target_absent"`/`"unknown_op"`) and skipped. When directives are exhausted, delegate to `fallback.propose(...)` (a `RulesPlanner` by default). `RepairRun` must end up carrying the unplayable list — thread it through the loop's refusals tuple as `{"refusal": "directive_unplayable", ...}` entries so nothing is silently dropped.
- `repair_step_file(source, destination, *, profile=..., directives=())` builds `DirectedPlanner(directives, RulesPlanner())` when `directives` is non-empty, else exactly today's behaviour.

- [ ] **Step 1: Failing unit tests** — `test_directives_are_proposed_before_the_rules_planner`, `test_an_unplayable_directive_is_recorded_not_skipped_silently` (vanished fingerprint ⇒ appears in refusals as `directive_unplayable` with `target_absent`), `test_exhausted_directives_fall_back_to_rules`, `test_a_replay_directive_and_an_engineer_directive_keep_their_source`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement `directives.py`; wire `repair_step_file`.**
- [ ] **Step 4: Failing native test** — `test_replayed_sew_then_rules_leaves_nothing_behind`: run `repair_step_file` on `write_loose_faces_step` once, take the kept op from the report as a replay directive, run again from the ORIGINAL with that directive: the directive's sew is the first attempt and the outcome is not worse than the undirected run (same final defect count, kept ≥ 1).
- [ ] **Step 5: Run unit + native. PASS. Also `pytest tests/unit/cad tests/unit/pipeline -q` for the blast radius.**
- [ ] **Step 6: Commit.**

### Task 5: Per-defect triage and item minting in the node

**Files:**
- Modify: `src/meshpipeline/cad/repair/triage.py`, `src/meshpipeline/pipeline/repair_attempt.py`
- Create: `src/meshpipeline/application/repair_review_minting.py`
- Test: `tests/unit/cad/test_triage_per_defect.py`, `tests/unit/pipeline/test_repair_attempt_minting.py`, extend `tests/native/test_native_cad_repair.py` only if an existing assertion breaks honestly

**Interfaces:**
- Produces `triage.split(report) -> DefectSplit` — dataclass with `repairable: tuple[EntityDefect,...]`, `judgement: tuple[tuple[EntityDefect, str reason],...]` (reason in the product voice, e.g. "a very small face is usually intentional — a chamfer, a logo — so a person decides"). Codes `small_face` and `invalid_brep`, plus anything today's rules route to `manual_cleanup`/`ask_customer`, land in `judgement`; everything the catalogue answers lands in `repairable`. `recommend(...)` keeps its signature; `ROUTE_CONSERVATIVE_REPAIR` is now returned whenever `repairable` is non-empty OR review is enabled and `judgement` is non-empty (gate: `rcfg.CAD_REPAIR_REVIEW_ENABLED`; with it false, today's behaviour byte-for-byte — pin that with a test).
- Produces `mint_items(*, shape, report, repair_run_measured: dict, split: DefectSplit) -> list[dict]` in `repair_review_minting.py`: one item dict per spec source — judgement defects (`origin="triage"`), survivors of a kept run (`origin="survivor"`), every surviving defect of a stalled/all-rolled-back run (`origin="stall"`, carrying what was tried from `operations`), planner refusals (`origin="refusal"`, `pairing_key` filled for `paired_rim` from the refusal's fingerprints). Each item carries `camera_for(shape, defect)` (Task 3) and `group_key = f"{defect_code}:{proposed_op or 'none'}"`. Proposals here are RULES provenance only (what the planner would try); LLM fills gaps in Task 7.
- `node_repair_attempt` output gains `"repair_review_candidates": [item dicts]` in its returned state (empty list when review disabled or nothing unsure). THE NODE MINTS, THE TERMINAL PATH PERSISTS — the node has the shape and the report; the DB write happens in Task 6 where sessions exist.

- [ ] **Step 1: Failing tests** — `test_with_review_off_triage_is_unchanged` (a small_face+open_shell report routes `manual_cleanup` exactly as today), `test_with_review_on_repairables_run_and_judgement_defects_mint` (same report: loop invoked, candidates contain the small_face with origin `triage` and the open_shell absent because it was repaired — use the scripted doubles style of the node's existing tests), `test_a_stalled_run_mints_every_survivor_with_what_was_tried`, `test_paired_rim_refusals_mint_with_a_pairing_key`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement split + minting + node wiring.** The node's `_declined` paths are untouched for non-review reasons (disabled, timeout, unsupported suffix).
- [ ] **Step 4: Run the two unit files + `pytest tests/unit/pipeline tests/unit/cad -q` + `pytest tests/native -k "repair" -q`. PASS (plus the known bore_staging failure only).**
- [ ] **Step 5: Commit.**

### Task 6: The park — terminal path, state machine, wording

**Files:**
- Modify: `src/meshpipeline/persistence/job_state.py` (LEGAL_SOURCES: add `JobStatus.pending_review` to `pending`'s sources), `src/meshpipeline/application/pipeline_run.py` (terminal path), `src/meshpipeline/application/final_result.py` (outcome wording for a parked run), `src/meshpipeline/application/job_cancel.py` only if closing the park on cancel needs a hook
- Test: `tests/unit/persistence/test_job_state_review.py`, `tests/unit/application/test_pipeline_run_parks.py` (the existing pipeline_run test harness with mocked repos)

**Interfaces:**
- Consumes Task 2's repository and Task 5's `repair_review_candidates`.
- Park rule, exactly: review enabled AND derive_terminal_status would be `failed` AND candidates non-empty AND the job was not cancelled ⇒ status becomes `pending_review`; a `RepairReviewPark` row is created with `kept_operations` = the kept entries of `state["repair_attempt"]["operations"]`, `geometry_session_id` from the run's geometry session, `source_sha256` of the upload; items persisted with `park_id`. On a SUCCEEDED run with candidates: items persisted with `park_id=None`, status stays `succeeded`, delivery untouched.
- The conversation gets one assistant message on park (through the same channel the outcome message uses): the outcome text for a parked run must say the run is waiting, how many things, and that nothing was changed without them — final_result wording keyed on the parked status.
- Cancel of a `pending_review` job closes the open park (`closed_by="cancel"`).
- Produces for Task 8: `pending_review → pending` is now a legal transition (the re-run dispatch path).

- [ ] **Step 1: Failing tests** — `test_pending_review_to_pending_is_legal_and_terminal_states_still_are_not`, `test_a_failed_run_with_candidates_parks_and_persists_them`, `test_a_failed_run_with_no_candidates_fails_exactly_as_today`, `test_a_succeeded_run_with_candidates_stays_succeeded_and_items_have_no_park`, `test_cancel_closes_the_open_park`, `test_a_rerun_that_parks_again_opens_a_second_park_and_the_first_stays_closed` (Review Focus 2), `test_the_parked_outcome_message_names_the_count_and_promises_nothing_was_changed`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** The park decision sits beside the existing single status commit (`pipeline_run.py:1043` region), never after it.
- [ ] **Step 4: Run both files + `pytest tests/unit/application tests/unit/persistence tests/unit/pipeline -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 7: The LLM seat — proposer and interpreter

**Files:**
- Create: `src/meshpipeline/agents/repair_review/__init__.py`, `src/meshpipeline/agents/repair_review/proposer.py`, `src/meshpipeline/agents/repair_review/interpreter.py`
- Modify: `src/meshpipeline/contracts/model_inference.py` + `src/meshpipeline/adapters/model_inference/router.py` (new role call beside the intake one)
- Test: `tests/unit/agents/test_repair_review_proposer.py`, `tests/unit/agents/test_repair_review_interpreter.py` (seam stubbed via `set_model_router`, as the intake agent tests do)

**Interfaces:**
- Seam: `async call_repair_review_model(messages: Conversation, job_id: str = "", name: str = "RepairReview") -> ModelRoundResult`, routed like `call_intake_model`.
- Produces `async propose_for_groups(items: list[dict], *, job_id) -> list[dict]`: for groups with `proposal is None`, at most `CAD_REPAIR_REVIEW_LLM_CALLS_MAX` batched calls (one per defect-code group, largest groups first), each bounded by `CAD_REPAIR_REVIEW_LLM_TIMEOUT_S`; a reply must name one catalogue operation from the fixed set {`fix_small_edges`,`fix_face_boundary`,`sew_faces`,`fill_planar_hole`,`fix_shape_global`} or decline; anything else (timeout, parse failure, unknown op) leaves `proposal=None` and records a decline row via `TrainingLogger(job_id).log("repair_review", op_id="review:propose", ...)`. Returns items with proposals filled, `provenance="llm"`, rationale ≤ 200 chars in the product voice.
- Produces `async interpret(text: str, item: dict, group: list[dict], *, job_id) -> Interpretation` — dataclass: `kind` in {`directive`,`refusal`}; for `directive`: the Task-4 wire dict plus `scope` in {`item`,`group`}; for `refusal`: `reason` (honest, names what the catalogue cannot do). The playback string shown to the engineer is `Interpretation.playback: str`.
- Both functions MUST be total: no model router configured ⇒ proposer returns items unchanged, interpreter returns a `refusal` saying review is running without a model. (Degraded mode from the spec.)

- [ ] **Step 1: Failing tests** — `test_proposals_are_batched_per_group_and_capped` (9 groups, stub counts calls ≤ 4), `test_an_unknown_operation_from_the_model_is_a_decline_not_a_proposal`, `test_no_router_degrades_without_raising` (both functions), `test_a_redirect_becomes_a_group_scoped_directive_with_playback`, `test_a_redirect_the_catalogue_cannot_express_is_an_honest_refusal`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (prompts live beside the code as module constants; messages carry the defect context the spec lists: code, measurements, what was tried, the part's story).
- [ ] **Step 4: Run + `pytest tests/unit/agents -q`. PASS.**
- [ ] **Step 5: Wire the proposer into Task 6's park path** (fill proposals before persisting items; park latency bound respected by the caps). One integration-style unit test: `test_parking_fills_missing_proposals_before_persisting`.
- [ ] **Step 6: Run `pytest tests/unit -q` (whole tier). PASS. Commit.**

### Task 8: The review API

**Files:**
- Create: `src/meshpipeline/api/v1/repair_review.py`
- Modify: `src/meshpipeline/api/v1/router.py` (mount under the simulation prefix), `src/meshpipeline/application/pipeline_run.py` or `dispatch_contract.py` only as needed for the re-run payload, the spend-gate call site for the free continuation
- Test: `tests/unit/api/test_api_repair_review.py` (ASGI harness + mocks, as `test_api_simulation.py`)

**Interfaces (the console consumes exactly these):**
- `GET /api/v1/simulation/{job_id}/review` → 200 `{job_id, status: "open"|"offered"|"closed", geometry_session_id, groups: [{group_key, defect_code, proposed_op, count, sampled_item_ids: [first CAD_REPAIR_REVIEW_SAMPLE_SIZE ids], apply_all_allowed: bool (false when any member has pairing_key), items: [...full item dicts...]}], decided: int, open: int}`. Ordering: blocking first then count desc (spec). Owner-auth identical to the job GET; 404 for another owner's job; 409 when review is disabled or there are no items.
- `POST /api/v1/simulation/{job_id}/review/decision` body `{item_id?, group_key?, action: "accept"|"leave"|"redirect"|"apply_all", text?}` → records via Task 2 (`apply_all` → `record_group_decision`; refused 409 on a paired group); `redirect` calls Task 7's `interpret` and returns `{playback, kind}` — a `directive` is stored on the item as the decision, a `refusal` stores nothing and returns the reason. Every recorded decision also writes `TrainingLogger(job_id).log("repair_review", op_id="review:decide", payload={item/group, action, proposal, text, latency_ms})`.
- `POST /api/v1/simulation/{job_id}/review/run` → closes the open park (`closed_by="run"`), builds directives = kept-op replay (from `park.kept_operations`, `source="replay"`) + every `accepted`/`redirected` decision in review order (`source="engineer"`), marks still-`open` items `unreviewed`, transitions `pending_review → pending`, and dispatches via `pipeline_run.dispatch` with the payload gaining `{"repair_directives": [...], "review_continuation": true}`; `review_continuation` makes the spend gate charge nothing (billing decision: a parked run's re-run is the same purchase). On a `succeeded` job (offered review) the same route responds 409 with a body naming "run it again" as the path — delivered-run re-runs ride the existing run-again flow unchanged in this slice.
- `node_repair_attempt` passes `state["repair_directives"]` into `repair_step_file(..., directives=...)` (one-line wiring + one node test here).

- [ ] **Step 1: Failing tests** — happy GET shape; `test_apply_all_is_refused_for_a_paired_group_end_to_end`; `test_decisions_on_a_foreign_or_unparked_job_record_nothing` (Review Focus 3: 404 foreign, 409 unparked); `test_run_builds_replay_then_engineer_directives_in_order_and_skips_the_spend_gate`; `test_run_marks_unanswered_items_unreviewed`; `test_a_park_with_no_proposals_still_serves_and_runs` (Review Focus 4); `test_review_disabled_means_409_everywhere`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement routes + wiring.**
- [ ] **Step 4: Run + `pytest tests/unit/api tests/unit/pipeline -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 9: The console surface

**Files:**
- Create: `ui/js/review/review.js`, `ui/css/review.css` (+ link from `ui/index.html` beside its peers)
- Modify: `ui/js/main.js` (entry: a job whose status is `pending_review` opens the review; a terminal run whose review GET answers `offered` gets a quiet entry point beside the repair panel), `ui/js/api/endpoints.js` (the three routes), `ui/js/realtime/stream.js` only if `pending_review` needs passing through `onStatus`
- Test: `tests/ui/test_repair_review_surface.py`

**Interfaces:**
- Consumes Task 8's payloads verbatim and the geometry stage's existing primitives: load the skin via the geometry session, place a pin at `camera.centroid_mm`, drive the camera with the stage's `look`/`fit` machinery, highlight via `select`. Builder may embed a reduced stage rather than `openGeometryStage` wholesale; the bar is pin + camera-to-entity + orbit, per the spec.
- Produces the full-screen review: progress rail of groups (done/current/todo), one item per screen inside a group's sample, three controls per item — primary "Do that" (hidden when no proposal), "Leave it", and the free-text redirect box whose reply plays back the interpretation for confirmation before it records; after a group's sample, the apply-all screen with "Fix all N the same way" (absent when `apply_all_allowed` is false — paired groups say why), "Keep reviewing one by one", "Show me the largest first" (reorders by `measurements` size desc); a persistent "Run it" that POSTs `/review/run` and returns to the run page. Copy in the product voice; the parked run page states the count and the promise ("nothing was changed without you").

- [ ] **Step 1: Failing ui-tier tests** — drive the module directly in the browser harness with a stubbed `fetch` (the tier's existing pattern): `test_a_parked_run_opens_review_with_the_first_groups_sample`, `test_apply_all_appears_only_after_the_sample_and_never_for_paired_groups`, `test_a_redirect_plays_back_before_it_records`, `test_run_it_posts_and_the_unanswered_count_is_said_out_loud`, `test_items_without_proposals_offer_leave_and_redirect_only` (Review Focus 4 UI half).
- [ ] **Step 2: Run — FAIL.** `CHROME_BIN=... pytest tests/ui/test_repair_review_surface.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the file, then the whole ui tier. PASS (all ~95).**
- [ ] **Step 5: Commit.**

### Task 10: Decision outcomes close the loop

**Files:**
- Modify: `src/meshpipeline/pipeline/repair_attempt.py` (the training row it already writes), `src/meshpipeline/application/repair_review_minting.py`
- Test: `tests/unit/pipeline/test_review_outcome_capture.py`

**Interfaces:**
- On a directed re-run, each engineer directive's fate (kept / rolled back + reason / `directive_unplayable`) is joined back: the node's existing `TrainingLogger` payload gains `"directives": [{item_id, source, outcome, reason}]`, and items whose directive was kept are marked `state` unchanged but `decision["outcome"]="kept"` (rolled back ⇒ `"rolled_back"` and the item RE-OPENS on the next park — "could not be done safely" from the spec).
- Minting on that next park must carry prior decisions as history on re-minted items (`decision_history: [prior decision dicts]`).

- [ ] **Step 1: Failing tests** — `test_a_kept_directive_marks_its_item_and_logs_the_outcome`, `test_a_rolled_back_directive_reopens_its_item_with_history`, `test_unplayable_directives_reopen_as_could_not_be_done_safely`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run + `pytest tests/unit/pipeline -q`. PASS.**
- [ ] **Step 5: Commit.**

### Task 11: The dev-deploy proof and the video

**Files:**
- Create: `devtools/validation/smoke_repair_review.sh` (hosted smoke: review routes answer, dormant-off honoured)
- No source changes; this task is evidence.

**Steps (each verified by the command named, per AGENTS.md):**

- [ ] **Step 1: Host gates green** — the Global Constraints command list, outputs captured.
- [ ] **Step 2: Deploy** — `gh workflow run deploy.yml --ref "$(git branch --show-current)" -f slug=pranav -f app=true -f console=true` then `-f fleet=true`; roll the worker; set `CAD_REPAIR_REVIEW_ENABLED=true` in the pranav `worker.env` GCS object (beside `CAD_REPAIR_ENABLED=true`, no credentials) and in the API service env via the console deploy's mechanism; verify with the worker's boot log and the API's env.
- [ ] **Step 3: Pick the demo part from the corpus logs** — candidates, in order of preference: a NIST part from the stalled eight (real translator output, parks with items), else `nist_stc_07` (2 open_shell + 18 small_edge + 34 small_face: judgement + repairables together), else a `/tmp/cadcorpus` OCCT part that parks. Requirement on the chosen part, proven locally first with the corpus tool + a scripted park: it parks, its review has ≥2 groups, and at least one approve-all leads to a re-run whose repair keeps ≥1 operation.
- [ ] **Step 4: Record** — extend `/tmp/cadvid/record.mjs`: upload → park ("waiting for your review") → open review → sample 2–3 items with the camera visibly driving to each defect → one redirect with playback → apply-all on the big group → Run it → re-run → terminal. Screenshots at every stage; the video must show the progress rail, a pin on a real defect, and the apply-all moment.
- [ ] **Step 5: Verify server-side** — worker logs show the directed run (`planner=directed-v1`, replay then engineer directives); the state record carries the review decisions; pull the repaired artifact THROUGH THE UI and re-scan it with `/tmp/cadvid/scan.py`.
- [ ] **Step 6: Assemble** `~/Desktop/hexera-repair-review-demo/` — video (4× + realtime), stills, README.md in the slice-6 demo's voice with the real numbers, the corpus candidate logs.
- [ ] **Step 7: Open the PR** (branch already carries slice 6 — this PR is the same branch updated; refresh PR #201's description to cover both slices, or stack a second PR if the diff review is cleaner — executor's call, say which). PR body ends with the required attribution line.

---

## Self-review (done at writing time)

- Spec coverage: every section maps — per-defect triage (T5), park/lifecycle/wording (T6), directives+replay (T4, T8), LLM seat bounded + degraded (T7), viewport pin/camera (T3, T9), sampling/apply-all/rim exemption (T2, T8, T9), training capture both directions (T7, T8, T10), billing default (T8 `review_continuation`), deliverables (T11). Benchmark: explicit non-goal, no task.
- Type consistency: directive wire dict defined once (T4) and consumed by T8/T10; item dict defined in T2 and produced by T5/T7; `camera_for` (T3) consumed by T5.
- Review Focus lines each own a named test in T4/T6/T8/T9.
- Proportion: plan ≈ spec ×2 with no function bodies — signatures, test names, and pinned values only.
