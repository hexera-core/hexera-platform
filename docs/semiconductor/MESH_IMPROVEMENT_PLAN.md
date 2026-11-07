# What to change about the coupled mesh, in order

Written 7 November 2026 off the measurements in [MESH_QUALITY_CBOX2B.md](MESH_QUALITY_CBOX2B.md) and
[MESH_TIME_TO_ONE_HOUR.md](MESH_TIME_TO_ONE_HOUR.md). Ranked by value, not by effort.

Each row says whether the gain is measured or projected. Four of the top five are measured, which is
the reason this list is worth acting on rather than discussing.

| # | change | time | accuracy | basis |
|---|---|---|---|---|
| 1 | drop the boundary layer pass | **-5,843 s** | **Nusselt -4.02% to +2.18%** | measured |
| 2 | deliver decomposed, drop reconstructs and serial checks | **-12,377 s** | none | measured |
| 3 | retype `mappedWall` to `wall` on a check-time copy | +14 s | enables 9 checks never run before | measured |
| 4 | coarsen the solid away from the interface | large | none expected | projected |
| 5 | ~~align the grid origin to the channel array~~ | 0 s | **nothing** | **refuted** |
| 6 | `writeFormat binary` in the mesher | -118 s per read | none | measured |
| 7 | repair `fluid_fine.stl` | 0 s | where refinement lands | measured defect |
| 8 | one more refinement level in the channels | very large | flow +8.0% to +2.0%, **and the only fix for the quantisation** | blocked |

---

## 1. Drop the boundary layer pass

**Measured.** Saves 5,843 s of the 7,483 s the layer step costs, and moves Nusselt from -4.02% to
+2.18%, a 6.2 point improvement. Wall shear is exact either way.

The reasoning is in MESH_QUALITY_CBOX2B.md section 7 and it is worth restating because it inverts
the usual instinct. For fully developed laminar duct flow the cell-centred one-sided wall gradient is
**exact at any resolution**, because the discrete solution is the exact parabola plus a constant
offset and the offset cancels in the gradient. There is no wall gradient error for a boundary layer
to fix. What the layer stack does instead is move cells away from the core, and Nusselt depends on
the bulk temperature, which the core carries. So refining the wall degrades the only part of the
calculation that was in error.

Removing the 3.43 um sliver does not rescue it: grading 3 layers to fill the first cell exactly gives
-4.41%, marginally worse. It is the layers, not the sliver.

**The one condition.** This holds while the flow is thermally developed. The longest straight channel
run is 12.8 mm = 67.8 Dh, and the thermal entrance length exceeds that above **Re 226** at Pr 6.0.
Above that the flow is developing everywhere, the near-wall thermal gradient is steep and not
parabolic, and layers do earn their cost. We do not have the operating flow rate. **This is the
single most valuable number to ask the customer for**, because it decides item 1 outright.

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

## 4. Coarsen the solid away from the interface

**Projected.** The largest single lever, and the only one on this list I have not measured.

| region | cells | share | physics |
|---|---|---|---|
| solid | 327,126,204 | **74.2%** | conduction in copper |
| fluid | 82,529,515 | 18.7% | all the flow, all the convection |
| chip | 31,012,960 | 7.0% | conduction in silicon |

Three quarters of the mesh is solving the easiest equation in the problem. The solid needs 20.1263 um
cells at the coolant interface and nowhere else. Every stage in the chain scales with total cells, as
does the solver run that follows, so this shortens everything at once.

I have not measured what a banded solid costs to build or saves to solve, so there is no number here.
Getting one is a single `couple_box.sh` run with a refinement band on the solid side.

A caution from the record: a 4x coarser far field changed cell count by 0.9%, and no-stretch gave
2.73x more cells. Octree cell counts on this part have repeatedly failed to move the way the geometry
suggests they should. Measure before believing.

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
channel from 5 cells to 10, flow error from +8.0% to +2.0%, Nusselt from +2.18% to +0.67%, and would
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

1. Ask the customer for the **flow rate and fluid**. One number decides item 1 and sets the error
   bars on the whole quality assessment.
2. Land items 2 and 3 in `couple_box.sh`. Both are measured, neither costs accuracy, and together
   they take the chain from 3.00 h to 46 min.
3. Run item 4's banded-solid test. One run, and it is the largest lever on everything. It is also
   the prerequisite for item 8, because item 8 needs the cell budget item 4 frees.
4. Do **not** spend a run on item 5. The phase is uniform and an origin shift provably does nothing.
