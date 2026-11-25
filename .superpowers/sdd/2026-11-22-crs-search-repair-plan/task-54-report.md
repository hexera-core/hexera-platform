# Task 54 report: the last three staged-healer-only ops, and what op coverage still does not buy

Branch `sdd/task-54`, based on `de96441c`. Changed files: `src/meshpipeline/cad/repair_v4/search.py`,
`tests/unit/cad/repair_v4/test_det_coverage.py`.

## 1. The claim, checked rather than assumed

Lane C3's smaller finding was that `tighten_tolerances`, `fix_poles` and `to_canonical` run only in the pre-search
staged healer, so during the search only the AI can re-run them. That is what the tree says.

Where the staged healer applies them: `stages.py:175` (`tighten_tolerances`, stage `normalise`, whole part,
`target_rel=1e-5`), `stages.py:180` (`fix_poles`, stage `edges`, over `s.ids("F")` = every face),
`stages.py:187` (`to_canonical`, stage `faces`, over every face at `tol_mm=0.01`).

Where `DetGenerator` emitted rows before this change: `search.py` `_fill_rows`, `_sweep` and `_coverage` — 16 ops, and
none of the three. The check is now mechanical rather than a grep: the new completeness test takes the registry at
import time, sweeps one site of every kind, and asserts the difference. Run against the unchanged
`search.py` it reported the difference as `{fix_poles, to_canonical, tighten_tolerances}` and nothing else.

So all three were genuinely unreachable from the search's deterministic generator. **Two of the three are now
deterministic candidates. The third is not, and that is a decision, not an omission** — see §2.

## 2. `tighten_tolerances`: unreachable, and deliberately left unreachable

It is the one candidate that would cost budget for nothing, because its result cannot survive the driver's duplicate
check:

* its kernel half mutates tolerances only and returns the shape it read, with an empty history
  (`ops/edges.py:308-400`, `_finish(..., old, base, base, _no_history(), force, start)` at `ops/edges.py:400`);
* `_fingerprint` is `(tuple(sorted(ws.ids.values())), ws.signature)` (`search.py:801`). The values are
  centroid-and-box fingerprints (`ws_kernel.fingerprint`, `ws_kernel.py:54-61`) and `ws.signature` is
  `ws_kernel.shape_signature`: top type, compound/compsolid/solid/shell counts, closed-shell count and per-shell
  signed volume to six significant figures (`ws_kernel.py:86-121`). **Neither half can see a tolerance.**
  `Workspace` does not carry one at all (`workspace.py:88-104`).
* so a `tighten_tolerances` child always carries its parent's fingerprint, and `search.py:1312-1313` records it as
  `duplicate` and drops it before its verdict is used.

That cuts both ways, which is why leaving it out is the fair choice rather than the convenient one: the AI's
`tighten_tolerances` proposal is discarded by exactly the same rule, so the op buys the model nothing that det is
being denied. Adding a det row would spend one op per expansion on a state that is thrown away — precisely the
"redundant candidate" the brief says not to add.

It is recorded in code, not just here: `search.py` now carries `DET_UNREACHABLE_OPS`, a dict of
op -> why-it-is-not-swept, and the completeness test accepts an op as AI-only **only** if it is listed there. The
entry names the fix (a tolerance term in `shape_signature`, as Task 45 added orientation to it) and that it is a
kernel change needing a native gate. Not done here.

`run_stage` is listed in the same dict with lane C3's reason, so the one place that says "the AI may ask for this and
det may not" is now a single enumerated list.

## 3. What was added

Both new rows live in `DetGenerator._coverage` — the mechanism lane C3 established — not in a second mechanism.
They are appended after the site kind's own sweep rows, so `k_propose` cuts them first.

| op | gate (evidence) | sweep | why these values |
|---|---|---|---|
| `fix_poles` | site kind `pole` only (a mesher's own `pole` error, `sites._MESHER_KINDS`), and the site names at least one face | one candidate per face, the site's first `_MAX_POLE_FIXES` = 2, `params={}` | the kernel half RAISES when `ShapeFix_Face.Perform` changes nothing on ANY targeted face (`ops/edges.py:538-540`), so one face per call: one already-sound face must not sink the others. Not swept on `periodic` / `invalid`, which `ops.edges.FixPoles.applies_to` allows (`ops/edges.py:614`) but where nothing observed a broken wire — a failure there costs an op and teaches nothing. |
| `to_canonical` | the op's own `site_kinds` (`proximity`, `sliver`, `invalid`; `ops/faces.py:1207`) and a MEASURED free-form carrier on the site (`_FREEFORM`, the same set `reapproximate` uses) | one candidate per free-form face, first 2, at `tol_mm = 0.5 x fidelity_bound` | the op declines the WHOLE call on the first target that is already analytic (`"no_change: a face is already a Plane"`) or is not canonical within the tolerance (`ops/faces.py:966-989`), so one face per call. Half the bound, not a quarter: the recogniser needs slack to see a near-plane, and the staged healer already tried every face at once at 0.01 mm, which a single unrecognisable face is enough to decline. An analytic face is never proposed; a free-form face that is not near-canonical IS proposed, because only the kernel can know that (Task 47's "unknown means propose"). |

Absences are notes, as before: `fix_poles not proposed: the site names no face`,
`to_canonical not proposed: no free-form face is known on the site`, and the shared no-diagonal note for
`to_canonical` (it needs the bound).

### Fingerprint and cycles

* `fix_poles` adds degenerate edges / a seam and rebuilds the faces through a `ShapeBuild_ReShape` history, so the
  entity set and the boxes move: the child is a new fingerprint.
* `to_canonical` replaces a face's carrier (`_replace_surfaces`), so geometry moves by up to the tolerance, which is
  5e-2 mm on a 120 mm part against a fingerprint quantum of ~1e-4 mm (`identity.quantum_for`): the child is a new
  fingerprint. The residual case — a snap so symmetric that no box and no shell volume moves at six significant
  figures — would be deduped, and is a hand-back below rather than a claim that it cannot happen.
* Neither fires unconditionally. Both gates are re-measured facts of the state being expanded: once the pole is fixed
  the mesher stops reporting a `pole` site, and once a face is canonical its measured surface type leaves `_FREEFORM`.
  So the evidence that earns the row is also what withdraws it; I added no history-based guard and no new state.
* Budget: at most 2 extra candidates on a `pole` site and 2 on a `proximity`/`sliver`/`invalid` site that carries a
  free-form face, under the existing `k_propose = 12`, after the kind's own rows. No charging rule is assumed
  (Task 53 is parallel): the rows are per-site and bounded, not per-round unconditional.

## 4. Red, then green

Tests first, in the file lane C3 created (`tests/unit/cad/repair_v4/test_det_coverage.py`); I extended `_coverage`'s
test module rather than writing a parallel check, for the reason the brief gives — the next op added must not be able
to repeat the bug.

Red (the new tests against the unchanged `search.py`):

```
PYTHONPATH=src python -m pytest tests/unit/cad/repair_v4/test_det_coverage.py -q
-> 5 failed, 14 passed                                                             exit 1
   test_a_pole_site_gets_fix_poles_one_face_at_a_time
   test_fix_poles_needs_a_face_and_an_observed_pole
   test_to_canonical_snaps_one_free_form_face_per_candidate_at_half_the_bound
   test_to_canonical_is_not_offered_for_analytic_faces_or_without_a_diagonal
   test_the_det_sweep_reaches_every_registry_op_except_the_ones_it_explains
```

Green, after the two `_coverage` blocks and the constants: `24 passed`, exit 0.

New tests, beyond the per-op gate/sweep/negative ones:

* `test_the_det_sweep_reaches_every_registry_op_except_the_ones_it_explains` — the structural guarantee. It snapshots
  `ops.REGISTRY` at import (the autouse `world` fixture adds a fake op per test, and asserts the snapshot is clean),
  sweeps one site of each of the 10 kinds, and asserts
  `REAL_OPS - swept == set(DET_UNREACHABLE_OPS) - {RUN_STAGE}`, plus that every listed op is genuinely not swept and
  carries a reason. A new op in `REGISTRY` now fails this test until it is either swept or explained.
* `test_each_covered_op_is_reached_from_the_search_det_arm` is now parametrised over `DET_COVERED_OPS`, one minimal
  site per op, each through a real `search()` on the `search_det` arm at the PRODUCTION `k_propose = 12` rather than
  at a cap raised for the test. (The old single-search version could not have shown the new rows at all: four sites'
  rows overflow 12 per expansion, which is itself worth knowing.)

Mutation check: with `_POLE_KINDS = ()` and `_CANONICAL_KINDS = ()`, 8 of the 24 fail (both positives, both
negatives — they assert the NOTE that records the absence — the schema test, the completeness test, and the two new
`search()` reach cases); the other 16 still pass. Restored afterwards.

## 5. Gates

Run in `/Users/kitts/archductor/sdd-worktrees/task-54`. `PY` below is
`/Users/kitts/archductor/assets/ocpvenv/bin/python` (3.12.12); mypy is `sdd-worktrees/task-21/.venv/bin/mypy`,
which is the pinned 2.1.0. This worktree has no `.venv`.

| command | exit |
|---|---|
| `PYTHONPATH=src $PY -m pytest tests/unit/cad/repair_v4 -q --continue-on-collection-errors` | 1 — **715 passed**, 1 collection error |
| `PYTHONPATH=src $PY -m pytest tests/unit/dev -q --continue-on-collection-errors` | 1 — 1208 passed, 15 failed, 4 collection errors |
| `PYTHONPATH=src $PY -m ruff check src/meshpipeline/cad/repair_v4/search.py tests/unit/cad/repair_v4/test_det_coverage.py` | **0** |
| `PYTHONPATH=src $PY -m ruff format --check <same two files>` | 1 — see below |
| `PYTHONPATH=src mypy src/meshpipeline/cad/repair_v4/search.py` | **0** — `Success: no issues found in 1 source file` |

**I did not run the full `tests/unit` tier: the coordinator instructed me not to** (the host was at load ~314 with
several lanes running it concurrently, and will run it once centrally on an x86 gate VM on the merged result).
Nothing here should be read as the full tier passing. I ran the tier for the directory I changed
(`tests/unit/cad/repair_v4`) and the only other unit directory that imports `repair_v4.search`
(`tests/unit/dev` — `test_v4_arms.py`, `test_v4_report.py`; both passed).

The non-zero exits above are this host's environment, not the change:

* `tests/unit/cad/repair_v4/test_proposer_wave_resilience.py` fails to collect: `ModuleNotFoundError: No module named
  'anyio'`. The interpreter available to me has pytest, numpy and cadquery-ocp and none of the framework
  dependencies; nothing in that module touches `search.py`.
* The 15 `tests/unit/dev` failures are all in `test_validation_runner_isolation.py`, which probes host-virtualenv
  shapes and the validation runner; this worktree has no `.venv` and no Docker here. They neither import nor exercise
  anything I changed.
* `ruff format --check` is not this repository's formatter: `make lint` is `ruff check $(LINT_PATHS)` only
  (`Makefile:522-523`), and `ruff format --check` reformats untouched files too (checked on `stages.py` and
  `proposer.py`, both "would be reformatted"). I ran it because the brief asked; its failure is repo-wide and
  pre-existing, and the file conforms to `[tool.ruff] line-length = 120`, which `ruff check` enforces.

**No native run.** The OCCT tier needs an x86 host and `meshpipeline-mesh:native`, and no gate VM is up; I did not
exercise `fix_poles` or `to_canonical` against the real kernel. The proposal logic is fully covered hermetically, and
both ops already have native tests of their own behaviour
(`tests/native/test_native_repair_v4_ops_edges.py:315`, `tests/native/test_native_repair_v4_ops_faces.py:469`). What
is NOT proved on a kernel is the pairing: that a det `fix_poles` on a real `pole` site's face applies, and that a
single-face `to_canonical` at half the bound is accepted where the staged whole-part call declined. Lane C3 added
`tests/native/test_native_repair_v4_det_coverage.py` for its four ops; the two new rows deserve the same two cases
there, and I did not write them because I could not run them.

## 6. The coordinator's two questions

### 6.1 Is any op reachable ONLY from inside a stage?

No — not beyond lane C3's three, and after this change only `tighten_tolerances` is.

Ops applied by `stages.py` (every `s.apply` in the file, 16 distinct):
`tighten_tolerances` :175, `fix_poles` :180, `rebuild_edge_as_intersection` :182, `to_canonical` :187,
`rebuild_face` :190, `orient_and_close` :195/:214/:260, `make_volume` :197/:225, `remove_small` :210,
`fill_loop` :212, `sew` :224, `conformalise` :235, `unify_same_domain` :240, `split_angle` :246,
`split_closed` :248, `split_continuity` :250, `split_by_proximity` :253.

Ops `DetGenerator` can emit (18 distinct, all in `search.py`): `fill_loop` :581 (`_fill_rows`),
`rebuild_face` :596, `fix_poles` :604, `rebuild_edge_as_intersection` :630, `reapproximate` :638,
`to_canonical` :648, `refit_cluster` :657 (all `_coverage`), `split_by_proximity` :696, `split_angle` :704,
`split_closed` :706, `split_continuity` :710, `orient_and_close` :711/:745, `make_volume` :715/:744,
`remove_small` :723, `unify_same_domain` :729, `sew` :735, `conformalise` :751,
`remove_duplicate_faces` :760 (all `_sweep`). `_recipe_rows` :558 can emit any op a recorded recipe step names, but
that is data, not a guarantee, so I excluded it from the sets.

* **stages − det = `{tighten_tolerances}`** (before this change: `{tighten_tolerances, fix_poles, to_canonical}`).
* **det − stages = `{reapproximate, refit_cluster, remove_duplicate_faces}`** — three ops no stage applies at all.
  Both arms reach them directly, so they are no asymmetry; worth knowing only because it means `run_stage` is not a
  superset of the sweep in either direction.

The completeness test now makes this difference a gate rather than a grep, so your scope question is answered
permanently: op-level, det is missing exactly one op, and that one is enumerated with its reason.

### 6.2 The one-state-transition asymmetry

**I agree it is real, and the code says it is bigger than a budget-charging question.** Three findings, all
readable in the tree; none needs a native run.

**(a) No state is pruned, so this is not about reachability in principle.** Every cheap-judged child is pushed to the
frontier regardless of its score (`search.py:1335`), and `_entry` (:776) orders by rank with deeper-first on ties.
So a chain whose intermediate states score WORSE than their parent is not discarded — it just sinks in the frontier
and is expanded only when nothing better is left.

**(b) But the stopping rule is counted per EXPANSION, and only a CONFIRMED child can reset it.** `progressed` is set
only inside the confirmation loop, and only for a child whose FULL score is `>= best.score` (:1376-1381); `improved`
needs `> best.score`. `flat = 0 if (improved or progressed) else flat + 1` and `flat >= patience_now` ends the
search (:1403-1405). Two consequences:

* a round whose only children score worse than the best so far is a flat round, so a k-op chain with a dip costs k
  flat rounds against `patience_now = max(3, open_sites + 2)` (`scaled_patience`, :1023). A 3-op dip exhausts the
  floor exactly.
* only the top `k_confirm = 3` children BY CHEAP SCORE are judged with the full panel (:1354). An intermediate state
  that is not in that top 3 cannot reset patience at all, and cannot become `best` (so cannot be delivered) even if
  it is on the only path to a clean file.

A `run_stage` candidate is immune to both: it is ONE candidate, ONE op of budget, ONE child and ONE verdict
(:1273 routes it, `_stage_outcome` :843 applies the whole chain), so its internal dips are invisible to the
frontier, to `k_confirm` and to patience. The explicit design note at `_entry` handles chains that TIE
("a fill_loop that closes one hole of three improves nothing yet"); it does not handle chains that get worse before
they get better. The AI's macro does not need that handling, and the det arm has nothing else.

**(c) Worse than the ordering asymmetry: even op-for-op, the det arm cannot issue the same CALL.** Stage bodies
apply ops to the WHOLE part at stage defaults — `to_canonical` over `s.ids("F")` at 0.01 mm (:187), `fix_poles` over
every face (:180), `split_angle`/`split_closed`/`split_continuity` over every face (:246-250),
`unify_same_domain` with `scope="all"` (:240), `tighten_tolerances` over the whole part (:175). Every `DetGenerator`
row is site-local and diagonal-relative, and deliberately capped (at most 2 faces for `to_canonical`, 8 for
`reapproximate`, 6 for `refit_cluster`, 2 edges, 2 face rebuilds). So for a part whose repair needs the whole-part
call, no sequence of det candidates reproduces the stage's output, at any budget. `_fingerprint` is not the obstacle
— it only merges states that genuinely reached the same geometry; the obstacle is that the det arm never proposes
that geometry.

**So: op-level coverage is necessary but not sufficient for a fair baseline.** That is what the code says, and it
means lane C3's work and this task close the op-access gap without closing the reach gap. What I cannot settle
without runs on real files is the SIZE of the effect: how often a repair needs a chain with a dip, and how often it
needs a whole-part call that no site-local row makes. Both are measurable without the kernel being rebuilt — the
trace already records `candidate` events with `source`, and a run_stage candidate's `StagedResult` names the ops it
applied — so the honest next step is a wave-number question for triage (lane C2), not another code change. I did not
change anything for this, per your instruction.

If you want it narrowed cheaply, the two smallest experiments are: (1) a hermetic test that a 3-op chain with a dip
is unreachable at `patience=3` while the equivalent `run_stage` is reached in one round — that would pin (b) as a
test rather than a reading; (2) count, in the last wave's traces, the `run_stage` candidates the AI proposed that
became `best`, and how many ops each applied.

## 7. What I did not do

* No native run, and no native test for the two new rows (§5).
* No tolerance term in `shape_signature`, so `tighten_tolerances` stays out of the search for BOTH arms (§2).
* No fix for the stage/macro asymmetry, and no change to `run_stage` (your call, escalated).
* No history-based "do not repeat this normalisation" guard; the measured gates make the rows self-withdrawing (§3),
  and a guard would have changed behaviour for lane C3's rows too.
* No charging rule assumed or touched (Task 53 is parallel).
* I did not touch `tests/unit/cad/repair_v4/conftest.py` or add an `__init__.py` there, per your instruction.

## 8. Risk handed back

1. **A det row that only ever declines costs an op per expansion.** `to_canonical` on a face that stays free-form and
   is not near-canonical will be re-proposed on every state that still carries that site, declining each time. This
   is inherited from lane C3's rows (`reapproximate` has the same shape) and bounded by `k_propose`, but
   `DetGenerator` has no `on_rejected` memory, so nothing learns from a decline within a search. A small, general
   fix — remember declined `(op, targets, params)` per lineage — would help every row, and belongs in its own task.
2. **A symmetric canonical snap could still be deduped.** If a `to_canonical` child moved no entity box past the
   fingerprint quantum and no shell volume at six significant figures, it would be discarded as a duplicate like
   `tighten_tolerances`. I judge it unlikely at 0.05 mm against a ~1e-4 mm quantum but have not proved it on a
   kernel; the native case in §5 would.
3. **`search_det` moves again.** As with lane C3, the AI marginal must be measured on a commit that includes this
   change; any number from before it overstates the AI, now by two more ops.
4. **The pole gate is narrow on purpose.** A pole defect that layer 1 reports as `brep_invalid` without any mesher
   raising `pole` gets no `fix_poles` row. Widening it to `invalid` would buy coverage at the price of an op spent on
   a `RuntimeError` whenever the wire is in fact sound; the evidence to decide that is a wave number
   (`ops.fix_poles.det` evaluated vs failed), not a judgement I can make here.
