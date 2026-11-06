# The grid, not the gradient, is the defect

Measured 5 November 2026 on the delivered 413.27M cell coupled mesh (`cbox2`) and on
`~/clean_stl/fluid.stl`, the surface it was built from.

## What I found

| | CAD | delivered mesh |
|---|---|---|
| channel width, median | 100.04 um | 110.02 um |
| channel width, mean | 100.7 um | 103.14 um |
| 93.7% of channels lie in | 96 to 104 um | nothing lies there |
| widths actually present | one population, CV 0.89% in the core | 88.014 (36.1%), 110.017 (52.7%), 132.021 (6.4%) |
| distance from a whole number of cells | 0.437 cells | 0.0008 cells |
| w^3 conductance spread, p95/p5 | 1.09x | 3.38x |

The cell is 22.0035 um. A 100.04 um channel is 4.546 cells. The conjugate interface sits on
existing octree cell faces, so every channel has to be a whole number of cells. 88.014 um or
110.017 um. There is no lattice width between them, so the mesh cannot represent the width the
part is actually built to, and which of the two a given channel gets depends only on where it
happens to fall on the grid.

Slot flow goes as width cubed. 88.014 um carries 0.673x the flow of a 100.04 um channel and
110.017 um carries 1.331x. Two channels the CAD made identical come out 1.98x apart.

The mean width is only +2.4% off, so this is not a bias. It is scatter. The grid imposes about
three times the channel-to-channel variation that the part's own manufacturing does, in a
topology-optimised network whose entire purpose is to control flow split.

## The array is uniform, so this is free to fix

| | median | CV within +-6 um of the median | share within that band |
|---|---|---|---|
| channel | 100.04 um | 0.89% | 94.5% |
| fin | 101.25 um | 0.40% | 94.8% |
| pitch | 201.26 um | 0.46% | 94.5% |

Channel and fin are each almost exactly half the pitch, so an even number of cells per pitch
splits evenly into both.

| pitch | dx | maxCellSize | channel | error |
|---|---|---|---|---|
| 8 cells | 25.1578 um | **402.525 um** | exactly 4 cells, 100.631 um | +0.59% |
| 10 cells | 20.1263 um | **322.020 um** | exactly 5 cells, 100.631 um | +0.59% |
| today | 22.0035 um | 352.056 um | 4 or 5 or 6 cells | -12.0% / +10.0% / +32.0% |

Both replace the three-way split with one width everywhere and zero scatter. Both are the same
octree depth as today, so neither carries any new label or buffer risk.

Scaling cell count as dx^-3 from today's 413.27M:

- pitch-8 at 25.1578 um: about 276M cells, 33% fewer than today, 4 cells per channel everywhere.
- pitch-10 at 20.1263 um: about 540M cells, 8.5% finer linear resolution than today, 5 cells per
  channel everywhere, still inside the 700M ceiling.

pitch-10 is the better deliverable. It is finer than what we shipped and it has no scatter.

## What this does not fix

Nothing here touches the staircase. The wetted area is still +7.8% overall and +11.6% on the
channels, and that multiplies the convective area directly. Refinement halves the staircase
amplitude but never removes the area inflation.

## Method

`scratchpad/chan_quant.py` reads the 21,708,529 `fluid_to_solid` face centres and points out of
the boundary index sidecar, recovers each face's outward normal from its winding, keeps the
axis-aligned x-normal faces in a 200 um slab at mid channel depth, and pairs each inward-facing
wall with the next outward-facing one. 284,906 gaps. Every face in the slab was an axis-aligned
quad, so no approximation enters.

`scratchpad/cad_quant.py` and `scratchpad/cad_pitch.py` run the identical rule on the STL
triangles, with no mesh involved, on 35 and 62 independent scan lines through the dense part of
the network.

I had measured channel gaps once before, by raycasting the CAD. That can only ever return the
CAD's own width, which is why this went unnoticed until now.
