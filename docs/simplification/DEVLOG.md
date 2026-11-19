# CAD simplification - devlog

Branch `raymond/cad-simplification`. Design: `docs/superpowers/specs/2026-11-19-cad-simplification-design.md`.
A working log, not a document: chronological, dead ends included, and later entries correct earlier
ones. For the design, read the spec.

## Where it stands

| | |
|---|---|
| Phase | P1, ground-truth harness (one feature removed at a time, mesh and solve both, compare) |
| Environment | `dev-raymond` (two-run first deploy, 2026-11-19), bench VM `dev-raymond-bench` |
| Corpus | NIST MBE PMI test parts, AP203 geometry-only (public domain, "can be used without any restrictions"), 10 of 11 usable |
| Solver | CalculiX 2.21 (Ubuntu `calculix-ccx`), C3D10 from gmsh 4.15.2, linear static |

## Compute

Nothing heavy runs on the laptop. Fast gates (`make check-fast`) run there; meshing and solving run on
`dev-raymond-bench`:

- `n2-standard-16` (16 vCPU, 64 GB), `us-central1-a`, 200 GB pd-balanced, Ubuntu 24.04. About
  $0.78/h while running. c2d-standard-16 was the first choice and was stocked out in all four
  `us-central1` zones on 2026-11-19.
- Labels `app=hexera, deployment-id=dev-raymond, component=simplify-bench, managed-by=hand`, so the
  `destroy-env` sweep reports it.
- `devtools/simplify/bench_startup.sh` is its startup script: installs CalculiX, a Python 3.12 venv at
  `/opt/bench/venv` with the repo's pinned gmsh/OCP/meshio/trimesh, and an **idle watchdog** that
  powers it off after 45 min with no ssh session and no ccx/gmsh/bench-python process. A stopped VM
  costs only its disk.
- Code and parts reach it by `gcloud compute scp` into `~/bench/{code,corpus}`; results come back the
  same way. `devtools/simplify/run_corpus.sh` runs the harness over every part, skipping finished ones.

```bash
gcloud compute instances start dev-raymond-bench --zone us-central1-a --project hexera-dev
gcloud compute ssh dev-raymond-bench --zone us-central1-a --project hexera-dev
gcloud compute instances stop dev-raymond-bench --zone us-central1-a --project hexera-dev
```

`make seed-secrets SLUG=raymond` (handover step 0) was **not** run: it derives the project
`hexera-dev-<slug>` from the one-project-per-developer era. In `hexera-dev`, a slug's deploy reuses
the shared secret containers and mints its own `minio-secret-key-<slug>`
(`.github/workflows/deploy.yml`, target job).

## 2026-11-19 - harness, first part

`devtools/simplify/harness.py`. Per part: recognise holes (concave full cylinders, with coaxial cones)
and blends (tori and part-cylinders of radius < 3% of the diagonal); invent a load case (fixed at the
low end of the longest axis, 1000 N bending at the high end); remove each feature alone with
`BRepAlgoAPI_Defeaturing`; mesh with gmsh (10-node tets, size diag/30, 12 elements per 2π of
curvature, so small features cost cells); solve in CalculiX; compare compliance, tip deflection, max
displacement and max von Mises away from the ends and away from the removed feature's own zone.

**nist_ftc_09** (158 faces, diagonal 338 mm): 59 features, 7 skipped (touch an end), 52 solved, 203 s
wall on 16 cores. Original: 64,467 tets, 6.6 s mesh, 48 s solve.

| | |
|---|---|
| noise floor (same part, size x0.93) | compliance -0.02%, tip -0.02%, max disp -0.06%, far stress **+1.64%** |
| best single removal | `hole17` (r 1.98 mm): -8.2% cells, compliance -0.05%, far stress -0.7% |
| worst global error | `blend44` (concave, r 6.35): compliance -1.1% |
| a removal that COSTS cells | `hole13` (r 9.52): **+18.8% cells** - filling a large hole adds volume to mesh |

Signs are physical: filling a hole stiffens (compliance down), removing a concave fillet removes
material (compliance up).

What this says:

- Far-field stress below about 2% is inside the meshing noise on this load case. Global QoIs resolve
  to about 0.1%. Any accept/reject threshold must sit above the floor of the QoI it judges.
- Cost is not monotone in "simpler". A feature must be cost-attributed before it is proposed.
- **Solve wall time is not a cost metric here**: 16 concurrent jobs on one box moved it by ±40% on
  identical meshes. Elements and nodes are the cost; timing needs isolated runs.

**Defect found and fixed**: `BRepAlgoAPI_Defeaturing.IsDone()` is true when OCCT could not remove the
faces - it hands the input back and records a warning. 12 "removed" blends had dV ~ 1e-15 and an
identical mesh, which read as free, harmless simplifications. `remove()` now refuses a result with
the same face count and volume. Most of the 12 are 180° concave part-cylinders - slot ends, not
fillets - so the blend recogniser also needs to tell a full round from a fillet.

Open (superseded by the next entry where marked):

| | |
|---|---|
| full NIST corpus | running on the bench |
| slot ends vs fillets | recogniser treats every concave part-cylinder < 3% diag as a blend |
| stress probes | far stress is a weak QoI on a bending load (noise 1.6%); add a local QoI at named points |
| invented load case | one bending case per part; a second (torsion or axial) to see whether rankings hold |

## 2026-11-19 - the ground truth was noise; fixed; T1 and T2 measured against it

**Correction to the entry above.** Its noise floor (0.02% on compliance) was one sample, and the
load case behind it (a force on faces wholly inside a 1% end band) found no such faces on
nist_ctc_01/03, so the corpus run gave those parts no load case. The fix tried next (every NODE in a
3% end band fixed or loaded, force-controlled) raised the floor to 0.29%, and displacement control
on the same node bands did not lower it: 0.31%.

`converge.py` (five meshes of the same part per size, sizes x0.96-x1.04) said why:

| size (diag / N) | elements | compliance spread, node-band BC | spread, trimmed-face BC |
|---|---|---|---|
| 20 | 58-60k | 0.18% | 0.035% |
| 30 | 63-64k | **1.07%** | **0.042%** |
| 45 | 70k | 0.47% | 0.013% |
| 60 | 76-78k | 0.30% | 0.009% |

With the node band, coarser meshes came out SOFTER - which a displacement-based FE model cannot do on
its own: the band's edge cut through elements, so how much material was clamped depended on the
mesh. `prepare()` now trims 3% off each end with two planes and puts the clamp and the prescribed
displacement on the two planar cut faces, the same continuum problem for every mesh. Refinement now
converges the right way (5.0735 -> 5.0766 -> 5.0787 -> 5.0802 e-5, finer is softer).

Also: elements barely grow with the background size (58k -> 76k for a 3x finer h_max), because the
curvature rule (12 per 2 pi) and the features set the size almost everywhere. Features ARE the cost.

**nist_ftc_09 on the trimmed BC**, 38 removable features, T3 = one global solve each:

| | |
|---|---|
| noise floor (original, size x0.93) | compliance **+0.003%**, max disp -0.001%, far stress +0.35% |
| largest effects | blend40 -1.25%, hole20 -0.69%, hole4 -0.62%, hole25 -0.60%; median ~0.06% |
| sign physics | filling a hole stiffens every time; removing a convex blend softens (blend35 +0.43%) |

**T2 (local.py, k = 3, local mesh h/2)** against T3, compliance change: Spearman **0.970**, Pearson
0.886, sign agreement **37/38**, median |T2/T3| **0.62** - consistently low. Local pieces: about 10k
elements (orig + simp) against 64k for the global part. **T1 (holes, one solve)** against |T3|:
Spearman **0.984**.

Read with care:

- T2 under-reads by ~40%. Two misses are larger than that: hole20 (T3 -0.69%, T2 -0.11%) and hole25
  (-0.60% vs -0.06%), both with a small cut mismatch (0.008), so the cut check did not flag them.
  T1 ranks both near the top (eta 4.6e-3, 4.2e-3). Screening with T1 before trusting T2 catches them;
  T2 alone would not. Whether the under-read is the box or the first-order energy argument is what
  the k = 6 run is for.
- "max von Mises away from the feature" is not a usable QoI: about 20 holes all show the same
  +5.45% because the peak sits at a sharp corner that moves with the mesh, not with the feature.
  Replace with stress at fixed probe points, or a region mean.
- One part, one invented load case. Nothing here generalises yet.

**Earlier runs of this entry, superseded** (kept so the path is honest): with node-band BCs T2 vs
T3 gave Spearman 0.86 (force control) and 0.49 (displacement control), T1 0.72 and 0.37, and a median
|T2/T3| of 0.19 / 0.10 - all of it the 0.3-1% mesh noise in T3, not the estimators.

## 2026-11-19 - product slice 1: inspection only, the way CAD repair started

Integrated as CAD repair is (spec, "How it fits the repository"):

- `cad/simplify/` - `contracts.py` (`SimplifyResult` etc., source identity reused from CRS's
  `RepairInput`), `recognise.py` (holes, blends, and a new `slot_end` class that is reported but
  never removable - the 12 no-op "removals" above), `inspect.py:inspect_features`.
- `pipeline/simplify_inspect.py:node_simplify_inspect`, wired `node_repair_attempt ->
  node_simplify_inspect -> node_geometry_admission`, one unconditional edge, no geometry returned,
  inconclusive on timeout / exception / oversize, a `simplify_inspect` capture row.
- State v16 (`simplify_status`, `simplify_report`), `data_contract` entries, settings
  `CAD_SIMPLIFY_INSPECT_TIMEOUT_S` / `_MAX_BYTES` in their own inventory group, rulebook 73 (TIME-6
  now names the simplification inspection clock).
- Cost per feature is an ESTIMATE: surface triangles at the PLAN-fea-4 hot-spot size. To be
  calibrated against the harness's measured cells saved.

No kill switch yet: nothing in this slice can change geometry, and CRS's `CAD_REPAIR_ENABLED`
arrived with the code it guards.
