# How good is the coupled mesh

Measured off the delivered mesh, the customer's CAD and probe runs of cfMesh itself. Where a
number corrects something I published earlier, it says so.

## Verdict: pretty good

| dimension | grade |
|---|---|
| mesh quality and conditioning | **great** |
| resolution for conjugate heat transfer | **pretty good** |
| cell budget and optimisation | **okayish** |
| solver readiness | **pretty good** |

The conditioning is top-decile and the geometry is faithful. It is not "great" overall because
the mesh is roughly 2x larger than it needs to be for the fidelity it delivers, and because the
central physics number rests on 4.6 cells across a channel with no convergence study behind it
and none possible at the current resolution floor.

## 1. Quality: great

Every boundary face in all three regions, measured with `checkMesh -writeFields`.

| region | faces | non-ortho median | p99 | max | over 50 deg | over 70 deg |
|---|---|---|---|---|---|---|
| fluid | 21,711,994 | 0.000 | 35.294 | 49.311 | **0** | **0** |
| solid | 23,651,176 | 0.000 | 2.212 | 48.974 | **0** | **0** |
| chip | 2,309,716 | 0.000 | 0.000 | **0.000** | 0 | 0 |

| region | skewness max | over 2 | aspect ratio median | max | over 4 |
|---|---|---|---|---|---|
| fluid | 1.651 | 0 | 3.000 | 3.698 | 0 |
| solid | 1.647 | 0 | 3.000 | 3.663 | 0 |
| chip | 1.3e-12 | 0 | 1.000 | 1.001 | 0 |

915,659 of 47,672,886 measured cells (1.92%) exceed 20 degrees, 102,876 (0.216%) exceed 40, and
**nothing anywhere exceeds 50** against checkMesh's 70 degree bar. The aspect ratio plateau at
exactly 3.000 is the `scaleY 3` stretch and costs nothing numerically.

The plate mesh previously shipped to this customer had non-orthogonality 84.08 with 41,700 faces
over 70. This one has none over 50.

**Caveat on what was measured.** These are boundary-adjacent cell values. They cover most of the
fluid's cells but at most 8.7% of the solid's, and the solid's worst face at 51.4648 degrees is
INTERNAL, so it sits outside the measured population entirely. The interior is characterised by
one number per region.

**The one defect:** `fluid_to_solid` / `solid_to_fluid` is "multiply connected (shared edge)" with
87 bad edges and 174 non-manifold points, in both passes. That is 1.6e-5 of a 21,708,529-face
interface, bounding any energy imbalance it can cause at about 0.002% of the plate-to-coolant
load. Cosmetic, but real.

## 2. Resolution: pretty good, and the two errors cancel

### The geometry, measured by raycasting the CAD

| | measured |
|---|---|
| coolant channel width | **100.76 um** median (min 97.86) |
| channel height | 1499.90 um |
| copper fin between channels | **102.01 um** median (min 42.96) |

Tall narrow slots about 101 um wide and 1500 um tall, separated by 102 um copper fins, essentially
1:1. Hydraulic diameter about 187 um.

| direction | cell | gap | cells across |
|---|---|---|---|
| across the channel | 22.0 um | 100.76 um | **4.6** |
| up the channel | 66.0 um (stretched) | 1499.90 um | 22.7 |
| across the fin | 22.0 um | 102.01 um | **4.6** |

**The stretch is well judged.** `scaleY 3` is applied along the 1500 um dimension, which has 22.7
cells to spare, not across the 101 um dimension, which has 4.6. That single decision is what
turned a 38.5 hour mesh into a 17 minute one.

### Where the fine cells are
Binned on each face's true short edge. *(A first pass binned on sqrt(area) and was badly wrong: a
22x66 um face reads as 38 um. These are the corrected numbers.)*

| cell size on the wetted surface | faces | area | share |
|---|---|---|---|
| **22.0 um** | 20,737,354 | 242.33 cm2 | **37.5%** |
| 44.0 um | 38,166 | 1.61 cm2 | 0.2% |
| 88.0 um | 131,476 | 22.29 cm2 | 3.4% |
| **176.0 um** | 757,698 | 380.70 cm2 | **58.8%** |

The microchannels are properly at 22 um. The plenums, branching manifold and board cooling circuit
are at 176 um, eight times coarser. That is fine for plenum pressure drop and thin for predicting
**flow maldistribution between branches**, which is the most likely way a real branching plate
underperforms.

### Staircasing inflates the wetted area by 7.8%

| | area |
|---|---|
| mesh `fluid_to_solid` | 653.14 cm2 |
| CAD `fluid.stl` | 605.88 cm2 |
| **inflation** | **+7.80%** |

It checks out analytically. A staircased facet's area is multiplied by `|nx|+|ny|+|nz|`; summing
that over the real facet angles predicts **+6.5%** against +7.8% measured.

| surface | axis-aligned area | predicted inflation |
|---|---|---|
| whole coolant surface | 73.3% | +6.5% |
| **microchannel core only** | **91.1%** | **+2.9%** |

For contrast the flat chip-to-plate contact comes out at 27.359 cm2 against the CAD's 27.445,
**-0.31%**. A flat axis-aligned face staircases to nothing.

Narrowing to the microchannels alone, by taking only the mesh faces whose short edge is 22 um:

| | area |
|---|---|
| mesh faces at 22 um (the +4 band) | 242.33 cm2 |
| CAD `fluid_fine.stl` | 217.13 cm2 |
| **inflation on the microchannel surface** | **+11.6%** |

A theoretical staircase over randomly oriented walls would inflate by 4/pi, +27%. The measured
+11.6% is well below that, because 91% of the microchannel area is already axis-aligned.

### The two errors have opposite signs and nearly cancel

For OpenFOAM's wall stencil on a plane channel with N uniform cells, the discrete flow rate at
fixed pressure gradient is high by exactly 2/N^2. At N = 4.58 that is +9.5%, so fRe is low by 8.7%
and by the thermal analogue Nu is low by about **8.7%**.

Against that the wetted area is high: +11.6% on the channels, +7.8% over the whole coolant
surface. Heat transfer goes as `h x A`:

```
  1.116  (channel area, high)  x  0.913  (h, low)  =  1.019     +1.9% net
  1.078  (whole surface)       x  0.913            =  0.984     -1.6% net
```

so **the net sits inside +/-2% either way**. The errors genuinely cancel, and that is only
visible because both were measured rather than one being assumed. This was the single number an
adversarial review flagged three times as unmeasured and as the thing that could flip the sign of
the heat-transfer error; it does not flip it, and it bounds it tightly.

Two things keep this from being "great". There is **no grid convergence study**, and none is
possible on this part: 22.0035 um is the 32-bit label floor. And the **thermally developing
entrance region is unresolved** (the thermal layer is a few um shortly past a bifurcation against
a 22 um cell), which is exactly the enhancement a branching plate is designed around.

## 3. Cell budget: okayish

| region | cells | volume | % of cells | cells/cm3 |
|---|---|---|---|---|
| fluid | 68,806,800 | 53.42 cm3 | 18.9% | 1,288,048 |
| solid | 271,759,108 | 222.05 cm3 | 74.5% | 1,223,842 |
| chip | 24,173,140 | 16.48 cm3 | 6.6% | 1,466,692 |
| air | 48,534,275 | 583.75 cm3 | discarded | 83,141 |

Against a uniform 22 um mesh the saving is 24.3x, 25.6x and 21.3x. The effective solve cost is
better still: only 18.9% of cells carry five equations, so it is about 640M equation-cells against
2.07B if the whole thing were fluid.

But only about a quarter of the budget is refinement anyone asked for:

| where | cells | share |
|---|---|---|
| targeted +4 channel and fin skin | ~106M | ~26% |
| chip +2 band, 3.6 mm two-sided | ~104M | ~25% |
| of which in the **discarded air region** | **35.0M** | **8.5%** |
| plate_skin and coolant +1 bands | ~14M | ~3% |
| 352 um far field | ~17M | ~4% |
| level-2 blanket through bulk metal | ~177M | **~43%** |

**The chip rule is the biggest avoidable cost.** A 3,600 um two-sided band dilates a
70.0 x 3.5 x 78.6 mm die into a 77.2 x 10.8 x 85.8 mm shell, 71.21 cm3, which at 88 um is 104M
cells. That is 41 cells through 3.6 mm of copper for an elliptic conduction field whose gradient
length scale under a die is millimetres. Half of it points upward into air that is deleted.

**On the 43% "unexplained" level-2 blanket:** I do not think it is unexplained. cfMesh's octree
enforces 2:1 balance, so a +4 block 60 x 12 x 52 mm forces a graded halo out through +3 and +2
before reaching the background. A large level-2 population in the surrounding copper is the
expected cost of going four levels deep in the middle of a solid block, not a dict error. It is
still the place to look for savings, but it is not free to remove.

### A real bug in the dict
The anisotropic stretch box does not cover the surface it stretches.

| axis | surface span | box span | margin, low / high |
|---|---|---|---|
| x | 59.995 mm | 59.858 mm | +0.176 / **-0.313 mm** |
| y | 11.836 mm | 12.188 mm | +0.176 / +0.176 mm |
| z | 51.850 mm | 51.338 mm | **-0.759 mm** / +0.247 |

y was padded by exactly one base cell (0.352 mm) on purpose. x and z were not, and the box cuts
0.313 mm and 0.759 mm **inside** the microchannel block, which is 14 and 34 cells of 22 um. The
stretched-to-unstretched transition therefore happens among the finest cells in the mesh instead
of out in the 176 um plenum. Padding x and z the same way y was padded is the cheapest quality
lever in the file.

## 4. Solver readiness: pretty good

Nothing blocks a first timestep. `regionProperties` groups `fluid (fluid)` and
`solid (solid chip)`; both coupled pairs are `mappedWall` with matching counts; the three patches
that faced the undelivered air region were retyped, which is what stops the case fatalling; the
ports are type `patch`.

What a user must still do:

1. Write the case. No `0/`, `constant/` or `system/` ships, so roughly 35 to 40 dictionaries
   across three regions.
2. Decompose it. The mesh ships reconstructed, and `decomposePar` on 271.8M cells is serial.
   `redistributePar -decompose -parallel -region <name>` is the faster route.
3. Decide which port is the inlet. Their CAD names them; they have not confirmed.
4. Know the chip region is **two disjoint bodies**. `checkMesh` flags `*Number of regions: 2`.
   Correct, they are two dies, but a setup assuming one body per region needs checking.

## 5. If I could change one thing

**Not boundary layers. I checked, and cfMesh cannot put them there.**

> **SUPERSEDED 2026-11-06.** The sentence above is true about cfMesh and the wrong
> conclusion to draw from it. `snappyHexMesh` run as a pure layer adder
> (`castellatedMesh false; snap false; addLayers true;`) on the SPLIT fluid region does add
> graded layers, and the conformal interface survives: measured 100% coverage, all hexes,
> Mesh OK, interface area unchanged to 2.5e-7 percent. It had looked impossible because the
> shipped `meshQualityDict` carries `minVol 1e-13`, an absolute pyramid volume 56x above
> what a 22 um cell produces, so every face of a perfect mesh is flagged and the run exits
> 0 having extruded nothing. Layers are still not the first thing to do, but the reason is
> the staircase cancellation below, not capability. See CORINTIS.md and the 2026-11-06
> devlog entry.

`cartesianMesh` meshes one domain whose boundary is `domain.stl`, a plain box. `fluid.stl` and
`fluid_fine.stl` appear only under `surfaceMeshRefinement`, so the channel walls are internal at
mesh time. They become patches only when `splitMeshRegions` cuts the regions apart. Run on a
coarse copy of the real case with a `boundaryLayers` block naming the coolant surface:

```
--> FOAM Warning : Cannot find any patch names matching fluid.*
--> FOAM Warning : Cannot find any patch names matching coolant.*
```

and the result has one patch, `patch0`, the box. Layers can only ever land on the outside of the
brick.

**What does work: anisotropic refinement.** Measured on a plain box, maxCellSize 500 um:

| scaleX, scaleY | cells | max cell volume vs base | effect |
|---|---|---|---|
| 1, 1 | 3,072 | 1.00x | baseline |
| 1, **3** | 1,152 | **3.07x** | 3x coarser in y, what we used |
| **0.5**, 1 | 5,888 | **0.501x** | **2x finer in x** |
| **0.333**, 1 | 8,704 | **0.334x** | **3x finer in x** |
| 0.5, 3 | 2,240 | 2.21x | both, aspect ratio 5.49 |

A scale below one refines by exactly the factor asked. Crucially it adds no octree level, so it
does not hit the 32-bit label wall that makes microchannels +5 impossible with this image.

**So: add `scaleX 0.5` to the microchannel anisotropic box.** Cells become 11 x 66 x 22 um, the
channel goes from 4.6 to **9.2 cells across**, aspect ratio rises to about 6 which is far below
where a second order scheme cares, and the band roughly doubles to about 240M for a total near
530M, inside the 700M ceiling.

**And pay for it by halving the chip band from 3.6 mm to 1.8 mm**, and by clipping its upper half
so it stops refining air that gets deleted. That is 35M cells free with zero fidelity cost, plus
roughly 25M more from the metal side.

Both together land near 400 to 450M cells, about where we are now, with twice the resolution where
the physics is and less of it where it is not.

## What would change this verdict

- The flow rate and fluid, which are unknown. Every error estimate above scales with them.
- A grid convergence study, which needs a `WM_LABEL_SIZE=64` image before it can be run.
- Measuring the interior cell quality rather than only the boundary ring.
