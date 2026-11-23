# Task 21 report: eval arms, waves, report

Branch `sdd/task-21`, worktree /Users/kitts/archductor/sdd-worktrees/task-21, based on e39b3965.
Head: 04cc21e6 (feature commit 8a86ee7c + runbook commit). Not merged, not pushed, nothing deployed, no VM, model or GCP call.

## What was built
- `devtools/validation/v4_eval/evalspec.py` (extended): `Budget`, `Arm`, `ARMS` (one line per arm), `ARM_ALIASES`, `WaveDef`
  (wave_type mandatory, validated against the ledger's `WAVE_CAPS_USD`), `WAVES`, `claim_wave_slot` (held-out once, checkpoint <= 3,
  same wave_id may resume), `WavePlan`/`plan_wave` (only way to a launchable wave; `open_ledger` = `WaveLedger.for_shard`),
  `refuse_heldout_for_tuning`.
- `devtools/validation/v4_eval/report.py` (new): `ArmRow`, `load_wave` (only way to a `WaveResult`), `build_report`, `render_text`, CLI.
- `devtools/validation/v4_eval/collect.py`: `load_shards(parse_rows=...)` seam so arm waves go through the SAME exit-code / shard-count /
  one-commit / one-image / missing-shard gates (and `check_partition`). Default behaviour unchanged.
- RUNBOOK.md section; tests `tests/unit/dev/test_v4_evalspec.py` (19), `tests/unit/dev/test_v4_report.py` (37).

## Gates: exactly what ran (revised after coordinator review)
The `make` targets did NOT run. The worktree has no `.venv`, ruff is not on PATH and mypy is not installed, so:
- `ruff check src tests alembic devtools` with ruff 0.17.4 from `/Users/kitts/archductor/assets/ocpvenv/bin/ruff` (the paths `make lint` uses): exit 0. NOT `make lint`.
- `PYTHONPATH=src uvx --offline mypy --ignore-missing-imports` (mypy 2.4.1, from uv's cache) on evalspec.py, report.py, collect.py: exit 0. NOT `make typecheck` (the mypy ratchet did not run).
- `make test-fast` did NOT run. I ran pytest directly with `/Users/kitts/archductor/assets/ocpvenv/bin/python` (3.12) and `PYTHONPATH=src`.
- `pytest tests/unit/dev` as a whole: exit 2, four UNRELATED files fail to collect for missing dependencies on that interpreter
  (test_integration_runner_boundary, test_onboarding_contract, test_rebuild_authority, test_sarif_findings).
- My two files (test_v4_evalspec.py, test_v4_report.py): exit 0, 59 passed.

## Failure-id diff against the base commit
Same 10 test files run in my worktree and in the main workspace (HEAD e39b3965), `pytest -rf`, failing ids extracted and sorted.
(An earlier attempt of mine passed the file list as one quoted string, pytest exited 4, and both id lists were empty: a false green, discarded and redone with a proper array.)
- mine (exit 1, 1 failed / 302 passed): `test_v4_reprobe.py::test_run_arm_drops_f360_from_the_manifest_visibly`
- base (exit 1, 2 failed / 301 passed): that same test, plus `test_v4_fleet_launcher.py::test_v3_dry_run_still_refuses_and_plans_as_before`
- `comm -23 mine base` (failures only in mine): EMPTY.
- `comm -13 mine base` (failures only in base): NOT empty, one id: `test_v4_fleet_launcher.py::test_v3_dry_run_still_refuses_and_plans_as_before`.
  It fails deterministically in the main workspace (2 of 2 reruns) and passes in my worktree (2 of 2), with the test file and its code identical in both;
  so it depends on the checkout, not on my change. I did not find which input differs. My change introduces no new failure, but "fails the same way" was
  not exactly right: the base has one extra environment-dependent failure.
- The reprobe failure is in both: `AttributeError: 'Namespace' object has no attribute 'sources'` in run_arm.py (untouched by me).

## What each check would catch, and proof it can fail
Mutations applied to the code, suite re-run, then reverted (all via sed + `git checkout`):
- inconclusive limit set to 1.0: 1 test fails (30% inconclusive arm no longer NOT MEASURED).
- `clean` derivation ignores the panel: 29 tests fail.
- input_only "output must equal input" check off: fails. Input-vs-split-verdict check off: fails.
- F360 row source check off: fails. Held-out slot claim off: fails. Shard spend-vs-slice check off: fails.
Fake-resistance design (the sprint's recurring failure): clean is DERIVED from valid/faithful/panel and a stored flag that disagrees
is refused; every arm's input must equal the split's own verdict (a constant "clean" fake is refused); input_only must be identity;
arms must agree on one file's input; arms with identical per-file outcomes, or a non-floor arm with a constant outcome, are flagged
UNRELIABLE with a "DO NOT READ" banner; a marginal involving an unmeasured arm is None, not a number.

## Decisions to confirm (open questions)
1. Arm names (confirmed by the coordinator: plan names canonical, aliases kept with a comment): the brief says baseline/heal_det/search_det/search_ai; the plan and spec say input_only/staged/search_det/search_ai/
   v3_meshability_pass_ai. I used the plan/spec names as canonical and made `baseline`->v3_meshability_pass_ai, `heal_det`->staged aliases.
   If "baseline" meant input_only, change one alias.
2. MAX_INCONCLUSIVE_FRACTION = 10%: accepted by the coordinator as a PROVISIONAL default owned by the owner. The inconclusive rate is now its own column on every arm (and per defect kind) in every report, under the limit or not.
3. Wave types: dev_deterministic/dev_search = normal ($50), dev_checkpoint = checkpoint, heldout = held_out, clean_control = normal. The
   plan gives clean control no cap of its own; "normal" is my assumption. Held-out split and held_out type are welded together.
4. `v3_meshability_pass_ai` model spend is v3's own and is not metered by this ledger; the report requires 0 for it.
5. `Budget` here is the plan's eval budget (wall_s=600, ops=60, model_usd); `repair_v4.Budget` is a placeholder with other fields. Not reconciled.

## Deliberately left for follow-up / not testable without the fleet
- The harness side: `run_arm --mode harness` emitting `ArmRow`s plus wave provenance (`wave`, `wave_id`, `wave_type`, `split`, `arms`,
  `wave_cap_usd`, `ledger="shard"`), and the launcher calling `plan_wave`/`open_ledger`. Waits on search Tasks 17-19. The row/provenance
  contract is defined and tested only against fake shard directories; nothing here proves a real harness emits it.
- Plan metrics not built: cheap/full agreement (the re-probe's collect report already has it) and per-layer counts beyond valid / faithful / panel-all / clean.
- The once-only / three-checkpoint slot is enforced at planning on a shared marker store; only a real bucket marker store makes it
  hold across hosts (tested with an in-memory store).
- Held-out firewall: `refuse_heldout_for_tuning` is a function tuning code must call; nothing yet calls it from a tuning path (none exists).

## Review follow-ups
- Alias comment added in evalspec.py. Inconclusive rate printed on every arm (test: 2.5% shown with the clean rate present, 0.0% on the others).
- Firewall hook: tests call it directly (refuses heldout split name and file path, passes dev/clean) and through `report.py --purpose tuning` (exit 1). Neutering the hook makes 2 tests fail; restored.
- Final tests after these changes: 59 passed in my two files; ruff exit 0; mypy exit 0 (same commands as above).

# Fix round (head before: c463d42b)

No VM, no model call, no GCP call, no spend. Files: `devtools/validation/v4_eval/report.py`, `evalspec.py`, `tests/unit/dev/test_v4_report.py`, `test_v4_evalspec.py`.
Mutation method: `/tmp/mut.py` applies one source edit, runs both test files, records what failed, restores. Every mutation below was CAUGHT (24 of 24 after two test additions; the first run had two survivors, 5e and m5, now covered).

## Important 1 - NOT MEASURED withholds everything
- `report.py` `_arm_block` (~385-436): for an unmeasured arm `clean_in`, `clean_out`, `clean_rate`, gain, `layers`, `regressions`, and per-kind `clean` / `clean_rate` are all None; per-kind `ok` now requires the ARM to be measured. `numbers_valid` (`build_report`, ~523) is `not warnings`; the text prints "withheld" in the regress and per-kind columns.
- Test: `test_not_measured_withholds_every_figure_a_rate_can_be_recovered_from` (kind 0 has zero inconclusive files, so the old per-kind test would have printed its rate); sibling asserts a measured arm keeps its figures and `numbers_valid` is True on the good wave.
- Mutations: per-kind `ok` without `measured and`; `clean_out`, `layers`, `regressions`, per-kind `clean` un-withheld; `numbers_valid` constant True. All fail that test.

## Important 2 - paired marginal
- `_paired_columns`: each measured arm's clean / rate / gain over the files EVERY measured arm judged. An unmeasured arm is not in the shared set (it has no rate to pair, and its dropouts would shrink everyone else's); the set's arms and size are in `paired_set`. Header text says only the PAIRED columns support arm-to-arm comparison; the columns are titled PER-ARM (own files; not for comparison) and PAIRED (shared files) with `n=` printed.
- Test: `test_paired_column_equalises_denominators_when_dropouts_differ` (det drops 3 files it would not fix, ai drops 3 it would; own denominators 37/37 over different files, paired 34/34; paired counts computed independently from the fixture; both per-arm rates differ from paired) and `test_a_not_measured_arm_does_not_shrink_the_paired_set`.
- Mutation: paired set taken from one arm's own conclusive files fails the first test.
- Not done: no paired per-defect-kind table (per-kind rows stay per-arm and are labelled so).

## Important 3 - v3 spend
- `evalspec.Arm.spend_metered` (False for `v3_meshability_pass_ai`). Report block carries `spend_metered`, `model_usd` None; text prints "not metered (calls a model; spend is v3's own)"; the spend line says it EXCLUDES v3; `budgets[...].model_usd` None. Real v3 spend is not obtainable here. A v3 row reporting spend > 0 is still refused (the ledger cannot meter it).
- Test: `test_v3_baseline_spend_is_not_metered_never_zero` (staged still shows 0.00 as the sibling), `test_v3_is_the_one_arm_whose_spend_is_not_metered`. Mutation: drop `spend_metered=False`.

## Important 4 - slot at read time
- `WavePlan.provenance()` now carries `slot`. `evalspec.slot_keys(type)` is shared by `claim_wave_slot` and the report. `load_wave` (~287-304) requires, for held_out and checkpoint waves, a `slot` that every shard agrees on and that is one of the type's own keys; with `markers=` (CLI: `--markers-dir`, a `FileMarkerStore`) it must be held by THIS wave_id. The result carries `slot` and `slot_verified`; the report text and JSON say whether the store was consulted.
- Tests: heldout slot None / "" / wrong key / checkpoint key refused; checkpoint needs a slot; shards disagreeing; store verification (own id, other id, empty); a real `plan_wave(...).provenance()` loads end to end; CLI against a directory store (empty store exits 1, claimed exits 0); `test_plan_provenance_carries_the_slot...`.
- Mutations: slot not required; marker not verified; provenance without slot. All caught.
- FIXED: the get-then-create race. A concurrent start of the SAME wave_id whose `create` loses used to fall through to the next key and burn a second checkpoint slot; `claim_wave_slot` now re-reads after a failed create (`test_a_concurrent_start_of_the_same_wave_does_not_burn_a_second_slot`, with the different-wave sibling taking slot 2). Mutation: remove the re-read.
- REMAIN CALLER OBLIGATIONS (documented in the docstring): (a) the marker store must be durable and shared; the in-memory dict in tests, or a fresh store, defeats the count; (b) resuming a wave_id that already COMPLETED is allowed, because the function cannot know it finished, so the launcher must not relaunch a completed held-out wave_id; (c) the report only knows a shard claimed a slot if the harness copies `plan.provenance()` into PROVENANCE.json, and `run_arm` does not write wave provenance yet. If the future harness omits it, the held-out report refuses.

## Important 5 - the row carries 18b's fields
- `ArmRow` gains `reason`, `verdict_candidates`, `inconclusive_candidates`, `infrastructure_failed` (names as on `SearchResult`; task-18b is not merged into this branch, so I mirrored them from its code at e79e7687 and did not import them). `derived_inconclusive` = `reason == "no_verdict"` or `infrastructure_failed` or (inconclusive candidates > 0, no verdict candidates, and not out_clean), which is 18b's own rule. A row whose derived value is True and whose `inconclusive` is False is REFUSED; `no_verdict` with `infrastructure_failed=False` is refused as contradictory; non-integer / negative counts refused; a conclusive `search_det` / `search_ai` row missing reason or either count is refused (otherwise a harness that folds infrastructure death into False passes by omission).
- Tests: five parametrized contradictions including the exact no_verdict-start-returned-as-failure path; the same search marked inconclusive is excluded from the rate and not counted as a failure; a real failure with verdicts (patience, 5 verdicts, 2 inconclusive candidates) is still a failure; missing-field and malformed-field refusals; inconclusive + no_verdict + infrastructure_failed=False refused.
- Mutations: derived check off; search fields not required; only the reason clause; infrastructure clause; the contradiction check. All caught (5e initially survived because the derived check shadowed it; added the inconclusive=True variant).
- Limit: a harness that writes `reason="patience"`, counts 5/0 for a search that really got no verdict would still pass; the row can only be as honest as the fields it copies from `SearchResult`.

## Important 6 - firewall
- `refuse_heldout_for_tuning` now compacts case and separators, so "held-out", "held out", "Held_Out", "held.out" match in the name or any parent directory, and a `.json` of ANY name whose own `split` field says held-out is refused. Tests: six spellings x three placements; a file `split_b.json` with `"split":"heldout"` refused, a dev-split file, a non-JSON file and an absent path pass. Mutations: old match; content check off.
- It is still a NAME check plus one content check, and `--purpose` is self-declared: `load_wave` defaults to `report`, so the hook is opt-in and only holds if the tuning path calls it. The structural guard I would want: put the held-out split where tuning code cannot read it by construction (a bucket object with IAM that only the report identity holds, or the file encrypted with a key only the report job has), and make `rp.read_split` for the held-out split require a `WavePlan` for a held_out wave (the once-only claim), so reading the held-out files IS claiming the slot and a tuning path has no plan to present.

## Minors
- Per-file `model_usd` over the arm's ceiling refused (exactly $1.00 allowed): `test_a_files_model_spend_over_the_arms_ceiling_is_refused`. The old shard-slice test could no longer be reached with a $8 row, so it now uses 100 files at $1.00 on the $50 normal wave.
- `input_only` identity now also compares `out_valid` and `out_faithful_ok`; rows must carry `in_valid` / `in_faithful_ok` (required on input_only, optional elsewhere but cross-checked across arms). Tests for each field, for missing input layers, for arms disagreeing. Still runs only on conclusive rows (an inconclusive row carries no verdict to compare).
- Threshold 10: a judgement, not data (n=5 coin flip is constant 6% of the time, n=10 about 0.2%); the comment says so.
- clean_control = "normal": the plan (T4 "Held-out + clean control", held-out $250 once; spec line 77) gives the clean control no cap or count of its own, so it is still an assumption, now pinned by `test_the_clean_control_wave_is_a_normal_wave_with_no_slot`. Flag for the owner.
- Detector false positives: `FALSE_POSITIVE_NOTE` is appended to both the constant-outcome and the indistinguishable warnings (two deterministic arms that truly agree, or a perfect arm on the clean-control split, are real results); tested on both.
- Fixture: `_fixed` is no longer nested (staged i%4==0 ->10, search_det i%3==0 ->14, search_ai i%2==0 or i%7==1 ->23, v3 i%8==0 ->5) and tests compare the report with `expected_clean(arm, idx)`, an independent count over the same function, not typed-in numbers. Caveat: this proves the report counts the right files, not that a harness varies an arm; that is the detector's job and the held-out real wave's.

## Gates actually run (venv built: `uv venv --python 3.12 .venv` + requirements/dev.txt, runtime.txt, constraints.txt; needs `PYTHONPATH=src`)
- `make lint`: exit 0.
- `PYTHONPATH=src .venv/bin/mypy` on report.py, evalspec.py, test_v4_report.py: 0 errors. `test_v4_evalspec.py` has one `unused type: ignore` at line 93, present at c463d42b, not mine.
- `PYTHONPATH=src make typecheck`: exit 2, 2 NEW errors, `sched_getaffinity` attr-defined in `src/meshpipeline/cad/fea_joins.py` and `engines/radius_chords.py`. Files I did not touch; a Linux-only API on a macOS host. Nothing from my files.
- The two v4 test files (`test_v4_report.py`, `test_v4_evalspec.py`): 106 passed.
- **`make test-fast`: NOT RUN to completion - gated by the controller at merge.** I started it twice; one run was disturbed by a stash I made while it ran, the second was stopped at ~49% on the controller's instruction. No result is claimed.
- Earlier observation: `test_v4_reprobe.py::test_run_arm_drops_f360_from_the_manifest_visibly` failed at c463d42b with and without my changes (stale hand-built Namespace; fixed by Task 23 `a34ea20d`).

## For when the harness lands (the review's one check, recorded so it is not lost)
Run a small real dev wave with a forced `CheckerUnavailable` (no gmsh) and assert: the arm row is `inconclusive: true`; it is excluded from the arm's rate and from the paired set; for search arms the row carries `reason="no_verdict"` and `infrastructure_failed=True`; `input_only`'s `in_clean` for the file matches the split's verdict (or the file is inconclusive for every arm); the shard PROVENANCE carries `plan.provenance()` including `slot`; and the report run with `--markers-dir` on the real marker store says the slot is held by this wave_id.

## Coordinator follow-ups (same round)
- Paired set kept as is (NOT MEASURED arms excluded). The population is now stated next to the paired columns: a footnote line "PAIRED columns cover the N of M files judged conclusively by ALL of these K measured arm(s): ...; left out because NOT MEASURED: ..." (`render_text`), and `paired_set.excluded_not_measured` in the JSON. Test `test_the_paired_footnote_names_exactly_the_arms_that_were_included` parses the footnote and asserts the listed arms equal the measured arms and `paired_set.arms`, and that each has `paired.n` equal to the stated population. Mutation (footnote lists every arm) fails it.
- The missing-slot refusal now says plainly what is missing: "the harness must copy plan_wave(...).provenance() (it includes `slot`) into each shard's PROVENANCE.json". Test `test_the_refusal_for_a_missing_slot_says_what_to_wire`; mutation (bare message) fails it. Booked as a harness-wiring gap: `run_arm` does not write wave provenance yet, so a held-out report refuses until it does (fail closed).
- Task 18b fields are MIRRORED from `sdd/task-18b` at `e79e7687`, not imported. At merge `reason`, `verdict_candidates`, `inconclusive_candidates`, `infrastructure_failed` in `ArmRow` and `ArmRow.derived_inconclusive` (a copy of the search driver's `infrastructure_failed` rule) must become a real import / shared definition, or the report and the driver can disagree about whether a file was measured.
- `clean_control` as a normal wave stays a flagged assumption for the owner (pinned by test).
- Benchmark data is internal only (owner's ruling); nothing here changes.
