# Task 9 report
Implemented plan.py, hermetic + native tests verbatim from the brief.
RED: unit file -> ModuleNotFoundError at collection (expected). GREEN: unit 10 passed; native 2 passed (RAN, not skipped).
Bore fixture proposals in order: sew_faces edge:372e010dab934712, sew_faces edge:adb607eb318c84f4 (each then marked ineffective), then None. Never fill_planar_hole. Both rims in paired_rim_fingerprints; two paired_rim refusals recorded.
Open-shell: sew_faces, then fill_planar_hole (rims empty, no refusals).
Small-edge fixture: fix_small_edges for each of two edges.
lint OK; typecheck 0 new (28 baseline). make test-fast: 33 failed / 13318 passed; visible failures are tests/unit/deploy/test_setup_is_self_contained.py and tests/unit/settings/test_config_parity.py (compose/deploy, unrelated to plan.py; not verified against clean main).
Self-review: ordering severity then entity/index, ops sorted by scope; ledger keyed (fingerprint, op); refuse deduped; OCP imports function-local (conservative/operations imports are inside paired_rim_fingerprints).
Concern: the "worst severity then narrowest scope, global last" relies on fix_shape_global having the largest scope.

## Fix round 1
Refusal now recorded at detection in propose() together with mark_ineffective(rim,"fill_planar_hole") (durable ban); paired_rim_fingerprints body fully wrapped in try; untargetable refusals keyed entity_type:index; docstring corrected; label/to_dict asserted. 5 new hermetic tests.
Result: unit+native 17 passed (native ran). lint OK, typecheck 0 new.
Mutation (mark_ineffective removed): test_the_rim_ban_survives_the_other_rim_being_closed FAILS (see run output: 1 failed); restored afterwards.

## Fix round 2
Run-level refusal: RunHistory.fill_refused_because + refuse_filling(); propose skips fill_planar_hole whenever set; mark_ineffective for rims removed (ineffective stays "tried and rolled back"). Finding-3 test now has boundary_edges defects and a free_wire_containing that raises ImportError.
Result: unit+native 19 passed (native ran); lint OK; typecheck 0 new.
Mutations (each reverted): refuse_filling removed -> 3 tests fail (fresh-fingerprint, not-a-failed-attempt, ban-survives). mark_ineffective re-added -> test_a_rim_refusal_is_not_recorded_as_a_failed_attempt fails. try narrowed to _paired_rims only -> test_the_rim_question_never_raises... fails.
Note: the round-1 version of the finding-3 test passed even with a narrowed try on this host (kernel present, real lookup swallows non-shapes); hence the monkeypatched ImportError.
