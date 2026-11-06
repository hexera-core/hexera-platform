# The route to the final mesh

Internal. Everything planned between the mesh we have and the mesh we want, in the order it has to
happen, with what each step costs and the gate that decides whether to take the next one.

Boundary layers are in this plan. They are phase 2, not phase 1, and the reason is a cancellation
rather than a doubt about whether they work. That reason is in phase 2.

## What "final" means

The mesh I am aiming at, stated as checks rather than adjectives:

| | shipped (`cbox2`) | target |
|---|---|---|
| cells across every channel | 4 or 5 or 6 | exactly 5, no exceptions |
| flow spread between identical channels | 3.38x | 1.0x |
| first cell centre off the wall | 11.0 um | 1 to 2 um |
| wetted area error from staircasing | +7.8% overall, +11.6% channels | corrected or removed |
| conformal interface, faces each side | equal | equal, unchanged at every step |
| `checkMesh` per region | Mesh OK | Mesh OK, nothing over the 70 degree bar |
| total cells | 413,273,323 | under 600M |
| quality evidence | built with a broken `minVol` | rebuilt and meaningful |

Under 600M rather than 700M because 700M is not the real ceiling. At a measured 3.02 faces per
cell, 2^31 faces is about 711M cells, so the 32-bit face label binds first. Our usable ceiling is
600 to 650M.

## Phase 0: two free measurements, before anything is meshed

Both are an afternoon, no compute, and both change what later phases should buy.

**0a. Junction-to-junction segment lengths**, from `PlateAndChip.step`. A branching plate restarts
the thermal boundary layer at every junction, and the segment-averaged Nusselt error at 4.6 cells
is about -1% on a 2.45 mm segment, -10% at 0.73 mm, -25% at 0.24 mm and -43% at 0.07 mm. The
slot's thermal entrance length is 1.4 to 5.7 mm. I have never measured where this network sits in
that range, so I do not know whether the entry region is a rounding error or a 40 point error.

Note the familiar `0.05*Re*Pr*Dh` rule is the circular-tube rule and overestimates this slot by 4.3
to 6.3x. It would say the flow never develops over 55 mm when the truth is a few millimetres.

**0b. Fix `system/meshQualityDict`**, `minVol 1e-13` to `1e-20`, and re-run `checkMesh` on every
region. That value is an absolute face-pyramid volume and a 22 um cell produces about 1.8e-15, so
the shipped dict flags every face of a perfect mesh. Any report we hold built with it is worthless.

This is also a hard precondition for phase 2: the same value is what makes layer addition work at
all, and with it wrong the layer run silently extrudes nothing and exits 0.

While there, correct every document quoting a 50 degree quality bar. Our own bar is 70 and our
measured max is 51.46.

## Phase 1: re-mesh at a commensurate cell size

The one change that matters, and the only one in the whole plan that improves the mesh and makes
it cheaper at the same time.

| | shipped | phase 1 |
|---|---|---|
| `maxCellSize` | 352.056 um | **322.020 um** |
| finest cell | 22.0035 um | 20.1263 um |
| cells per channel | 4 / 5 / 6 | 5 for 85% of them, measured, see the result below |
| channel width in the mesh | 88.014 / 110.017 / 132.021 um | 100.631 um, +0.6% |
| flow spread | 3.38x | 1.73x measured, not none |
| octree depth | level 14 | level 14, unchanged |

Why one number does this: the coolant boundary lies on octree cell faces, so a channel must be a
whole number of cells wide, and at 22.0035 um no whole number gives 100 um. At 20.1263 um the
201.263 um channel+fin pitch is exactly ten cells and the channel is exactly five. Full derivation
in [MESH_QUANTISATION.md](MESH_QUANTISATION.md).

Also in phase 1: tighten the chip entry's `refinementThickness` from 3.6e-3 to 3.6e-4. It is an
88 um band reaching 3.6 mm into the plate and it also refines a shell of air around the chip.

```bash
couple_box.sh cbox2b 0.000322020 1 4 2 1 8 12
```

| | predicted | basis |
|---|---|---|
| cells | about 496M, nearer 440M with the chip band tightened | cells go as h^-2.038, measured |
| time | about 2.2 h | scaled off `cbox2`'s own stage table, see [MESH_TIME.md](MESH_TIME.md) |

**Gate, and what it returned.** Run on 6 November as `cbox2b`. Non-orthogonality, skewness and
the interface face counts all held. The channel histogram did not: 85.00% of channels came out at
exactly 5 cells rather than all of them, and "zero spread" was my prediction, not the result. The
measurement and the cause are in the phase 1 result at the end of this document.

I also did **not** tighten the chip `refinementThickness`, deliberately: the pitch fix is the proven
change and the chip trim's thermal cost is unmeasured, so I changed one variable. At 506M cells the
band costs nothing we need.

## Phase 2: boundary layers and the staircase, together

**These two have to move together.** The staircase inflates wetted area by 11.6% on the channels,
which biases heat transfer high. The coarse near-wall cell biases it low. Today they cancel to
about +10% on total heat. Fix the near-wall gradient alone and the integrated answer goes from
about +10% to about -5%: more cells, more work, worse number. That is the whole reason layers are
phase 2 and not phase 1.

### 2a. The gate first, one hour

Carve two subsets of about 2M cells each from the phase-1 fluid region with `topoSet` and
`subsetMesh`: one straddling the 4x refinement transition including a branching junction, one
entirely inside the fine zone. Run the layer dict on each, about 5 minutes and 3 GB apiece, and
read four lines.

| read | ship it | usable | stop |
|---|---|---|---|
| `Extruding N out of M faces (P%)` | 100% | 60 to 99% | under 30%, the level transition is on the patch |
| interface `nFaces` **and** integrated area | unchanged to 1e-6 | | anything else, 1:1 is lost |
| `checkMesh` | Mesh OK, nothing over 70 deg | | |
| polyhedra on an all-hex input | zero | | |

**Kill condition:** if the subset entirely inside the fine zone comes in under 60%, the branching
junctions are defeating the medial axis and this route is dead for our part. Go to phase 4.

If coverage fails only on the transition subset, split the interface by cell level in `topoSet` and
layer the fine zone only.

### 2b. The layers

`snappyHexMesh` as a pure layer adder, on the **post-split fluid region**, keyed on the
`fluid_to_solid` patch:

```
castellatedMesh false;
snap            false;
addLayers       true;
// and minVol 1e-20 in meshQualityDict, or this extrudes nothing and exits 0
```

Measured on a 110 um slot built to mimic ours: 4 layers at 0.978 / 1.456 / 2.166 / 3.224 um
against an 18.871 um core, expansion ratio 1.4886, 100% coverage, all hexes, Mesh OK, and the
interface area unchanged to 2.5e-7 percent. The conformal coupling survives because layers shrink
the interior and insert cells rather than moving the extrusion base.

Two counterintuitive settings, both measured: **fewer and thicker beats more and thinner** (6
layers at 1 um gave 24% coverage, 4 at 4 to 8 um gave 100%), and a 2:1 refinement transition
sitting on the layer patch is fatal while the same transition one cell away is harmless.

| | |
|---|---|
| layers | 2 to 4, fluid side only |
| cost | about +65M cells at the phase-1 interface size, so roughly 505 to 561M total |
| first cell centre | 11.0 um to 1 to 2 um |
| memory | about 251 GB for the fluid region at 1.61 kB per output cell, so this wants a 512 GB box |
| time | 1 to 2 h on 8 to 16 ranks |
| what it costs us | the "zero faces over 50 degrees" line. Non-orthogonality goes to about 49 to 53 on a staircased interface |

**Fluid side only, not both.** Both sides is +130M and would put us at 570 to 626M, against the
ceiling. Physics agrees: the fluid-side Dirichlet weight is 0.99850, so the solid contributes
0.15% of the interface stiffness, and Biot on the fin half-thickness is 0.0016 to 0.0041, which is
25 to 60x below the thermally-thin threshold. The copper does not need it.

### 2c. The staircase, which is the harder half

Nothing in our current toolchain removes it. Layers left interface area bit-identical in all 13
verification runs, `refineWallLayer` refines normal to the staircase without straightening it, and
`refineMesh` never moves a point. Three options:

| option | what it gives | cost |
|---|---|---|
| **correct for it analytically** in the reported heat, using the measured +7.8% / +11.6% | honest numbers today, no mesh work | does not improve the mesh |
| **CF-MESH+ 14-day trial**, free, no card | the only tool found that offers a surface-fitted internal conjugate interface | needs a GUI to author the setup, which is a real problem for a headless pipeline |
| **phase 4** | removes it outright | weeks |

Aim the trial at exactly one number: define coolant and copper as two domains on a 5 mm cut-down
from `~/clean_stl`, and measure the fitted coolant interface area against the CAD area. Within 1 to
2% against today's +11.6% and the licence buys a real error reduction. Near +11.6% anyway and the
question is closed for good. Do not test it for layers, we have those for free.

## Phase 3: publish the error budget

Not mesh work, but it belongs on the route because it is what the mesh is for.

| quantity | error at 4.6 cells |
|---|---|
| chip-to-coolant dT | about 1.4% low |
| Nusselt | about 4% **high**, not low |
| fRe, so pressure drop | about 10% low |
| per-channel flow scatter | 3.38x today, 1.0x after phase 1 |
| staircase wetted area | +7.8% overall, +11.6% channels |
| the two combined, on total heat | about +10% |

The 2/N^2 law is exact and it governs **flow rate**, not Nusselt: `u_FV/u_exact = 1 + 2/N^2`,
confirmed to five decimals. Nusselt is over-predicted because no-slip forces the second derivative
of temperature to zero at the wall, so the two-point gradient error loses its leading term.

Any Nusselt number recorded anywhere has to name its thermal boundary condition. The four
candidates span 2.907 to 7.214 for this aspect ratio, a factor of 2.5, which is larger than every
mesh error above put together.

## Phase 4: the Gmsh 2.5D swept route, strategic

This is the mesh this part class deserves, and it was proven on the real dimensions including a
branching T-junction. It fixes the staircase, the near-wall gradient and the quantisation in one
move, because the walls follow the CAD curves instead of a lattice.

Two 2D `BoundaryLayer` size fields on the shared wall curves, each listing the other material's
surface in `ExcludedSurfacesList`, both registered with `setAsBoundaryLayer`, then a graded
`Extrude` through the 1499.9 um depth.

| measured | |
|---|---|
| first cells | 2.0 um, graded on both sides of a conformal internal interface |
| duplicated nodes | zero, so conformal by construction |
| element types | hexes and prisms only, zero tets |
| density | 10,137 3D cells per mm2 of footprint, against about 46,900 for the equivalent band of ours |
| net | 4.6x lighter with an 11x finer wall cell |

Hard constraints: `gmshToFoam` reads ASCII MSH2 only, first order only, no polyhedra, serial, with
a reported failure near 400M elements and success "under 80 M". At 102 bytes per 3D cell a 60M-cell
mesh is a 6 GB file, which is where this route lands. Wall-cell aspect ratio goes to 50 to 100, and
`FanPointsList` at junction corners is mandatory: one reentrant triangle came out at quality
0.0081.

**Precondition.** Section the STEP at 10%, 50% and 90% of channel depth. Identical cross-sections
means prismatic and the route is live. Their own patent claims 1 to 10 degrees of DRIE sidewall
taper, so if the channels taper the extrusion needs a scaling law or the route degrades.

Do **not** build on Gmsh's 3D boundary-layer machinery: the released 4.15.2
`Plugin(BoundaryLayer)` 3D path ships but is inert, 100 tets in and 100 tets out. The working code
is in git master only, AGPL inside a GPL2 project.

## The cell budget across the whole route

| | cells | under 600M |
|---|---|---|
| shipped | 413M | yes |
| phase 1 | 440 to 496M | yes |
| phase 1 + 2 layers, fluid side | about 480 to 530M | yes |
| phase 1 + 4 layers, fluid side | about 505 to 561M | yes, tight |
| phase 1 + 4 layers, both sides | 570 to 626M | **no** |
| phase 4 | tens of millions | comfortably |

## Decisions already taken, so nobody reopens them

**Cancel the `WM_LABEL_SIZE=64` image rebuild.** Our own `BUFFER_PROBE.txt` refuted the overflow
diagnosis: the error is `MPI_ERR_BUFFER` and `MPI_Buffer_attach` takes an int, which
`WM_LABEL_SIZE` cannot touch. And the octree cube struct is 16 bytes at int32 and 32 at int64, so a
64-bit build **halves** the box ceiling and would leave the mesh we ship today at 1.91 GB against a
2.147 GB cap. If sub-20 um octree resolution is ever wanted, halve the octree root from 0.360505 to
0.180253 m instead; the part is 177.500 mm in x so it fits.

**Refuted, do not re-attempt.** Each has a measured reason in
[RESEARCH_BOUNDARY_LAYERS.md](RESEARCH_BOUNDARY_LAYERS.md):

| route | why |
|---|---|
| cfMesh `boundaryLayers` during `cartesianMesh` | the guide states it in section 4.5; our error is the documented design |
| cfMesh `generateBoundaryLayers` post-split | calls `meshOptimisation()` unlocked, would silently drift the interface off the solid while both sides still report equal face counts |
| non-conformal AMI coupling | measured weights 0.599 to 1.345, so +34% / -40% local flux error replacing exactly zero |
| `snappyMultiRegionHeater` as a precedent | ships with `addLayers false`; its layers block is an external box patch |
| scaleY 4 as a compromise | 7.5x slower for 7% fewer cells, on the real part |
| a cuboid domain instead of the box | refuted on the real part; the reproducer lied |
| a 4x coarser far field | changes total cells by 0.9%, not a lever |
| aggressive wall clustering to fix Nusselt | measurably worse than uniform at matched cell count in laminar internal flow |

## Order, and what blocks what

```
phase 0a  segment lengths      free, afternoon      changes what phase 2 buys
phase 0b  minVol + checkMesh   half a day           BLOCKS phase 2
   |
phase 1   re-mesh at 322.020   2.2 h                the deliverable fix
   |
phase 2a  layer gate, subsets  1 h                  BLOCKS 2b
phase 2b  layers, fluid side   1 to 2 h + 512 GB    needs 2a green
phase 2c  the staircase        1 to 2 weeks         must land WITH 2b, not after
   |
phase 3   error budget         write-up             needs 1, wants 2
   |
phase 4   Gmsh 2.5D            weeks                strategic, not this quarter
```

Phase 0b blocks phase 2 because with `minVol` wrong the layer run extrudes nothing and reports
success. Phase 2c must land with 2b because separating them makes the integrated answer worse.
Everything else can slip without breaking what came before.

---

## Added 2026-11-06: two things measured after the first draft

### The staircase cannot be reduced by re-orienting the part, and now I know why

The staircase factor is `|cos theta| + |sin theta|` per wall, area-weighted. That does not depend
on cell size, which is the reason refining never removes the area error and only shrinks the step.
But it does depend on angle, and cfMesh meshes on global axes while we choose how the geometry is
oriented. So rotating the STL about y before meshing and rotating back afterwards is free, and if
the network had a dominant off-axis direction there would be a saving sitting there.

Swept every angle against the real CAD wall normals, area-weighted, on `fluid.stl`:

| rotation about y | inflation on the vertical walls |
|---|---|
| **as built, 0 deg** | **+8.44%, and this is the minimum** |
| 15 deg | +23.88% |
| 30 deg | +33.99% |
| 45 deg | +37.61%, the worst |
| 90 deg | +8.44%, by symmetry |

The part is already in the best orientation available. 61.1% of wall area sits within 5 degrees of
one axis and another 14.9% within 5 degrees of the other, so 76.0% is effectively aligned already.
The remaining 24.0% is off-axis at a mean 25.42 degrees and scattered across every direction,
because it is a branching tree, and no single rotation can align many directions at once.

The model predicts +6.2% overall against the +7.8% measured on the mesh, so it accounts for most of
the effect and the scale-invariance conclusion holds.

**So three routes to the staircase are now measured dead, not assumed dead:** finer cells
(scale-invariant), re-orienting (already optimal), and the three refinement utilities (interface
area came back bit-identical in all 13 verification runs). What remains is phase 2c.

### The plate-only mesh had a fitted outer skin and the coupled mesh does not

I had this wrong. `block.stl` is not a box despite the name: it is the plate's own sewn envelope,
**27,696 triangles**, and the delivered plate mesh's `meshDict` points `surfaceFile` straight at
it. cfMesh projects the domain boundary onto its surface, so that mesh's outer skin was genuinely
body-fitted.

| mesh | domain handed to cfMesh | triangles | outer skin |
|---|---|---|---|
| plate only, `ws_shell`, 270.5M | `block.stl` | 27,696 | **fitted** |
| coupled, `cbox2`, 413M | `cb_domain_box.stl` | 12 | staircase |

`couple_box.sh` names this as its one cost. It was accepted because cfMesh takes exactly one shell
as its domain and five attempts to hand it plate-plus-chip as one shell all failed: two
concatenated-shell variants returned `chipCells 0`, and both OCC fuses died with no traceback. So
the fitted skin is not recoverable for a coupled mesh without solving that fuse, and `ws_fitted`,
which tried, was 6.2x slower for 19.8% fewer cells and never finished.

**What did not change either way:** the coolant walls and the chip contact are staircase cuts in
both meshes, because they are interior to the domain whatever surface bounds it. The +11.6%
channel area error is identical in both. Only the plate's outside differs.

### Phase 0c: find out whether losing the fitted skin cost anything

New, and it follows from the above. Measure the staircase area error on `wall_outer`, the plate's
outer surface, the same way the channels were measured: face centres and outward normals out of
the solid region's boundary index, against `block.stl`.

| | |
|---|---|
| effort | an hour, no meshing, on the sidecar we already hold |
| `wall_outer` faces in the delivered mesh | 615,176 |
| why it might not matter | the outer skin is where heat leaves to ambient, not where it crosses into the coolant, so its area error scales external loss rather than the chip-to-coolant path |
| why it might | if the deliverable includes case temperature or an external convection coefficient, an inflated outer area overstates how much heat escapes without going through the water |

If the error is small, the box domain is settled and nothing more is owed to it. If it is large,
the fuse problem is worth one more attempt, because the fitted skin is the only thing that fixes
it and we abandoned it on an uncontrolled comparison.

Run this alongside phase 0a and 0b. It changes nothing about phase 1.

## Added 2026-11-06 evening: phase 1 is built, and the gate was only half met

`cbox2b`. 506,362,044 cells, 3.00 h, Mesh OK on all four regions. Same surfaces, same octree
depth, same ranks and threads as the mesh we shipped. `maxCellSize` 352.056 to 322.020 um was the
only change I made.

### The gate, line by line

| gate | asked for | returned | |
|---|---|---|---|
| channel width | every gap exactly 5 cells, zero spread | 85.00% at exactly 5 cells | **not met** |
| non-orthogonality | must not move | 49.45 coolant, 50.38 plate | met |
| skewness | must not move | 1.657 coolant, 1.654 plate | met |
| interface faces | equal both sides | 25,685,941 both sides | met |
| cells | about 496M | 506,362,044 | met, 2.1% over |

I wrote "zero spread" and that was wrong. The reason is in the next two sections and it is worth
knowing, because it is the thing that limits any grid-aligned mesh of this part.

### What the re-mesh did buy

Measured on 791,205 channel gaps in the microchannel array, with the manifold and plenum channels
excluded by width band, on both meshes with the same script.

| the ~100 um channel array | the part | shipped `cbox2` | new `cbox2b` |
|---|---|---|---|
| median width | 100.04 um | 110.000 um | **100.630 um** |
| error on the median | | +9.96% | **+0.59%** |
| mean width | 100.7 um | 103.133 um | 102.716 um |
| standard deviation | | 12.986 um | **7.509 um** |
| coefficient of variation | 0.89% | 12.59% | **7.31%** |
| share at the right width | 93.7% | 55.39% | **85.00%** |
| widths the grid can produce | one population | 88.0 / 110.0 / 132.0 | 80.5 / 100.6 / 120.8 |
| flow spread, p95/p5 of w^3 | 1.09x | 3.375x | **1.730x** |

The three-way split is gone. The width is right to 0.6% instead of 10% wide, and the
channel-to-channel flow scatter is halved. That was the point of the run and it worked.

### Why 85% and not 100%, which is a property of the part

Commensurability fixes the width a channel is aiming at. It does not fix where the channel sits on
the grid. Each wall snaps to the nearest lattice plane independently, so a channel comes out at
5 cells only while both walls round the same way.

| | measured |
|---|---|
| channel width variation in the CAD | CV 0.89%, so 0.045 of a cell |
| pitch variation in the CAD | CV 0.46%, so 0.046 of a cell per pitch |
| tolerance to stay at 5 cells | half a cell across the pair of walls |
| distance of a meshed wall from a lattice plane | median 0.0016 cells |

Width variation alone is eleven standard deviations inside the tolerance, so it is not the cause.
The pitch wander is, because it moves the grid phase along a row of sixty pitches and the phase
only has to cross one half-plane for a channel to pick up or drop a cell.

Finer cells would help this, which is the one place in this whole plan where finer cells help: at
ten cells per channel the same wobble is half as large relative to the tolerance. The next even
pitch is 12 cells at 16.772 um, and that is 775M cells on the measured scaling, over the ceiling.
So 20.1263 um is the practical floor and 85% is what a grid-aligned mesh of this part gives.

### The residual is lopsided, and that part is fixable

| | cells per channel, mesh | cells per channel, CAD | bias |
|---|---|---|---|
| `cbox2` at 22.0035 um | 4.6871 | 4.5766 | +0.1105 cells, +2.43 um |
| `cbox2b` at 20.1263 um | 5.1036 | 5.0034 | +0.1002 cells, +2.02 um |

12.67% of channels come out a cell too wide against only 2.33% too narrow. Both meshes put the
mean channel the same tenth of a cell too wide, so this is not noise and not a function of cell
size. The mesh is claiming about 2 um of fin as coolant on every channel, the same amount each
time, which is what a one-sided inclusion test at the surface would do.

The coolant volume says the same thing independently, against the volume of the CAD shell itself:

| | coolant volume | against CAD |
|---|---|---|
| CAD shell, `clean_fluid.stl` | 53,692.22 mm3 | |
| `cbox2` | 53,419.40 mm3 | -0.51% |
| `cbox2b` | 55,102.60 mm3 | **+2.63%** |

So the re-mesh made the width right and the volume slightly worse. Both are measured and both
matter, the width for flow split and the volume for pressure drop and residence time.

**The lever.** Offsetting the coolant surface inward by about 1 um would move the mean onto 5.000
cells, collapse the 6-cell group and take the volume back toward zero error. It costs nothing to
mesh. I have not done it, because it changes how we represent the geometry rather than how we grid
it, and that is a decision rather than a fix.

### Full quality, both meshes

| region | cells `cbox2` | cells `cbox2b` | non-ortho max | skewness max | aspect max | Mesh OK |
|---|---|---|---|---|---|---|
| coolant | 68,806,800 | 82,529,515 | 49.31 to 49.45 | 1.651 to 1.657 | 3.738 to 3.708 | both |
| plate | 271,759,108 | 327,126,204 | 51.46 to 50.38 | 1.647 to 1.654 | 3.722 to 3.697 | both |
| chip | 24,173,140 | 31,012,960 | 0 to 0 | 0 to 0 | 1.0014 to 1.0014 | both |
| air | 48,534,275 | 65,693,365 | 54.19 to 50.12 | 1.435 to 1.437 | 5.957 to 5.427 | both |
| total | 413,273,323 | **506,362,044** | | | | |

Nothing got worse. The plate and the air both improved, which is the octree depth being unchanged
while the far field is now resolved slightly finer relative to the part.

### Three things I checked because they looked like defects, and are not

**The coolant is one piece.** `checkMesh` says three regions. `splitMeshRegions -detectOnly` says
82,529,513 cells, one cell, one cell. Two stranded cells in 82.5 million. The shipped mesh had a
clean one-piece coolant, so the two strays are new, and they are the only thing in this run that
is worse than what we shipped.

**It is not our surface.** The earlier four-piece coolant was traced to a two-piece `fluid.stl` of
ours. That does not apply here. `cb_fluid.stl` is byte for byte the verified clean surface: 52,466
triangles, one connected shell, closed on every edge, 53,692.2192 mm3. I checked rather than
assumed, because I had already assumed wrong about a surface once on this part.

**The chip being two regions is correct.** `cb_chip_fit.stl` is genuinely two closed bodies, 68 and
28 triangles, 3,307.59 and 12,987.26 mm3. The mesh split 6,388,480 and 24,624,480 cells, the same
ratio as the volumes. The shipped mesh did the same.

The partition itself closes: the plate envelope minus the coolant is 222,672.07 mm3 and the meshed
plate region is 222,527 mm3, 0.065% apart.

### Cell count scales as h^-2.28 on this part

| basis | predicted | actual | error |
|---|---|---|---|
| h^-2.038, measured `probe_edge` to `ws_fitted` | 496M | 506,362,044 | -2.1% |
| dx^-3, as [MESH_QUANTISATION.md](MESH_QUANTISATION.md) first wrote it | 540M | 506,362,044 | +6.6% |
| fitted on these two runs | | | h^-2.2795 |

Use 2.28 for this part from now on. It sits between a surface count and a volume count because
most of the cells are in banded refinement around a surface, not filling a volume.

### Timing, against the mesh we shipped

| stage | `cbox2` | `cbox2b` | ratio |
|---|---|---|---|
| cartesianMesh | 652 s | 799 s | 1.23 |
| topoSet | 467 s | 575 s | 1.23 |
| splitMeshRegions | 817 s | 869 s | 1.06 |
| checkMesh in parallel, four regions | 515 s | 621 s | 1.21 |
| reconstruct, three regions | 4,179 s | 4,857 s | 1.16 |
| checkMesh in serial, three regions | 2,728 s | 3,075 s | 1.13 |
| **total** | **9,359 s, 2.60 h** | **10,796 s, 3.00 h** | **1.15** |

22.5% more cells for 15% more time. The serial tail is still the whole problem: reconstruct plus
serial checkMesh is 7,932 s of the 10,796, which is 73%. Meshing is 7.4%.

### What this leaves open

- The 1 um surface offset above, which is a geometry decision and needs a call.
- Phase 0a and 0c are untouched and still free.
- Phase 2a, the one-hour layer gate, now has a mesh to carve its subsets out of.
