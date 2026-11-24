# Lane B7 report: the overlap depth is how far the cut moves the surface

Branch `sdd/lane-b`. Native work ran on the `jig` context: one labelled container at a time (`owner=lane-b7`), since removed along with `/srv/work/lane-b7`. No model calls, no local Docker. Finding (2), the layer-3 asymmetry, is **recorded only** (see `lane-b6-report.md`) and not changed.

## Change (`src/meshpipeline/cad/repair_v4/body_kernel.py`)
`_overlap_depth` (`max(2V/A, thinnest AXIS-ALIGNED bbox extent of the overlap)`) is replaced by `_cut_depth(victim, keeper)`, which measures **how far the cut moves the victim's surface**:
- The overlap C = victim ∩ keeper is bounded by two kinds of face: pieces of the victim's surface lying inside the keeper (the cut removes them), and pieces of the keeper's surface lying inside the victim (the cut leaves the victim bounded by them). Each face of C is classified by distance to the two bodies' surfaces, using one probe point per face.
- The depth is the maximum distance from up to 400 sample points on the victim-side faces to the keeper-side faces.
- Result: a slab of thickness *t* reads *t*; a corner cube of side *s* reads *s* (where 2V/A reads *s*/3); and it reads the same in any orientation.
- **Fallback to the mean thickness 2V/A** (`method: "mean_thickness"`), never to 0, in three cases: a duplicate or contained body (no victim surface lies inside the keeper alone); a face that lies on neither surface; or the 20 s budget running out.
- The depth is now measured **per cut direction**, because the victim matters. `method` is recorded in the decline's `detail` next to `value` and `bound`.
- The bound (`min(0.1 mm, 0.001 × diagonal)`) and the 2% volume guard are unchanged.

## Tests (`tests/native/test_native_repair_v4_ops_bodies.py`, run on jig: 18 passed)
- **A thin INCLINED overlap under the bound is now cut, not declined.** Two 10 mm cubes overlap in a 0.02 mm slab, and the part is turned 30° about z and 20° about y.
  - The bound is 0.035 mm.
  - The new depth is 0.02 mm (`measured`), and `cut_by` applies: valid, 2 bodies, no overlap left.
  - **The old kernel declined it with a depth of 8.13 mm**, verified by running the same case against the old `body_kernel.py`.
- A thick (0.5 mm) inclined overlap is still declined, on its real depth of 0.5 mm.
- Slab 0.3 mm reads 0.3; corner cube 0.3 mm reads 0.3 (not 0.1).
- A duplicate body falls back to 2V/A (10/3), not 0.
- The existing 14 body-op tests (within-bound cut, beyond-bound decline, volume-change cap, …) are unchanged and pass.

## The four C4 files with the new depth (`lane-b6/*.b7.json`, the real op with its guards on)

| File | Bound | Old depth | New depth by cut direction (method) | Side of the bound |
|---|---|---|---|---|
| A 00613121 (duplicate bodies) | 0.1 | 50.8 | 37.16 / 37.16 (mean_thickness: duplicates have no movement to measure) | beyond, unchanged |
| B 00521884 | 0.084 | 4.6 | 0.60 / 0.60 (measured) | beyond, unchanged |
| C 00541957 | 0.1 | 18.9 | 1.60 (mean_thickness) / 2.00 (measured) | beyond, unchanged |
| D trj4 (3 pairs) | 0.1 | 5.5-10.0 | 0.50-0.92 across 6 directions (5 measured, 1 mean_thickness) | beyond, unchanged |

**None of the four changes side.** Every new depth is 6-370x the bound. The old estimate overstated by 7-19x, but the true movement is beyond the bound anyway. The snapshot's "beyond the bound" classification for these files stands, now on a defensible number. The method fell back to the mean thickness in 3 of 12 directions, recorded per row: A (duplicates, expected), C with B4 as the victim, and one D pair.

## Gates
- Native (jig): `pytest tests/native/test_native_repair_v4_ops_bodies.py` gives 18 passed, rc 0, on the final code. The 4 new tests fail against the old kernel (`_cut_depth` is absent, and the old guard declines the inclined case at 8.13 mm).
- Host: `pytest tests/unit/cad/repair_v4 -k "bod or conformal or overlap"` gives 8 passed.
- ruff and mypy on `body_kernel.py` are clean; ruff on the test file is clean.
