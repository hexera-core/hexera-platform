# CRS v4 wiring audit, round 2 (lane C)

Audited at tip `254af0bf` (read-only pass), then updated for the fixes lane C made on `sdd/lane-c`. Rows those fixes
change say **FIXED on sdd/lane-c** and give the before state. Line numbers are as of `254af0bf` unless a row says
otherwise. Paths are repo-relative.

For every capability in design §5.1-5.11 and the plan's cost controls, the table names its PRODUCTION caller (a call
chain from a real entry point, not a test) and the END-TO-END test that exercises that path.

- The engine chain is `pipeline/repair_attempt.py:528-530` (`asyncio.to_thread(repair_step_file, ...)`) -> `cad/repair/conservative.py:1073-1074` -> `_repair_via_v4` (`:1027-1048`) -> `repair_v4/__init__.py:271` `repair_v4` -> `_run_search` (`:183`).
- The eval chain is `v3_eval/launch_fleet.py` (V4 profile) -> `v3_eval/vm_startup.sh:255-268` (`--mode arms ... --cache-uri ... --plan-provenance`) -> `v4_eval/run_arm.py:main` -> `run_arms` (`:187`) -> `arms.run_file` (`:113`).
- "E2E" means a test that enters through `repair_v4()`, `repair_step_file`, `run_arm.main`/`run_arms` or `arms.run_file` and does not call the function under audit directly. A test that does call it directly is listed as unit.

| Capability | Spec ref | Production caller (file:line chain) | End-to-end test (path::test) | Status |
|---|---|---|---|---|
| Layer 1 valid | 5.1 | `__init__.py:228` judge.full -> `search.py:282-285` `_judged` -> `verdict/assemble.py:146` `check_valid` | tests/native/test_native_repair_v4_end_to_end.py::test_repair_v4_produces_a_step_a_verdict_and_a_measured_answer | WIRED |
| Panel: GmshProd, GmshDefault, GmshAlt, NetgenOCC | 5.1 | `assemble.py:108-116` `default_members("full")` -> `assemble.py:161-162` run_panel | same test (asserts the member names, `end_to_end.py:217`); `::test_run_file_produces_a_measured_report_row` (`panel_size`, `:292`) | WIRED |
| Layer 3 faithful | 5.1 | `assemble.py:167-172` `check_faithful_steps`; re-run on BREPs for declared edits `search.py:243-249` | test_native_repair_v4_end_to_end.py::test_a_fixable_injected_case_ends_clean (fill_loop declares an added face) | WIRED |
| Verdict assembly / `clean_of` | 5.1 | `assemble.py:119-175` (one rule; also `search.py:215-226`) | e2e as above | WIRED |
| Score tuple | 5.1 | `assemble.py:225` `score` -> `Judge.score` -> ranking, plus delivery `search.py:297` `panel_passes` | e2e as above | WIRED |
| Cheap tier (rank only) | 5.1 | `search.py:857-869` judge_cheap in `evaluate` `:1033`; stage gate `stages.py:412-427` | e2e search_det arm (`end_to_end.py`) | WIRED |
| Cheap/full agreement measured and reported | 5.1, amend. 19:15 #1 | **FIXED on sdd/lane-c:** `arms._search_arm` -> `arms.cheap_full_cells(result.trace.of_kind("confirm"))` -> row `cheap_full` -> `report._cheap_full_block` -> `cheap_full_agreement` (JSON) and `## Cheap/full agreement` (text), per search arm and pooled. tests/unit/dev/test_v4_arms.py::test_a_search_row_carries_the_cheap_full_table_of_the_states_it_confirmed (real search(), fake world); tests/unit/dev/test_v4_report_dry_run.py::test_heldout_report_dry_run_renders_every_task21_metric. Before (`254af0bf`): `reprobe.agreement` `reprobe.py:496` -> `collect.py:119`, fed ONLY by the cheap-first `probe_pool` (`run_arm.py:158`). The search's own cheap-vs-full pair (trace `confirm`, `search.py:1110-1117`) is never aggregated, and `arms.py` drops the trace. `report.py:589` defers to collect | tests/unit/dev/test_v4_reprobe.py::test_agreement_counts_the_cheap_tiers_misses (unit) | WIRED (eval, sdd/lane-c); was PARTIAL |
| Candidate pool | 5.2 | `v4_eval/pool.py:431` main (CLI) -> pool.json -> `run_arm.py:144,201` read_pool | tests/unit/dev/test_v4_pool.py::test_pool_dedupes_by_sha | WIRED |
| Defect injectors | 5.2 | `inject.py` / `inject_pool.py:468` main (CLI) | tests/native/test_native_v4_inject.py::test_unsew_perturb; the e2e defect fixture uses `inject.inject` (`end_to_end.py:62-71`) | WIRED |
| Re-probe = full verdict on EVERY file | 5.2, amend. 19:15 #1 | `run_arm.py:158` still calls `probe_pool` (cheap on all, then full on cheap fails plus a 10% sample: `reprobe.py:475-493`). Task 48 report (lines 80, 90) left it unchanged | tests/unit/dev/test_v4_arms.py::test_reprobe_mode_is_unchanged_and_attaches_no_ledger | PARTIAL (amendment not implemented) |
| build_splits stratification (layer, failing members, coarse kind, source) | 5.2, amend. #3 | `collect.py:386` -> `reprobe.py:600` build_splits -> `_stratum` `reprobe.py:562-566` | tests/unit/dev/test_v4_reprobe.py::test_splits_disjoint_and_stratified | WIRED (the `cheap_failed` top-up `reprobe.py:658-680` is still live) |
| Per-file wall-clock kill | 5.2 | **Arms branch now covered end to end on sdd/lane-c:** test_v4_report_dry_run.py runs run_arm.main with `--file-timeout-s 1` and a runner that sleeps; the real SIGALRM kill writes `file_timeout` to EXCLUDED and the report counts it inconclusive. Before (`254af0bf`): arms `run_arm.py:243` `_WallClock`; reprobe `reprobe.py:427` | tests/unit/dev/test_v4_reprobe.py::test_file_wall_clock_excludes_the_file (calls `reprobe()` directly; nothing covers the arms branch) | WIRED |
| 4 arms, eval path | 5.2, 5.7 | **Start state FIXED on sdd/lane-c:** search arms now run `engine.run_staged` and search from its output, and delivery is judged against the input (see the engine-bug row). `--h-mm` is still a fixed CLI value, not the engine's per-part `default_judge_for` (open). Test: test_v4_arms.py::test_a_search_arm_starts_from_the_staged_healers_output_as_the_engine_does. Before (`254af0bf`): `run_arm.py:249` -> `arms.py:137-146`; search arms `arms.py:163-185`. **They do not run `run_staged` before `search`** (`arms.py:178` searches from the raw input ws) and use a fixed CLI `--h-mm` (`arms.py:58-59`). The engine runs staged first (`__init__.py:166`) and sizes h from the part (`__init__.py:175-180`) | tests/native/test_native_repair_v4_end_to_end.py::test_run_file_produces_a_measured_report_row; tests/unit/dev/test_v4_arms.py::test_input_only_staged_and_search_det_write_rows_the_report_accepts | PARTIAL (start state fixed on sdd/lane-c; `--h-mm` open) |
| 4 arms, engine path | 5.11 | `__init__.py:77` `ARM` module constant (only monkeypatch changes it); prod = `search_det` with `model_calls=0, model_usd=0` (`conservative.py:1035-1036`) | end_to_end.py::test_repair_v4_produces_a_step_a_verdict_and_a_measured_answer (parametrised over the 4 arms) | WIRED (prod is search_det only, by design) |
| v3 arm (`v3_meshability_pass_ai`) | 5.2 | Listed in the `heldout`/`clean_control` waves (`evalspec.py:328-329`) but not dispatchable (`evalspec.py:281`). `report.py:359` refuses missing (file, arm) rows. Merge step "Not built" (task-32-report.md:39) | none | UNWIRED |
| Workspace / stable IDs / Diff / apply_history / resolve -> UnknownTarget | 5.3 | `__init__.py:223` `Workspace.from_brep`; `ops/__init__.py:194-195,263`; `search.py:1039` | e2e (every arm); tests/native/test_native_repair_v4_workspace.py | WIRED |
| Split ops (split_angle/closed/continuity/by_proximity, unify_same_domain) | 5.4 | det sweep `search.py:448-491`; stages `condition`/`simplify` `stages.py:238-255` | exercised by the staged arm in the e2e, nothing asserted; tests/native/test_native_repair_v4_ops_split.py (unit) | WIRED-UNTESTED-E2E |
| Body ops (conformalise, remove_duplicate_faces) | 5.4 | det sweep for overlap/intersection sites `search.py:507-530`; stages `_bodies` `stages.py:232-235` | tests/native/test_native_repair_v4_ops_bodies.py (unit); no e2e defect has an overlap | WIRED-UNTESTED-E2E |
| Edge ops (tighten_tolerances, fix_poles, rebuild_edge_as_intersection) | 5.4 | tighten/fix_poles: stages `stages.py:174-181`. `rebuild_edge_as_intersection` only runs when `params["targets"]` is set, which defaults to `[]` (`stages.py:85`). Nothing passes params (`__init__.py:166`, `arms.py:70`). It is not in DetGenerator, so only the AI proposer can reach it | tests/native/test_native_repair_v4_ops_edges.py (unit) | PARTIAL |
| Face ops (to_canonical, rebuild_face, fill_loop, reapproximate, refit_cluster) | 5.4 | fill_loop: det `search.py:426-446` (e2e repairs to clean). to_canonical: stages `stages.py:187`. `rebuild_face` needs `params["rebuild"]` (default `[]`, `stages.py:86`). **`reapproximate` and `refit_cluster` have no generator and no stage**: AI proposer only | test_native_repair_v4_end_to_end.py::test_a_fixable_injected_case_ends_clean (fill_loop); tests/native/test_native_repair_v4_ops_faces.py (unit) | PARTIAL |
| Shell ops (sew, make_volume, orient_and_close, remove_small) | 5.4 | det sweep `search.py:476-505`; stages `_shells`/`_solids`/`_finalise` `stages.py:200-261` | tests/native/test_native_repair_v4_patience.py::test_a_three_hole_part_reaches_make_volume_and_ends_clean | WIRED |
| validate_call | 5.4 | `search.py:573-575` called at `:998`; `proposer.py:574` | tests/unit/cad/repair_v4/test_ops_registry.py::test_validate_call_rejects_out_of_bounds (unit) | WIRED |
| Staged healer `run_staged` | 5.5 | `__init__.py:150,166`; `arms.py:70,140` | end_to_end.py (staged arm); tests/native/test_native_repair_v4_stages.py::test_unsew_box_comes_back_clean (unit) | WIRED |
| `run_stage` macro to search / AI | 5.5 | `search.py:1019-1021` -> `_stage_outcome` `:594-599` -> `stages.py:529`. Only AIProposer proposes it (`proposer.py:158`, `search.py:747` stage_names); DetGenerator never does. So it is reachable only in eval search_ai | tests/unit/cad/repair_v4/test_search_wiring.py::test_a_run_stage_candidate_is_routed_to_stages_run_stage (fakes) | WIRED-UNTESTED-E2E |
| `staged_start` | 5.5 | none. Only test callers (`test_judge_cache_wiring.py:193,287`); the docstring at `stages.py:520` claims "as the engine calls it", but the engine calls `run_staged` | n/a | UNWIRED (dead) |
| find_sites | 5.6 | `search.py:285` `Judge.sites` -> `sites.py:291`, called each expansion | tests/native/test_native_repair_v4_open_shell_candidates.py::test_an_open_shell_yields_an_open_site_and_judged_candidates (calls search() directly); e2e | WIRED |
| observation() | 5.6, 5.8 | `proposer.py:263-266` -> `sites.py:823`; `_user_turn` `proposer.py:488-490` | tests/native/test_native_repair_v4_ai_wire.py::test_search_ai_through_repair_v4_admits_every_request_once (requests are sent; their content is not asserted) | WIRED-UNTESTED-E2E |
| render.view (images) | 5.6, 5.8 | `search.py:942-956` (model generators, IMAGE_KINDS) -> `render.py:20-37` sets `site.view_png` -> `proposer.py:500-505` | unit: test_search_wiring.py::test_the_picture_reaches_the_proposers_message_payload_as_the_rendered_bytes; kernel: test_native_repair_v4_sites.py::test_render_view_is_a_512px_png. No E2E asserts an image on the wire | WIRED-UNTESTED-E2E |
| search() | 5.7 | `__init__.py:172`; `arms.py:178` | end_to_end.py (search_det/search_ai) | WIRED |
| DetGenerator | 5.7 | `search.py:722` (arm_generators), `:752` (ai_generators) | end_to_end.py::test_a_fixable_injected_case_ends_clean | WIRED |
| k_confirm full-panel confirm | 5.7 | `search.py:1100-1104` (default `k_confirm=3`, `:785`) | e2e clean result; unit test_search_wiring.py::test_only_confirmed_states_can_be_best_or_delivered | WIRED |
| Patience max(3, open+2), tie-admit = progress | 5.7, amend. #1 | `search.py:771-775`, settled at `:936-940`, progress `:1124-1127`, stop `:1150` | tests/native/test_native_repair_v4_patience.py::test_a_three_hole_part_reaches_make_volume_and_ends_clean (via `repair_v4`, 80 ops) | WIRED (with prod `CAD_REPAIR_MAX_OPS` 6, a 3-hole part stops on `ops`: task-46 report) |
| deliverable() delivery rule | 5.7, 5.9 | `__init__.py:251`; `arms.py:184` | end_to_end.py::test_a_fixable_injected_case_ends_clean | WIRED |
| AIProposer real OpenAI route | 5.8 | eval: `run_arm.py:224` -> `_prepare_model_route` `:281-291` (require_key, install_router, probe_route_chain) -> `arms.py:170-175` -> `search.py:728`; routes `contracts/model_inference.py:232`. Engine: unreachable in prod (ARM constant, `model_usd=0`) | test_native_repair_v4_ai_wire.py::test_search_ai_route_can_be_pinned_to_the_second_route; tests/unit/dev/test_v4_arms.py::test_search_ai_reaches_the_real_router_with_the_ledger_attached_and_fixes_the_file (fake transport under the real router; never a real model) | WIRED (eval only) |
| Schema error returned as a tool result | 5.8 | `proposer.py:446-450` (`tool_result_turn`), validate `:551-574` | tests/unit/cad/repair_v4/test_proposer.py::test_schema_error_returned_as_tool_result (direct) | WIRED-UNTESTED-E2E |
| usd ceiling per file | 5.8 | `proposer.py:336` guard, `:466` admit; eval ceiling `arms.py:173` = `evalspec.py:275` ($1.00) | test_native_repair_v4_ai_wire.py::test_a_file_never_spends_past_its_ceiling_even_when_the_budget_asks_for_more | WIRED |
| declare_unfixable feeds the det sweeps | 5.8 | `search.py:744-752` (one shared registry) -> `DetGenerator.propose` `search.py:418`; `proposer.py:579-588` | tests/unit/cad/repair_v4/test_search_wiring.py::test_a_site_the_model_declares_unfixable_is_no_longer_swept_and_no_longer_shown (through search(), fakes) | WIRED-UNTESTED-E2E |
| Search arms keep the staged healer's gain (engine) | 5.5, 5.7, 5.9 | `__init__.py:166-172` `_find_best` searches from `staged.ws`; `search.py:1182` sets `changed = best.id != s0.id` (s0 = the STAGED start); `search.py:1261` `deliverable` returns None when not changed; `__init__.py:251-253` then passes the INPUT through. A file the healer improved (even made clean) that the search cannot improve further ships unrepaired from `search_det`/`search_ai`. Found in this round | tests/unit/cad/repair_v4/test_engine_arms.py::test_a_search_arm_delivers_the_staged_healers_gain_when_the_search_adds_nothing (strict xfail, fails on `delivered is False`) | **BROKEN (engine, src; handed to the lead)**. The eval arm applies the rule correctly on sdd/lane-c, so eval and engine differ here until it is fixed |
| Delivery gate: a verdict-clean state ships | 5.9 | `__init__.py:251-266` (also ships an "improved" state with panel passes >= the input's) | end_to_end.py::test_a_fixable_injected_case_ends_clean | WIRED |
| LLM reviewer off / optional | 5.9 | v4 has no reviewer hook (`conservative.py:1027-1048`). v3's review minting (`repair_attempt.py:171`) and LLM planner prepass (`repair_attempt.py:381-398`) are gated by their own flags, not by `CAD_REPAIR_ENGINE`. Under `search`, planner directives would be paid for and then ignored. The "optional extra gate" does not exist for v4 | none | PARTIAL |
| Recipe memory | 5.10 | none. Out of scope per the plan (line 635) | n/a | OUT-OF-SCOPE |
| CAD_REPAIR_ENGINE flag, default v3, strict parse | 5.11 | `settings/cad_repair.py:359-381`; `settings/inventory.py:414` | tests/unit/cad/repair_v4/test_engine_dispatch.py (subprocess parse) | WIRED |
| Dispatch in repair_step_file | 5.11 | `pipeline/repair_attempt.py:528-530` (and `repair_handback.py:219`) -> `conservative.py:1073-1074` -> `:1038` `repair_v4` | test_engine_dispatch.py::test_an_engine_that_cannot_run_is_unavailable_not_a_verdict (real repair_v4, dead pool). No test runs `repair_step_file` with `search` to a delivered STEP | WIRED-UNTESTED-E2E |
| Prod posture never sets it | 5.11 | `.github/workflows/deploy.yml` prod arm | test_engine_dispatch.py::test_prod_posture_does_not_set_engine | WIRED |
| Flag plumbing (create-api-service.sh / startup.sh) | 5.11 | ABSENT: `deploy/gcp/worker/startup.sh:77-107` maps 31 `cad-repair-*` keys and not the engine; `deploy/gcp/scripts/create-api-service.sh:245+` likewise; no hit in `deploy/` or `.github/`. `CRS_V4_VERDICT_CACHE_URI` is not plumbed either | none | UNWIRED |
| Verdict cache: key and read-before-mesh | Cost | `assemble.py:154-164` (get before run_panel, put after); key `cache.py:162-163`; version `cache.py:33` | tests/native/test_native_repair_v4_judge_cache.py::test_the_second_judgement_of_a_real_box_meshes_nothing | WIRED |
| Verdict cache: STEP header clock | Cost | `stages.py:320-333` `_deterministic`, applied in `step_for` `:357` | test_native_repair_v4_judge_cache.py::test_step_for_is_a_function_of_the_geometry_alone | WIRED |
| Verdict cache shared across processes | Cost | `cache.py:199-218` env URI -> GcsStore/DirStore; `run_arm.py:110-119` `make_cache` exports the env; `launch_fleet.py:235` `v4-cache-uri` -> `vm_startup.sh:268` `--cache-uri` | tests/unit/dev/test_v4_arms.py::test_the_wave_cache_reaches_the_engines_own_judges_and_is_restored; test_judge_cache_wiring.py::test_a_durable_cache_survives_a_new_process_view_of_the_store | WIRED (eval). Engine/prod: in-memory per process |
| Wave refuses without a shared cache | Cost | `run_arm.py:614-618` (exit 7), only for `--mode arms/reprobe`; `--mode harness` with a plan runs the same arms with no check | tests/unit/dev/test_v4_arms.py::test_a_shard_without_a_shared_cache_refuses_before_any_work | WIRED (harness+plan hole) |
| `--model-usd-cap` required | Cost | No such flag anywhere. Replaced by `--wave-type` -> `WAVE_CAPS_USD` (`model_ledger.py:74`) / N; parse refusal `run_arm.py:520-522`; run refusal `run_arm.py:197-200`; `shard_argv` `evalspec.py:424-427` | tests/unit/dev/test_v4_arms.py::test_search_ai_with_no_ledger_is_refused_before_anything_starts | WIRED (substituted) |
| Shared ledger | Cost | Per-shard slice `WaveLedger.for_shard` (`model_ledger.py:320`), built in `run_arm.py:424-431,587`. Not one pooled ledger | tests/unit/cad/repair_v4/test_proposer.py::test_wave_cap_is_shared_across_shards_and_marks_the_rest_ai_capped | WIRED (as a slice) |
| `ai_capped` marking | Cost | Set in `search.py:912-916,1158`, `proposer.py:473` (`ledger.mark`). **Not written into eval rows** (`arms.py:180-182`); the string `ai_capped` appears nowhere in `devtools/validation/v4_eval/` | test_search_wiring.py::test_a_file_ceiling_is_ai_capped_and_names_its_reason (unit) | PARTIAL |
| ledger.admit per-call reservation | Cost | `proposer.py:457-466` -> `model_ledger.py:393-406`; release/settle `proposer.py:421-440` | test_native_repair_v4_ai_wire.py::test_search_ai_through_repair_v4_admits_every_request_once | WIRED |
| Prompt caching | Cost | `proposer.py:236-239,370,486` cache_key -> `adapters/model_inference/router.py:290` -> `protocols/responses_request.py:196-197` `prompt_cache_key` | test_proposer.py::test_the_static_prefix_is_identical_across_files_and_rounds_and_keyed_for_the_cache (unit); no E2E asserts `prompt_cache_key` on the wire | WIRED-UNTESTED-E2E |
| Images only for intent sites | Cost | `proposer.py:59` IMAGE_KINDS={overlap, open}; driver `search.py:945` | test_proposer.py::test_images_are_not_attached_for_kinds_that_need_no_intent_call; test_search_wiring.py::test_the_search_det_arm_renders_nothing_whatever_the_sites | WIRED-UNTESTED-E2E |
| Stop a file when clean | Cost | `proposer.py:399-400`; `search.py:1145-1147` | end_to_end.py::test_a_fixable_injected_case_ends_clean; test_proposer.py::test_a_clean_state_makes_no_call | WIRED |
| Per-file $ ceiling | Cost | see the usd-ceiling row | test_native_repair_v4_ai_wire.py::test_a_file_never_spends_past_its_ceiling_even_when_the_budget_asks_for_more | WIRED |
| Spot VMs, self-delete, 3 h backstop | Cost | `launch_fleet.py:371-372`: **`--provisioning-model=STANDARD`** (not SPOT) + DELETE + `--max-run-duration`; V4 default **8 h** (`launch_fleet.py:316`); self-delete `vm_startup.sh:94` | tests/unit/dev/test_v4_fleet_launcher.py::test_v4_dry_run_is_one_wave_of_v4_labelled_self_deleting_vms | PARTIAL |
| Preflight VM-hour projection, refusal over ~600 | Cost | `evalspec.py:39,145-190`; `launch_fleet.create_wave` -> `enforce_budget`; `launch_reprobe.py:57-61`; IAM/image preflight `launch_fleet.py:274-283` | tests/unit/dev/test_v4_fleet_launcher.py::test_v4_over_ceiling_wave_is_refused | WIRED (the projection still assumes cheap+full, `evalspec.py:146-154`) |
| plan_wave slot claim + provenance into shard PROVENANCE | Cost | `launch_fleet.py:617-621` (claim in `claim_slot`, `:882-894`) -> `plan_metadata` `:248-256` -> `vm_startup.sh:182,259` -> `run_arm.py:597-605` `prov.update(plan)` | tests/unit/dev/test_v4_harness_rows.py::test_plan_provenance_is_copied_into_the_shard_and_the_report_can_verify_the_slot | WIRED |
| MIN_PAIRED_FRACTION reported with the actual fraction | Cost, amend. #3 | **FIXED on sdd/lane-c:** `paired_set` carries `fraction` and `min_fraction`, and the text prints `paired fraction X% (minimum 80%)` every time. Tests: test_v4_report.py::test_paired_column_equalises_denominators_when_dropouts_differ; the dry run. Before (`254af0bf`): `report.py:80`, used only inside a warning (`report.py:560-565`); `paired_set` JSON (`report.py:504-505`) and the text (`:612-617`) carry n/of but no threshold or fraction field | tests/unit/dev/test_v4_report.py::test_four_measured_arms_that_each_drop_different_files_warn_that_the_paired_set_shrank (warning path only) | WIRED (sdd/lane-c); was PARTIAL |

## Gaps

Status after lane C: gaps 3 and 7 are FIXED on `sdd/lane-c`; gap 1 is half-fixed (the start state; `--h-mm` remains);
gap 0 is new and is the most serious. The rest are handoff items for the lead.

0. **Engine: a search arm throws away the staged healer's gain.** See the BROKEN row above. `_find_best` must judge
   `changed` against the input workspace (for example `changed or staged.ws.brep_path != ws.brep_path`), then the strict
   xfail in `test_engine_arms.py` is removed. Until then, production `search_det` delivers strictly no more than
   `staged`, and any file the healer alone fixes ships unrepaired. Owner: lead / engine seam. Size: XS plus the xfail.

1. **The eval search arms do not measure the engine's pipeline.** `arms._search_arm` searches from the raw input (`arms.py:178`) without `run_staged`. The engine runs staged then search (`__init__.py:166-172`), and spec §5.7 says "Start state: the output of the staged healer". The eval also takes a fixed `--h-mm` (`arms.py:58-59`) where the engine uses `h_mm_for(part)` (`__init__.py:175-180`). The search_det/search_ai numbers would describe a pipeline that never ships. Owner: eval lane. Size: S (call `engine.run_staged` and use `default_judge_for`; keep `--h-mm` only as an override), plus a native assertion that the eval and engine paths agree on one file.
2. **The re-probe is still cheap-first.** `run_arm.py:158` uses `probe_pool` (`reprobe.py:489-493`), against the 19:15 amendment #1. Task 48 explicitly left it (task-48-report.md:80,90). This also leaves the clean control dependent on a 10% cheap-pass sample and the `cheap_failed` top-up (`reprobe.py:658-680`), and `evalspec.project` (`:146-154`) still sizes cheap+full. Owner: Task 47/48 follow-up. Size: S-M (make `full_tier_entries` return every non-excluded entry or add a `full_only` path; update the projection).
3. **Cheap/full agreement for search ranking is not reported anywhere.** The only agreement figure is the reprobe's (`collect.py:119`), and it vanishes once gap 2 is fixed. The search's own `confirm` events (`search.py:1110-1117`, cheap_score vs full score) are discarded by `arms.py`. Owner: eval lane. Size: S (count cheap-vs-full pass/fail per confirm in SearchResult, put it in the row, aggregate it in report.py).
4. **The v3 arm row merge is not built.** `heldout` and `clean_control` list `v3_meshability_pass_ai` (`evalspec.py:328-329`), nothing in v4 dispatches it, and `report.py:359` refuses the missing rows (task-32-report.md:39 "Not built"). The held-out and clean-control reports cannot load. Owner: eval lane. Size: M.
5. **`CAD_REPAIR_ENGINE` and `CRS_V4_VERDICT_CACHE_URI` are not plumbed** through `deploy/gcp/worker/startup.sh` (`:77-107`) or `deploy/gcp/scripts/create-api-service.sh` (`:245+`), and nothing in `deploy/` or `.github/` mentions them. A personal env cannot select v4, so the AGENTS.md personal-slug loop cannot test it. Owner: deploy/integration. Size: S (one metadata/env row each, dev-only, keeping `test_prod_posture_does_not_set_engine` green).
6. **`ai_capped` never reaches eval rows or the report.** It is set in `search.py:916,1158` and `proposer.py:473`, but `arms.py:180-182` drops it and no v4_eval file contains `ai_capped`. The plan says files run deterministic-only "and are marked ai_capped". Owner: eval lane. Size: XS.
7. **`MIN_PAIRED_FRACTION` only appears inside a warning** (`report.py:560-565`). Amendment #3 requires the threshold and the actual paired fraction to be reported always. Owner: eval lane. Size: XS (add `threshold` and `fraction` to `paired_set` and the text line).
8. **Fleet VMs are STANDARD, not spot, and the v4 backstop is 8 h, not 3 h** (`launch_fleet.py:316,371-372`). This is either a deliberate deviation that should be recorded, or a fix. Owner: owner decision, then the launcher. Size: XS.
9. **Four ops are deterministically unreachable.** `reapproximate` and `refit_cluster` have no generator or stage. `rebuild_edge_as_intersection` and `rebuild_face` run only with stage params that nothing passes (`stages.py:85-86`). All four are reachable only via the AI proposer, which prod never runs. `run_stage` is likewise AI-only. Owner: search lane. Size: S per op family (sweep rows for invalid/self_overlap sites), or record them as AI-only.
10. **No E2E covers several pieces.** `repair_step_file` with `search` running to a delivered STEP has no test (dispatch is tested with a fake `repair_v4` or a dead pool). Prompt-cache key on the wire, images in a request, schema-error tool results, declare_unfixable and `run_stage` are covered only at the unit or fake-driver level. Owner: test lane. Size: S (extend `test_native_repair_v4_ai_wire.py` to assert `prompt_cache_key`, an `image_url` part for the open site, and one malformed call answered as a tool result; add one native `repair_step_file` run).
11. **Smaller items:**
    - `staged_start` (`stages.py:519`) is dead code with a misleading docstring.
    - `--mode harness` with `--plan-provenance` runs real arms without the shared-cache refusal (`run_arm.py:608-618`).
    - The v3 LLM planner and review minting in `repair_attempt.py:171,381-398` are not gated off under `CAD_REPAIR_ENGINE=search`. Planner directives are ignored by `_repair_via_v4` but would still be paid for, and no optional reviewer gate exists for v4 (§5.9).
    - The prod op budget `CAD_REPAIR_MAX_OPS` (6) cuts off multi-hole repairs (task-46 report).
    - `devtools/validation/v4_eval/RUNBOOK.md:10,109` still calls `preflight.py` unmerged.
12. **Recipe memory (§5.10):** out of scope per the plan (line 635). Nothing is built.

## Report dry run (lane C)

`tests/unit/dev/test_v4_report_dry_run.py` runs the whole host-side report path through each tool's own CLI. No CAD, no
model, no bucket is involved.

1. **Fake re-probe, `collect.py` CLI.** Two shards hold 61 pool files: 30 panel failures, 10 layer-1 failures, 6
   declined-stratum injects (`face_delete_large`), 12 clean files, one `no_solids`, one unbounded (`out_of_scope`) and
   one EXCLUDED timeout. The cheap tier disagrees with the full panel on four files. `collect.main` accepts the wave
   and cuts dev/held-out/clean at 20/20/10. `no_solids`, the unbounded file and the excluded file reach no split, the
   held-out split carries declined files, and the re-probe's agreement reports `compared=60` with 2 misses and 2 false
   alarms.
2. **Fake held-out wave, `run_arm.py` CLI.** It runs `--mode harness` on two shards with `plan_wave(...).provenance()`,
   a real `WaveLedger` and a directory marker store. The arm outcome is the faked `ARM_RUNNER`. Every non-v3 arm runs
   on every file, and these rows are built on purpose:
   - an arm that **crashes** (an exception, giving `error: ...` in EXCLUDED);
   - an **inconclusive** search row (`no_verdict`);
   - a **timeout** (the runner sleeps past `--file-timeout-s 1`, so the real SIGALRM wall kill fires);
   - a panel member lost for **pool reasons** (`arms._usable`, EXCLUDED);
   - an **out_of_scope** output (`arms._usable`, on the v3 arm);
   - one **fewer-panel-passes** regression;
   - one declined-stratum file that comes out unexpectedly clean.
3. **`report.py` CLI** with `--frozen-commit` and `--markers-dir`. It first REFUSES, because the v3 rows are missing
   (handoff item 2 below). After the simulated merge it renders `OK`, `PRIMARY ARTIFACT`, slot verified. The test then
   checks each Task 21 metric numerically against counts it makes itself from the scenario:
   - clean gain per arm and paired;
   - both regression kinds (clean-to-unclean and fewer panel passes);
   - the AI marginal and the deterministic marginal (paired);
   - cost (model $ per arm, v3 "not metered", the wave cap) and wall per file;
   - cheap/full agreement (new);
   - the paired fraction with its 80% threshold (new);
   - the declined stratum on its own line.
4. **Clean-control wave.** Both regression kinds are counted, as `staged` 1/1 and `search_det` 1/0, and the normal
   wave cap is reported.

**What the dry run cannot show:** real verdicts, a real ledger charging (the runner reports `model_usd` itself), and
real v3 rows. The v3 merge is simulated, because it is not built.

## Handoff items for the lead (not fixed on sdd/lane-c)

1. **Engine bug, gap 0 above.** A search arm discards the staged healer's gain. This is the top item.
2. **The v3 row merge is not built.** Without it the held-out and clean-control reports refuse
   (`report.py:359`). The dry run's `merge_v3_rows` shows the shape it must produce.
3. **The clean-control report is UNRELIABLE exactly when it passes.** An arm that keeps all ≥10 clean-control files
   clean trips the constant-outcome warning (`report.py:545-548`), which sets `status=UNRELIABLE` and
   `numbers_valid=false` (exit 2). That is the desired result on that split. Suggested fix: skip the constant warning
   for an all-clean outcome on the `clean` split, or downgrade it to a note there. This is an owner call, because the
   warning was written to catch broken harnesses.
4. **The re-probe is still cheap-first** (`run_arm.py:158` calls `probe_pool`), which contradicts amendment 19:15 #1.
   `evalspec.project` still sizes cheap+full. Once this is fixed, the re-probe's own agreement figure is empty: the
   search's table (now wired) is the measurement that remains.
5. **The eval `--h-mm` is a constant** where the engine sizes h per part (`default_judge_for`), so the eval search
   arms find sites at a different scale from production.
6. **`ai_capped` never reaches a row.** `SearchResult.ai_capped` and `ai_stop_reason` are dropped by `arms.py`. This
   is an XS fix in arms + report, left out of this change to keep it reviewable.
7. **An EXCLUDED file is billed 0 s of wall** in the report: the arm's per-file wall mean omits the time a timeout
   actually burned. That understates cost on the files that cost the most.
8. **`failure_reason` doubles the exception type** (`error: RuntimeError: RuntimeError: ...`). Cosmetic.
9. Gaps 5, 8, 9, 10 and 11 above stand as written.
