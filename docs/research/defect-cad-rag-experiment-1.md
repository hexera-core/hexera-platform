# Experiment 1: independent action sweep + exact kNN over defect patches

**Date:** 2026-10-29 · **Code:** `labs/defect_cad_rag/` · **Design:**
`docs/superpowers/specs/2026-10-29-defect-cad-rag-design.md` · **Direction:**
`docs/research/defect-cad-rag-literature-review.md` (iterations A–D)

Everything below was measured on this host (10-core mac, pinned `.venv`, engine commit
`f6eebb22`, lab flags all on, `CAD_REPAIR_REVIEW_ENABLED=false`). Raw outputs:
`/tmp/dcr-sweep` (OCCT), `/tmp/dcr-sweep-dg` (defectgen), reports `/tmp/dcr-matrix*.json`,
`/tmp/dcr-knn*.json`, `/tmp/dcr-stability-14.json`.

## What was run

1. **Patch + descriptor (iteration B).** `isolate_patch()`: three nested face sets (own faces /
   adjacent ring / second ring clipped to 4× local scale). `describe()`: 65 intrinsic dimensions
   (counts, free-edge fraction, surface/curve type mixes, log size ratios), `hand-v2`.
2. **Stability tests (B).** 14 defect-bearing OCCT files, 584 defects, three variants each:
   rigid motion, STEP round trip, both.
3. **Independent action sweep (A + C).** Every applicable catalogue operation tried alone from the
   same pre-repair snapshot through the unchanged `RepairSession` (caps, rollback, progress rule
   all live). One subprocess per file, ≤24 stratified defects per file, 150 s file budget.
   Corpora: 331 OCCT files, 50 defectgen files (manifest families).
4. **Leave-one-family-out exact kNN (D).** Same-code neighbours, failure-aware shrinkage to the
   code prior, ties to the rules choice. Baselines: `rules` (narrowest applicable op) and `prior`
   (best kept rate for the code, no geometry). Family-bootstrap CIs.

## Results

### Stability (gate for everything else)

- Descriptor **exactly invariant under rigid motion** (max drift 0.0 over 584 defects).
- STEP round trip: max drift 0.22 (vector norm ~tens), self-retrieval 96.4%; two drift causes were
  found and fixed in `hand-v2` (length floor at 1e-6 of part scale for zero-length edges; lower
  quartile instead of minimum edge length).
- **Box fingerprints do not survive rigid motion** (47/581): expected — they are axis-aligned —
  but it means the fix memory cannot key on fingerprints across poses, only within one shape.
- **The localiser itself is not pose-invariant**: as1_motor reports 308 small_edge in the original
  pose and 320 rotated. `SMALL_EDGE_LENGTH_FRACTION` thresholds on the AABB diagonal, which grows
  under rotation. Harness/eval issue to note; not changed in this experiment.

### Action-outcome matrix (the honest label base)

OCCT: 331 files → 320 swept, **11 crashed/errored and were recorded as rows, not lost** (the
trj4/xr1 group plus bug30790 kill or fault the interpreter — the crash-isolation requirement is
real). 9 040 located defects, 1 761 sampled, 2 923 judged attempts. 8 files arrive already over
the 0.1 mm tolerance cap (`precap.py`) — every attempt on them rolls back for a reason that says
nothing about the operation, so they are excluded from labels.

Kept attempts per (code → op), OCCT excluding precapped, then defectgen:

| code → op | kept | judged | files/families with a keep |
|---|---:|---:|---|
| open_shell → sew_faces | 143 | 437 | 14 / 6 |
| open_shell → fill_planar_hole | 5 | 81 unguarded | 5 / 5 |
| small_edge → fix_small_edges | 18 | 583 | 4 / 4 |
| small_face → fix_small_faces (defectgen) | 22 | 263 | 5 / 4 |
| small_edge → fix_small_faces (defectgen) | 7 | 135 | 2 / 1 |
| self_intersection → heal | 0 | 464 | — (multi_body declines dominate) |
| invalid_brep → fix_shape_global | 0 | 29 | — |

**184 independent kept labels** against 21 planner-selected kept records in all previous evidence
combined — the sweep multiplies the usable label base ~9× from existing files, without a new
defectgen campaign. 390 of 454 sampled OCCT open_shell fills were withheld by the paired-rim
guard, as they should be.

### Retrieval value

**Operation selection is the wrong first target.** Leave-family-out top-1 kept rate: rules
0.1012, prior 0.1012, knn 0.1012, **oracle 0.1018** (n=1601). The oracle — pick the best judged
op per defect — barely beats the rules, because the catalogue rarely offers two viable operations
for one defect. No reranker can win meaningfully here until the catalogue widens.

**Outcome prediction is the right first target.** "Will the rules-chosen operation be kept?"
(the abstention question), AUC on held-out families:

| corpus / code | knn | code prior | n (kept) |
|---|---:|---:|---|
| combined, all | **0.895** | 0.791 | 1999 (184) |
| OCCT open_shell | **0.716** | 0.246 | 437 (143) |
| combined small_face | **0.750** | 0.047 | 351 (23) |
| defectgen small_face | **0.852** | 0.055 | 263 (22) |
| combined small_edge | **0.562** | 0.063 | 718 (18) |

Within one code the prior has nothing to say (its within-code AUC is degenerate); the lift is
local geometry alone. Patch descriptors predict repair outcome well for open_shell and
small_face, weakly for small_edge (18 positives — label-starved, not disproven).

### Timing (phase-0/1 acceptance numbers)

| metric | p50 | p90 | max |
|---|---:|---:|---:|
| isolate_patch, s/defect | 0.0003 | 0.0043 | 0.31 |
| describe, s/defect | 0.0004 | 0.0024 | 0.21 |
| one op attempt, s | 0.61 | 2.25 | 85.0 |
| sweep file, s | 7.5 | 67.5 | 195.7 |

Isolation + description is ~1 ms per defect — noise against `CAD_REPAIR_TIMEOUT_S=120`. The
expensive thing is *attempting* an operation. At thousands of defects in flight, a predictor at
these AUCs that skips doomed attempts buys real budget; that is where retrieval pays first.

## Conclusions for the design

1. **Reframe first use: outcome prediction / attempt ordering, not operation reranking.** Shadow
   metric becomes "AUC of kept-prediction for the rules op per code", and the first live behaviour
   is ordering or skipping attempts inside the existing budget — the caps still judge everything.
2. The sweep is the label factory; it already exists and multiplied labels ~9×. Re-run it per
   engine commit. **No new defectgen campaign needed yet** (confirms the zero-budget default).
3. `hand-v2` passes the stability gate; exact kNN is adequate at this scale (2k vectors). No
   encoder, no pgvector yet.
4. Harness findings to carry into the spec: crash isolation is mandatory (11/331 OCCT files);
   precapped files must be excluded from labels; the localiser's AABB-diagonal thresholds are not
   pose-invariant; box fingerprints are pose-local.
5. small_edge and heal/self_intersection stay label-starved — small_edge keeps are rare
   everywhere; every heal attempt declines (`multi_body`, `not_isolated`) or trips the tolerance
   cap. These need operation work, not retrieval work.

## Limits

- One campaign of defectgen (18 families) and one OCCT release; families are a name heuristic on
  OCCT (`sweep.family_of`), a manifest on defectgen.
- ≤24 defects sampled per file; `small_face → fix_small_faces` on OCCT was budget-truncated
  (84 of 172 attempts hit the 150 s file budget).
- kNN is same-code with shrinkage; no calibration curve or abstention threshold chosen yet — that
  is the next experiment, after the small_face/small_edge label base grows.
