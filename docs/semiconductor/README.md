# Semiconductor meshing

Conjugate solid/fluid meshes for chip cooling, on the engine the platform already uses, with an
external solve that says whether each mesh held up.

## Running it

One command, from the repository root:

```
devtools/semiconductor/run.sh list                              what parts there are, and the cost
devtools/semiconductor/run.sh ports  <part>                     where the coolant gets in and out
devtools/semiconductor/run.sh mesh   <part> --across 13         build the two-region mesh
devtools/semiconductor/run.sh check  <part> --across 13         build it, solve it, judge it
devtools/semiconductor/run.sh sweep  <part> --across-list 2 5 13
devtools/semiconductor/run.sh solve  runs/<dir> --reuse         re-judge a run without redoing it
devtools/semiconductor/run.sh import --from <dir> --scale 1e-3  bring new CAD in
```

`run.sh` runs everything inside the mesh image, because OpenFOAM lives there and not on the host.
Runs land in `runs/<part>-<N>across/` with their logs, both meshes, `mesh-report.json` and
`solve-report.json`.

Nothing has to be typed twice. The coolant volume, the passage count and the narrowest feature come
from `cad/semiconductor/parts.json`, which `index_parts.py` measures off the STLs. The cell size is
derived from the narrowest feature and the cells-across asked for. The inlet and outlet are derived
from which block faces the coolant actually reaches. Each of those used to be an argument, and each
one was a number that was going to be wrong eventually.

Every run is costed before it starts, and refused if it will not fit in memory. A chip-scale part at
the platform's own resolution runs to hundreds of millions of cells; being told that in advance beats
finding out when the machine starts swapping.

### Costing it, and the two numbers that are not the same number

The first version of the estimate used the 909 bytes per cell measured on a finished mesh, predicted
4.9 GB for a build, passed it, and watched the kernel kill `cartesianMesh` at 8.0 GB resident with
14.3 GB of virtual address space. Two things were wrong with it.

**The mesher's peak is not the mesh's footprint.** The octree, its candidate cells and the smoothing
passes all exist at once before anything is written out. Fitted on two measured builds: 1010 bytes
per cell plus 128 MB that is there whatever the mesh size.

**The metal around the coolant is refined too.** `surfaceMeshRefinement` refines within
`refinementThickness` of the coolant surface on BOTH sides, so a shell of metal that thick is filled
at the interface cell and not at the background. On a microchannel part that shell is most of the
metal there is, and leaving it out made the estimate 2.4 times short. The model is now three terms:
the coolant at the interface cell, wetted area times thickness of metal at the interface cell
(capped at the metal there is), and the rest at the background.

It still comes in **under**, by 11% at 5 cells across and 38% at 2, because the octree grades
between levels rather than stepping and the graded cells are not in the model. Every run measures
its own peak into `mesh-report.json` as `peak_rss_bytes`, so the constant stops being a guess the
moment a run finishes. Measured against the prediction so far: 881 to 1201 bytes per cell on parts
over half a million cells, and higher on the small ones where the fixed 128 MB dominates.

On this laptop (15.5 GB) only `pin_fin_300um_staggered` fits at the platform's own 13 cells across.
That is the honest picture: the laptop is for development and real resolution belongs on a bigger
box.

## Why this exists

A chip-cooling answer is conjugate: the silicon and the coolant are solved together, exchanging heat
across the channel walls. That needs two mesh regions with a **conformal** interface, where every
face on one side has exactly one partner on the other.

cfMesh cannot produce that. It meshes manifold domains only and discards zone information.
snappyHexMesh can, through `locationsInMesh`, but carries a hard cell ceiling that a chip-scale job
walks straight into. So the engine that scales could not do conjugate work and the engine that could
do conjugate work would not scale.

## The approach

Never ask cfMesh for regions. Give it one manifold domain, the outer block of the whole part, and let
it build a single octree across metal and coolant alike. Then cut the finished mesh in two.

    block surface  ->  cartesianMesh  ->  topoSet (tag + name)  ->  splitMeshRegions  ->  2 regions

The interface is conformal **by construction**: both regions are the same cells from the same octree,
so the surface between them is a set of internal faces that were always there. Nothing is
reconstructed and there is no tolerance to tune. Meshing the two regions separately and stitching
their surfaces afterwards is the approach that fails.

## The coolant needs a way in and out

A cold plate cools by moving coolant through itself. A sealed blob of liquid is not a cooling answer,
it is a thermal battery, and it is also unsolvable: with no boundary anywhere fixing pressure the
pressure matrix is singular. The first conjugate solve run here died inside the preconditioner for
exactly that reason, while the energy equation solved happily and heat conducted into coolant that
never moved.

So **the block goes to cfMesh as six named faces**, not one anonymous box. cfMesh takes its patch
names from the `solid` names of an ASCII STL, and `splitMeshRegions` carries those patches into both
regions, keeping on each side only the faces that side owns. A face the coolant reaches arrives in
the fluid mesh already named, with a face count that proves the coolant is really there.

That turned out to be most of the work. Of the ten parts here, eight already have their channel ends
flush with a block face: nothing had to be cut, the faces only had to be named.

| part | coolant reaches | flow |
|---|---|---|
| `unit_cell_100um` | ymin, ymax | ymin to ymax |
| `hotspot_array` | ymin, ymax | ymin to ymax |
| `corintis_straight_baseline_100um` | xmin, xmax, ymin, ymax, zmax | ymin to ymax |
| `tuckerman_pease_1981_81F9` | xmin, xmax, ymin, ymax, zmax | ymin to ymax |
| `lee_garimella_2005_test1` | xmin, xmax, ymin, ymax, zmax | ymin to ymax |
| `embedded_silicon_D9_10um` | xmin, xmax, ymin, ymax | ymin to ymax |
| `manifold_two_level` | xmin, xmax, ymin, ymax | ymin to ymax |
| `pin_fin_300um_staggered` | xmin, xmax, ymin, ymax | ymin to ymax |
| `murray_ported_4gen` | ymin, zmax | ymin to zmax |
| `murray_branching_4gen` | nothing: sealed | refused |

The default picks the pair on the axis **one passage** runs furthest along, and the word "passage"
is doing real work there. The first version ranked axes by the whole coolant's bounding box, which
is right on eight of these parts and wrong on Tuckerman and Pease: 200 channels, each 14 mm long,
spread across 20 mm of silicon, so the fluid's bounding box is widest ACROSS the channels and the
flow came out pointing sideways into two hundred dead ends. Measured per passage it picks the
channel. `parts.dominant_passage_extent` finds it, and a test pins both answers so the wrong one
cannot come back.

A face the coolant reaches that is NOT a port is sealed as a wall, and that is correct rather than a
compromise. A block cut out of a wider channel array has coolant flush with the sides it was cut on,
and those sides are walls. The same goes for a part whose channels are open at the top: walling zmax
is a lid of zero thickness. Pass `--lid 0.2` to grow 200 um of real metal over them instead, which
makes the channel roof part of the interface and lets it conduct.

`murray_branching_4gen` is the one part that is genuinely sealed, with a 120 um wall all round. It is
kept as it is, because it is still the best test of branching topology in the set, and
`murray_ported_4gen` is the same tree with the trunk carried out through ymin and one 119 um hole per
leaf tip up through zmax. Fed from the side and drained through the lid, which is how a cold plate is
really plumbed.

## Judging the mesh with a solver

The solver is **external**, lives in `devtools/`, and nothing in `src/` imports it. It is a measuring
instrument for meshes, not a platform feature.

It exists because every geometric check a mesh can be given passes on meshes that cannot be solved,
and we have the receipts. A coolant region in 15 disconnected islands passed every bar `checkMesh`
has and died in the preconditioner. A sealed coolant region passed them too and died the same way.
Conformal face counts, volume within a fraction of a per cent, "Mesh OK" on both regions: all true,
on meshes no solver would touch.

What makes it a test rather than a demonstration is that heat goes in at a **known rate**: a fixed
flux on a named face, so the watts in are that flux times the patch area and nothing else. At steady
state the same watts have to come out, and four independent readings of that number have to agree.

| reading | where it comes from |
|---|---|
| in at the heated face | the boundary condition, flux times area |
| out through the solid interface | integrated flux, solid side |
| in through the fluid interface | integrated flux, fluid side |
| carried away by the coolant | mass flux times Cp times the bulk temperature rise |

The solid side against the fluid side tests the **coupling**: a conformal interface that is not
transferring what it says it is shows up here and nowhere else. The coolant against the heated face
tests the whole chain, and it is the one that moves when staircasing has changed the wetted area,
because heat crosses area while the staircase table only ever measured volume.

Measured on `unit_cell_100um` at 5 cells across, 2026-10-22:

| reading | watts |
|---|---|
| in at the heated face | 6.5000 |
| out through the solid interface | -6.5000 |
| in through the fluid interface | 6.5000 |
| carried away by the coolant | 6.4731 |

The first three agree to 0.00% and the coolant to 0.41%. Mass balanced exactly, the coolant rose
20.18 K, and every equation's residual fell between 7 and 11 decades.

Water with 25% propylene glycol against silicon at 148 W/m/K, 1 m/s in, 100 W/cm2 into zmin, coolant
in at 300 K. Reynolds comes out near 70 in a 100 um channel, two orders below transition, so the run
is laminar and a turbulence model would be inventing physics.

### The solver scales and the mesher does not

Worth keeping straight, because the two measurements point opposite ways.

| | serial | 8 ranks |
|---|---|---|
| one SIMPLE iteration, 1.2 M cell conjugate case | 4.6 s | 0.94 s |

That is 4.9x, and it is the difference between a sweep and no sweep: serial, a converged answer on
the smallest part in the set was two hours away. cfMesh is the opposite and was measured here
saturating around four cores at 2.5x, with MPI buying nothing at all and changing the mesh when it
was used. A CHT solve is a domain-decomposed linear solve, which is what MPI is for.

`decomposePar -allRegions`, never one region at a time: a multi-region case has to be cut up the same
way in both regions or the interface stops being conformal across processor boundaries.

### Convergence is measured here, and the run is stopped from out here

`chtMultiRegionSimpleFoam` will not stop when it is finished, and two mechanisms that look like they
would were tried first. Both are worth not trying again.

`residualControl` in the SIMPLE dict does nothing. The solver has no `simpleControl` in it at all,
so the block is read by nobody and refused by nobody: it looks like the run stops when it is
converged, and nothing is checking.

`runTimeControl` with an `equationInitialResidual` condition and `satisfiedAction end` **is** run,
and ended a solve at iteration 3 with every residual still above 0.2. A stop that fires immediately
is worse than no stop: the run comes back looking like a mesh that cannot be solved.

So the run is **chunked**. `--chunk` iterations at a time, with `startFrom latestTime` on each one
after the first, and between chunks the residuals are read out of the log and compared with
`cht.RESIDUAL_TARGETS` - the same targets the final verdict uses, so the run cannot stop on one
criterion and be judged on another. `--iterations` is a cap. The 5-across unit cell converged at
1000 of a 2000 cap in 1136 s and gave the same physics as a flat 1500-iteration run: 6.4725 W
against 6.4731 W, the same 20.18 K rise.

That matters beyond the saved time. An unconverged run has a coolant that has not warmed up yet,
which reads as a broken energy balance and is nothing of the kind, and the iteration count becomes
a measurement in its own right: how hard this mesh was to solve.

## Sizing

`engines/gap_sizing` owns resolution and holds every passage to `PASSAGE_CELLS_ACROSS`, with the
resolution_floor gate refusing anything below `PASSAGE_FLOOR_CELLS` (12). This path does not
second-guess it. `--across` says what to ask for and the background cell is derived from it:
`(narrowest / across) * 2**levels`, because the octree halves the cell once per level and handing
over the interface size as the background produces a mesh 512 times too big.

`engines/staircase_error` says what a resolution is **worth**, measured on real meshes rather than
cited from practice. The original table was one part per row:

| cells across | volume error |
|---|---|
| 2 | 3.21% |
| 5 | 1.04% |
| 12 | 0.12% |

Eight parts were then meshed at 2 cells across and five at 5, and the spread is the finding:

| cells across | error range | median | parts |
|---|---|---|---|
| 2 | 0.05% to 27.54% | 5.53% | 8 |
| 5 | 0.13% to 13.26% | 1.53% | 5 |
| 12 | 0.12% | 0.12% | 1 |

**The 3.21% was neither the best nor the typical one**, and the range at a single resolution covers
nearly three orders of magnitude. The sign moves too: at 2 across the errors ran from -27.5% to
+5.9%.

What sorts them is how many differently placed walls the part averages over. A wall falling between
two rows of cell centres loses a row of cells and one falling just outside gains one, so a part
presenting the same wall placement over and over gets that error multiplied instead of cancelled.
Ordered by error, at 2 cells across:

| part | distinct passages | error at 2 across |
|---|---|---|
| `corintis_straight_baseline_100um` | 100 | **-0.05%** |
| `lee_garimella_2005_test1` | 10 | -1.75% |
| `hotspot_array` | 24 | +2.20% |
| `manifold_two_level` | 1 network, 16 channels | +5.33% |
| `murray_ported_4gen` | 1 network, 31 segments | +5.73% |
| `murray_branching_4gen` | 1 network, 31 segments | +5.92% |
| `pin_fin_300um_staggered` | 1 chamber around posts | -15.26% |
| `unit_cell_100um` | ONE prismatic channel | -27.54% |

Monotone in passage count across all eight but for the inversion between 10 and 24, which is 0.45
of a percentage point.

### A prediction that was wrong, and what it cost to find out

Before `corintis_straight_baseline_100um` was run, it was predicted to come out BADLY - near the
unit cell - on the reasoning that its 100 channels are identical and at identical pitch, so they
would share one alignment against the octree and have nothing to average against each other. It
came in at -0.05%, the best of the eight. A hundred identical channels do average.

The second-order effect is real but much smaller. The same part, same resolution, with the block
grown on one face so the octree falls differently:

| block | fluid volume | error |
|---|---|---|
| 0.200 x 32.500 x 1.300 mm | 1.88392 mm3 | -27.54% |
| 0.200 x 32.500 x 1.350 mm | 1.77274 mm3 | -31.82% |
| 0.200 x 32.500 x 1.400 mm | 1.89764 mm3 | -27.01% |
| 0.200 x 32.500 x 1.500 mm | 1.88683 mm3 | -27.43% |

So where the walls fall moves the error by about four points on a part already at twenty-seven,
and it jumps rather than trending, which is what alignment looks like. Passage count is the first
order term and alignment is the second.

`staircase_error.single_passage_error_pct` is the figure to quote for a part with one passage or a
few similar ones, and `volume_error_pct` is the one for a part with many.

### One more thing a run now says before it starts

`conjugate.background_complaint` reports when the background cell is larger than the block's
thinnest direction, because cfMesh cannot place a 400 um cell inside a 200 um wide block and the
octree levels asked for are then not the levels delivered. At 2 cells across it fires on
`unit_cell_100um` (0.50 background cells across), `pin_fin_300um_staggered` (0.83) and
`lee_garimella_2005_test1` (1.78).

It was added expecting it to separate the accurate meshes from the inaccurate ones. It does not:
those three parts measured -27.5%, -15.3% and -1.75%, the worst two and the third best. It is kept
because the condition it reports is real on its own terms, and it says in its own message that it
tells you nothing about accuracy.

### The coarse background is mostly notional on these parts

Worth correcting, because the module's own opening says octree levels are spent only where the
coolant surface is. `refinementThickness` defaults to one background cell, so the fine band reaches
a background cell into the metal from every coolant surface - and a channel array at 200 um pitch
with a 400 um background has no metal outside the band at all.

Measured on the 100-channel part at 2 cells across: the solid region came out at a 50 um mean cell
against a 400 um background. The conjugate mesh was effectively uniform at the interface size. That
is the whole reason the first cell estimate was 2.4 times short, and every run now records
`solid_mean_cell_um` beside the background it was asked for, so the gap is visible rather than
assumed.

The mesh also reports what it actually achieved, as `cells_across_achieved`: the cube root of the
mean cell volume against the part's narrowest passage. It is a lower bound, because refinement only
reaches `refinementThickness` from the coolant surface and cells down the middle of a wide passage
are coarser. It came out 2.0 to 2.2 on every part asked for 2, and 5.0 to 5.1 on every part asked
for 5.

## What every mesh is checked for

Five checks, each catching something the others cannot.

**Conformality** proves the two sides share a surface, and on its own almost nothing else: a split
that put half the metal in the coolant region would be perfectly conformal.

**The fluid volume against the CAD** is what proves the tagging was right, which cells ended up on
which side. The two together are the real test.

**Connectivity** is the one `checkMesh` will not give you. It prints the piece count with its own
warning asterisk and still says "Mesh OK", because disconnection is not fatal to it. On a coarse
build the coolant came back as 15 pieces: one body of 5,386 cells and 14 islands of 24 to 32. Every
geometric bar was green and the solve died in the preconditioner, because an island has no face path
to the pressure reference cell.

**Ports**, for the same reason in a different shape. The failure mode worth catching is a named patch
with zero faces: the name survives the split whether or not any coolant reached it.

**Each region's own `checkMesh`**, because `-allRegions` reports the PRE-SPLIT mesh, which looks like
a successful verification and is a measurement of the wrong thing.

## Where the time goes

Not the mesher, which is the thing worth knowing before promising a turnaround.

| | 100-channel part (81.6M cells) | branching part (1.2M cells) |
|---|---|---|
| cartesianMesh | 617 s | 9.3 s |
| everything after | over an hour | 18.0 s |
| post-processing share | >85% | 66% |

`topoSet` tests every cell centre against the coolant surface and `splitMeshRegions` writes two full
meshes. Both are serial. A conjugate mesh in thirty minutes is a post-processing problem, not a
meshing one. `ConjugateReport.seconds` carries the per-stage split so the attribution is measured on
every run rather than assumed.

## The test parts

`cad/semiconductor/` holds ten, in metres. `parts.json` is measured off the STLs rather than
maintained by hand, because the passage count is the check that catches a mesh too coarse to hold a
channel together and a written-down count goes stale the moment the CAD is rebuilt. 100 channels that
never touch must come back as 100.

| part | narrowest | coolant | passages | what it stresses |
|---|---|---|---|---|
| `unit_cell_100um` | 100 um | 2.600 mm3 | 1 | one channel, 325:1 aspect ratio; the cheapest real case |
| `manifold_two_level` | 100 um | 4.560 mm3 | 1 | two levels joined only at slots: severable connectivity |
| `murray_branching_4gen` | 119 um | 6.312 mm3 | 1 | branching network, three-way junctions, sealed |
| `murray_ported_4gen` | 119 um | 6.391 mm3 | 1 | the same tree with a feed and sixteen drains |
| `hotspot_array` | 60 um | 10.080 mm3 | 24 | one passage per hot spot, none touching |
| `pin_fin_300um_staggered` | 300 um | 22.916 mm3 | 1 | flow around posts; far more wetted area per volume |
| `tuckerman_pease_1981_81F9` | 50 um | 42.280 mm3 | 200 | the 1981 original, 200 channels at 100 um pitch |
| `lee_garimella_2005_test1` | 194 um | 43.560 mm3 | 10 | published test section |
| `corintis_straight_baseline_100um` | 100 um | 260.000 mm3 | 100 | 100 channels that never touch each other |
| `embedded_silicon_D9_10um` | 10 um | 0.750 mm3 | 200 | 200 channels separated by 15 um fins |

None is a replica of any customer part and none should be presented as one.

Conformal interfaces on all three of the original topologies, 2026-10-21:

| part | interface faces | fluid volume vs CAD |
|---|---|---|
| murray | 1,065,510 both sides | +0.12% |
| corintis 100ch | 13,700,762 both sides | -1.04% |
| embedded D9 | 6,747,560 both sides | -3.21%, solid +0.05% |

On D9 the solid landing within 0.05% is what proves the 15 um fins survived rather than letting
neighbouring channels merge into one region.

And the parts added since, meshed 2026-10-22. Every one conformal on both sides, every one with
both ports carrying faces, and **every passage count matching the CAD's**, which is the check
`checkMesh` will not make:

| part | cells across | fluid cells | passages | volume vs CAD |
|---|---|---|---|---|
| `corintis_straight_baseline_100um` | 2 | 2,080,000 | **100 = 100** | -0.05% |
| `unit_cell_100um` | 2 | 20,800 | 1 = 1 | -27.54% |
| `unit_cell_100um` | 5 | 383,736 | 1 = 1 | +13.26% |
| `manifold_two_level` | 2 | 38,834 | 1 = 1 | +5.33% |
| `manifold_two_level` | 5 | 581,155 | 1 = 1 | +1.53% |
| `murray_branching_4gen` | 2 | 31,808 | 1 = 1 | +5.92% |
| `murray_ported_4gen` | 2 | 32,218 | 1 = 1 | +5.73% |
| `murray_ported_4gen` | 5 | 474,082 | 1 = 1 | +0.13% |
| `hotspot_array` | 2 | 383,520 | **24 = 24** | +2.20% |
| `lee_garimella_2005_test1` | 2 | 52,400 | **10 = 10** | -1.75% |
| `lee_garimella_2005_test1` | 5 | 784,576 | **10 = 10** | +0.58% |
| `pin_fin_300um_staggered` | 2 | 6,336 | 1 = 1 | -15.26% |
| `pin_fin_300um_staggered` | 5 | 110,640 | 1 = 1 | +3.97% |

The 100-channel part coming back as exactly 100 passages, and the 24-hotspot part as exactly 24,
is what the connectivity check is for: a mesh one cell too coarse breaks a channel in half and
`checkMesh` still says Mesh OK.

The manifold coming back as one passage is the other one worth pausing on: it is two stacked channel
levels joined only at three slots, and a mesh too coarse to hold those slots open would split it in
two and still pass every geometric bar there is.

`murray_ported_4gen` keeping both its ports at 2 cells across is the other one. Its drains are
119 um holes through a 120 um wall, meshed with a 59 um cell, and they came through with 84 faces on
the feed and 52 on the drains. They were built that thin deliberately, so that there is one part in
the set where a coarse enough mesh will staircase a port away and the gate has something real to
catch.

## Adding CAD

The geometry is authored in **millimetres** and an OpenFOAM case is in **metres**. A surface handed
over still in millimetres makes every cell size a thousand times wrong, and that does not fail: it
runs until somebody kills it. So `import` scales on the way in and recomputes every facet normal,
and anything whose longest side comes out over 0.3 m is refused as being in the wrong unit, a 300 mm
wafer being the largest thing this work will ever see.

The authoring scripts are in `devtools/semiconductor/geometry/` with their own README. They write
millimetre STL and STEP into `devtools/semiconductor/cad/`, which is a staging area and not in git;
a part becomes a test part only once it has come through the step that scales it:

```
devtools/semiconductor/run.sh import --from devtools/semiconductor/cad --scale 1e-3
python devtools/semiconductor/index_parts.py
```

A part with no `smallest_feature_um` in a manifest is refused rather than guessed at, because the
cell size is derived from that number and a guess there is a guess about the whole run.

## What was removed, and why

`devtools/semiconductor/conjugate_part.py` is gone. It took two STL paths and had the coolant volume
and the passage count typed on the command line, which `index_parts.py` now measures off the STL
itself. Keeping both meant two places to pass a resolution and two readers of the same numbers, and
the hand-typed one was the one that could be wrong.

Three readers inside the engine went the same way:

- `conjugate` no longer parses `checkMesh` output. `foam_exec.parse_check_mesh` has read it for
  every cfMesh path since long before this one existed, so a second reader was a second thing that
  could disagree with the first about what `checkMesh` said. It was split out of `check_mesh` so it
  can be called with a string and tested, which it never could before. `Total volume` was added to
  it, since that is the field the conjugate path needed and every other caller can use.
- `conjugate` no longer parses `polyMesh/boundary`. `polymesh_surface.read_boundary_patches` does,
  in the same package, for `engines/passage` and `engines/passage_flow`.
- Going through `check_mesh` also means the conjugate path now runs `scan_case_dicts` before
  measuring, which every other cfMesh path already did and this one was quietly skipping.

## One thing to watch, in main and not only here

Going through the shared `check_mesh` immediately found something, and it is not confined to this
path.

`splitMeshRegions` leaves `constant/cellToRegion` behind: one integer per cell saying which region
that cell went to. Nothing reads it afterwards. But it sits in `constant/` and it is not inside a
`polyMesh/`, so `sandbox.foam_case_guard` scans it as if it were an authored dictionary, and above
its 8 MB cap it refuses the whole case rather than read something it cannot bound. On a 1.4 M cell
part that file is 9.9 MB, so every `checkMesh` after the split came back empty, both regions came
back with no cells, and a perfectly good mesh - conformal, ported, 305,696 interface faces on each
side - was reported as having none.

This path now moves the file out of `constant/` after the split. It was not fixed in the guard:
exempting the name would fix every engine at once and would also leave one more filename a model
could write a `#codeStream` into and have gone unscanned, and that is not a trade to make quietly.

**`snappy_multiregion` and `snapgrid` both run `splitMeshRegions` and neither handles this file**,
so a multi-region mesh over roughly 1.2 M cells walks into the same wall on those paths. Worth
someone deciding what the right fix is there, which is probably widening the guard's data-file rule
rather than ten copies of a `move` call.
