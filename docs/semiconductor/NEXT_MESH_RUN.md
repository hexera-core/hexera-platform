# The next mesh run: what changes, and what it buys

Internal. This is the run I am doing next on the Corintis plate, what each change is for, and how
to tell afterwards whether it worked.

Two numbers in `meshDict` and one in `meshQualityDict`. No new code, no new tool, same octree
depth, and the mesh comes out better on every axis I can measure.

## The one change that matters

| | shipped (`cbox2`) | next run |
|---|---|---|
| `maxCellSize` | 352.056 um | **322.020 um** |
| finest cell (background / 16) | 22.0035 um | 20.1263 um |
| cells across a 100.04 um channel | 4 or 5 or 6 | **exactly 5, every channel** |
| channel width in the mesh | 88.014 / 110.017 / 132.021 um | 100.631 um |
| channel width error | -12.0% / +10.0% / +32.0% | **+0.6%** |
| flow spread between channels the CAD made identical | 3.38x | none |
| octree depth | level 14 | level 14, unchanged |

The reason this works is in [MESH_QUANTISATION.md](MESH_QUANTISATION.md): the coolant boundary
lies on octree cell faces, so a channel has to be a whole number of cells wide. At 22.0035 um
there is no whole number that gives 100 um. At 20.1263 um there is, because the channel+fin pitch
I measured at 201.263 um becomes exactly ten cells and the channel is exactly five of them.

The array is uniform to half a percent, which is the only reason one number fixes the whole plate.
That may not hold on their next part.

## The second change: the chip band

`couple_box.sh` emits the chip entry as `additionalRefinementLevels 2; refinementThickness
0.0036;`. That is an 88 um band reaching **3.6 mm** into the plate around the chip, and it also
refines a shell of air around the chip. Measured off the air zone's own cell count rather than the
body volume, that shell alone is 2.434e-05 m3 of 88 um cells.

Tightening `refinementThickness` to 3.6e-4 keeps the refinement where the contact is and drops the
rest. The generator's own comment already suspected this entry.

## The third change: a config value that has made our quality evidence meaningless

`system/meshQualityDict` ships with `minVol 1e-13`, an absolute face-pyramid volume. A 22 um cell
produces about 1.8e-15, which is 56x below it. So `checkMesh -meshQuality` with the shipped dict
flags **every face** of a geometrically perfect mesh.

Set `minVol 1e-20`, or 1e-4 times the smallest cell volume. Then re-run checkMesh on every region,
because any report we hold built with the shipped dict is worthless.

The same value is what makes layer addition work at all, so this is also a precondition for
anything in [RESEARCH_BOUNDARY_LAYERS.md](RESEARCH_BOUNDARY_LAYERS.md).

## What it costs

Cells go as h^-2.038 on this part. That exponent is measured, not assumed: it fits probe_edge's
16,186,600 cells at 0.0014 m to ws_fitted's 269,650,437 at 0.000352056 within a percent.

| option | `maxCellSize` | cell | cells per channel | predicted cells |
|---|---|---|---|---|
| shipped | 352.056 um | 22.0035 um | 4 / 5 / 6 | 413,273,323 measured |
| **pitch-10** | **322.020 um** | 20.1263 um | exactly 5 | about 496M |
| pitch-8 | 402.525 um | 25.1578 um | exactly 4 | about 315M |

Minus the chip band, pitch-10 should land nearer 440M. Both options are inside the 700M gate that
`couple_box.sh` now enforces before topoSet, and inside the real 600 to 650M ceiling the 32-bit
face label imposes.

**I had this wrong twice and both corrections are worth recording.** I first said 540M, scaling as
dx^-3; the measured exponent is 2.038, not 3. And I said 3 to 3.5 hours by scaling a 2.6 h figure;
the right baseline is `cbox2`'s own stage table, which gives about 2.2 h. See
[MESH_TIME.md](MESH_TIME.md).

## What it does not fix

**The staircase.** The coolant boundary is still a cell-face staircase, so wetted area stays about
7.8% high overall and 11.6% high on the channels. A finer cell halves the staircase's amplitude
and never removes the area bias. After this run that is the largest error left.

**Boundary layers.** Still zero. They are achievable on the mesh as built, and deliberately not
part of this run, because the staircase's +11.6% area currently cancels most of the near-wall
gradient error. Fixing the gradient alone moves total heat from roughly +10% to roughly -5%. The
two have to move together or not at all.

**The entry region.** A branching plate restarts the thermal layer at every junction, and the
segment-averaged error at 4.6 cells is about -1% on a 2.45 mm segment but -43% on a 0.07 mm one.
The slot's thermal entrance length is 1.4 to 5.7 mm. I have never measured the junction-to-junction
segment length distribution, so I do not know which end of that range the plate sits at. That is an
afternoon with the STEP and no meshing.

## The command

```bash
# on a box with enough RAM; cbox2 ran on n2-highmem-96
couple_box.sh cbox2b 0.000322020 1 4 2 1 8 12
```

Arguments are background cell size, then coolant +1, microchannels +4, chip +2, plate skin +1,
ranks, hours. scaleY 3 over the microchannel band only, which is the setting that matters most for
time and is unchanged here.

Edit the chip entry's `refinementThickness` to 3.6e-4 before running.

## How to tell whether it worked

Four checks, in order. The first is the one that decides it.

| # | check | pass |
|---|---|---|
| 1 | re-run the channel histogram from `MESH_QUANTISATION.md` method | every gap exactly 5 cells, zero spread |
| 2 | total cells | in the predicted 440 to 496M band |
| 3 | non-orthogonality and skewness per region | no worse than `cbox2`: 49.31 fluid, 51.46 solid, nothing over the 70 bar |
| 4 | interface face counts | equal on both sides, which is the conformality the whole approach rests on |

Check 1 is the deliverable. Run it on the boundary index sidecar exactly as before: read the
`fluid_to_solid` face centres and points, recover each face's outward normal from its winding,
keep the axis-aligned x-normal faces in a slab at mid channel depth, and pair each inward-facing
wall with the next outward-facing one.

If check 3 moves, stop and look: the octree depth has not changed, so non-orthogonality and
skewness should not move either.

## Why this run rather than anything else

Ranked against every other option the research turned up, this is the only one that improves
fidelity and reduces cost at the same time. Everything else either costs cells (layers,
finer bands), costs weeks (the Gmsh swept route), or has been refuted (non-conformal coupling,
cfMesh boundary layers, the 64-bit image rebuild).

It also fixes the defect that is actually in the mesh rather than one we went looking for. We
spent 413 million cells making the walls smooth and then rounded every channel to the wrong width.
