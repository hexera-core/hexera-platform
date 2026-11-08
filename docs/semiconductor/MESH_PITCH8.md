# cbox8: a layered conjugate mesh in 65 minutes

Built 2026-11-08 on `corintis-farfield`. Label `cbox8`, layered coolant `bl8`. Every number here
is read off a run that finished. Where a figure corrects something published earlier it says so.

## Verdict

> **The conjugate interface is 16,619,777 faces**, `fluid_to_solid` matched one to one against
> `solid_to_fluid`, and the chip contact is **270,816**, `chip_to_solid` against `solid_to_chip`.
> Both are read from the mesh's own decomposed boundary files and cross-checked against
> checkMesh's global counts: real patches plus `processor` patches equals (faces - internal faces)
> exactly on all three regions. An earlier draft of this file quoted 25,685,941, which is
> `cbox2b`'s interface - the pitch-10 mesh, not this one.


| | cbox2b + bl3p, what was delivered | cbox8 + bl8 |
|---|---|---|
| background cell | 20.1263 um | **25.1578 um** |
| cells across a channel | 5 | **4** |
| delivered cells | 440,668,679 | **270,424,307** |
| layered coolant | 143,236,900 | **102,536,327** |
| **layer coverage** | 90.7146% | **95.5530%** |
| **faces in error after layers** | 23,833 | **471** |
| end to end, no layers | 1,852 s binary / 10,796 s as run | **1,174 s** |
| **end to end WITH layers** | 10,796 + 10,038 = 5.79 h | **3,925 s = 65.4 min** |

The whole deliverable, layers included, now costs **65 minutes** against 5.79 hours, and the layer
stack covers 95.55% of the conjugate interface instead of 90.71%.

## 1. Why a coarser cell is the better lattice

The conjugate interface lies on octree cell faces, so a channel is forced to a whole number of
cells. The pitch is 201.26253 um, so a cell of pitch/k puts the channel on exactly k/2 cells with
no rounding, for even k. Only two values are available:

| k | dx | cells across | predicted cells | available |
|---|---|---|---|---|
| 6 | 33.5438 um | 3 | 109M | too coarse, +22.2% flow |
| **8** | **25.1578 um** | **4** | **258M** | **built** |
| 10 | 20.1263 um | 5 | 505M | `cbox2b` |
| 12 | 16.7719 um | 6 | 872M | **over the 2^31 face wall at 107.9%** |

`4 x 25.1578 = 5 x 20.12625`, so pitch 8 and pitch 10 give the **identical 100.631 um channel**.
The only difference is how many cells are across it.

**The trade, stated honestly.** Flow bias is the product of a clean term and a messy one:

| | pitch 10 | pitch 8 |
|---|---|---|
| discretisation, exactly `2/N^2` | +8.00% | **+12.50%** |
| quantisation, from the phase model | +0.80% | +0.99% |
| total | +8.86% | +13.61% |

Pitch 8 is 4.75 points worse, and **every point of that is in the clean term**, which is uniform
across every channel and divides out exactly (verified to ratio 1.000000 at N = 5, 10, 20, 40, 80).
The messy term, the only one that redistributes flow between branches, is +0.99% against +0.80%.
So the trade is: a larger but exactly-correctable bias, for half the cells and half the time.

Predicted 258M cells from the measured `dx^-3` scaling; **actual 301,945,509** before the air zone
is dropped, 270,424,307 delivered. The scaling is softer than `dx^-3` because the refined bands do
not scale with the background.

## 2. What it cost, stage by stage

| stage | cbox8 | cbox2b, same stages |
|---|---|---|
| cartesianMesh, 8 ranks | **401 s** | 618 s |
| topoSet | **229 s** | 392 s |
| splitMeshRegions | **306 s** | 471 s |
| regionDicts | 1 s | 0 s |
| checkMesh -parallel, 3 regions | **185 s** | 293 s |
| port split (topoSet -noZero + createPatch) | **52 s** | 78 s |
| copy into the layer case | 2 s | 47 s |
| **addLayers, 8 ranks** | **2,624 s** | 4,714 s on **16** ranks |
| checkMesh -parallel -meshQuality | **125 s** | 2,508 s serial |
| **TOTAL** | **3,925 s = 65.4 min** | |
| reconstructParMesh, afterwards | 1,105 s | 1,640 s |

The layer pass is 1.8x faster on **half** the ranks, because there are fewer cells and because it
converges in fewer repairs. It is still 67% of the whole chain and remains the only long pole.

## 3. Quality

| region | cells | Mesh OK | non-ortho max | avg | skew max | aspect max |
|---|---|---|---|---|---|---|
| fluid | 55,303,441 | yes | **47.92** | 6.01 | 1.646 | 3.739 |
| solid | 199,394,482 | yes | 50.51 | 3.49 | 1.645 | 3.757 |
| chip | 15,726,384 | yes | 0.0024 | 5.8e-6 | 4.1e-5 | 1.00008 |

The coolant is **better conditioned than cbox2b's** (47.92 against 49.45) at a coarser cell. The
solid is marginally worse (50.51 against 48.29). Both are far inside the 65 degree bar.

Ports split clean: `port_a` 1,331 faces and `port_b` 1,308, total 2,639. cbox2b's were 2,092 and
2,048, total 4,140. The ratio 2,639/4,140 = 0.637 is the area scaling `(20.1263/25.1578)^2 = 0.640`,
which is the check that the same two physical ports were cut.

## 4. The layer dict defect, and two failed attempts before the fix

**The defect.** The delivered run requested `firstLayerThickness 4.63e-06` with
`expansionRatio 1.5`, so the stack is `4.63 x (1 + 1.5 + 2.25) = 21.9925 um` into a **20.1263 um
background cell. It does not fit.** The log shows what follows: iteration 0 adds the full 3 layers
at full thickness over 99.7653% of the patch and immediately has 82,548 illegal faces, and the
repair loop then spends ten iterations taking coverage down to 90.7146% while removing only 6.6% of
the interpolation-weight failures. Its only tool is thinning, and thinning manufactures hairline
cells.

A second contradiction sat underneath it: `minThickness 4.0e-07` permits a 0.400 um cell, while
`minFaceWeight 0.02` against a 20.1263 um neighbour requires at least 0.4107 um. So the mover's own
floor produced illegal faces by construction.

**The fix is three keys**, and the stack geometry is the one that matters:

| key | was | now |
|---|---|---|
| `firstLayerThickness` | 4.63e-06 | **4.15e-06** |
| `expansionRatio` | 1.5 | **1.2** |
| `minThickness` | 4.0e-07 | **3.0e-06** |

Stack `4.150 + 4.980 + 5.976 = 15.106 um`, which is 60.0% of a 25.1578 um cell, leaving a
**10.052 um remnant and a 2.50x volume jump** against the delivered 3.426 um and 5.87x.

**Attempt 1 changed six keys and was worse.** It also raised `nSmoothSurfaceNormals` 1 to 6,
`nSmoothThickness` 10 to 50 and `nSmoothNormals` 3 to 10, against a reviewer who said to leave them
alone. Iteration 0 came out at 96.451% coverage against attempt 2's 97.1958%. **The mechanism is
worth keeping: the conjugate interface is a staircase of axis-aligned faces, so smoothing its
normals points the extrusion diagonally across steps rather than along a face.** The low smoothing
values in the delivered dict were load-bearing, not an oversight.

Two further launches failed on case setup rather than physics, and both are worth recording: the
fast chain's `system/` carries no `meshQualityDict`, so a dict that `#include`s one dies; and
`snappyHexMesh` requires `castellatedMeshControls` to exist even with `castellatedMesh false`. The
working route is to copy the proven dict and change only the keys in question.

**The result.**

| iteration | bl3p coverage | cbox8 coverage |
|---|---|---|
| 0 | 99.7653% | 97.1958% |
| 1 | **92.3855%** | **96.2613%** |
| 2 | 91.8166% | 95.7636% |
| final | **90.7146%** | **95.5530%** |

bl3p lost 7.38 points in the single step out of iteration 0. cbox8 lost 0.93. The stack fits, so
the repair loop can fix faces instead of collapsing them.

Final quality on the layered coolant, nine criteria:

| criterion | bl3p | cbox8 |
|---|---|---|
| interpolation weights < 0.02 | 16,379 | **405** |
| face twist < 0.02 | 352 | 55 |
| volume ratio < 0.01 | 1,044 | 5 |
| tet quality < 1e-30 | 3 | 6 |
| determinant < 0.001 | 1,141 | 0 |
| non-orthogonality > 65 | 4,914 | 0 |
| **total faces in error** | **23,833** | **471** |
| max aspect ratio | 345.071 | **322.161** |
| max skewness | 3.475 | **3.256** |
| max non-orthogonality | 77.6263 | **76.6676** |

**50x fewer faces in error**, and the interpolation-weight failure that was 69% of the old defect
fell by 40x. It still reports `Failed 1 mesh checks`, so this is not a clean sheet; 471 faces of
102,536,327 cells is 4.6 per million cells.

## 5. What pitch 8 costs, which is not only the discretisation term

**The coolant volume is 5.28057e-05 m3 against cbox2b's 5.51021e-05, which is 4.17% smaller.** The
coarser lattice fits less fluid into the narrow passages. This is a geometric fidelity loss, not a
discretisation error, and it does not divide out. It is the strongest argument for pitch 10 and it
should be stated to the customer rather than buried next to the speed figure.

## 6. Where it is

| | |
|---|---|
| the three unlayered regions, 8-rank decomposed | `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/cbox8/cbox8_{solid,fluid,chip}.tar.gz`, 8.42 + 2.52 + 0.62 GB |
| the layered coolant, reconstructed, ASCII | `.../corintis/mesh/bl8/bl8_fluid_layered.tar.gz`, 3.38 GB |
| logs, dicts and the run report | `.../mesh/cbox8/cbox8_meta.tar.gz`, `.../mesh/bl8/bl8_meta.tar.gz` |

**One format caveat that matters.** The three unlayered regions are `writeFormat binary`, so they
cannot be indexed, viewed or given per-face quality: `boundary_index.py:88` refuses a binary
polyMesh by design. The layered coolant was written ASCII deliberately and can be. See the binary
section of `MESH_TIME_TO_ONE_HOUR.md`.

## 7. The grading, measured per cell

Asked 2026-11-08 whether the mesh is uniform or actually coarse where it can be. It is graded, and
the grading is the single biggest thing keeping the cell count down. Measured with
`checkMesh -writeFields "(cellVolume)"` run `-parallel` on the decomposed mesh, cube root of each
cell's volume as its equivalent edge. All three regions `Mesh OK` on that run.

| region | cells | volume m3 | finest cell | median edge |
|---|---|---|---|---|
| fluid | 55,303,441 | 5.280576e-05 | 34.696 um | 36.284 um |
| solid | 199,394,482 | 2.280566e-04 | 24.231 um | 36.284 um |
| chip | 15,726,384 | 1.602614e-05 | 100.630 um | 100.631 um |
| **total** | **270,424,307** | 2.968885e-04 | 24.231 um | 36.284 um |

**The headline.** A uniform mesh at the finest delivered cell would need **20,867,373,888 cells**
to fill the same volume. We deliver 270,424,307. The grading is worth **77.2x**. Against the
nominal 25.1578 um background cell it is 68.9x.

**Where the cells are, and where the volume is.** These two columns are the whole point: the cell
budget goes to the channels, and the volume is covered by a handful of huge cells.

| | 1x 25.16 um | 2x | 4x | 8x 201 um | 16x+ 402 um |
|---|---|---|---|---|---|
| fluid, share of CELLS | 92.0% | 2.2% | 2.6% | 3.1% | 0.1% |
| fluid, share of VOLUME | 4.6% | 0.9% | 8.3% | **69.6%** | 16.6% |
| solid, share of CELLS | 92.1% | 1.6% | 2.9% | 2.5% | 0.9% |
| solid, share of VOLUME | 3.0% | 0.3% | 4.9% | 31.9% | **59.8%** |

In the metal, **0.9% of the cells cover 59.8% of the volume**, and 92.1% of the cells are packed
into 3.0% of it - the channel walls. That is what "coarse where it can be" looks like when it is
measured rather than asserted. The chip is deliberately one uniform size, 100.631 um, because it
is a plain block with no gradient to resolve.

**The median cell is anisotropic and that is the stretch doing its job.** 36.284 um equivalent
edge is exactly `cbrt(25.1578 x 25.1578 x 75.4734)`, i.e. the background cell with `scaleY 3`
applied. So the median delivered cell is 25.16 x 25.16 x 75.47 um: three times fewer cells along
the axis the geometry does not vary on. That is a separate 3x on top of the 77.2x above.

**What has already been refuted as a further lever,** so it is not re-attempted: a 4x coarser far
field changes cell count by 0.9% (`MESH_IMPROVEMENT_PLAN.md`), and `scaleY 4` is 7.5x slower for
7% fewer cells (`project_hexera_deliverable_220h`). The lattice choice, not the grading, is what
moved this mesh from 440,668,679 cells to 270,424,307.

## 8. What is still open

- **Running the solve is not ours and never was.** We deliver a mesh; the customer solves on it.
  So "no solver has been run here" is a statement of scope, not a defect, and earlier drafts of
  this record were wrong to file it under what is open. Two things in that neighbourhood ARE ours
  and both are met: the mesh must be *solvable*, which is why the port split, the conformal
  16,619,777-face interface and `Mesh OK` on every region matter; and the error figures we hand
  over must be honest, which is why the flow bias is stated as +13.61% with the clean and messy
  parts separated rather than quoted as a single number.
- **4 cells across is the thinnest resolution anyone here has delivered.** +12.50% on flow is
  exact and correctable, but the thermal entrance error at 4 cells has not been computed; the
  Graetz work in `MESH_QUALITY_CBOX2B.md` section 7 was done at 5.
- The 4.17% coolant volume loss is measured and unexplained in detail: it is known to be the
  coarser lattice in the narrow passages, but not apportioned between the channels and the plenums.
- `Failed 1 mesh checks` on the layered coolant. 471 faces, cause not localised.
