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

`devtools/semiconductor/chan_quant.py` reads the 21,708,529 `fluid_to_solid` face centres and points out of
the boundary index sidecar, recovers each face's outward normal from its winding, keeps the
axis-aligned x-normal faces in a 200 um slab at mid channel depth, and pairs each inward-facing
wall with the next outward-facing one. 284,906 gaps. Every face in the slab was an axis-aligned
quad, so no approximation enters.

`scratchpad/cad_quant.py` and `scratchpad/cad_pitch.py` run the identical rule on the STL
triangles, with no mesh involved, on 35 and 62 independent scan lines through the dense part of
the network.

I had measured channel gaps once before, by raycasting the CAD. That can only ever return the
CAD's own width, which is why this went unnoticed until now.

## Measured 6 November on the re-meshed part

I re-meshed at `maxCellSize` 322.020 um, the pitch-10 row of the table above, and measured the
same way. `cbox2b`, 506,362,044 cells, 3.00 h.

| the ~100 um channel array, 791,205 gaps | CAD | `cbox2` at 22.0035 um | `cbox2b` at 20.1263 um |
|---|---|---|---|
| median width | 100.04 um | 110.000 um | **100.630 um** |
| error on the median | | +9.96% | **+0.59%** |
| widths present | one population | 88.0 / 110.0 / 132.0 | 80.5 / 100.6 / 120.8 |
| share at the right width | 93.7% | 55.39% | **85.00%** |
| coefficient of variation | 0.89% | 12.59% | **7.31%** |
| w^3 flow spread, p95/p5 | 1.09x | 3.375x | **1.730x** |

The prediction in the table above was "exactly 5 cells everywhere". That is not what happened.
85.00% came out at exactly 5 cells, 12.67% a cell wide and 2.33% a cell narrow.

**Why.** An even cell count per pitch fixes the width a channel is aiming at. It does not fix where
the channel sits on the grid, and each wall snaps to its nearest lattice plane on its own. The
pitch has CV 0.46%, which is 0.046 of a cell per pitch, so the grid phase wanders along a row of
sixty pitches and only has to cross one half-plane for a channel to gain or lose a cell. Channel
width variation is not the cause: at CV 0.89% it is eleven standard deviations inside the half-cell
tolerance.

Finer cells do help this one, unlike the staircase. At ten cells per channel the same wander is
half as large against the same tolerance. The next even pitch is 12 cells at 16.772 um and that is
775M cells on the measured h^-2.28 scaling, over the ceiling. So 20.1263 um is the floor and 85% is
what a grid-aligned mesh of this part gives.

**The residual is one-sided, and that part is not the grid.** Both meshes put the mean channel the
same tenth of a cell too wide: 5.1036 cells against 5.0034 in the CAD at 20.1263 um, 4.6871 against
4.5766 at 22.0035 um. That is about 2 um of fin counted as coolant on every channel, the same
amount at either cell size. The coolant volume agrees independently, against the CAD shell at
53,692.22 mm3: `cbox2` came in at 53,419.40 mm3, which is -0.51%, and `cbox2b` at 55,102.60 mm3,
which is +2.63%. So fixing the width made the volume slightly worse.

Offsetting the coolant surface inward by about 1 um would move the mean onto 5.000 cells, collapse
the wide group and take the volume error back to roughly zero, at no meshing cost. It changes how
we represent the geometry rather than how we grid it, so it is a decision and not a fix I have
taken.

**Cell count.** 506,362,044 against 413,273,323 over a 1.0933x cell-size ratio fits h^-2.2795. The
dx^-3 used above predicted 540M and was 6.6% high.

**Method.** the 5 November copy of this script was lost from the scratchpad, so I rewrote it, put it in the
repo as `devtools/semiconductor/chan_quant.py` where it cannot be lost again, and ran the rewrite
against the shipped mesh before trusting it on the new one. It returned 88.001 / 110.000 / 132.002 um at
36.09 / 52.68 / 6.34% and a w^3 spread of 3.383x, which reproduces the numbers at the top of this
file, so the same script on the new mesh is comparable. It reports twice, all gaps and then the microchannel band
only, so the wider manifold and plenum channels, which are 4.9% and 7.4% of gaps on the two meshes,
are not counted as scatter. `devtools/semiconductor/stl_shells.py` is the surface-side companion
that counts shells and checks closedness.
