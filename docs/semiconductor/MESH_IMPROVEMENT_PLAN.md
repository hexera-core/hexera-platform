# What to change about the coupled mesh, in order

Written 7 November 2026 off the measurements in [MESH_QUALITY_CBOX2B.md](MESH_QUALITY_CBOX2B.md) and
[MESH_TIME_TO_ONE_HOUR.md](MESH_TIME_TO_ONE_HOUR.md). Ranked by value, not by effort.

Each row says whether the gain is measured or projected. Everything on it is now measured one way or
the other, and **two of the items I originally ranked in the top five turned out to be refuted** once
I measured instead of reasoning: the grid-origin shift, and coarsening the solid. Both are kept here
with the measurement that killed them, because they are the obvious ideas and the next person will
have them too.

| # | change | time | accuracy | basis |
|---|---|---|---|---|
| 1 | drop the boundary layer pass | **-5,843 s** | **a trade, not a win**: fine on mean Nu below Re 152, worse on every local metric at every Re | measured, **corrected** |
| 2 | deliver decomposed, drop reconstructs and serial checks | **-12,377 s** | none | measured |
| 3 | retype `mappedWall` to `wall` on a check-time copy | +14 s | enables 9 checks never run before | measured |
| 4 | ~~coarsen the solid away from the interface~~ | large | **nothing** | **refuted** |
| 5 | ~~align the grid origin to the channel array~~ | 0 s | **nothing** | **refuted** |
| 6 | `writeFormat binary` in the mesher | -118 s per read | none | measured |
| 7 | repair `fluid_fine.stl` | 0 s | where refinement lands | measured defect |
| 8 | one more refinement level in the channels | very large | flow +8.0% to +2.0%, cuts the 43-90% entrance error, **and the only fix for the quantisation** | blocked |

---

## 1. Drop the boundary layer pass: a trade, not the free win I claimed

**Corrected.** The earlier version of this item said dropping the layers saves 5,843 s *and* moves
Nusselt from -4.02% to +2.18%, a 6.2 point improvement, so it cost nothing. Both figures were wrong
and the accuracy half reverses. MESH_QUALITY_CBOX2B.md section 7 has the full correction, the three
verification checks, and the reason: the wall-gradient exactness argument is a momentum result and
the energy equation has a quartic solution, not a parabolic one, so it does not transfer.

The time saving is real and unchanged at **5,843 s**, and sub-one-hour is only reachable without the
layers. What changed is the price:

| metric | crossover, plain mesh against the layer stack as built |
|---|---|
| mean Nusselt over the 12.8 mm run | **Re 152** |
| length-weighted RMS local error | **Re 23** |
| **worst local error** | **none in 1 to 1e6; layers win at every Reynolds number** |

Fully developed, the plain mesh is **+0.54%** and the layered variants -0.73% to -1.54%, so the
plain mesh is the better of them but by under a point, not by six. Developing, the plain mesh's
worst local error runs from **42.9% at Re 10 to 90.0% at Re 2000**, against 2.1% to 44.3% for the
layered one. Mean Nusselt hides this because a coarse mesh under-predicts in the entrance and
over-predicts downstream: at Re 1000 it is -54.1% at 0.05 mm and +8.5% at 3 mm, averaging to +4.7%.

**So this is now a judgement with a price attached rather than a free saving.** Drop the layers to
reach the hour and accept an entrance-region error the mean does not show; keep them at 2.38 h if
local wall temperature is what the customer is buying. Wall shear is exact either way, and that part
was never in doubt because it is a momentum result.

**And fix coverage before adding layers.** The pass achieved 2.36 of 3 layers and 1.67e-05 m of
2.2e-05 m, which is 75.9%, at 90.71% coverage. The uncovered **9.29% of the interface is plain
5-cell mesh carrying the plain mesh's 43 to 90% local error**, so the worst case for the patch as a
whole does not improve until that fraction does, however good the other 90.71% is.

If layers are kept, keep them parallel: 16 ranks ran 11.0x faster than serial for 0.94 points more
coverage, and the illegal face count rose to 23,833 of 446,465,029, which is 0.053 ppm.

## 2. Deliver the mesh decomposed

**Measured.** Saves 12,377 s, 59% of the whole run, and costs nothing at all.

| dropped | s |
|---|---|
| three `reconstructParMesh` calls | 6,497 |
| three serial `checkMesh` calls | 5,583 |
| `checkPar` on the dropped air region | 129 |
| `redistributePar -reconstruct`, rc=1 every time | 121 |
| copy before layering | 47 |

A solver wants the decomposed mesh. The current chain reconstructs it, then the layer step decomposes
it again, then reconstructs it again. The serial `checkMesh` is the same binary and the same nine
criteria as the `checkMesh -parallel` that already passed, at 3,075 s against 621 s.

One thing to carry over: the serial pass is what currently writes the quality fields the boundary
sidecars are built from. The parallel pass has to write them instead, or the viewer loses its input.

## 3. Retype `mappedWall` to `wall` on a COPY before checking

**Measured.** Costs 14 s on a 610 s check, 2.3%.

**On a copy, and never on the delivered mesh.** `chtMultiRegionFoam` needs `mappedWall` on both
sides of the conjugate interface to couple the regions at all, so retyping the mesh we ship would
silently decouple the solve. The retype is a check-time transformation of a throwaway copy of the
boundary file, which is why it costs seconds.

`checkMesh -allGeometry` has FOAM-fatalled on every conjugate mesh we have ever built, so `mesh_ok`
has never been true and nine geometry checks have never run on any mesh we have shipped. Two arms on
a copy of the chip region:

| arm | change | result |
|---|---|---|
| A | none | rc=1 after 610 s |
| B | `chip_to_solid`: `mappedWall` to `wall` | **rc=0 after 624 s, `Mesh OK`** |

The cause is that checking one region in isolation cannot resolve a `mappedWall` patch's
`sampleRegion`, because the other region is not loaded. Nothing about the mesh is wrong.

I had previously guessed the dead `air` region was responsible. **That was refuted by arm A.**

Arm B passes everything the flag adds: patch topology ok on both patches, face interpolation weight
check OK, face volume ratio minimum 0.998759, average 0.999859.

## 4. Coarsen the solid away from the interface: REFUTED

I ranked this the largest single lever on the premise that the solid sits at 20.1263 um because the
coolant interface needs to and nowhere else. **That premise is wrong. The solid is already graded
exactly like the fluid.**

| region | cells | share | min cell vol m3 | max cell vol m3 | total vol m3 | **mean cell vol m3** |
|---|---|---|---|---|---|---|
| solid | 327,126,204 | 74.2% | 7.30955e-15 | 1.10727e-10 | 2.22527e-04 | **6.803e-13** |
| fluid | 82,529,515 | 18.7% | 2.09433e-14 | 1.11352e-10 | 5.51026e-05 | **6.676e-13** |
| chip | 31,012,960 | 7.0% | 5.21595e-13 | 5.23607e-13 | 1.61816e-05 | 5.218e-13 |

Same coarsest cell to within 0.6%, same mean cell to within 1.9%, spanning the same four levels of
refinement. The solid is 327M cells for exactly one reason: **it is 4.04x the volume of the coolant
at the same average refinement**, and 82,529,515 x 4.039 = 333M against 327M measured. There is no
over-refined bulk to find.

And the part that is fine cannot be coarsened. The channel walls ARE the conjugate interface, so the
metal between channels is pinned to the coolant side's resolution by conformity. Coarsening the fins
means coarsening the channels.

**So there is no large cell-budget lever here, and the 440.7M live cells are close to what 5 cells
across a channel costs on this geometry.** The two things that remain are both small or structural:

- **The chip is uniform and could drop a level.** Its min and max cell volumes differ by 0.4%, so it
  is a single level throughout, for pure conduction in silicon that only needs detail at its 424,080
  face contact with the plate. One level coarser is 8x fewer cells, saving about 27M, which is 6.1%
  of the mesh. Worth doing, not worth celebrating.
- **The only structural answer is not an octree at all.** The research sweep proved a Gmsh 2.5D swept
  route on the real dimensions including a branching T junction: 2.0 um first cells, conformal by
  construction, zero tets, 4.6x lighter per mm2, and it kills the staircase outright rather than
  mitigating it. `gmshToFoam` is ASCII MSH2 only and serial, which is the obstacle.

The caution from the record stands and is now better evidenced. The far field rests on three points
spanning a 4x range, 269,516,428 then 267,282,301 then 267,133,252 cells at 1x, 2x and 4x background
cell size with the channel resolution held fixed. Read as a whole that is -0.88% for 4x coarser, and
the useful reading is the decomposition: **the first doubling takes 0.83 of the 0.88 points and the
second takes 0.06**, so it saturates on the first step rather than declining steadily. Alongside it:
no-stretch gave 2.73x more cells, and now the solid turns out to be already graded. **Octree cell
counts on this part have never once moved the way the geometry suggested they should. Measure before
believing, including when I am the one asserting it.**

## 5. Align the grid origin to the channel array: REFUTED

**Measured, and it cannot work.** Leaving it here rather than deleting it, because it is the obvious
idea and the next person will have it too.

The channel population is purely discrete, because the conjugate interface sits on octree cell faces.
Binned the same way, so is the part's:

| width | cells | CAD | mesh |
|---|---|---|---|
| 80.5 um | 4 | **0.00%** | **1.85%** |
| 100.6 um | 5 | **90.87%** | **79.91%** |
| 120.8 um | 6 | 6.77% | 10.79% |
| 140.9 um | 7 | 1.29% | 3.17% |
| 161.0 um | 8 | 0.45% | 1.45% |
| 181.1 um | 9 | **0.00%** | 0.81% |

The part has its own tail near 119 um, so the mesh is not inventing it, but the mesh **doubles the
off-nominal share from 9.13% to 18.86% and passes 10.1% too much flow** conductance-weighted. That
net bias is larger than anything in the resolution budget and compounds with the +8.0% there for
about +19% in total.

It also invents a width the part does not contain: 1.85% of channels at 80.5 um, where the CAD's
narrowest in-window run is 97.963 um. That is -20% on width and -49% on flow for those channels.

**It is not a pitch problem.** The CAD pitch measured wall-to-wall over two steps is 201.26253 um,
which is 9.999977 cells, so `maxCellSize 322.020` already hit it to two parts in a million. Core
n=103,980, mean 201.403 um, sd 0.771 um, CV 0.383%. There is no drift to remove.

**And it is not an origin problem.** The phase of channel walls within a cell is uniform. Binned into
20 bins across the 20.1263 um cell, the counts run 290 to 482 about a mean of 370, with no peak
anywhere. A flat phase distribution means no translation of the octree can put walls on cell faces.
An origin shift only permutes which channels land on 5 cells and which on 6, and the off-nominal
fraction is invariant under it. **That is why this item is refuted rather than untested.**

**What it actually is:** the part's own width spread straddles the cell-count boundaries. The CAD's
5-cell bin runs 97.963 to 110.533 um while 5 cells is 100.632 um, so a large part of that bin sits
above the boundary and rounds up, and the narrow end rounds down to a 4-cell width the part does not
contain. A 12.38% width CV against a 20.1263 um quantum produces the observed population, and the
only lever that touches it is a smaller quantum.

So item 8 is not one option among several. **It is the only fix for this defect**, which raises its
priority well above where I first put it.

## 6. `writeFormat binary` in the mesher

**Measured.** 1.74x on every read, 1.64x smaller on disk, mesh unchanged to the last printed digit.

| | ASCII | binary |
|---|---|---|
| `checkMesh` on 31.0M cells | 278 s | 160 s |
| polyMesh on disk | 6.46 GB | 3.95 GB |

Set it in the mesher's `controlDict` so the write is free. Do **not** convert an existing mesh in
order to check it once: `foamFormatConvert` costs 157 s to save 118 s.

## 7. Repair `fluid_fine.stl`

**Measured defect.** 111 open edges, so its signed volume is meaningless. It is only a refinement
region, so the damage is confined to where refinement lands rather than to the geometry, but a
refinement region with holes in it refines the wrong cells and nobody will notice.

`fluid.stl`, `chip.stl` and `block.stl` are all closed. `chip.stl` is two pieces by design.

## 8. One more refinement level in the channels

**Blocked, and expensive, and now the only route to the biggest fidelity defect.** Would take the
channel from 5 cells to 10, flow error from +8.0% to +2.0%, developed Nusselt from +0.54% to +0.14%,
and would cut the 43 to 90% thermal-entrance error that no layer stack fully removes, and would
halve the quantum relative to the part's own 12.38% width spread, which item 5 shows is the one thing
that can reduce the off-nominal population.

Two obstacles. The 32-bit label wall makes microchannels +5 impossible with the current image by any
route, in the parallel load balancer and in serial octree node addressing both, so it needs a
`WM_LABEL_SIZE=64` image first. And it multiplies the cell count in the region that is already the
most refined.

Worth it only after item 4 has freed the budget, and it is also the prerequisite for the grid
convergence study that would put error bars on everything in the quality document.

---

## What to do first

1. Ask the customer for the **flow rate and fluid**, and ask what they are buying: a mean heat
   transfer coefficient or a peak junction temperature. The flow rate sets Reynolds number against
   the Re 152 and Re 23 thresholds in item 1, and the metric decides which threshold applies. The
   metric question matters more than the flow rate, which is the opposite of what this list said
   before.
2. Land items 2 and 3 in `couple_box.sh`. Both are measured, neither costs accuracy, and together
   they take the chain from 3.00 h to 46 min.
3. Do **not** spend a run on item 4 or item 5. Both are refuted: the wall phase is uniform so an
   origin shift provably does nothing, and the solid is already graded like the fluid so there is no
   bulk to coarsen.
4. Decide on the `WM_LABEL_SIZE=64` image. It is now the only route to the biggest fidelity defect,
   which is a different and stronger reason than the one it was cancelled on.
