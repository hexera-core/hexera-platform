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
| distance from a whole number of cells | 0.4535 cells | 0.0007 cells |
| w^3 conductance spread, p95/p5 | 1.09x | 3.38x |

The cell is 22.0035 um. A 100.04 um channel is 4.546 cells. The conjugate interface sits on
existing octree cell faces, so every channel has to be a whole number of cells. 88.014 um or
110.017 um. There is no lattice width between them, so the mesh cannot represent the width the
part is actually built to, and which of the two a given channel gets depends only on where it
happens to fall on the grid.

Slot flow goes as width cubed. 88.014 um carries 0.681x the flow of a 100.04 um channel and
110.017 um carries 1.330x. The two lattice widths are exactly 4 and 5 cells, so two channels the CAD
made identical come out exactly (5/4)^3 = 1.953x apart.

The mean width is +2.42% off, which is small beside the scatter. The grid imposes about fourteen
times the channel-to-channel variation that the part's own manufacturing does, CV 12.59% against
0.89%, in a topology-optimised network whose entire purpose is to control flow split. The 3.1x that
stood here was the ratio of the two w^3 p95/p5 spreads, which is not a ratio of variations.

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
wall with the next outward-facing one. 566,274 gaps, of which 538,564 are in the 70 to 145 um
microchannel band. Every face in the slab was an axis-aligned quad, so no approximation enters. The
284,906 that stood here came from the lost 5 November script and does not reproduce; see Method at
the end of this file.

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
| share at the modal lattice width | 93.7% lies within 96 to 104 um | 55.39%, and nothing within 96 to 104 um | **85.00%** |
| coefficient of variation | 0.89% | 12.59% | **7.31%** |
| w^3 flow spread, p95/p5 | 1.09x | 3.375x | **1.730x** |

The prediction in the table above was "exactly 5 cells everywhere". That is not what happened.
85.00% came out at exactly 5 cells, 12.67% a cell wide and 2.33% a cell narrow.

### The cause, corrected after an audit

I first wrote that the grid phase causes this. **That is impossible and the claim is withdrawn.**
If a gap is exactly n cells wide then both of its walls sit at the same phase, so
`round(p + n) - round(p) = n` for every p: sliding a commensurate channel along the lattice cannot
change its cell count. Checked analytically and by Monte Carlo at the measured pitch CV, which
returns 0.0000% miscounts.

The cause is the width the mesher actually sees, which is the 2 um bias in the next section. I had
measured that on this same page and was treating it as a separate effect.

| | |
|---|---|
| mesh mean channel, `cbox2b` | 5.1036 cells |
| what that mean alone predicts | 89.64% at 5 cells, 10.36% at 6 cells, **0% at 4 cells** |
| measured | 85.00% at 5, 12.67% at 6, 2.33% at 4 |
| the excess over that prediction | +2.31 points at 6 and +2.33 at 4: symmetric, and mean-preserving |

So the bias sets how far off a whole number the typical channel is, and that fixes the split between
two adjacent counts and nothing more. A third group can only come from width dispersion. The
dispersion the mesher sees has a standard deviation of about 3.1 um, 3.5x the CAD's own 0.89 um,
because the wall positions carry the staircase as well as the part.

One exact identity makes the floor model-free. With only three counts present,
`P(n+1) - P(n-1) = mean cells - n`. Measured: 12.6733 - 2.3292 = 10.344 points against
5.1036 - 5 = 0.1036 cells, the same number. So the off-target share can never be smaller than
`|mean width / dx - n|`.

### Finer cells make this worse, not better

The opposite of what I first wrote, and the identity above is why: the bias is a fixed length, so in
cell units it grows as the cells shrink.

| | dx | channel | floor on the off-target share | modelled share at exactly n cells |
|---|---|---|---|---|
| pitch 8 | 25.1578 um | 4 cells | >= 8.28% | about 88% |
| **pitch 10, built** | **20.1263 um** | **5 cells** | **>= 10.36%** | **85.00% measured** |
| pitch 12 | 16.7719 um | 6 cells | >= 12.43% | about 82% |
| ten cells per channel | 10.0631 um | 10 cells | >= 20.72% | about 70% |

The per-pitch phase wander says the same thing: 0.046 of a cell today, 0.055 at pitch 12, 0.092 at
ten cells per channel, against a half-cell tolerance that shrinks with the cell. Twice as large, not
half.

**So 85% is not a property of the part, and 20.1263 um is not a floor that 85% sits on.** 85% is a
property of this surface representation. The free 1 um inward offset below would put it at roughly
88 to 96%.

**And the coarser commensurate row is the better one on this measure.** pitch 8 at 25.1578 um gives
exactly the same 100.631 um channel, because 4 x 25.1578 = 5 x 20.12625, and projects to about 88%
at exactly 4 cells for about 276M cells rather than 506M. It is not a free win: at 4.08 cells across
instead of 5.10 the 2/N^2 flow-rate error grows from +7.7% to +12.0%. That is the trade. I had
called pitch 10 "the better deliverable" on reasoning that was wrong.

### The residual is one-sided, and that part is not the grid

| | mesh | CAD mean | bias |
|---|---|---|---|
| `cbox2` at 22.0035 um | 4.6871 cells | 4.5766 | +0.1105 cells, +2.43 um |
| `cbox2b` at 20.1263 um | 5.1036 cells | 5.0034 | +0.1002 cells, +2.02 um |

Two points cannot establish that this is independent of cell size, and the two do not agree closely:
10% apart measured in cells, 21% apart measured in microns, so the cell-invariant reading fits
marginally better. Against the CAD median rather than its mean the bias is larger, +0.1330 and
+0.1406 cells.

It is **not** a one-sided cell-inclusion test, which I had offered as the mechanism. That would bias
the gap by a full cell, +20.1 um, which over-predicts by a factor of ten and would put the channel
mass at 6 or 7 cells. The signature is a sub-cell outward offset of the coolant surface, about 1.0
to 1.2 um per wall.

Nor is it "about 2 um on every channel". 85% of channels are +0.59 um out, 12.67% are +20.1 um and
2.33% are -20.1 um. The 2 um exists only as a population mean.

### What the volume and the area do NOT say

I first wrote that the coolant volume and the wetted area corroborate the width bias independently.
**Withdrawn, on both counts.**

| | coolant volume | against the CAD shell at 53,692.22 mm3 |
|---|---|---|
| `cbox2` | 53,419.40 mm3 | -0.51% |
| `cbox2b` | 55,102.60 mm3 | +2.63% |

The microchannel array is at most 5.7% of the coolant volume, so a 2% width bias can account for at
most 0.11 of the 2.63 points, 23x short. And the signs run the wrong way: `cbox2` has the LARGER
over-width and a volume DEFICIT. Between the two meshes the median width fell 8.5% while the volume
rose 3.1%. So the +2.63% coolant volume on the new mesh is a separate defect that nothing here
explains, and it should be carried as one.

The wetted area is the same story. The 2 um bias accounts for about 0.13 of the 8.3 points of area
excess and predicts the two meshes in the wrong order. That excess is the staircase.

**The lever is still worth pulling, but it is a smaller lever than I first claimed.** A 1 um inward
offset moves the mean by about half the residual, so it would shrink the 6-cell group rather than
collapse it, and it would take roughly 0.1 of the 2.63 points off the volume. It still costs nothing
to mesh, and it is still a geometry decision rather than a gridding one.

**Cell count.** 506,362,044 against 413,273,323 over a 1.09327x cell-size ratio fits h^-2.27798. The
dx^-3 used above predicted 540M and was 6.6% high. pitch 12 at 16.772 um is 767M cells on that
exponent, not the 775M I first wrote.

**Method.** The 5 November copy of this script was lost from the scratchpad, so I rewrote it, put it
in the repo as `devtools/semiconductor/chan_quant.py` where it cannot be lost again, and ran the
rewrite against the shipped mesh before trusting it on the new one. It reproduces the three widths
and their shares to four digits, and the w^3 spread as 3.375x against the 3.383x printed above, but
it returns 566,274 gaps where the lost script reported 284,906, a factor of 1.99. So it reproduces
the distribution and not the sample, and "to three decimals" was stronger than the check earns. It
reports twice, all gaps and then the microchannel band only, so the wider manifold and plenum
channels, 4.9% and 7.4% of gaps on the two meshes, are not counted as scatter.
`devtools/semiconductor/stl_shells.py` is the surface-side companion that counts shells and checks
closedness.

---

## 7 November 2026: the pitch-10 cell fixed the split, and the CAD was not 100 um

Re-measured on `cbox2b`, built at `maxCellSize` 322.020 um for a 20.1263 um cell, against the same
`fluid.stl`. Full detail in [MESH_QUALITY_CBOX2B.md](MESH_QUALITY_CBOX2B.md) section 5.

**The three-way split is gone.**

| | `cbox2` as delivered | `cbox2b` |
|---|---|---|
| widths present | 88.014 (36.1%), 110.017 (52.7%), 132.021 (6.4%) | 100.6 to 100.7 (**79.91%**), then whole-cell steps |
| share on the nominal width | **none** | **79.91%** |
| conductance spread, w^3 p95/p5 | 3.38x | 2.75x |

88 um does not appear anywhere in the new distribution.

**And a correction to this document.** The table at the top gives the CAD channel as 100.04 um
median, 100.7 um mean. Measuring the CAD by exactly the method used on the mesh, casting rays along
the pitch axis and reading coolant runs off the surface crossings with the same at-least-20-crossings
filter, gives:

| | CAD | `cbox2b` | error |
|---|---|---|---|
| channel p5 | 98.295 um | 100.598 um | +2.34% |
| channel p50 | **100.446 um** | **100.630 um** | **+0.18%** |
| channel p95 | 121.487 um | 140.900 um | +15.98% |
| CV | 12.38% | 18.94% | |
| **pitch** | **201.2625 um** | **201.263 um** | **-0.0002%** |

So the median width is right to +0.18%. Earlier today I published +0.63% for this, computed against
an assumed 100.000 um nominal, and then -0.074%, computed from the dominant periodicity of wall
positions in the STL. Both are wrong. The 100.000 um nominal is not what the part is built to, and
the wall-position fundamental is the mean of channel and fin spacing, not the channel width. The
table above is the number.

The part's own conductance spread is wider than the 1.09x in the table at the top of this document,
because that figure was restricted to the microchannel band and this one is not.

**Bin both sides by cell count and the real defect appears.** This is the only comparison that
separates what the mesh invents from what it inherits, and it overturns a claim I made earlier today
that the mesh adds 1.45x of spread over the part:

| width | cells | CAD share | mesh share | CAD mean width |
|---|---|---|---|---|
| 80.5 um | 4 | **0.00%** | **1.85%** | nothing there |
| **100.6 um** | **5** | **90.87%** | **79.91%** | 100.495 |
| 120.8 um | 6 | 6.77% | 10.79% | 119.335 |
| 140.9 um | 7 | 1.29% | 3.17% | 143.035 |
| 161.0 um | 8 | 0.45% | 1.45% | 158.756 |
| 181.1 um | 9 | **0.00%** | 0.81% | nothing there |

The part genuinely has wider distribution channels near 119 um, so the mesh is not inventing the
tail. But it does not reproduce it either: off-nominal goes from **9.13% in the CAD to 18.86% in the
mesh**, and conductance-weighted the array goes from 1.1467 to 1.2620, so **quantisation alone passes
10.1% too much flow.** That net bias compounds with the +8.0% from having only 5 cells across, for
about +19% in total.

Per channel, the forced snapping is:

| cells | CAD range forced onto one width | mesh width | width error p5 / p50 / p95 |
|---|---|---|---|
| 5 | 97.963 to 110.533 um | 100.632 um | -4.05% / +0.19% / +2.49% |
| 6 | 110.734 to 130.787 um | 120.758 um | -6.21% / +0.76% / +8.54% |
| 7 | 130.838 to 150.736 um | 140.884 um | -6.08% / -3.98% / +7.68% |

So -6.2% to +8.5% on width, which is -17% to +28% on flow.

**And the mesh invents narrow channels the part does not have.** The CAD has nothing at 4 cells and
its narrowest in-window run is 97.963 um. The mesh puts 1.85% of channels at 80.5 um, a full cell
narrower than anything in the part, a -20% width and -49% flow error. I had this the wrong way round
earlier today, writing that sub-100 um channels round up and pass 8% too much. They round down.

**The pitch prediction at the top of this document was right.** `maxCellSize 322.020` was chosen so
that ten cells equals the measured 201.263 um pitch, and it does. Measured wall-to-wall over two
steps on `fluid.stl`, the CAD pitch is 201.26253 um, which is 9.999977 cells: core n=103,980, mean
201.403 um, sd 0.771 um, CV 0.383%. Right to two parts in a million.

**But the prediction that every channel would then be exactly 5 cells was wrong, and here is why.**
The phase of channel walls within a cell is uniform. Binned into 20 bins across the 20.1263 um cell,
the counts run 290 to 482 about a mean of 370, with no peak anywhere. There is no preferred phase for
an exact pitch to lock onto, and so **no translation of the octree origin helps either**: a shift
only permutes which channels land on 5 cells and which on 6, leaving the off-nominal fraction alone.

What is left is the part's own width spread straddling the cell-count boundaries. The CAD's 5-cell
bin runs 97.963 to 110.533 um while 5 cells is 100.632 um, so a large part of that bin sits above the
boundary and rounds up, while the narrow end rounds down to a 4-cell width the part does not contain.
A 12.38% width CV against a 20.1263 um quantum produces exactly the observed population.

So the remaining lever is neither the cell size nor the origin. It is a smaller quantum, meaning one
more refinement level, which the 32-bit label wall blocks until there is a `WM_LABEL_SIZE=64` image.
[RESEARCH_BOUNDARY_LAYERS.md](RESEARCH_BOUNDARY_LAYERS.md) cancelled that rebuild on the grounds that
the overflow diagnosis was wrong and a 64-bit build halves the box ceiling. That still stands, but the
decision now has a second and stronger reason to go the other way, so it is worth re-opening on these
grounds rather than the ones it was closed on.
