# Catalogue gap analysis: what `no_operation_claims_it` is actually refusing

Status: evidence document. Read-only analysis of the Arm A baseline rows (default catalogue, all
expansion flags off) over 90 real failing CAD parts, cross-read against the 9,956-file corpus
aggregate (`devtools/validation/evidence/2026-11-19-corpus-harvest/aggregate.json`) and against
[the capability-expansion plan](2026-11-19-repair-capability-expansion.md).

**The headline, stated before the detail, because it changes the question that was asked.** The
2,242 refusals are not spread over a list of unclaimed defect codes. They are **one code**
(`small_face`) on **15 parts**, and `small_face` is **already claimed** by an operation that
exists in the catalogue — `fix_small_faces`. That operation returns no targets, and therefore
reads as "nothing claims it", solely because its own consent flag `CAD_REPAIR_FIX_SMALL_FACES`
defaults to `false` and Arm A ran with the flags off. The refusal census does not describe a
catalogue gap. It describes a switch.

## 0. The script

Run from the repository root; the rows are the Arm A shards.

```python
import json, glob, re
from collections import defaultdict

rows = [r for p in sorted(glob.glob("/tmp/eval-baseline/rows-*.json")) for r in json.load(open(p))]

def code_of(ref):            # detail reads "<code> needs a judgement no bounded operation makes"
    return ref["detail"].split(" needs a judgement", 1)[0].strip()

def solkind(r):
    n = (r.get("counts_before") or {}).get("solids")
    return "unknown" if not n else "single" if n == 1 else "multi"

parts, refus, nomesh = defaultdict(set), defaultdict(int), defaultdict(set)
stage, solids, located, reasons = (defaultdict(lambda: defaultdict(set)),
                                   defaultdict(lambda: defaultdict(set)),
                                   defaultdict(lambda: [0, 0]), defaultdict(int))
for row in rows:
    for ref in row.get("refusals", []):
        reasons[ref["reason"]] += 1
        if ref["reason"] != "no_operation_claims_it":
            continue
        c = code_of(ref)
        refus[c] += 1
        parts[c].add(row["file"])
        if row.get("meshes_after") is False:
            nomesh[c].add(row["file"])
        stage[c][str(row.get("mesh_kind_before"))].add(row["file"])
        solids[c][solkind(row)].add(row["file"])
        located[c][0 if re.fullmatch(r"[a-z_]+:[0-9a-f]{8,}", ref["defect"]) else 1] += 1

print(len(rows), dict(reasons))
for c in sorted(parts, key=lambda c: -len(parts[c])):
    print(c, refus[c], len(parts[c]), len(nomesh[c]),
          {k: len(v) for k, v in solids[c].items()},
          {k: len(v) for k, v in stage[c].items()}, located[c])
```

The extrapolation in §3 uses a second pass that buckets each row by
`(mesh_kind_before, single/multi)` and multiplies the per-stratum hit rate by that stratum's
`all.families[family].solids[kind]` count in the aggregate; it is reproduced inline there.

## 1. The refusal census

Over the 90 rows: **2,242 `no_operation_claims_it`** refusals and **4 `paired_rim`**. Nothing
else. Parts, not refusals, are the unit that matters:

| defect code | refusals | distinct parts | of those, fail to mesh before | fail to mesh after |
|---|---|---|---|---|
| `small_face` | 2,242 | **15** | 15 | 15 |
| *(every other code)* | 0 | 0 | — | — |

Three figures that keep this honest:

- **The 2,242 is inflated about 2x by re-scanning.** For every one of the 15 parts, the refusal
  count is exactly twice that part's `located_before["small_face"]` count — (32, 16), (20, 10),
  (48, 24), (48, 24), (72, 36), (10, 5), (20, 10), (20, 10), (392, 196), (184, 92), (448, 224),
  (626, 313), (48, 24), (68, 34), (206, 103). The sum of `located_before["small_face"]` over the
  15 is **1,121**. One refusal is emitted per finding per scan pass, and the loop scanned twice.
  The number of distinct *findings* refused is therefore ~1,121, not 2,242. (2,146 distinct
  fingerprint strings appear, because the second pass re-fingerprints after a kept operation
  changed the shape; the same face can be counted under two fingerprints.)
- **The refusals are concentrated.** 5 parts carry 1,856 of the 2,242 refusals (626, 448, 392,
  206, 184). The median refusal count across all 90 rows is 0. Three rows are identical in
  every count (faces=477, solids=7, 48 refusals each) and may be the same model submitted three
  times — the rows do not carry a content hash, so this cannot be confirmed from them.
- **The denominator is 20, not 90.** Only 20 of the 90 rows carry planner bookkeeping at all
  (`stopped_because`, `complete_scan`, `operations_attempted`, a non-empty `refusals` list). The
  other 70 rows have `refusals: []`, `operations_attempted: None`, and an `error` string such as
  `nothing was kept (0 operation(s) attempted...)`; 66 of 90 rows carry an `error`. So the
  measured statement is: **15 of the 20 parts that actually ran the repair loop to a stop (75%)
  hit this refusal.** Whether the other 70 parts would also have hit it is *not recorded in these
  rows* — see §5.

`small_face` is present in `defects_before` on 51 of the 90 parts and on 16 of the 20 that ran a
loop; 1 of those 16 (`open_shell`-only pathing) produced no refusal. Across all 90 rows the
inspector located **27,634** `small_face` findings, so the 1,121 refused findings are ~4% of the
`small_face` population in this sample — the rest sit on parts whose run never reached the
planner.

## 2. The three facts that decide whether any operation could act

For the one unclaimed code:

| | `small_face` |
|---|---|
| **stage** | **volume-stage, all 15.** `mesh_kind_before` is `overlapping_facets` on 11 parts and `edge_through_face` on 4. In the corpus aggregate both families are essentially 3D-stage: `overlapping_facets` 5,840 of 5,871 at 3D and 0 at 2D; `edge_through_face` 2,349 of 2,376 at 3D and 0 at 2D. **Zero** of the 15 are in a surface-stage family. |
| **solids** | **12 multi-solid, 3 single-solid** (by `counts_before.solids`). |
| **located coordinates** | **Yes, 2,242 of 2,242.** Every refused defect is named by a fingerprinted entity (`face:<16 hex>`); not one refusal was `untargetable`. A deterministic operation has something to aim at. |

The `small_face` findings themselves are CAD-stage findings on a part whose *mesher* failure is
volume-stage. That asymmetry is the central caveat of §3: the refusals are aimable, but the thing
that stops these parts meshing is not the sliver.

## 3. Ranked shortlist of candidate operations

### Rank 1 — no new operation: turn on `fix_small_faces` and measure it

- **Code claimed:** `small_face` (and `small_edge`, which it also answers).
- **Why it reads as unclaimed today:** `FixSmallFaces.targets_for` in
  `src/meshpipeline/cad/repair/operations.py` opens with
  `if not rcfg.CAD_REPAIR_FIX_SMALL_FACES: return ()`. `RepairPlanner.propose` treats an
  operation that answers the code but offers no targets as not claiming it, and emits
  `no_operation_claims_it`. The setting defaults to `"false"`
  (`src/meshpipeline/settings/cad_repair.py`), and Arm A is the flags-off arm. The operation is
  built, bounded, re-measures each aim against the localiser's own area-fraction criterion, caps
  removals, and declares `faces_removed` to the sanity cap.
- **Measured reach in this sample:** 15 parts (15 of the 20 that ran a loop).
- **Extrapolated reach on the 9,956 corpus:** stratum-weighted **~1,823 parts**; flat scaling
  (15/90) gives ~1,659. The weighting, per stratum (`sampled / hit / corpus size / projected`):
  `overlapping_facets/multi` 29/10/3,641 → 1,256; `overlapping_facets/single` 19/1/2,230 → 117;
  `edge_through_face/multi` 12/2/1,394 → 232; `edge_through_face/single` 9/2/982 → 218; every
  other stratum had 0 hits. **This number is soft in both directions**: the `timeout` (587) and
  `crash` (45) families carry `solids: unknown` for every file in the aggregate, so the 6 timeout
  rows in the sample project nothing at all, and the hit rates come from strata with 9-29
  samples.
- **Risk class:** **counted removals** — second-lowest band in the plan's ordering
  (text < counted removals < vertex movement < single-face rebuild < body ownership). It is
  below items 4 and 7 of the plan and above items 1-2.
- **Does the harvest support it?** Supports the *reach*, and the harness itself carries a prior
  measurement: `repair_corpus.repair_settings_of`'s docstring records that
  `CAD_REPAIR_FIX_SMALL_FACES` "took a corpus from 0 to 5 repaired files while the sealed
  settings — and so the settings digest — stayed byte-identical". **Contradicts the hope that
  reach equals repair:** all 15 parts are in volume-stage overlap/edge-through-face families, and
  plan items 5 and 6 say that family is mesher-side (tolerant meshing) rather than CAD surgery.
  Removing slivers will claim the 15 refusals; whether it makes any of the 15 *mesh* is exactly
  what the measurement run has to answer, and the rows give no basis to predict it.

### Rank 2 — nothing

There is no rank 2. No second defect code drew a single `no_operation_claims_it` refusal in this
sample, so there is no measured demand for any other new operation. The honest shortlist has one
entry and it is a flag, not code.

## 4. Not worth building

| Family | The figure that says so |
|---|---|
| **Overlapping facets** — surface-stage face surgery (`rebuild_face`) | 5,871 corpus files (59%), 5,840 volume-stage, **0 surface-stage**. 11 of the 15 refusal parts are in this family, so it is the largest single contributor to the refusal count, and it is still the wrong place to build: the plan marks `rebuild_face` NO-GO and routes the family to tolerant meshing (item 6) plus model judgement. Building a deterministic operation because 11 parts appeared here would be building against the one figure the harvest already closed. |
| **Edge through face** — body ownership | 2,376 files (24%), all volume-stage; 982 single-solid (outside `fragment_bodies`' reach) and 1,394 multi-solid where which body keeps the shared volume is a model judgement. 4 of the 15 refusal parts sit here. Highest risk class in the plan's ordering, and last in its build order. |
| **No-volume tiny shells** | 168 files, 162 single-solid, median 21.5 faces. The plan says measure with the existing sew/fill catalogue first, build nothing. This sample contains 3 `no_volume` rows and **0 refusals** among them — it adds no evidence either way. |
| **Open loop / periodic surface** | 227 and 390 surface-stage files; `close_wire_gaps` and `split_closed_faces` already exist. 6 rows in this sample (3 + 3), **0 refusals**. Nothing to build. |

## 5. What this data refuses to answer

- **Whether the other 70 parts would refuse too.** 70 of 90 rows carry no planner bookkeeping at
  all — empty `refusals`, `operations_attempted: None`, `complete_scan: None` — because the run
  kept nothing and promoted nothing. The rows do not record refusals that the engine may have
  emitted before giving up. Every parts-count in this document is therefore a count over a
  20-part loop-completing denominator, and the projection in §3 assumes the 70 behave like the
  20, which is unverified.
- **Whether the removal would make anything mesh.** `meshes_after` is recorded on only 20 of 90
  rows (`False` on all 20, `None` on the other 70). There is no post-repair mesh result for 70
  parts, so no before/after mesh delta can be computed from this sample for any candidate.
- **Whether the three identical rows are one part.** No content hash or stable id in the rows.
- **The touching-vs-interpenetrating split** of the 1,394 multi-solid `edge_through_face` files,
  which the plan flags as still open. These rows do not carry a body-pairs census.
- **Surface area or absolute size of the refused slivers.** `declared_area_mm2` is 0.0 on the
  rows inspected, and the refusal records carry a fingerprint and a code but no measurement, so
  "how small is small" cannot be read back from the rows.

## The single recommendation

**Build nothing. Run one measurement arm with `CAD_REPAIR_FIX_SMALL_FACES=true` and nothing else
changed**, on the same 90 parts, and report three numbers: refusals removed (expected: all 2,242),
`faces_removed` declared versus the cumulative-cap verdict, and `meshes_after` on the 15. If that
arm moves `meshes_after` on none of the 15 — which the plan's items 5 and 6 predict, since all 15
are volume-stage overlap or edge-through-face parts — then the refusal count was never the
bottleneck, and the owner's instinct to add operations should be spent on plan item 3 (mesher
handback) instead, where the mesher names the faces it actually disliked.
