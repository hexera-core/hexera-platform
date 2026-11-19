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
