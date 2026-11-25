# Lane B12 report: the fidelity caps are removed (owner decision 2026-11-24)

Branch `sdd/lane-b`, based on `bcab7d28`, rebased onto the feature tip `8dbba191`+ (with C7 and D14) before hand-off. The native gate ran on `jig` in one labelled container (`owner=lane-b12`), which was removed afterwards along with `/srv/work/lane-b12`. No model calls, no VM, no local Docker.

## What was deleted (not loosened)
- **Score.** `verdict.assemble.score()` key 0 is now **validity**, no longer faithfulness. The tuple is (valid, panel passes, min SICN, ...), and deviation and churn are tie-breakers only. A state that meshes in more members outranks any state that moved the part less.
- **Staged healer keep rule.** `stages._judge` no longer rejects a transaction on layer 3, and `_SOLIDLESS_OK_REASONS` is gone. It keeps a transaction on `score(v)` alone.
- **ops/faces.**
  - The `_over_bound` declines in fill_loop, rebuild_face, replace_surfaces and refit_cluster are gone.
  - The 5%-of-diagonal fill_loop span decline is gone.
  - `_tolerance_gate` is gone, and `MAX_AREA_CHANGE_RATIO` is gone.
  - `tol_mm` has no schema maximum.
  - `fidelity_bound` is still computed so that `bound_mm` is **reported** beside each measurement.
- **ops/edges.** The rebuild_edge `over_bound` decline is gone. `max_move` and `fidelity_bound` are still reported.
- **ops/shells.**
  - `remove_small` declines only `max_width_mm <= 0`, so defeaturing by width is allowed.
  - `sew` declines only rungs below the numerical floor (1e-6 x diagonal). There is no upper bound.
  - The schema maxima are removed.
- **ops/split.** `guard()` no longer declines on area or volume change. It keeps the solid-count and validity checks, which are structural rather than fidelity checks.
- **body_kernel and ops/bodies (after the rebase onto C7 `d18ef84a`).**
  - C7 had already landed cut_by at any depth and a separate `fuse` op. I **took C7's version** and dropped my own `fuse` conformalise mode, so there is one fuse, C7's.
  - On top of C7 I deleted what was left:
    - the `fuzzy_mm > bound` `exceeds_fidelity` decline;
    - the `fuzzy_mm` 0.1 mm schema maximum, on conformalise and on fuse;
    - the unused `MAX_VOLUME_CHANGE`.
  - **B7's measured depth stays a measurement.** C7's cut records depth, method, volume and bound, and the triage overlap probe still reports them.
- **Search.** `_fill_rows` proposes fill_loop for every loop, whatever its span. Overlap sites get fuse rows (C7's, kept on the rebase). The sweep's fractions-of-bound parameter choices are kept as **proposals**, not limits.
- **Proposer prompt.** It says "A file is clean when it is VALID and MESHABLE", followed by "FAITHFULNESS IS REPORTED, NOT REQUIRED". The bound is gone from the op summaries and from DECLARE_UNFIXABLE.
- **limits.MAX_FILL_SPAN_RATIO** remains a **reporting** threshold only: it feeds faithful_kernel's fill_too_large and the injector's strata.

## Kept
- The spend caps, the worker kill and timeout, the validity guards, and the sew floor.
- The tighten_tolerances `target_rel` range.
- The cut's victim-split body-count check.
- `clean_of` and `search._clean_with`: **not touched**. The plan amendment assigns the verdict and report change to A10.

## Injector (devtools/v4_eval/inject.py)
The op no longer declines an unfaithful fill, so `_fill_restores` now applies the bound to the op's **own measured** `fill_to_surroundings`. The `face_delete_inclined` stratum keeps its meaning: a loop that a planar fill cannot restore within the bound. **Open for the owner or LEAD-2:** under the new goal, that stratum's `expected_outcome: correctly_declined` (and face_delete_large's) is arguably no longer right, because a fill that meshes is now clean. I did not relabel the benchmark strata.

## Tests
- **New `tests/unit/cad/repair_v4/test_no_fidelity_gate.py` (11).**
  - A large-deviation state **wins the search and is delivered**.
  - Score key 0 is validity.
  - Each op schema accepts a big tol, width or fuzz, including fuse and deep cut_by.
  - The det sweep proposes fuse.
- **New `tests/native/test_native_repair_v4_no_fidelity_gate.py`.**
  - An open-box fill above 50% of the diagonal applies.
  - A 5 mm cut_by cuts and reports a depth of about 5.
  - `remove_small` at a 15 mm width is not refused.
- **Every native and unit test that pinned a fidelity decline** was rewritten to assert the op applies and reports its measurement, or never declines on fidelity:
  - fill_cap
  - ops_bodies (5)
  - ops_edges
  - ops_faces (5)
  - ops_shells (2)
  - ops_split guard
  - scaled_ladders
  - inject (3)
  - unit score, stages, search_wiring, scaled_ladders and proposer
- **Native on jig, after the rebase** (`pytest tests/native -k "repair_v4 or v4_inject"`): **300 passed, 1 failed**. The failure is `test_repair_v4_is_callable_from_a_script_without_a_main_guard`, pre-existing and identical on base (see B8).
- **Host `tests/unit/cad/repair_v4 tests/unit/dev`, after the rebase**, head against the feature tip: base 2231 passed / 42 failed, head 2243 passed / the SAME 42 (host /tmp-path; `comm` empty both ways).
- ruff clean on every changed file; mypy clean on the 11 changed src files.

## For LEAD-2: possible overlaps
- **A10.** I changed `score()` key 0; A10 owns the verdict change. If A10 also rewrites `score`, keep one of the two.
- **C7.** Resolved on the rebase: C7's fuse op and cut are kept, and mine were dropped.
- **D14.** `defeature` (merged) has no fidelity gate, and none was added. B12's own share of face removal is deleting the remove_small width bound.
