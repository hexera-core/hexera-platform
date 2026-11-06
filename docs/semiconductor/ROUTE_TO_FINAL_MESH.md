# The route to the final mesh

Internal. Everything planned between the mesh we have and the mesh we want, in the order it has to
happen, with what each step costs and the gate that decides whether to take the next one.

Boundary layers are in this plan. They are phase 2, not phase 1, and the reason is a cancellation
rather than a doubt about whether they work. That reason is in phase 2.

## What "final" means

The mesh I am aiming at, stated as checks rather than adjectives:

| | shipped (`cbox2`) | target |
|---|---|---|
| cells across every channel | 4 or 5 or 6 | 85% at exactly 5, measured; see the phase 1 result |
| flow spread between identical channels | 3.38x | 1.730x measured, not 1.0x |
| first cell centre off the wall | 11.0 um shipped, 10.06 um after phase 1 | 1 to 2 um |
| wetted area error from staircasing | +8.29% overall measured, +11.6% channels | corrected or removed |
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
the shipped dict flags every face of a perfect mesh.

**DONE 6 November, and the premise was half wrong.** `couple_box.sh` writes no `meshQualityDict` and
never passes `-meshQuality`, so the reports we already held never read that value and were not
worthless. The corrected run is in the phase 0b section at the end of this document: zero faces in
error on all nine criteria, all three regions Mesh OK. The value still matters for `snappyHexMesh`,
so this remains a real precondition for phase 2.

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
| cost | **stale, see the corrected layer budget at the end of this document.** +51.4M / +77.1M / +102.7M at 2 / 3 / 4 layers, so 557.7M / 583.4M / 609.1M |
| first cell centre | 10.06 um on the phase-1 mesh, to 1 to 2 um |
| memory | 298 GB at 4 layers, 257 GB at 3, 216 GB at 2, at 1.61 kB per output cell. The box we have is 755 GB |
| time | about 2.0 h serial, extrapolated from the 2a gate. Any parallel figure is unmeasured |
| what it costs us | the "zero faces over 50 degrees" line. Non-orthogonality goes to about 49 to 53 on a staircased interface |

**Fluid side only, not both.** Both sides at 4 layers is +205.5M and would put us at 711.8M, on
the 2^31 face ceiling itself. Physics agrees: the fluid-side Dirichlet weight is 0.99850, so the solid contributes
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
| per-channel flow scatter | 3.38x shipped, 1.730x measured after phase 1 |
| staircase wetted area | +8.29% overall measured, +11.6% channels |
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
| phase 1, as built | 506,362,044 | yes |
| phase 1 + 2 layers, fluid side | 557.7M | yes |
| phase 1 + 3 layers, fluid side | 583.4M | yes |
| phase 1 + 4 layers, fluid side | 609.1M | **no**, over the 600M line |
| phase 1 + 4 layers, both sides | 711.8M | **no**, and on the 2^31 face ceiling |
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

**Answered 6 November, +2.97% on the delivered mesh. The result is at the end of this document.**

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

## Added 2026-11-06 evening: phase 1 is built, phase 0b and 0c are done, and the layer gate passes

Three things landed on 6 November and one audit overturned part of my own write-up of the first. In
order: the re-mesh, what the gate asked for against what it returned, the cause corrected, phases 0b
and 0c, and the layer gate.

### The re-mesh

`cbox2b`. 506,362,044 cells, 3.00 h, Mesh OK on all four regions, and now also zero faces in error
on all nine `-meshQuality` criteria. Same surfaces, same octree depth, same ranks and threads as the
mesh we shipped. `maxCellSize` 352.056 to 322.020 um was the only change I made.

I did **not** tighten the chip `refinementThickness`, deliberately: the pitch fix is the proven
change and the chip trim's thermal cost is unmeasured, so I changed one variable.

### The gate, line by line

| gate | asked for | returned | |
|---|---|---|---|
| channel width | every gap exactly 5 cells, zero spread | 85.00% at exactly 5 cells | **not met** |
| non-orthogonality | must not move | 49.45 coolant, 50.38 plate | met |
| skewness | must not move | 1.657 coolant, 1.654 plate | met |
| interface faces | equal both sides | 25,685,941 both sides | met |
| cells | about 496M | 506,362,044 | met, 2.1% over |

### What the re-mesh bought

Measured on 791,205 channel gaps in the microchannel array, with the manifold and plenum channels
excluded by width band, on both meshes with the same script.

| the ~100 um channel array | the part | shipped `cbox2` | new `cbox2b` |
|---|---|---|---|
| median width | 100.04 um | 110.000 um | **100.630 um** |
| error on the median | | +9.96% | **+0.59%** |
| mean width | 100.7 um | 103.133 um | 102.716 um |
| standard deviation | | 12.986 um | **7.509 um** |
| coefficient of variation | 0.89% | 12.59% | **7.31%** |
| share at the modal lattice width | 93.7% lies in 96 to 104 um | 55.39%, and nothing in 96 to 104 um | **85.00%** |
| widths the grid produced | one population | 88.0 / 110.0 / 132.0 | 80.5 / 100.6 / 120.8 |
| flow spread, p95/p5 of w^3 | 1.09x | 3.375x | **1.730x** |

The three-way split is gone. The width is right to 0.6% instead of 10% wide, and the
channel-to-channel flow scatter is halved. That was the point of the run and it worked.

The two bands are not quite the same measurement, and the comparison is slightly unfair to `cbox2b`
in a way worth stating: `cbox2`'s band is 70 to 145 um and `cbox2b`'s is 70 to 132 um, so a real
135 um channel is inside one and outside the other. The sample sizes differ for the same reason,
538,564 against 791,205, so "791,205 gaps on both meshes" would be wrong.

### Why 85% and not 100%, corrected after an audit

**I first wrote that the grid phase causes this, and that is impossible.** If a gap is exactly n
cells wide then both walls sit at the same phase, so `round(p + n) - round(p) = n` for every p:
sliding a commensurate channel along the lattice cannot change its cell count. Checked analytically
and by Monte Carlo at the measured pitch CV, which gives 0.0000% miscounts. The claim is withdrawn.

The cause is the width the mesher sees, which is the 2 um bias in the next section. I had measured
that in the same write-up and was treating it as an independent effect.

| | |
|---|---|
| mesh mean channel | 5.1036 cells |
| what that mean alone predicts | 89.64% at 5 cells, 10.36% at 6, **0% at 4** |
| measured | 85.00% at 5, 12.67% at 6, 2.33% at 4 |
| the excess over that | +2.31 points at 6 and +2.33 at 4: symmetric, mean-preserving |

The bias fixes the split between two adjacent counts and nothing more; a third group can only come
from width dispersion, which on the meshed walls is about 3.1 um, 3.5x the CAD's own 0.89 um. With
only three counts present there is an exact identity, `P(n+1) - P(n-1) = mean cells - n`: measured
12.6733 - 2.3292 = 10.344 points against 5.1036 - 5 = 0.1036 cells. So the off-target share can
never be below `|mean width / dx - n|`, with no model at all.

### Finer cells make this worse, which is the opposite of what I wrote

The bias is a fixed length, so in cell units it grows as the cells shrink.

| | dx | channel | floor on the off-target share | modelled at exactly n cells |
|---|---|---|---|---|
| pitch 8 | 25.1578 um | 4 cells | >= 8.28% | about 88% |
| **pitch 10, built** | **20.1263 um** | **5 cells** | **>= 10.36%** | **85.00% measured** |
| pitch 12 | 16.7719 um | 6 cells | >= 12.43% | about 82% |
| ten cells per channel | 10.0631 um | 10 cells | >= 20.72% | about 70% |

Per-pitch phase wander says the same: 0.046 of a cell today, 0.055 at pitch 12, 0.092 at ten cells
per channel, against a half-cell tolerance that shrinks with the cell. Twice as large, not half.

**So 85% is not a property of the part and 20.1263 um is not a floor it sits on.** 85% is a property
of this surface representation, and the free 1 um offset below would put it at roughly 88 to 96%.

**The coarser commensurate row is better on this measure.** pitch 8 at 25.1578 um gives the same
100.631 um channel, since 4 x 25.1578 = 5 x 20.12625, and projects to about 88% at exactly 4 cells
for about 276M cells rather than 506M. Not a free win: at 4.08 cells across instead of 5.10 the
2/N^2 flow-rate error grows from +7.7% to +12.0%. That is the trade, and I had called pitch 10 the
better deliverable on reasoning that was wrong.

### The residual is one-sided, and that part is not the grid

| | mesh | CAD mean | bias |
|---|---|---|---|
| `cbox2` at 22.0035 um | 4.6871 cells | 4.5766 | +0.1105 cells, +2.43 um |
| `cbox2b` at 20.1263 um | 5.1036 cells | 5.0034 | +0.1002 cells, +2.02 um |

Two points cannot establish that this is independent of cell size, and they are 10% apart in cells
and 21% apart in microns, so the cell-invariant reading fits marginally better. Against the CAD
median rather than its mean the bias is larger, +0.1330 and +0.1406 cells.

It is **not** a one-sided cell-inclusion test, which I had offered as the mechanism: that would bias
the gap by a whole cell, ten times what is seen, and would put the channel mass at 6 or 7 cells. The
signature is a sub-cell outward offset of the coolant surface, about 1.0 to 1.2 um per wall. Nor is
it "2 um on every channel": 85% of channels are 0.59 um out, 12.67% are 20.1 um out and 2.33% are
20.1 um under. The 2 um is a population mean only.

### What the volume and the area do NOT corroborate

I first wrote that the coolant volume and the wetted area confirm the width bias independently.
**Withdrawn on both counts.**

| | coolant volume | against the CAD shell at 53,692.22 mm3 |
|---|---|---|
| `cbox2` | 53,419.40 mm3 | -0.51% |
| `cbox2b` | 55,102.60 mm3 | +2.63% |

The array is at most 5.7% of the coolant volume, so a 2% width bias accounts for at most 0.11 of
2.63 points, 23x short. The signs run the wrong way too: `cbox2` has the larger over-width and a
volume deficit, and between the two meshes the median width fell 8.5% while the volume rose 3.1%.
**So the +2.63% coolant volume is a separate defect that nothing here explains, and it is carried as
one.** The wetted area is the same story: the bias accounts for about 0.13 of 8.3 points and gets
the two meshes in the wrong order. That excess is the staircase.

**The 1 um lever is real but smaller than I claimed.** It moves the mean by about half the residual,
so it shrinks the 6-cell group rather than collapsing it, and takes roughly 0.1 of the 2.63 points
off the volume. It still costs nothing to mesh and is still a geometry decision.

### Quality, both meshes

| region | cells `cbox2` | cells `cbox2b` | non-ortho max | skewness max | aspect max |
|---|---|---|---|---|---|
| coolant | 68,806,800 | 82,529,515 | 49.31 to 49.45 | 1.65072 to 1.65686 | 3.738 to 3.708 |
| plate | 271,759,108 | 327,126,204 | 51.46 to 50.38 | 1.64676 to 1.65373 | 3.722 to 3.697 |
| chip | 24,173,140 | 31,012,960 | 0 to 0 | ~0 to ~0 | 1.00136 to 1.00137 |
| air | 48,534,275 | 65,693,365 | 54.19 to 50.12 | 1.43496 to 1.43734 | 5.957 to 5.427 |
| total | 413,273,323 | **506,362,044** | | | |

"Nothing got worse" was too strong. Four of the twelve maxima moved the wrong way, all by under
0.5%: coolant non-orthogonality +0.28%, coolant skewness +0.37%, plate skewness +0.42%, air skewness
+0.17%. Plate mean non-orthogonality rose 3.26%, the largest move in the wrong direction. The
non-orthogonality maxima improved on the plate by 2.1% and the air by 7.5%, and three of the four
aspect maxima improved. **Nothing moved enough to matter** is the honest summary.

### Three apparent defects, resolved, and one that was not a defect at all

**The coolant is one piece.** `checkMesh` says three regions. `splitMeshRegions -detectOnly` says
82,529,513 cells, one cell, one cell. Two stranded cells in 82.5 million, 2.4e-8 of the region. The
shipped mesh had a clean one-piece coolant, so these two strays are the only **topological**
regression; the volume and area biases above also moved the wrong way, so they are not the only
regression of any kind, which is what I first wrote.

**The outer coolant surface is clean, the fine one is not.** `cb_fluid.stl` is identical to the
verified `clean_fluid.stl` on every measured figure and by `md5sum`, so that part is settled:
52,466 triangles, one connected shell, closed on every edge, 53,692.2192 mm3. But the OTHER coolant
surface handed to cfMesh, `cb_fluid_fine.stl`, is **open on 91 triangles and has a negative signed
volume**, so its winding is inconsistent too. I measured that in the same sweep and did not flag it.
It is a refinement surface rather than a region boundary, so it does not have to be closed for
cfMesh to work, but "it is not our surface" was too broad a statement and is narrowed to the outer
surface only.

**The chip being two regions is correct.** `cb_chip_fit.stl` is genuinely two closed bodies, 68 and
28 triangles, 3,307.59 and 12,987.26 mm3. The mesh split 6,388,480 and 24,624,480 cells, which is
20.60% of the cells against 20.30% of the volume: close to the volume ratio, 1.87% apart, not "the
same ratio" as I first wrote. The two bodies read +0.78% and -1.07% against their STLs, opposite
signs, so it is two small independent errors rather than one scale factor.

**The ports needed splitting, and now are.** The shipped mesh carries `port_a` (1,759 faces) and
`port_b` (1,706 faces) as separate patches of type `patch`, while `cbox2b` came out of the chain
with a single `fluid_to_air` of 4,140. `cbox2`'s own RESULT.txt records that its run also produced
one `fluid_to_air`, of 3,465 faces, exactly 1,759 + 1,706, so this was a post-processing step
applied there and not here: outstanding work rather than a regression. **Done**, and the result is
in the port-split section at the end of this document.

The partition closes, but less tightly than I first claimed. `block.stl` minus the CAD coolant is
222,672.07 mm3 against a meshed plate of 222,527 mm3, 0.065% apart, and that particular pairing
holds only on `cbox2b`. Comparing like with like, meshed plate plus meshed coolant against the
envelope, it is +0.46% on `cbox2b` and -0.32% on `cbox2`. **The partition closes to about half a
percent**, which is 7x looser than the figure I quoted.

### Cell count scales as h^-2.278 on this part

| basis | predicted | actual | error |
|---|---|---|---|
| h^-2.038, measured `probe_edge` to `ws_fitted` | 496M | 506,362,044 | -2.1% |
| dx^-3, as [MESH_QUANTISATION.md](MESH_QUANTISATION.md) first wrote it | 540M | 506,362,044 | +6.6% |
| fitted on these two runs | | | h^-2.27798 |

Use 2.28 for this part. It sits between a surface count and a volume count because most of the cells
are in banded refinement around a surface rather than filling a volume. pitch 12 at 16.772 um is
767M cells on that exponent, not the 775M I first wrote.

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
serial checkMesh is 7,932 s of 10,796, which is 73%. Meshing is 7.4%.

---

## Phase 0b is done

`couple_box.sh` never writes a `meshQualityDict` and runs bare `checkMesh` with no `-meshQuality`
flag, so **the reports we already held were never built with the broken `minVol 1e-13`** and were
not worthless. RAW confirms it independently: minimum cell volumes of 2.80523e-14 and 7.30955e-15
are already far below a 1e-13 bar, yet every region reported Mesh OK, which is impossible if that
bar had been applied.

I wrote a corrected dict with `minVol 1e-20` and `minTetQuality` left where it ships, and ran
`checkMesh -meshQuality` on all three reconstructed regions. **Zero faces in error on every one of
the nine criteria, all three Mesh OK.**

| criterion | coolant | plate | chip |
|---|---|---|---|
| non-orthogonality > 65 deg | 0 | 0 | 0 |
| face pyramid volume < 1e-20 | 0 | 0 | 0 |
| tet quality < 1e-30 | 0 | 0 | 0 |
| concavity > 80 deg | 0 | 0 | 0 |
| skewness > 4 internal, 20 boundary | 0 | 0 | 0 |
| interpolation weights < 0.02 | 0 | 0 | 0 |
| volume ratio < 0.01 | 0 | 0 | 0 |
| face twist < 0.02 | 0 | 0 | 0 |
| determinant < 0.001 | 0 | 0 | 0 |

The `minVol` value still matters for `snappyHexMesh` layer addition, which is the genuine phase 2
precondition, so the "blocks phase 2" relation stands even though the "half a day of outstanding
work" did not.

---

## Phase 0c is done: losing the fitted outer skin costs about 3%

Measured off both coupled meshes' own sidecars against `block.stl`, the surface the plate-only mesh
fitted. Comparing full envelope to full envelope, so that no port-cap area has to be estimated: the
mesh's outer envelope is solid_to_air plus solid_to_chip plus the coolant's own port mouths, and the
CAD's is `block.stl` entire.

| area | CAD | `cbox2` | `cbox2b` |
|---|---|---|---|
| plate outer envelope | 47,955.416 mm2 | 49,377.540 mm2, **+2.97%** | 49,653.512 mm2, **+3.54%** |
| coolant envelope | 60,410.451 mm2 | 65,421.376 mm2, **+8.29%** | 65,518.632 mm2, **+8.46%** |
| chip envelope | 17,863.558 mm2 | 17,892.140 mm2, +0.16% | 17,861.246 mm2, **-0.013%** |
| chip to plate contact alone | 2,744.50 mm2 | 2,735.930 mm2, -0.31% | 2,748.484 mm2, +0.15% |
| the port mouths themselves | | 107.457 mm2 | 107.410 mm2 |

**The answer.** About 3% of outer area, and it only feeds the external convection term, which is
small here beside the coolant. The fitted skin was worth having and is not worth a box-domain
redesign to get back.

Four other things fall out of the same measurement.

The coolant wetted-area inflation is now measured on the delivered mesh rather than inferred:
**+8.29%**, against the +7.8% [MESH_QUALITY_ASSESSMENT.md](MESH_QUALITY_ASSESSMENT.md) carried. That
+7.8% was computed against the broken two-piece `fluid.stl`, whose cut-closure faces add 177.6 mm2
of surface that is not wetted at all, so it flattered us. It is +8.46% on the new mesh, 0.17 points
worse, and that change is the staircase rather than the width bias.

**The chip envelope is right to 0.013%** on the new mesh. The chip is two axis-aligned boxes, so it
has no staircase at all: a flat axis-aligned surface staircases to nothing. That is the cleanest
demonstration in the whole set of what the staircase is and is not.

The chip-to-plate contact moved from -0.31% to +0.15%. Both are inside half a cell of perimeter
phase, 0.46 and 0.24 of a cell, so the sign flip is phase rather than an improvement the finer mesh
earned. "Agrees within half a cell on both meshes" is the right claim.

And both meshes carry about 0.19% non-quad boundary faces, 45,097 and 52,271, at the refinement
transitions. Any area measurement here needs a general polygon formula; my first attempt assumed
quads and also read one past the offsets array.

Method: `devtools/semiconductor/skin_area.py`, exact for planar polygons by the 3D shoelace.

---

## Phase 2a is done: the layer gate PASSES

Two subsets of `cbox2b`'s coolant region, carved with `topoSet` and `subsetMesh`, then
`snappyHexMesh` as a pure layer adder keyed on `fluid_to_solid`: 3 layers, expansion ratio 1.5,
`firstLayerThickness` 4.63e-06, `maxThicknessToMedialRatio` 0.6, `minVol 1e-20`.

| | A, entirely in the fine zone | B, straddling the refinement cliff |
|---|---|---|
| cells in | 1,616,182 | 738,470 |
| layer patch | 495,965 faces | 176,211 faces |
| **faces extruded** | **495,055 = 99.82%** | **158,050 = 89.69%** |
| cells added | 1,298,207 of 1,487,895 = 87.25% | 435,609 of 528,633 = 82.40% |
| layers achieved of 3 | 2.62 | 2.47 |
| overall thickness | 1.85e-05 m = 84.1% of target | 1.76e-05 m = 80.1% |
| faces in error | **0, "finished without any errors"** | 101 |
| hexahedra | 2,827,271 | 1,142,707 |
| polyhedra | 87,118 = 2.99% | 31,372 = 2.67% |
| prisms | 0 | 0 |
| non-orthogonality max | 53.13 | 64.56 |
| skewness max | 1.711 | 2.878 |
| aspect max | 46.32 | 78.31 |
| checkMesh | Mesh OK | Mesh OK |
| interface faces | 495,965, unchanged | 176,211, unchanged |
| wall clock | 136 s | 465 s |

**The kill condition was under 60% coverage in the fine zone. It returned 99.82%.** The route is
alive.

**And the research's fatal case did not reproduce.** It measured a 2:1 refinement transition on the
layer patch collapsing coverage to 2.96%, with three rescue attempts going to 0%. The straddling
subset here got 89.69%. The shortfall, 176,211 - 158,050 = 18,161 faces, is of the same order as the
coarse faces in that window, so the mechanism is real; it just costs about 10% here instead of
everything, because on this part the coarse faces are 4.37% of the interface and are not interleaved
with the fine ones across the whole patch.

What the gate does NOT say: coverage is counted in faces, and the cell count tells a softer story.
87.25% of the requested cells appeared and the stack reached 2.62 of 3 layers at 84.1% of the target
thickness, because the medial-axis limiter thins the stack wherever the channel is locally narrow.
So "100% coverage, all hex" is not what this part gives even in its clean zone: 2.99% of cells come
out as polyhedra. That is still a usable mesh and `checkMesh` says Mesh OK with zero faces in error.

**Where the cliff actually is.** The interface is 95.63% at the finest cell. Classified by shortest
edge, not by area, because `scaleY 3` makes the cells 20.102 x 60.400 x 20.102 um and area alone
reads that 3:1 as a refinement level:

| shortest edge | faces | share |
|---|---|---|
| 20.102 um | 24,564,561 | 95.63% |
| 40.301 um | 43,145 | 0.17% |
| 80.500 um | 171,323 | 0.67% |
| 161.000 um | 906,912 | 3.53% |

Every coarse face centroid is at y >= 24.092 mm. **That does not separate the two sets**, which is
what I first said: the fine faces run up to y = 33.494 mm, so the ranges overlap over 9.4 mm, and no
y plane separates them. What a cut at y = 24.092 does give is one-sided containment: the slab below
it is coarse-face-free and is therefore a usable transition-free fine zone, at the cost of
discarding at least 1,228,228 fine faces, 5.0% of the fine population. And because these are
centroids, a 161 um coarse face centred at 24.092 reaches 80 um below the line, so even that needs a
margin. Splitting the interface by cell level in `topoSet` is the robust form, not a plane.

### The layer budget, corrected

Every cost figure in phase 2b below was computed on `cbox2`'s 21,708,529-face interface and is stale
now that the interface is 25,685,941 faces. At the measured 1 added cell per extruded face per
layer:

| | added cells | total | under 600M |
|---|---|---|---|
| 2 layers, fluid side | +51.4M | 557.7M | yes |
| 3 layers, fluid side | +77.1M | 583.4M | yes |
| 4 layers, fluid side | +102.7M | **609.1M** | **no** |
| 4 layers, both sides | +205.5M | 711.8M | no, and on the 2^31 face ceiling |

So 4 fluid-side layers now **fail** the plan's own 600M line, where the table below marks them "yes,
tight". 3 layers is the most that fits. Peak memory at 1.61 kB per output cell is 298 GB at 4 layers,
257 GB at 3 and 216 GB at 2, not the 251 GB below, and all three fit this box with room.

**The face-count argument I made for 2 to 3 layers was wrong.** I said 4 layers would reach 1.84
billion faces, 86% of 2^31. That sums four separate post-split meshes, each of which has its own
32-bit label space and none of which is ever recombined. The genuine single-mesh loads are the
pre-split mesh at 1.53 billion faces, 71.3% of 2^31, which has already been built and split and does
not depend on layer count at all; the layered coolant region alone at 0.59 billion, 27.5%; and the
plate at 1.00 billion, 46.7%, untouched by fluid-side layers. The same objection voids the "70% of
2^31" in [RESEARCH_BOUNDARY_LAYERS.md](RESEARCH_BOUNDARY_LAYERS.md). Keep 3 layers, but on the cell
budget, not on label size.

Timing: the 1 to 2 h on 8 to 16 ranks below is unmeasured. The serial extrapolation at this
interface size is 2.9 h at 3 layers. The gate ran 495,965 faces in 136 s, which extrapolates to
about 2.0 h serial for 25,685,941 faces at 3 layers, so the serial figure is the one to plan on
until a parallel run is measured.

---

## The audit that produced most of the corrections above

Six auditors re-derived every quantitative claim in these documents from the raw tool output, and
each finding was then handed to an independent agent told to refute it. 411 claims checked, 316
reproduced, 76 findings raised, 45 survived refutation and 31 were themselves wrong. The
substantive survivors are folded in above and in the other documents. The pattern worth keeping:
every single one of the five biggest errors was a case of my stating a mechanism that sounded right
rather than deriving it, while the measurements themselves almost all reproduced.

---

## The ports are split, so the mesh can carry a flow boundary

Done 6 November, after the sidecar upload. `cbox2b` came out of the chain with a single
`fluid_to_air` patch of 4,140 faces and type `mappedWall`, which is a conjugate interface and not
something a solver can put an inlet on. The shipped mesh carries two patches of type `patch`
instead, so this was outstanding work rather than a design question.

| | shipped `cbox2` | `cbox2b` before | `cbox2b` now | ratio to shipped |
|---|---|---|---|---|
| `port_a` | 1,759 faces | | **2,092** | 1.189 |
| `port_b` | 1,706 faces | | **2,048** | 1.200 |
| `fluid_to_air` | | 4,140, `mappedWall` | removed as zero-sized | |
| sum | 3,465 | 4,140 | **4,140** | |

Both ratios land on 1.195, which is `(22.0035 / 20.1263)^2` to three digits: a port mouth is a
fixed area and the faces on it are a cell smaller each way, so that is the number it had to be.

**Where the cut went, and it is measured rather than guessed.** Off `cbox2`'s own index, `port_a`
spans z 73.507 to 81.252 mm and `port_b` z 39.357 to 47.279 mm, completely disjoint, while
`cbox2b`'s merged patch spans 39.414 to 81.277. So a plane at z = 60 mm separates them with 13 mm
of clearance either side. `topoSet` carved 2,092 and 2,048 faces out of the 4,140 and `createPatch`
made them patches, reporting "Removed zero-sized patch fluid_to_air".

| stage | seconds |
|---|---|
| copy the region into its own case | 7 |
| topoSet, two face sets | 368 |
| createPatch | 703 |
| checkMesh | 645 |
| rebuild the boundary index | 165 |
| rebuild the quality sidecar | 43 |

Done on a copy, and swapped in only after `checkMesh` returned Mesh OK and the two counts summed to
exactly 4,140. The mesh is on a self-deleting disk and the archive policy here is sidecars rather
than meshes, so a failed in-place `createPatch` would have cost the whole three-hour run. Quality
after the split is identical to before it, as it must be, since `createPatch` moves faces between
patches and does not touch a point: non-orthogonality 49.4505, skewness 1.65686, aspect 3.70845.

**One thing this leaves.** The air region still carries an `air_to_fluid` patch naming a patch that
no longer exists on the coolant side. The shipped mesh has the same loose end, and the air region is
only ever decomposed and is not part of the three-region deliverable, so this is parity rather than
a new defect. Worth closing if the air region is ever reconstructed.

**And it does not settle which port is which.** `port_a` is the OUTLET and `port_b` the INLET, from
the customer's own STEP naming, and that assignment is unchanged and still the thing not to get
backwards.
