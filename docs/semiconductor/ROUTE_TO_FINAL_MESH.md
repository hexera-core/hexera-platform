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
| cells per channel | 4 / 5 / 6 | exactly 5 |
| channel width in the mesh | 88.014 / 110.017 / 132.021 um | 100.631 um, +0.6% |
| flow spread | 3.38x | none |
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

**Gate.** Re-run the channel histogram. Every gap must be exactly 5 cells with zero spread.
Non-orthogonality and skewness must not move, because the octree depth has not changed. Interface
face counts must be equal on both sides.

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
