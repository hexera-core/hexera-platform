# Lane B status for LEAD-3 (thread 101), 2026-11-24

Written to a file because thread 101's session (176) failed mid-handoff and
`archductor session send ... --thread-id 101` refuses with `state=failed`. I did not start a new
session: it would be a fresh agent without LEAD-3's context. Read this, or ask me to re-send.

## 1. B22 - `ff8da7b05` (the rule) + `537e25efa` (the native proof). 20 tests.

`tests/unit/cad/repair_v4/test_b22_catastrophic_loss.py` (12)

- `test_the_worked_case_is_refused_on_the_PART_rule` (sha `3e7c2a1197c3`, 0.00069x)
- `test_a_FUSE_is_not_a_loss_however_far_the_body_count_drops`
- `test_one_deleted_body_is_refused_even_when_the_part_is_mostly_there`
- `test_the_threshold_is_UNDER_ten_percent_on_both_rules`
- `test_a_body_whose_boolean_FAILED_is_unmeasured_and_never_a_loss`
- `test_a_solidless_input_can_never_trip_the_rule`
- `test_a_failed_CALL_refuses_nothing_and_says_so`
- `test_a_payload_that_measured_NOTHING_claims_nothing`
- `test_the_gate_REFUSES_a_catastrophic_state_that_beats_the_input_on_the_panel`
- `test_the_gate_delivers_the_same_state_when_the_loss_was_NOT_measured`
- `test_a_result_with_no_loss_field_at_all_still_delivers`
- `test_an_explicit_loss_argument_overrides_the_result_and_refuses`

`tests/unit/cad/repair_v4/test_search.py` (3, the RANKING half)

- `test_B22_a_state_that_kept_almost_none_of_the_part_is_refused_as_best`
- `test_B22_a_refused_state_is_not_delivered_even_when_it_is_the_only_one`
- `test_B22_an_unmeasurable_loss_changes_nothing_about_the_search`

`tests/unit/dev/test_v4_arms.py` (1, the REPORT)

- `test_a_catastrophic_loss_refusal_is_COUNTED_per_arm_and_named`

`tests/native/test_native_repair_v4_volume_loss.py` (5, real OCCT)

- `test_an_unchanged_output_keeps_the_whole_part`
- `test_a_FUSE_of_the_two_bodies_loses_NOTHING_though_the_volume_ratio_falls`
- `test_a_DELETED_body_is_measured_as_a_lost_body`
- `test_a_FRAGMENT_of_the_part_is_measured_as_the_part_being_lost`
- `test_a_SOLIDLESS_input_is_measured_and_refuses_nothing`

## 2. B24 - head `90ae5433a` on `sdd/lane-b`, pushed. The "next" list is done.

- **Registered**: `evalspec.ARMS` has `search_seq` on the SHARED budget, det-only (no model ceiling);
  `DISPATCHABLE_ARMS` picks it up. Dispatched from the same staged-and-confirmed start as the search
  arms, through LEAD-3's phase recorder under the same phase names, so one table compares the arms
  phase by phase.
- **Row and triage**: the counters (`full_checks`, `local_checks`, `rounds`, `ai_picks`, defects
  found/fixed/skipped by code, `skipped_why`, overlap pairs, `resolved_by_earlier_repair`,
  `inconclusive_verdicts`, `local_s`/`full_s`) and the owner's readable trace under `triage.seq`.
  `report.py` has a `sequential` block headlined **FULL CHECKS PER FILE**.
- **End to end on a real file** (dev part `2f3034493d1d`, real pool, real sites, C17's real
  `verify_local`; this host has no netgen so the run's "full" check is the cheap gmsh pair - the loop
  is what the run proves, the panel's breadth is what the fast loop measures):

```
round 1:
  overlapping_facets on F3+F5+F7+F11+... (clash 0.0942 mm, narrowest face F5 0.371 mm, member gmsh_default)
    -> split_angle + run_stage:condition [table: top rule] -> not applied: stage 'condition' was rolled back: it lowered the cheap score
    -> remove_small{max_width_mm=19.4} + split_angle{max_deg=60} [table] -> still there (1.1 s)
    -> remove_small{max_width_mm=19.4} + defeature{source=located} [table] -> gone (0.3 s)
  low_quality on B1 -> reapproximate -> not applied: needs the faces the defect is on, and re-located onto B1
  low_quality on B1 -> split_by_proximity -> not applied: needs measured face pairs that are still live
  full check: not clean, score (1, 2, 0.313, -64.7, -8), improved (0.9 s)
round 2:
  full check: not run: the round kept nothing
HEADLINE full checks for this file: 1   (against 2 local checks)
```

- **Five bugs the real runs found**, all fixed, none reachable by a hermetic test: an INCONCLUSIVE
  verdict made `judge.score` raise and crashed the arm; three ops need a required parameter no
  measurement gives (one documented default each, **marked in the trace as a default**);
  `split_by_proximity` needs the part diagonal, which only `open` sites carry (now measured once per
  round); the SECOND STEP of a recipe used the first step's stale ids, which killed every two-step
  recipe; and the NEXT DEFECT's ids were stale after a kept repair, plus a measurement that outlived
  its ids - every defect is now re-located through C17's `relocate`, a defect that cannot be located
  is `resolved_by_earlier_repair`, and derived parameters are filtered to live ids.
- **What is left: nothing before the fast loop.** 30 arm tests pass. `tests/unit/cad/repair_v4` is
  28 failed / 1043 passed on my branch - the same 28 LEAD-3 measured, with 4 more passing tests than
  their 1039. The det-only fast loop is theirs to launch.

## 3. LEAD-3's three conflict resolutions, checked on my branch (my own merge of the tip kept all three)

- `arms.py`: the `run = phases.run` wrapper, `reset_verdict_phases()`, and `vphases` carried into
  `_triage.search.verdict_phases` are all present. Their fix is right: mine reassigned `phases` and
  would have silenced every phase line after the search.
- `verdict/assemble.py`: `_phase("key")` after the config hash; `_phase("panel")` in BOTH paths,
  inside `mesh()`'s `finally` for the single-flight path; `_phase("panel_cache_hit")` only when this
  unit meshed nothing. More correct than what I wrote - mine would have timed another unit's mesh as
  our panel.
- `test_search.py`: D22's parallel-ranking test and my three B22 loss tests are both there.

## 4. Four commits of mine are NOT in the tip yet

`fcbd71f1e` is an ancestor of my HEAD, so I am current with A16/A17/C17/D23.

| commit | what |
|---|---|
| `061f9a3fc` | search_seq registered: eval row, report block, staged macro, the inconclusive-verdict crash |
| `e43da6a05` | `local_verify_kernel` (C's kernel half: `local_check` was unregistered on my branch) + the part diagonal |
| `654208797` | merge of the tip into `sdd/lane-b` |
| `90ae5433a` | the stale-id fixes (re-locate before each defect; derive from LIVE ids only) |

## One thing to watch in the wave

On this part the table's TOP rule for `overlapping_facets` was rolled back by the STAGE for lowering
the CHEAP score, and the win came from the second alternative. The oracle judged on the FULL verdict;
stages keep or roll back on the cheap one. If that is common, the arm pays for a first choice the
stage refuses - the same cheap-vs-full trap as B21. Every step is on the row, so the loop's traces
will say how often.
