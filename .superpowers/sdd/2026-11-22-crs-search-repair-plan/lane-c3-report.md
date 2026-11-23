# Lane C3 report: the deterministic baseline reaches every op the AI can

## Why

The round-2 audit found four ops that only the AI proposer could reach. Until this change, `search_ai - search_det`
credited the model with ACCESS to those ops, not with judgement about them. Each op now has a small deterministic
sweep in `DetGenerator._coverage`. A sweep fires only on evidence from the site's own measured facts, its rows come
after the site kind's existing rows (so `k_propose` cuts them first), and every drop is recorded in `notes`.

**This changes the `search_det` baseline. The AI marginal must be measured on a commit that includes this change;
numbers from before it overstate the AI.**

## Ops that became deterministically reachable

| op | site kinds | evidence that gates it | sweep (mildest first) | why these values |
|---|---|---|---|---|
| `rebuild_face` | `self_overlap`; `invalid` only with a `self_intersection` or `brep_invalid` hard code | at least one face | `{"continuity": "G0"}` on each of the site's first 2 faces | The op takes exactly one face. G0 is the mild re-fill; the op honours G1 only where a neighbour constrains it, so G1 is left to the AI. |
| `rebuild_edge_as_intersection` | `invalid`, `self_overlap`, `overlap`, `intersection`, `proximity` (the op's own side kinds) | a two-face edge in the measured graph whose tolerance OR face-to-face gap is above `1e-5 x diagonal` | the worst 2 such edges, one candidate each | 1e-5 x diagonal is the normalise stage's own target, so these are edges the healer could not bring to the tolerance it aims for. One edge per candidate, so one bad edge cannot sink the others. Free and non-manifold edges are never chosen (the op needs exactly two faces). |
| `reapproximate` | `self_overlap`, `pole`, `periodic`, `proximity` (the op's `site_kinds`) | the measured surface type is free-form (B-spline, Bezier, offset, revolution, extrusion, other) | `(tol 0.25 x bound, max_degree 9)`, then `(tol 0.5 x bound, max_degree 6)`, on up to 8 such faces | The op is deviation-gated, so a sweep never takes the whole fidelity bound: it leaves room for later ops. Degree 9 keeps the shape; degree 6 actually simplifies it. An analytic face would only be converted to a spline, so it is not proposed. |
| `refit_cluster` | `sliver`, `overlap`, `self_overlap` (the op's `site_kinds`) | 2 to 6 site faces, measured and connected through shared edges, none spanning a full turn (a seam) | the whole cluster at `tol 0.5 x bound`, then `tol 1.0 x bound` | The op's documented limits are one connected outline and no seam faces, so a cluster it would refuse is not proposed. The cap of 6 keeps the fit local. The op declines above its own `tol_mm`, so the looser rung is a real second chance. |

Bound = `min(0.1 mm, 0.001 x diagonal)` (`ops.faces.fidelity_bound`). The diagonal is asked lazily, only when one of
these sweeps is about to run, so a state with no such site costs no extra kernel call. A diagonal that cannot be
measured gives a note, never a crash. Task 47's predictions are untouched: nothing here proposes on an `open` site,
and nothing re-adds a sew, make_volume, fill or orient proposal Task 47 removed.

## `run_stage`: deliberately not added, and a fairness finding

`run_stage` is a macro, not an op. Each stage body is a fixed chain of ops (`stages.py`), and the search already starts
from `run_staged` at default parameters. With this change, DetGenerator reaches every op a stage can run.
Re-proposing stages would duplicate the sweeps already proposed (for example `shells` = sew + make_volume, which
Task 47 spent a task cutting back).

**Finding for the lead / owner:** a `run_stage` candidate costs ONE op in the search budget, but runs several ops
(`shells` is sew + make_volume, and up to remove_small + fill + orient). The AI can therefore buy a chain for the
price of one op, while det pays per op. On an op-bounded comparison that is an AI advantage that is not judgement.
Two ways to fix it, both a budget-policy call outside this lane's brief: charge `run_stage` by the ops its stage
applied (`StagedResult` / the diffs), or give det the same macro. Not changed here.

## Also found: three more ops reachable only through the AI or the start-of-search stages

`tighten_tolerances`, `fix_poles` and `to_canonical` run in the staged healer before the search starts, but
DetGenerator never proposes them during the search. After any op changes the state, only the AI can re-run them.
That is a smaller handicap of the same kind. It is listed, not fixed, to keep this change to its brief. The same
evidence gates would serve: an edge tolerance over the target for `tighten_tolerances`, a `pole` site for
`fix_poles`, a near-analytic free-form face for `to_canonical`.

## Expected op-budget impact

Extra candidates per expansion, per site, at most:

| site kind | before | added (max) |
|---|---|---|
| `open` | unchanged | 0 (the 1-hole and 3-hole costs, 5 and 14 ops, are unchanged) |
| `self_overlap` | 3 (split_angle) | 8 = rebuild_face 2 + rebuild_edge 2 + reapproximate 2 + refit 2 |
| `proximity` | 6 | 4 = rebuild_edge 2 + reapproximate 2 |
| `periodic`, `pole` | 4 / 0 | 2 (reapproximate) |
| `sliver` | widths + unify | 2 (refit) |
| `overlap` | conformalise / duplicates | 4 = rebuild_edge 2 + refit 2 |
| `intersection` | conformalise / duplicates | 2 (rebuild_edge) |
| `invalid` | split_continuity, orient, (make_volume) | 4 = rebuild_face 2 (only with a self-intersection code) + rebuild_edge 2 |

Each row fires only on its evidence, and all of them sit under the existing `k_propose = 12` cap per expansion, after
the kind's own rows. With the eval budget of 60 ops, a site that gets every row spends at most 8 more ops per
expansion, so roughly 2 fewer expansions out of about 7. In return search_det can now try the repair at all. In
production (`CAD_REPAIR_MAX_OPS = 6`) the extra rows mostly fall outside the first expansion's 6 and matter only when
the kind's own sweep is short. The real impact is a wave number: run triage (lane C2) on the next dev wave and read
`ops.<op>.det` against `evaluated` / `declined`.

## Tests

- Unit `tests/unit/cad/repair_v4/test_det_coverage.py` (14): each op's gate, sweep and order; every negative
  (no evidence, analytic faces only, unmeasured graph, a disconnected / 7-face / seam cluster, no diagonal, the wrong
  site kind); every proposal passes `validate_call` against its op's own schema; each of the four is reached as a det
  candidate through `search()` on the `search_det` arm.
- Native `tests/native/test_native_repair_v4_det_coverage.py` (4), on the real kernel with the ops' own fixtures.
  The site graph is measured by the kernel (`sites._attach_graphs`) and the diagonal by `ws_diagonal`. Then
  `DetGenerator.propose`, then `REGISTRY[op].apply` on the real pool:
  - the inflated box edge: exactly that edge is proposed, and it applies;
  - the self-intersecting B-spline face: proposed at G0, and the rebuilt face no longer self-intersects;
  - a degree-8 B-spline: both rungs are proposed, and the ones the kernel accepts respect their degree and tolerance;
  - a 4-way split patch: the cluster is proposed at both rungs, and the mildest applies, declaring 1 added face and
    the 4 removed.
- Mutation: with the `_coverage` call removed, 13 of 18 tests fail (every positive test, unit and native); the 5 that
  still pass are the negative tests, as they should.
