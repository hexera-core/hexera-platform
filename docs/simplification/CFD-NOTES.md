# CAD simplification - CFD notes (phase P4)

Branch `raymond/cad-simplification`. Design: `docs/superpowers/specs/2026-11-19-cad-simplification-design.md`
(phase P4). Written in the devlog's style for the main session to fold into `DEVLOG.md`: what was run,
what it measured, what failed. Every number below was read from a `result.json` on `dev-raymond-cfd`;
the commands that produced them are listed with each part.

## Where it stands

| | |
|---|---|
| Phase | P4 ground truth: one feature removed at a time, mesh and solve both, compare against a measured noise floor |
| Harness | `devtools/simplify/cfd_harness.py` (imports `harness.py` for read/recognise/remove; edits none of the FEA files) |
| Flow cases | EXTERNAL (part in a box tunnel) and INTERNAL (the through-passage of the part) both run end to end |
| Physics | steady incompressible `simpleFoam` (SIMPLEC), laminar, 4 parts (2 external, 2 internal), 99 of 99 counted solves converged (plus 3 pilots); k-omega SST started and stopped unconverged |
| Mesher | `snappyHexMesh`, castellated + snap, no prism layers, curvature-driven refinement (`resolveFeatureAngle 20`) plus explicit feature edges |
| Toolchain | the repo's mesh image, OpenFOAM v2412, `us-central1-docker.pkg.dev/hexera-dev/mesh/mesh@sha256:02756d09ddc2...` (newest on 2026-11-19), pulled by the VM's service account without trouble |
| VM | `dev-raymond-cfd`, n2-standard-16, us-central1-a, 200 GB pd-balanced, Ubuntu 24.04 |

## Compute

- `devtools/simplify/cfd_startup.sh` is the startup script: docker.io, the mesh image pinned by digest
  and tagged `hexera-mesh:cfd`, a uv venv at `/opt/bench/venv` with `cadquery-ocp==7.9.3.1.1` (the
  bench's OCP pin) for the geometry side, and `bench_startup.sh`'s 45-minute idle watchdog, widened to
  count a running container or an OpenFOAM process as busy. The image login uses the metadata-server
  token (`docker login -u oauth2accesstoken`), so no gcloud on the VM is needed.
- Labels `app=hexera, deployment-id=dev-raymond, component=simplify-cfd, managed-by=hand`.
- Geometry (OCP: recognise, defeature, tessellate, build the internal fluid) runs on the host venv;
  every OpenFOAM step runs in a container, one per case: `docker run --name cfd-<run>-<case>
  --cpuset-cpus=<core>`, killed by name on timeout (`timeout docker run` would not stop it). Each stage
  runs under `/usr/bin/time -v`, which the image lacks; the host's binary is bind-mounted read-only and
  works in the jammy container.
- Code and parts reach it by `gcloud compute scp` into `~/bench/{code,corpus}`.
- Results stay on the VM's disk (`~/bench/results/<run>/`, 6.7 GB with the case directories); every
  `result.json` quoted here is also copied to `../corpus/results/cfd/<run>.json` (outside the repo,
  beside the FEA results). `mf3.json` is the INVALID first manifold run kept as the record of the
  boundary-face defect; `mf3i.json` is the valid one. The VM is left STOPPED.

```bash
gcloud compute instances start dev-raymond-cfd --zone us-central1-a --project hexera-dev
gcloud compute ssh dev-raymond-cfd --zone us-central1-a --project hexera-dev
cd ~/bench && /opt/bench/venv/bin/python -I code/cfd_harness.py corpus/ftc09.stp --out results/ftc09 \
    --re 500 --aoa 8 --levels 2,4 --jobs 14 --core-offset 3
/opt/bench/venv/bin/python -I code/cfd_harness.py --report results/ftc09/result.json   # the tables below
gcloud compute instances stop dev-raymond-cfd --zone us-central1-a --project hexera-dev
```

## The harness

Per part:

1. **Recognise**: `harness.recognise` (holes, torus and part-cylinder blends) plus a CFD-side
   addition, **freeform blends**: connected B-spline faces whose smallest principal radius of curvature
   is under 3% of the diagonal. The synthetic tees' junction fillets are B-splines, invisible to
   `harness.recognise`, and the bore-side one is the only flow-relevant feature on the part.
2. **Remove** each feature alone with `harness.remove` (so the "unchanged shape" refusal applies). A
   whole simplified part can be added as one more variant with `--extra name=file.step` (used for the
   corpus's `__simplified.step` packages).
3. **Flow case, BCs on exact faces.**
   - EXTERNAL: a blockMesh box fixed by the ORIGINAL part's bounding box (0.6 diagonal upstream, 1.5
     downstream, 0.5 each side) - identical for every variant and every jittered mesh, so inlet,
     outlet and the four slip sides are the same planes in every run. Inlet fixed velocity, outlet
     fixed pressure, part no-slip. Flow along the part's longest axis, lift along its shortest; `--aoa`
     turns the part about its middle axis. QoIs: drag, lift and side force (the `forces` function
     object on the part patch), pressure drop = area-average p on the inlet minus the outlet's.
   - INTERNAL: fluid = (part's bounding box) minus part, keeping the solids that do not reach a box
     corner. The openings are the fluid's own planar faces on the box planes - exact B-rep faces,
     written as separate STL solids, which snappy keeps as patches. The largest opening is the inlet
     (fixed velocity), the others are outlets at p = 0. QoIs: pressure drop inlet to outlet, the
     mass-flow split between outlets (absolute change of the fraction), the wall force.
   - DRY features (internal): a feature whose faces lie further than 1e-6 x diagonal from the fluid
     cannot change it and is not solved.
4. **Mesh**: STL at a fixed tessellation (chordal 2e-4 x diagonal, 0.15 rad) for every variant;
   background cell h0 = L / `--base`; `refinementSurfaces` level (lmin lmax) with `resolveFeatureAngle
   20` (a cell is refined toward lmax while the surface normal turns by more than 20 degrees inside
   it, so a small radius costs cells); feature edges (`surfaceFeatureExtract`, 150 degrees) at lmax; a
   distance band at lmin around the part; no layers. `checkMesh` gives the cell count.
5. **Solve**: `simpleFoam`, SIMPLEC, `linearUpwind` for U, limited-corrected 0.33 laplacian. Stated
   tolerance: initial residuals p < 1e-5 and U < 1e-6 (`residualControl`), at most 3000 iterations; a
   run that stopped on the tolerance is CONVERGED. Each row also records how much each QoI moved over
   its last 10% of iterations (all laminar runs: under 0.04% of the QoI).
6. **Noise floor**: the original at h0 x (0.96, 0.98, 1.02, 1.04) - the divisions are re-rounded, the
   box stays put - and the floor of a QoI is max - min over those plus the original, relative to the
   median (lift and side force relative to drag, a split as an absolute fraction). A change is bold in
   the tables when its magnitude is above that floor.

Per row: cells saved, mesh and solve seconds, iterations and convergence, and every QoI's change.

**Timing is not a cost metric here, cells are.** 14 single-core cases at once on 8 physical cores:
the ftc09 original took 87 s to mesh and 235 s to solve when it shared the VM with two other cases,
and 155 s / 575 s inside the 14-wide run; identical meshes in the same run spread 291-684 s of solve.
The same lesson as the FEA bench.

**Same mesh, same answer.** The original run alone (pilot) and inside the 14-wide run gave bit-identical
QoIs: snappy and simpleFoam are deterministic, so a change between two variants is the geometry plus
how the mesher responded to it, never run-to-run scatter.

## 2026-11-19 - nist_ftc_09, external, laminar

The NIST part is a **3.04 mm plate**, 190.5 x 279.4 mm (diagonal 338 mm), with 29 through holes and
their countersinks and pocket-corner blends. Scaled to metres; flow along the 279 mm axis, Re 500 on
the diagonal (U = 1 m/s, nu = 6.76e-4), the plate turned **8 degrees** about its 190 mm axis so it
carries lift and so a through hole connects a pressure side to a suction side. Re 100 at 0 degrees
was the first pilot: a plate edge-on to a viscous flow gives the holes nothing to do. h0 = diagonal /
14 = 24.2 mm, levels 2-4 (6.0 mm to 1.5 mm at the wall).

```bash
python -I code/cfd_harness.py corpus/ftc09.stp --out results/ftc09 --re 500 --aoa 8 --levels 2,4 --jobs 14 --core-offset 3
```

Original: **244,144 cells, mesh 87 s, solve 235 s, 201 iterations, converged** (p 9.8e-8, Ux 9.5e-7),
peak RSS 386 MB meshing, 393 MB solving (isolated pilot; 155 s / 575 s inside the 14-wide run).
59 features recognised, 12 refused by `remove()` (11 "left the shape unchanged", as in the FEA run),
47 solved. **All 52 runs converged**, 194-202 iterations. Wall time of the whole part: 52 min.

| noise floor (5 meshes, 217k-249k cells) | |
|---|---|
| drag | 0.176% |
| lift / drag | 0.298% |
| pressure drop | 0.289% |
| side force / drag | 0.004% (the side force is ~6e-4 of the drag: a near-zero QoI has a near-zero floor) |

Mesh convergence of the original, lmax 4 -> 5 (pilot, 244k -> 850k cells, 3.5x, solve 235 s -> 3277 s
when sharing the box): drag +0.16%, lift +0.42%, pressure drop +0.15%. The discretisation error at the
working mesh is the same size as the jitter floor.

Above the floor:

| feature | r mm | cells saved | drag | lift / drag | dp |
|---|---|---|---|---|---|
| `hole13` (the largest hole) | 9.52 | -0.05% (costs cells) | +0.054% | **+0.81%** | +0.002% |
| `blend42` (concave pocket corner) | 6.35 | -0.06% | -0.053% | **+0.34%** | -0.095% |
| `blend46` | 6.35 | -0.04% | +0.009% | +0.19% (below) | -0.003% |
| everything else (44 rows) | 0.4-4.6 | -0.01% to +0.03% | under 0.04% | under 0.07% | under 0.05% |

(Nine rows show a bold side-force change of 0.004-0.011% of drag. Each removes one of a mirror pair of
features and so breaks the part's symmetry; it is real and of no consequence, and it shows why a
threshold has to be an absolute budget, not "above the floor" of a QoI that is zero by symmetry.)

What this says:

- **Filling the 19 mm hole raises lift by 0.8% of drag** - the only clear finding. Physical: at 8
  degrees the hole bleeds flow from the pressure to the suction side; filled, the plate holds more
  pressure difference. Drag and pressure drop do not see it.
- **Removing a feature saved no cells on this part** (largest saving 0.03%; hole13 and the 6.35 mm
  blends ADD 0.04-0.06%). At lmax 4 the cell is 1.5 mm, half the plate thickness: every face of a 3 mm
  plate already sits at lmax because a cell that holds both faces of the plate sees its normal turn by
  180 degrees: 173k of the 244k cells (71%) are at level 4, in the original and with `hole13`
  filled alike. The thickness, not the features, sets the cost. Same lesson as FEA's `hole13` (+18.8%
  cells): cost is not monotone in "simpler", and on a thin part it is not attributable to features at
  all.
- Every hole moves drag and pressure drop by under 0.05% and lift by under 0.07% of drag, a fifth of
  the floor; the small holes (r 2 mm) move nothing at 1e-5. For this part and flow, the defeaturing
  answer is "everything is free to remove, and removing it buys nothing".

## 2026-11-19 - heatsink_8f_tips, external, laminar

`corpus/synthetic/heatsink/heatsink_8f_tips.step`: 120 x 80 mm base 6 mm thick, eight 3 mm fins 30 mm
tall along the 120 mm axis, r 2 concave fin-root fillets, r 1 convex fin-tip rounds, a 1 mm base chamfer,
four 3.4 mm mounting holes. Flow along the fins (x), Re 500 on the diagonal (148.6 mm), lift along the
fin height (z), no incidence. h0 = 12.4 mm, levels 3-4 (1.55 mm, 0.77 mm: 4 cells across a fin, 6 across
a 5 mm fin gap). The part is mirror-symmetric in y, so one feature of each mirror pair was run, plus
three mirrors as a check (`hole2`, `blend5`, `blend7`); 36 recognised (4 holes, 16 root fillets, 16 tip
rounds; the base chamfer is not recognised), 21 run, plus the corpus's `__simplified.step` package.

```bash
python -I code/cfd_harness.py corpus/heatsink_8f_tips.step --out results/hs8 --re 500 --base 12 --levels 3,4 \
    --jobs 10 --core-offset 6 --features hole0,hole1,hole2,blend4,...,blend34 --extra simplified=corpus/heatsink_8f_tips__simplified.step
```

Original: **476,009 cells (82% at level 4), mesh 440 s, solve 1331 s, 248 iterations, converged**, peak
RSS 783 MB meshing, 721 MB solving - with 10-16 cases on the box; not measured alone. **All 27 runs
converged** (237-258 iterations). Wall time of the part: 65 min on 10 cores.

| noise floor (5 meshes, 441k-541k cells) | |
|---|---|
| drag | 0.259% |
| lift / drag | 0.321% |
| pressure drop | 0.417% |

| | cells saved | drag | lift / drag | dp |
|---|---|---|---|---|
| 4 mounting holes (r 1.7), each | +0.08 to +0.09% | +0.002% | +0.003% | +0.002% |
| 8 fin-root fillets (r 2, concave), each | -0.10 to +1.34% | +0.001 to +0.006% | under 0.01% | under 0.01% |
| 9 fin-tip rounds (r 1, convex), each | -0.40 to +0.52% | +0.003 to +0.059% | under 0.05% | +0.005 to +0.077% |
| **package** (all of the above + base chamfer) | **-0.88% (costs cells)** | **+1.26%** | -0.11% | **+1.43%** |

What this says:

- **No single feature is above the floor; the package is, at 5x it.** Sharp fin tips (tip rounds
  removed) raise drag by up to 0.06% each, the other features by under 0.01%. Sixteen tip rounds at
  ~0.04% plus the rest add up to ~0.7%, against 1.26% for the package: the remainder is the base chamfer
  (no single-feature row; `recognise` has no chamfers) and interaction. **Per-feature verdicts do not
  certify a package**: the spec's error ledger ("errors from independent features roughly add") is
  right in direction and has to be checked by a T3 run of the package, as here.
- **And it buys nothing**: the package mesh is 0.88% LARGER. A sharp tip edge is refined to lmax along
  its length like the round it replaced; the root fillets' curvature is already resolved by the fin and
  base at lmax. Single removals scatter -0.4% to +1.3% in cells for the same reason - the castellation
  re-decides near the edit.
- The mirror check: `hole0`/`hole2` agree on drag, lift and dp to the printed 0.001% and flip the sign
  of the side force, as mirror images should; `blend4`/`blend5` (root fillets) agree to 0.002% of drag;
  `blend6`/`blend7` (tip rounds) to 0.001% on drag and dp, side force again flipped.

## 2026-11-19 - tee_dn50_4b, internal, laminar

`corpus/synthetic/tee/tee_dn50_4b.step`: flanged near-equal tee, run bore r 26, branch bore r 24, 4
bolt holes per flange, r 8 outer junction fillet, r 4 bore-junction round (the one the corpus marks
flow-relevant), r 4 flange-root fillets. Re 100 on the inlet's hydraulic diameter (52 mm), inlet the
run's -x end, outlets the run's +x end and the branch, both at p = 0.

Recognition and removal, 19 features: 12 bolt holes, 3 flange-root fillets and the outer junction fillet
(`fblend17`, freeform) are **DRY** - 6 mm or more from the fluid - and are not solved. The two bores are
recognised as holes and `remove()` refuses them ("left the shape unchanged"; good - filling the bore is
not a simplification). **The bore-junction round (`fblend18`, freeform, convex) is recognised but
`BRepAlgoAPI_Defeaturing` cannot remove it** ("left the shape unchanged"), so it was measured through the
corpus's `tee_dn50_4b__simplified.step` (every listed feature removed; on the fluid side that is exactly
the bore round, the rest being dry): fluid volume -0.024%.

The DRY test is on the faces, not on the rebuilt fluid: removing a flange-root fillet moved the
recomputed fluid volume by 6e-8 although no wetted face changed (boolean round-off), so "volume
unchanged" would have sent two dry features to the solver.

```bash
python -I code/cfd_harness.py corpus/tee_dn50_4b.step --mode internal --out results/tee2 --re 100 --base 8 \
    --levels 2,4 --noise 0.94,0.96,0.98,1.02,1.04,1.06 --extra simplified=corpus/tee_dn50_4b__simplified.step
```

| mesh | original cells | mesh s | solve s / it | floor dp | floor split | package: cells saved | dp | split to the run outlet |
|---|---|---|---|---|---|---|---|---|
| levels 1-3 (`results/tee`, 4 jitters) | 46,294 | 80 | 72 / 102 | 3.84% | 0.26% | 5.26% | +1.09% | **+0.71%** |
| levels 2-4 (`results/tee2`, 6 jitters) | 154,157 | 279 | 340 / 145 | 1.43% | 0.10% | 2.43% | +1.06% | **+0.72%** |

All 14 runs converged (81-146 iterations). Mass balance closes to 1e-9.

What this says:

- **Removing the 4 mm bore-junction round sends 0.72% more of the flow down the run** (0.7476 -> 0.7548
  of the inlet flow), 7x the split's floor on the finer mesh and the same on both meshes. Pressure
  drop +1.06%, below the 1.43% floor - not a finding, though it repeats on both meshes too.
- The package saves 2.4% of the cells (5.3% on the coarse mesh): a convex round at the junction is
  refined to lmax by curvature; the sharp edge left behind is refined too, but along a shorter line.
- **The floor is set by one coarse realisation per mesh level**: on the fine mesh the jitters x1.04 and
  x1.06 gave 132-134k cells instead of 150-167k (snappy's levels are integers, so a few percent of h0
  can drop a whole band of the bore a level), and x1.06 alone moved dp by 1.2%. Without it the dp
  spread is 0.17%. Max - min is the honest floor for "a different mesh"; it is also a reminder that a
  jitter can land on a different refinement regime.
- The 16 dry features are free for the flow QoIs by construction, which is the spec's "wetted vs dry"
  idea working: 16 of 19 candidates settled by a distance query, no solve.

## 2026-11-19 - manifold_3port, internal, laminar

`corpus/synthetic/manifold/manifold_3port.step`: drilled block 120 x 60 x 50 mm, a 12 mm gallery from
the -x face, three 8 mm ports to the top and one side port, 0.6 mm-deep thread grooves (3 per port)
and 0.8 mm port-mouth chamfers on the wetted side, 1.5 mm block-edge chamfers and four counterbored
mounting holes on the dry side. Re 100 on the gallery bore (12 mm); inlet the gallery, four outlets at
p = 0.

Feature by feature it gives nothing: 8 candidates (mounting holes, counterbores) are DRY, and the 29
others are bores, groove roots and lands that `recognise` calls holes and whose removal closes a port.
So it was measured as the corpus's `__simplified.step` package (grooves, mouth chamfers, edge chamfers,
mounting holes removed; on the fluid side: grooves and mouth chamfers, fluid volume -1.31%).

The first run compared two different problems (the mouth chamfers ARE the openings; see the failures
table: dp -51%). Run again with `--inset 1.2`: the openings are cut 1.2 mm inside the block, past the
0.8 mm chamfers and short of the first groove (2.0 mm down), so they are identical exact planes in
both variants (opening area change 0.0 on all five):

```bash
python -I code/cfd_harness.py corpus/manifold_3port.step --mode internal --out results/mf3i --re 100 --base 8 \
    --levels 1,3 --inset 1.2 --features none --extra simplified=corpus/manifold_3port__simplified.step
```

Original 199,065 cells, mesh 236 s, solve 195 s / 101 iterations, converged; all 6 runs converged
(101-107 iterations).

| | floor (5 meshes, 184k-213k cells) | package |
|---|---|---|
| cells saved | | **48.1%** (199k -> 103k) |
| mesh / solve s | | 141 / 92 vs 236 / 195 |
| pressure drop | 0.75% | **-0.90%** |
| split to the side port | 0.071 pp | **+0.22 pp** |
| splits to the three top ports | 0.02-0.09 pp | -0.08, -0.07, -0.08 pp (two above their floors) |
| wall force | 0.59% | -0.64% |

What this says: **the threads are half the mesh** - 12 grooves 1 mm wide and 0.6 mm deep, refined to
lmax by curvature - and removing them (with the mouth chamfers) moves the pressure drop by 0.9% and the
flow split by at most 0.2 percentage points. Above the floor, so a measurement, and the first case in
these runs where a simplification buys real cost (half the cells, half the solve) for an error near a
1% budget. Where the cost lives decides everything: here it is on the wetted surface, in features far
smaller than the flow's length scale.

## 2026-11-19 - k-omega SST: started, not converged, stopped

`--turbulence kOmegaSST` writes k, omega (1% inlet intensity, mixing length 0.07 L) and nut with wall
functions (`kqRWallFunction`, `omegaWallFunction`, `nutUSpaldingWallFunction`; no layers, so y+ is
whatever the castellated wall gives). Run on ftc09 at Re 1e6, 8 degrees, the same 244k-cell mesh: the
original, four jitters and six features (`hole1, hole13, hole17, blend29, blend44, blend49`).

```bash
python -I code/cfd_harness.py corpus/ftc09.stp --out results/ftc09_sst --re 1e6 --aoa 8 --levels 2,4 \
    --turbulence kOmegaSST --iters 2000 --jobs 6 --core-offset 0 --features hole1,hole13,hole17,blend29,blend44,blend49
```

Stopped after 36-152 iterations per case: 3.5-6.6 s per iteration with the heat sink sharing the box
(vs 1.2 s laminar alone), initial residuals still p 5e-4 to 1e-2 and k ~1e-3, and the original's drag
still moving 3% between iteration 25 and 100 (lift/drag ~4 at this Re, vs 0.86 laminar). At that rate
the 1e-5 tolerance is hours per case, if it is reached at all on a mesh without layers. **No SST
result is reported.** To do it properly: prism layers (snappy `addLayers`) sized for the wall
treatment, `decomposePar` over 4-8 cores per case, and a convergence rule on QoI drift rather than
residuals - and a turbulent noise floor measured first, because it will be larger than the laminar
one.

## What failed, and the defects found on the way

| | what happened | state |
|---|---|---|
| `BRepAlgoAPI_Defeaturing` on freeform blends | the tee's bore-junction round (`fblend18`, the flow-relevant feature) is refused: "left the shape unchanged". ftc09: 11 of 30 blends refused, as in FEA | open. Measured through the corpus's `__simplified.step` package instead (`--extra`) |
| chamfers, bosses, logos | `harness.recognise` has none of them; the heat sink's base chamfer and the Ahmed body's stilts and badge are invisible to the harness | open (FEA has the same gap). Only the `__simplified` package covers them |
| k-omega SST | not converged in 36-152 iterations, stopped (section above) | open |
| tee jitter x1.04 / x1.06 | landed on a coarser refinement regime (132k vs 150-167k cells) and dominates the dp floor | kept in the floor - it is a real mesh of the same problem |
| outlet BC regex | `"(outlet1|outlet2)"` declared before the wall's `"part.*"`: OpenFOAM tries regex keys last-declared first, so the outlets got the wall's zeroGradient p and every internal run died "Unable to set reference cell for field p" | fixed: outlets written by exact name |
| SIGFPE banner | "Floating point exception trapping enabled (FOAM_SIGFPE)" is printed by every solver at start; matching on "Floating point exception" marked converged runs as diverged | fixed: a run fails on `FOAM FATAL`, a `sigFpe::sigHandler` stack, a nan residual, or no `End` |
| `forces` patches | `patches (("part.*"))` - a list inside a list - killed the first heat-sink and SST launches at iteration 0 | fixed |
| DRY by volume | boolean round-off moved a dry feature's rebuilt fluid volume by 6e-8 | fixed: DRY is a face-to-fluid distance test |
| `pkill -f <pattern>` over `gcloud compute ssh --command` | matched the ssh command's own command line and killed the session twice | worked around: find the PID, then kill it in a second call |
| `timeout docker run` | (known) does not stop the container | every case is `--name`d and `docker kill`ed on timeout |
| a feature ON an opening | `manifold_3port`'s package removes the port-mouth chamfers, which ARE the inlet and outlet faces: inlet area -22%, outlets -31%, so at fixed inlet velocity the flow rate fell and dp "changed" -51%, drag -55% - two different problems compared | fixed twice: a variant whose opening areas differ is refused ("an opening (a boundary-condition face) changed area"), and `--inset` cuts the openings on exact planes inside the straight bore, past the chamfers (FEA's trimmed ends, again) |
| openings named by face order | variants' openings could be numbered differently from the original's, which would have put the inlet BC on an outlet | fixed: named by plane, then centroid |
| exterior-air test by bbox corner | took `manifold_3port`'s passage (three openings meeting at a block corner) for the outside air | fixed: the outside air reaches all six box planes, a passage fewer; the largest passage is kept (a through bolt hole is a passage of its own) |
| plane tolerance 1e-6 x diagonal | `Bnd_Box` pads by the shape tolerance, 1e-7 m whatever the part's size; on the 142 mm manifold no opening was found | fixed: 1e-5 x diagonal |
| thread grooves | `recognise` sees each groove root and land as a coaxial concave cylinder - a "hole" - and removing one closes the port ("openings changed": 29 of the 37 manifold candidates, bores and groove pieces alike). A groove is root + two flanks | open; measured only inside the package |

## 2026-11-19 - a first T1 for holes: the pressure across the hole, from the original's solve

`devtools/simplify/cfd_t1.py results/ftc09`: from the ORIGINAL's converged solution only, probe p on
each hole's axis just outside both mouths (OpenFOAM `probes` via `postProcess -latestTime -fields '(p)'`
in the mesh image; ~1 min for all 29 holes) and form eta = |p_a - p_b| x pi r^2 - the normal force a cap
over the hole would carry - relative to the drag. Compared with what T3 (the harness) measured for the
same 29 holes:

| | |
|---|---|
| Spearman(eta, \|T3 lift/drag\|) | **0.76** (n = 29) |
| Spearman(eta, \|T3 drag\|) | 0.60 |
| T1 top 3 | `hole13`, `hole2`, `hole3` |
| T3 above the floor | `hole13` only - T1 ranks it first |
| magnitude | eta(`hole13`) = 0.17% of drag vs T3 +0.81%: uncalibrated, it ranks only |

The big hole has the SMALLEST pressure difference across it (0.033 vs 0.07-0.29 for the 6 mm holes,
kinematic Pa): it already passes enough flow to equalise. Its eta leads on area alone. A cap force
measured on the solution that the hole has already relieved under-predicts what filling it does, which
is why the constant is off by 5x; a version that reads the flux through the hole as well would be the
next step. Holes only: a blend has no "across", and needs the surface-sensitivity screen below.

## What this says, across the parts

- **The noise floor is ~0.2-0.3% for global external QoIs** (drag, pressure drop) on a 250k-500k cell
  laminar snappy mesh, and ~0.1% absolute for an internal flow split; a single coarse realisation can
  lift a floor to 1-4%. That is 5-10x the FEA bench's global floor (0.02-0.06% compliance with BCs on
  exact faces): castellation re-decides which cells are cut when h0 moves 2%, where a tet mesh only moves
  its nodes. Any accept threshold for CFD must sit above ~0.5% on global QoIs, or the mesh must be
  frozen away from the feature (below).
- **Same background mesh, same answer**: two runs of one STL are bit-identical, and on the
  mirror-symmetric heat sink, mirror-image removals agree to 0.002% of drag - a paired comparison on
  one background mesh is ~100x more repeatable than the jitter floor. But the two meshes still differ
  near the edit, and whether that local difference is below 0.002% in general is not shown by a mirror
  pair (it sees the same local re-meshing twice). Until the frozen-mesh floor of "Next" is measured, the
  jitter floor is what a change must beat.
- **Cells saved were ~zero on every external part** (ftc09 <= 0.03%; the heat sink -0.4% to +1.3% per
  feature and -0.88% for its whole package - many removals cost cells), 2.4% on the tee, and **48% on the manifold**, whose cost is its thread grooves. With
  curvature-driven castellation a small feature costs cells only if it is the thing forcing lmax; on a thin plate the thickness already does, on a heat sink the fin gaps
  and fin edges do. The cost attribution the spec asks for (cells near the feature above what its
  surroundings already need) has to come first, or CFD defeaturing will "simplify" for nothing.
- **What did move the answers**: the heat sink's whole package (drag +1.3%, no single feature above
  the floor), a 19 mm through hole at incidence (lift, 0.8% of drag), a 6.35 mm
  concave pocket blend (lift, 0.34%), a 4 mm bore-junction round (flow split, 0.72 percentage points)
  and the manifold's threads and mouth chamfers (dp -0.9%, split 0.2 pp). The three single features
  are ones the flow goes through or turns around; the two packages are many small features whose
  effects add up. No single small hole in a wall the flow runs along moved anything.

## Recommendation: the CFD tiers

**T0 / DRY (free, exact)**: for internal flow, a feature whose faces are further than a tolerance from
the fluid cannot change any flow QoI. The face-to-fluid distance settled 16 of 19 tee candidates with
no solve. For external flow every face is wetted, so T0 there is the geometric check only.

**Cost gate (one mesh, before any solve)**: mesh the original once, and attribute cells to a feature as
those inside its padded box at a refinement level above what the surrounding surface gets with the
feature gone (snappy's per-level counts are in its log; a cell-to-face map would make it exact). On
ftc09 this would have said "nothing to save" for all 47 features before 52 solves.

**T1 (one solve, ranks)**: the **adjoint surface sensitivity** of each QoI. OpenFOAM v2412 (the image's
version) ships `adjointOptimisationFoam`; its `libadjointOptimisation.so` in the image carries the
objectives `objectiveForce`, `objectivePtLosses` and `objectiveFlowRatePartition` (drag/lift, pressure
loss, flow split - exactly these QoIs) and the sensitivities `sensitivitySurface`,
`sensitivitySurfacePoints` and `sensitivityShapeESI` (checked with `strings` on the library; not yet run); one primal plus one adjoint solve per QoI gives dJ/dn on every wall face. A
removal is a known normal displacement field between the original and simplified surfaces (the signed
distance between their STLs), so dJ ~ integral(S_n dn dA) per feature, for all features from one pair
of solves. Two cautions from these runs: (1) filling a THROUGH hole is a topology change, which the
shape derivative does not cover - for holes use the measured screen above (pressure across the hole,
Spearman 0.76 against T3 on ftc09, ranking the only real finding first; next: add the flux through the
hole) or a topological derivative; (2) like the FEA T1, its constant must be calibrated against T3
before it can accept anything. Run it on the ORIGINAL (where the feature still is), since a feature
the simplified solution never saw has no sensitivity there.

**T2 (local, certifies local QoIs)**: a sub-domain around the feature, cut from the fluid and driven by
the GLOBAL simplified solution: velocity mapped onto the cut faces where flow enters, pressure where it
leaves (`mapFields` or `timeVaryingMappedFixedValue`), solved with and without the feature on the
same frozen background mesh (snapping only the feature region differs), so the jitter floor drops out
of the comparison. The Saint-Venant check becomes a **flux check**: mass and momentum flux through each
cut face, with vs without the feature; if they differ, the box grows. Unlike FEA the box must be
asymmetric - a few feature sizes upstream and across, several downstream - because a wake carries the
disturbance downstream instead of letting it decay. For internal flow the natural T2 piece is the
junction or fitting itself with a few diameters of pipe each side and the global outlet pressures on
its cuts: the tee's flow split is decided there. For scale: the 46k-cell tee meshed and solved in 2.5
min on one core with 14 other cases on the box; the 244k-cell ftc09 global took 5.4 min nearly alone
and 12 min inside the 14-wide run.

**T3** is this harness, as it is for FEA.

## Next

1. Cost gate first (cells per level near each feature, from the mesher's own output), and stop solving
   features it says are free.
2. Freeze the background mesh for the original/variant pair (same blockMesh, same castellation outside
   the feature's padded box) and measure the floor THAT gives - it should be far below the jitter floor,
   and is the floor a T2 comparison actually faces.
3. Adjoint T1 on ftc09 and the tee: does dJ rank `hole13`, `blend42` and the bore round first?
4. A removal path for freeform blends (`BRepAlgoAPI_Defeaturing` refuses the tee's bore round), and
   chamfer/boss recognition, before the CFD corpus can be run feature by feature rather than by package.
5. SST with layers, parallel per case, and a QoI-drift convergence rule - then a turbulent floor.
6. More internal parts: `manifold_3port` (thread grooves and port-mouth chamfers on the wetted surface)
   is the obvious next one.
