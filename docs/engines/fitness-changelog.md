# Engine fitness table: changelog

`src/meshpipeline/engines/fitness_table.json` is the lab evidence the engine recommendation ranks
from (`engines/fitness.py`). It changes only through a reviewed PR, and every committed version has
an entry here with its `rows_sha256`. A test fails when the table's rows do not match their digest,
or when the version has no entry here.

How to rebuild it: `devtools/fitness/build_engine_fitness.py` (the usage is in its docstring). The
build needs `--split`, the frozen held-out split, and keeps only dev-side rows.

## 2026-10-20-0ddaa08f-85664054

- rows_sha256: `856640543b724c98936537f2e107e1439be3716a38edf3cc9296f05f9cc38080`
- rules_version: 2
- split: v1+grabcad2-20261018 (manifest `e469150c...`)

**Why:** the table held runs on files from the frozen held-out set, so the engine pick had learned
from files we promised never to tune on. It also held runs where no mesher ever ran, and runs whose
outcome had been rewritten by hand.

**What changed:**

- Same sources as 2026-10-17-0ddaa08f: the lab rows that table shipped with, plus the e2e sweeps
  `baseline-main` and `smoke1` (code `aa071f69`).
- Every row is matched to the frozen split by the sha256 of its input file. Only dev-side rows are
  kept. 318 held-out rows were dropped (lab and e2e together; counted, never named). 139 rows whose
  file is not in the split at all were dropped too.
- Runs where no mesher ran are gone (`no_mesh_run:*`, `geometry_refused:*`): 52.
- The 15 hand-relabelled VMTK rows (`recounted:`) are gone. The builder's `--recount` option is
  removed: a defect found later is fixed by re-running the case.
- e2e rows come from clean (gate) ORIGINAL files only: 360 format copies and 131 files outside the
  clean tier do not count. A format copy is the same geometry as its original.
- 1,037 rows on 486 shapes became 673 rows on 394 shapes: cfMesh 297, snappyHexMesh 269, Gmsh 71,
  VMTK 36. Multi-region has no dev-side rows left (its 41 runs were on files outside the split).

Rows per shape class, before and after (passed / runs), are in the PR that made this version.
