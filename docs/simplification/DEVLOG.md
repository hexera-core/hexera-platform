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

## 2026-11-19 - the clamp bug in T2, and the first cross-part run

**The two T2 misses on nist_ftc_09 were a bug, not the method.** hole20 and hole25 sit 23 mm from
the clamped (trimmed) face; their twins hole0/hole13 at the loaded end change compliance 17x less
(-0.04% vs -0.69%), which is the bending moment, so T3 was right. Their local region reached past the
clamp face, and the piece treated that face as FREE. `local.py` now drives any trimmed face inside a
region with the global field (zero at the clamp, the prescribed displacement at the load). Also:
`--k` did not move the region for small features (the `zone + 4h` floor dominates); `--grow` scales
the whole padding. k = 6 and k = 3 gave identical regions and identical answers, which is that bug.

**Corpus.** 30 downloaded parts and 7 synthetic families (58 STEP files with known features),
licences in `hexera-work/corpus/manifest.json` (public domain, CC-BY/CC-BY-SA, LGPL, Apache, or
generated). Excluded as unclear for company use: ABC/DeepCAD, unlicensed repos, GPL-origin test
files. Copied to the bench at `~/bench/corpus2/corpus`.

**First FEA set** (`run_set.sh fea_set1.txt`, scored with `score.py`; a row counts as a measurement
when |T3| clears 3x its own part's noise floor; budget 0.1% on compliance):

| part | noise | removals | measured | T2 rank | T2 linear | sign | T2/T3 | T1 rank | max abs T3 |
|---|---|---|---|---|---|---|---|---|---|
| NIST D2MI 904 (B-spline surfaces) | 0.007% | 7 | 4 | 1.00 | 1.00 | 1.00 | 1.02 | 0.20 | 1.48% |
| NIST D2MI 906 (same part, analytic) | 0.014% | 9 | 3 | n/a | 1.00 | 1.00 | 0.96 | n/a | 1.48% |
| holeplate_graded (r 1-10) | 0.006% | 10 | 10 | 0.98 | 1.00 | 1.00 | 0.94 | 0.88 | 1.13% |
| lbracket_b (pre-fix local) | 0.020% | 4 | 4 | 0.60 | 1.00 | 1.00 | 0.84 | 1.00 | 4.50% |
| lbracket_c | 0.029% | 2 | 2 | n/a | n/a | 1.00 | 0.49 | n/a | 0.22% |
| tee_dn50_4b | 0.012% | 5 | 0 | | | | | | 0.013% |
| KP08 housing | **0.268%** | 4 | 0 | | | | | | 0.27% |
| SHF08 | **1.224%** | 2 | 0 | | | | | | 0.05% |
| KFL08 housing | original did not mesh (gmsh high-order optimiser abort) | | | | | | | | |
| KiCad heat sink 125x35x50 | original did not mesh (same abort; ~1.1M tets a variant) | | | | | | | | |

Pooled, 43 removals, 23 measured: **T2 vs T3 rank 0.99, linear 0.99, sign 23/23, median ratio
0.94**; **T1 rank 0.98** (22 holes). At the 0.1% budget T2 alone accepts 24 removals correctly, makes
**one false accept** (holeplate hole6: T3 -0.112%, T2 -0.086%) and no false reject. The truly
acceptable removals each save a median 3.1% of cells, at most 17.8%.

Read with care: 23 measured rows is small; KP08 and SHF08 have noise floors (0.27%, 1.2%) that
swallow every feature, so the invented load case suits them badly (probably small, curved trimmed
ends); the 904/906 pair is the same part stored two ways and agrees with itself (1.480% vs 1.481%),
which is the recogniser holding across surface representations.

Fixed for the reruns: `harness._job` retries a mesh once without high-order optimisation when gmsh
aborts with "Failed to reach critical value ... ScaledJac".

## 2026-11-19 - CFD bench (summary of CFD-NOTES.md)

Laminar simpleFoam in the product's mesh image, BCs on exact faces, the same one-at-a-time method,
99 converged solves. The CFD result is different in kind from the FEA one:

- single small features saved ~no cells and rarely cleared the noise floor (drag 0.18-0.26%, dp
  0.29-1.43%); on nist_ftc_09 71% of the cells are at the finest level because of the plate's
  thickness, so removals cannot save anything;
- WHOLE PACKAGES did: the manifold's thread grooves were 48% of its mesh (199,065 -> 103,344 cells) for
  dp -0.90%; on the heat sink the single-feature changes summed to ~0.7% of the package's drag change
  (+1.26%), so per-feature verdicts did not certify the package;
- the flow-relevant features are few and specific (the tee's bore-junction round: split +0.72 pp on
  46k and 154k meshes) and most candidates of an internal part are DRY (16 of 19 on the tee) and need
  no solve at all;
- k-omega SST did not converge; no SST result is claimed.

Consequences for the design: T0 for CFD is a dry-feature filter plus a cell-cost gate before any
solve; verdicts must be on PACKAGES, which is what `package.py` now measures for FEA.

## 2026-11-19 - FEA packages: the savings are in packages, and the sum of errors is a safe ledger

`package.py`, on the eight parts whose original solved: `accepted` = every feature whose own removal
moved compliance by at most 0.1%, `all` = every removed-and-solved feature; each removed in ONE
defeaturing call (OCCT took every union), solved once, against the harness's reference solve.

| part | package | members | true dC | sum of members | ratio | cells saved | sum of members' cells |
|---|---|---|---|---|---|---|---|
| nist_ftc_09 | accepted | 24 | -0.284% | -0.716% | 0.40 | **54.3%** | 92.1% |
| nist_ftc_09 | all | 38 | -4.596% | -5.224% | 0.88 | 80.2% | 117.8% |
| D2MI 904 | accepted | 6 | -0.034% | -0.134% | 0.25 | 3.2% | 12.2% |
| D2MI 904 | all | 7 | -1.519% | -1.614% | 0.94 | 5.2% | 15.5% |
| D2MI 906 | accepted | 8 | +0.033% | -0.066% | -0.51 | **22.3%** | 30.8% |
| D2MI 906 | all | 9 | -1.452% | -1.547% | 0.94 | 25.6% | 34.8% |
| holeplate_graded | all | 10 | -5.398% | -5.448% | 0.99 | 92.6% | 125.8% |
| lbracket_c | all | 2 | -0.449% | -0.427% | 1.05 | 11.4% | 12.0% |
| tee_dn50_4b | accepted = all | 5 | -0.011% | -0.049% | 0.23 | **23.4%** | 25.9% |
| KP08 housing | accepted | 2 | +0.081% | +0.063% | 1.28 | **26.3%** | 27.3% |
| KP08 housing | all | 4 | +0.367% | +0.506% | 0.72 | 49.8% | 54.6% |
| SHF08 | accepted = all | 2 | +0.049% | +0.066% | 0.73 | 14.3% | 14.8% |

- **The savings are in packages.** One harmless feature saves a median 3%; the package of all of a
  part's harmless features saves 14-54% here, at a true compliance change of at most 0.08% except
  nist_ftc_09 (-0.28% for 54% fewer cells - over a 0.1% budget, inside a 0.5% one).
- **For FEA the SUM of members' errors is a conservative ledger.** Where the package effect is well
  above noise (`all`), the true change is 0.88-0.99x the sum (0.72-1.05x everywhere). Where it is
  small (`accepted`), the sum overstates it 2.5-4x - the members' individual T3 values each carry a
  little remeshing bias, and 24 small biases add while the package pays one. The only package above
  its sum is KP08 accepted (1.28x, 0.081% vs 0.063%), inside that part's 0.27% noise floor. So a
  ledger that sums member errors would have kept every package inside the budget it claimed.
- **Cells saved are sub-additive** (the sum overstates by 4-60%): features share the refinement zones
  around them. A cost estimate must be made for the package, not summed.
- Contrast CFD (entry above): there the members did NOT add up to the heat-sink package. The design
  therefore keeps a final package-level check, and for CFD it is mandatory.

## 2026-11-19 - full first set with the clamp fix; an acceptance margin

Reruns: `KFL08` now solves in the straight-sided mode (one feature measurable, noise 0.23%); the
KiCad heat sink and nist_ctc_04 ran past the 90-minute per-stage limit (each variant ~1.1M and ~2.1M
tets, ~1 h per CalculiX solve on one core) and are re-queued with `PART_TIMEOUT=14400`.

`score.py` over ten parts, 82 removals, 58 above 3x their part's noise floor:

| | |
|---|---|
| T2 vs T3, compliance | rank **0.99**, linear **1.00**, sign **58/58**, median ratio **0.88** |
| nist_ftc_09 alone, after the clamp fix | rank 0.99 (was 0.96), ratio 0.75 (was 0.61); hole20/hole25 no longer missed |
| T1 vs abs T3 (holes) | rank **0.96**, n = 48 |
| acceptable removals (abs T3 <= 0.1%), each alone | median 3.8% of cells saved, max 26.1% |

The remaining under-read shows at the budget's edge: four false accepts at 0.1%, every one just over
it (T3 0.111-0.133%, read as 0.086-0.097%). An acceptance MARGIN - accept only when abs T2 <= m x
budget:

| m | accepted | false accepts | false rejects (of 49 truly acceptable) |
|---|---|---|---|
| 1.00 | 50 | 4 | 3 |
| 0.90 | 48 | 2 | 3 |
| **0.80** | **45** | **0** | **4** |
| 0.70 | 44 | 0 | 5 |
| 0.60 | 40 | 0 | 9 |
| 0.50 | 38 | 0 | 11 |

m = 0.8 is the operating point on this data: no false accept, 45 of 49 harmless removals kept.
**It was chosen on the same 82 removals it is scored on**, so it is a hypothesis until it holds on
parts it has not seen (the heavy pair queued now is the first such test).

## 2026-11-19 - speed: solver settings, a one-solve fast path, and two corrections to the bench

**Solver settings** (CalculiX 2.21, Ubuntu build, SPOOLES; nist_ftc_09 original, 64k tets):

| setting | wall | peak RSS | reaction vs reference |
|---|---|---|---|
| SPOOLES, 1 thread (what the bench used) | 32.2 s | 1.5 GB | reference |
| **SPOOLES, 16 threads** | **17.4 s** | 2.6 GB | identical |
| ITERATIVE CHOLESKY, 1 / 16 threads | 95.5 / 93.8 s | 0.4 / 1.0 GB | **+1.9% / +1.7% wrong** (not converged) |
| ITERATIVE SCALING, 16 threads | 271.5 s | 1.0 GB | +0.001% |

No PaStiX in this build. A faster solver (AMG-preconditioned CG on the same mesh, proven against
CalculiX to 1e-6) is being built separately.

**Correction 1: the bench never applied PLAN-fea-3** (at least two elements through every wall). The
wall thickness (rays cast inward, 10th percentile, `harness.wall_thickness`) against the size used:

| part | wall | h used (diag/30) | h the rule asks (wall/2) |
|---|---|---|---|
| nist_ftc_09 | 3.04 | 11.27 | 1.52 |
| lbracket_b | 4.40 | 5.21 | 2.20 |
| holeplate_graded | 6.00 | 7.18 | 3.00 |
| tee_dn50_4b | 6.00 | 13.46 | 3.00 |
| D2MI 906 | 1.17 | 4.20 | 0.59 |
| KP08 housing | 1.38 | 2.11 | 0.69 |
| SHF08 | 1.89 | 1.67 | 0.95 |

Every part had one or two elements through its walls. T2 and T3 were computed at the same resolution,
so their AGREEMENT stands; the SIZE of the errors and of the cells saved does not, because removing a
feature let the mesher coarsen where it had been, and that stiffening was read as the feature's
effect. `harness.py` now caps h at wall/2 (`--no-wall-rule` restores the old sizing). The ground
truth has to be re-run under it.

**Correction 2: a mesh can miss part of its volume silently.** A region cut out of nist_ftc_09's
defeatured package was a shape OCCT calls invalid (healing made it valid; gmsh then failed with
"1D mesh not forming a closed loop"); in the run itself gmsh meshed it without raising, at a twelfth
of the tets of the same region of the original, and the simplified piece read 29% stiff. `mesh_brep`
now refuses any mesh whose tets do not add up to the CAD volume within 0.5%. Region pieces are now
made by cutting the ORIGINAL and defeaturing inside the piece.

**The fast path** (`local.py --driver-brep`): every feature's local check driven by ONE global solve
of the part with the whole package removed, instead of one global solve per feature. nist_ftc_09, 38
features, package `all` (a 4.6% compliance change - the hardest case for a shared driver):

| | global solves | wall | T2 rank | sign | false accepts at 0.1%, margin 1.0 / 0.8 / 0.5 |
|---|---|---|---|---|---|
| per-feature drivers | 38 | ~36 CPU-min | 0.99 | 1.00 | 3 / 0 / 0 |
| one driver, size 30 | 1 (4.7 s) | **61 s** | 0.88 | 0.94 | 4 / 1 / 0 |
| one coarse driver, size 15 | 1 (3.1 s) | **51 s** | 0.96 | 0.94 | 4 / 1 / 0 |

About 40x less compute for a stricter margin: at m = 0.5 the shared driver keeps 17-18 of 24 harmless
removals with no false accept (per-feature drivers keep 21 at m = 0.8). Two stages follow: the
one-solve screen settles the clear cases, and only features reading between about 0.5x and 1.2x the
budget get a driver of their own.

`package_local.py` (merged regions from one coarse solve) was accurate where it worked (hole plate:
-5.31% vs a true -5.40%) but is not a speed-up: dense features merge into one region the size of the
part, meshed finer than the global solve. Per-feature regions from one driver are the fast path.

## 2026-11-19 - fast_solve: CalculiX's answer on the same mesh, 3-16x faster, and past its memory wall

`devtools/simplify/fast_solve.py` solves the bench's problem (C3D10, isotropic, prescribed
displacements only) with CalculiX's own discretisation: its shape functions, its 4-point Gauss rule,
its Gauss-to-node extrapolation (with its 5-decimal weights) and plain nodal averaging, RF as the
element internal force, ELSE as the Gauss sum of sigma:eps/2. Dirichlet conditions by elimination.
Assembly in numba (parallel, element colouring, Morton node order, 3x3 blocks). Two solvers, `auto`
picking by size (direct up to 1M dofs):

- `pardiso`: MKL PARDISO, SPD, all cores, through `pardiso_64` (ctypes). The 32-bit interface that
  pypardiso wraps fails with error -2 once the factor passes 2^31 entries.
- `pmg`: PETSc CG to ||r|| <= 1e-10 ||b||, one MPI rank per physical core. The preconditioner is a
  two-level p-multigrid: Chebyshev(1) with point-block Jacobi on P2, then a Galerkin projection onto
  the corner-node P1 space and one GAMG V-cycle there (aggressive first coarsening). The matrix is
  assembled straight into /dev/shm and each rank maps its own rows.

API: `solve_meshed(m, [("NFIX", 1, 3, 0.0), ("NLOAD", d, d, delta)])` on a `harness.Meshed`, with no
deck (per-node values go in as `(tags, k, k, values)`, as local.py's cut faces need). Also
`solve(xyz, conn_rows, rows, comps, vals)` and `solve_deck("job.inp")`. All three return u, nodal s,
per-node reaction f and energy. Checked: `solve_meshed` and `solve_deck` give bit-identical results.
`bench_solvers.py` runs each solver in a fresh process and compares it with ccx.

Measured on dev-raymond-cfd (n2-standard-16: 8 cores, 16 threads, 62 GB), CalculiX 2.21 SPOOLES at 16
threads. Wall time is the whole process, interpreter start (about 0.6 s) included. Memory is the peak
summed PSS of the process tree. fast_solve's time is split into assemble / setup / solve:

| deck | tets | CalculiX | fast_solve, in memory | split (s) | iterations |
|---|---|---|---|---|---|
| nist_ftc_09, size 30 | 62,808 | 17.7 s, 2.6 GB | pardiso **5.6 s**, 2.8 GB (from the deck: 5.9 s) | 0.6 / 3.8 / 0.4 | direct |
| nist_ftc_09, size 290 | 460,581 | 312 s, 29.6 GB | pmg **20.1 s**, 8.2 GB (from the deck: 22.9 s) | 2.9 / 6.2 / 9.3 | 41 |
| | | | pardiso 50.2 s, 22.7 GB | 2.6 / 41.8 / 2.9 | direct |
| nist_ctc_04, size 30 | 2,090,620 | **killed by the OOM killer at 64.5 GB, after 20 min** | pmg **96 s**, 29.7 GB (from the deck: 108 s) | 12.0 / 22.2 / 55.1 | 59 |

How closely the two agree, as relative differences. The .dat prints 7 digits and the .frd prints 6, so
the run adds `*NODE PRINT, NSET=NALL / U` to get u from the .dat:

| deck | max \|u\| | reaction = compliance | strain energy | max nodal von Mises |
|---|---|---|---|---|
| 62,808 tets | 3.1e-9 | 6.4e-8 | 3.1e-8 | 6.3e-7 |
| 460,581 tets | 3.6e-9 | 3.1e-8 | 6.3e-8 | 1.6e-7 |
| five local pieces, 1.5k-22k tets | <= 1.4e-7 | per-node cut RF <= 4.2e-7 of the largest | <= 2.5e-7 | <= 1.3e-6 |

The von Mises figures needed CalculiX's own extrapolation weights, 1.92705 and -0.30902 (the exact
values are 1.9270510 and -0.3090170). With the exact weights the max differed by 7e-6 to 9e-6.
With CalculiX's, the whole field agrees to within 2.3e-6 once rounded to the .frd's 6 digits. CalculiX
cannot run the 2.1M deck on this VM, so there it is checked against itself: rtol 1e-10 against 1e-12
moves max |u| by 3e-10, the u field by 9e-9 and max von Mises by 5e-9.

Local pieces, the same five decks (1.5k / 4.8k / 7.2k / 9.4k / 21.7k tets). CalculiX takes 0.4 / 1.0 /
1.7 / 2.0 / 5.6 s. fast_solve in a fresh process takes 1.1 / 1.4 / 1.6 / 1.7 / 2.8 s. Inside an
already-running Python process it takes 0.5 / 0.8 / 1.0 / 1.1 / 2.0 s. The CalculiX times leave out
writing the deck and parsing the .frd in Python. So the crossover is around 5k tets: below it,
CalculiX's start-up is cheaper than loading numba's cache.

Notes from the measurements:

- **CalculiX at 16 threads gave a wrong answer in 2 of 11 runs of the same 64k deck.** In one, the
  reaction was 11,622 N instead of 5,503.68 and the energy 70,509 instead of 768.86, with lateral
  reactions of 25 and 319 N. In the other, the reaction was right but the energy was 28,517. The
  other nine runs, and the 1-thread run, were right. local.py's driver solve uses 16 threads, so a
  single 16-thread ccx run per mesh is not a reliable reference.
- **PARDISO cannot do the 2.1M deck either.** Its ordering predicts a factor of 24e9 entries
  (192 GB). SPOOLES's peak at 460k tets (29.6 GB) points the same way. A direct solver stops near
  half a million tets on a 64 GB machine, which is why the iterative path exists.
- **Alternatives that lost:**
  - pyamg smoothed aggregation (BSR, rigid-body modes, CG): 300 iterations on the 64k deck without
    converging (3.6e-8), in 214 s.
  - CHOLMOD (scikit-sparse 0.5.1): its factorisation took 3x as long as PARDISO's.
  - Plain GAMG on P2: 44 iterations at 460k tets, but 96 s of setup.
  - BoomerAMG (nodal): 978 iterations at 64k tets.
- **The 64k nist_ftc_09 mesh has slivers.** Its worst corner tet has a volume quality of 1e-5, against
  0.08 for the 460k mesh of the same part. On it, every iterative method needed 300-635 iterations,
  even with an exact coarse solve (211). That is the second reason small systems are factorised.
- **The P1 coarse operator's own GAMG hierarchy densifies.** At 2.1M tets it spent 45 s in Galerkin
  products until the first level was coarsened aggressively (11 s). The fine smoother at Chebyshev
  degree 1 does as well per iteration as degree 3.
- **numba compiles on the first call after an edit** (about 20 s); every later process loads the
  cache.

Dependencies (bench venv): numpy 2.5.5, numba 0.68.1, mkl 2026.2.1, psutil. For `pmg`, also petsc
3.24.6, petsc4py 3.24.6 and mpi4py 4.1.1 from PyPI, built against Ubuntu's Open MPI (apt
`libopenmpi-dev openmpi-bin`). Build the petsc wheel with `setuptools<75` as a build constraint, and
petsc4py without build isolation, with `CFLAGS=-I$PETSC_DIR/include`. Expect about 15 minutes.

## 2026-11-19 - fast_solve adopted by the bench; the 16-thread CalculiX fault reproduced

**Reproduced independently** on a different deck (lbracket_c original, 78k tets, dev-raymond-bench):
ccx 2.21, the same deck, once at 1 thread and ten times at 16. The 1-thread energy was 33.74006;
one 16-thread run read 33.74089 (+2.5e-5, the size of the bench's best noise floor), the others
33.74006. So a 16-thread ccx run is not a measurement. Every remaining ccx call on the bench is one
thread (every caller of `harness.run_ccx` passes 1), and the product verifier uses one
thread too.

**Adopted.** `harness.mesh_and_solve` solves with `fast_solve.solve_meshed` (`--solver ccx` or
`BENCH_SOLVER=ccx` restores CalculiX; a missing numba/mkl falls back to it, and result.json records
which ran). gmsh always meshes on one thread, so a mesh does not depend on how many jobs ran beside
it. The original is now solved alone on every core before the variants start; the variants get
cores / jobs threads each. `local._solve_piece` uses fast_solve above 5,000 tets and ccx (1 thread)
below, where ccx's start-up is cheaper than loading numba's cache.

Checked against the ccx (1 thread) ground truth of the same four parts (results3), harness rerun
with fast_solve: every mesh came out identical (same element count, gmsh on one thread both times),
and every row agreed:

| part | tets | original compliance, rel. diff | worst \|dC\| difference over its rows | harness wall time |
|---|---|---|---|---|
| lbracket_c | 78,370 | 4.3e-9 | 1.2e-9 | 259 s -> 66 s |
| lbracket_b | 37,006 | 2.4e-7 | 5.7e-7 | 63 s -> 33 s |
| SHF08 | 35,650 | 7.2e-8 | 1.7e-7 | 101 s -> 25 s |
| KP08 housing | 65,658 | 1.2e-7 | 4.6e-7 | 158 s -> 52 s |

The fast runs shared the machine with a PETSc build, so their times are upper bounds. The
original's solve alone went from 110 s to 19 s on lbracket_c.

### The small parts at the wall-rule size (fast3: lbracket_b, lbracket_c, SHF08, KP08 housing)

Rerun end to end with fast_solve: harness, per-feature local check (k 3, refine 2), packages, and
the one-solve driver (`--driver-brep geom/package_all.brep`).

- **With PLAN-fea-3 in force, removing these features saves almost nothing.** Cells saved by the
  whole package (`all`): lbracket_c 2.6%, lbracket_b 0.3%, SHF08 **-2.1%** (the simplified part meshed
  into MORE cells), KP08 12.1%. Before the wall rule the same packages read 11-92%. The wall rule, not
  the features, now sets the element size on these parts; a feature only pays for itself when it
  forces elements smaller than wall/2. This is the attribution step's job (estimate (a), the size the
  feature forces against the size the part needs anyway) and it should rank these features as
  costing nothing, so they are never proposed.
- **KP08's package broke the sum bound once**: the accepted pair changed compliance by +0.138%
  against a member sum of +0.119% (ratio 1.16), the whole package 0.99. Every pre-wall-rule package
  had ratio <= 1. So the sum of members is not a guaranteed bound, and the package-level check before
  promotion (loop step 6) is load-bearing, not a formality.
- **Two of the four parts cannot be judged at a 0.1% budget**: their noise floor (the original
  meshed at h and at 0.93 h) is 0.275% (SHF08) and 0.063% (KP08; 3x is 0.19%). A removal smaller than
  that has no ground truth at this resolution. `score.py` now counts such rows apart ("no truth")
  instead of as false accepts. On the remaining rows: T2 vs T3 spearman 0.93, pearson 1.00, sign
  8/8, median ratio 0.96 (per-feature drivers) and 0.94 (one driver). No false accepts, but also no
  truly acceptable row: every resolvable removal here costs more than 0.1%. The decision evidence has
  to come from the larger parts.
- Times with fast_solve, per part: per-feature local 35-71 s, packages 10-20 s, one-driver local
  43-76 s (driver solve 4-10 s). On parts this small the one-driver path is no faster than reusing the
  harness's per-feature fields, which already exist; its saving is the per-feature global solves the
  product would otherwise have to run.
