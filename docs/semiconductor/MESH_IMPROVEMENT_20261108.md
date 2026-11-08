# What would make the pitch-8 mesh better: the answer is almost nothing

Run 2026-11-08. Five dimensions analysed in parallel, 25 proposals, 10 adversarially verified,
**3 survived — and none of the three changes the mesh.** Every number below that I rely on, I
re-derived from source before accepting it.

## The verdict first

| | |
|---|---|
| proposals | 25 |
| verified adversarially | 10 (15 over the cap, listed unverified) |
| survived | **3** |
| of those, mesh changes | **0** |

Two survivors are corrections to figures I had published. The third is *"do not add a fourth
layer."* The mesh is at a local optimum that this analysis could not improve on.

## 1. The thermal entrance error at 4 cells, which was the largest open question

This was the biggest genuinely uncomputed number in the record, and it is now computed. The
solver was validated first: it reproduces every published pitch-10 Graetz figure to the last
digit before being used at 4 cells.

| | 5 cells (pitch 10) | 4 cells (pitch 8) |
|---|---|---|
| fully developed Nusselt error | +0.535% | **+0.679%** |
| mean Nu, Re 100, over the 12.8 mm run | — | **+1.41%** |
| mean Nu, Re 226 | — | **+2.40%** |
| mean Nu, Re 1000 | — | **+4.48%** |
| length-weighted RMS local Nu error, Re 226 | 4.47% | 5.98% (1.34x) |

**4 cells across is not the thermal risk.** It costs 0.14 points of Nusselt in the developed
limit and is 1.25x to 1.34x worse than 5 cells on local metrics — not a different regime. Going
back to pitch 10 for this would cost +55.4% cells on the layered mesh and 5.79 h against
65.4 min, to buy about 0.03 points of mean Nusselt. That trade is not close.

## 2. Two figures I published are wrong

Both corrected in `MESH_PITCH8.md` (commit `d79c7273`), both re-derived here independently.

**"471 faces in error" is not a number in any log.** `bl8/check.log` ends
`<<Writing 2936 faces in error to set meshQualityFaces`, and the breakdown sums to exactly that:
2,348 + 405 + 117 + 55 + 6 + 5. The only `471` anywhere in the bl8 logs is inside
`Detected 604471 illegal faces` in `snappy.log` — a different stage counting a different thing.
The published figure was the **tail of a six-digit number**. Consequently "50x fewer faces in
error" is **8.12x**, and "4.6 per million cells" is **28.6 per million**.

**"4.17% less coolant volume" is a ratio between two meshes, not a fidelity error** — and the
correction reverses the conclusion:

| | volume | vs CAD |
|---|---|---|
| CAD, `clean_stl/cb_fluid.stl`, 52,466 tris | 53,692.2192 mm3 | — |
| **cbox8** | 52,805.7576 mm3 | **-1.651%** |
| cbox2b | 55,102.6013 mm3 | **+2.627%** |
| cbox2 | 53,419.4018 mm3 | -0.508% |

cbox8 is **1.59x closer to the CAD** than the mesh it replaced. The paragraph called this "the
strongest argument for pitch 10"; it is an argument against it. Wetted area runs the same way:
+5.728% against +8.456%.

## 3. What ships is 317,657,193 cells, not 270,424,307

The headline counts the **unlayered** coolant. What is delivered is the layered one:

| | cells |
|---|---|
| solid | 199,394,482 |
| chip | 15,726,384 |
| coolant, **layered** (`bl8`) | 102,536,327 |
| **delivered total** | **317,657,193** |
| the same with the unlayered coolant, as published | 270,424,307 |

The layers add **47,232,886 cells**. This matters because 317.7M is just outside the 150-300M
band, where 270M is inside it.

## 4. Things the analysis explained rather than fixed

- **The non-orthogonality has an exact cause.** A 2:1 octree transition under the anisotropic
  stretch gives `atan(scaleY/3)` = 45.00 degrees at scaleY 3, which brackets the measured maxima
  of 46.9146 (fluid) and 50.5129 (solid). It is the stretch acting on refinement transitions, not
  a mesher defect. **Zero faces over 40 degrees anywhere in the microchannel field.**
- **Layer coverage restated honestly.** 95.5530% is over the whole extrusion patch. On the
  surface that can actually take a layer it is **99.8579%**, and 77.6% of the shortfall is
  geographically outside the plate block.
- **The error faces are 80% one criterion.** 2,348 of 2,936 are non-orthogonality > 65 — what a
  layer stack extruded off a staircase produces. checkMesh already wrote them to the sets
  `meshQualityFaces` and `nonOrthoFaces`, so locating them is a read, not new work.

## 5. Refuted, so nobody re-proposes them

| proposal | why it failed |
|---|---|
| thicken the 3-layer stack to 22.0 um | the stack must fit one background cell; this breaks the rule the last fix established |
| cap the coolant boundary cell at 4x instead of 8x | does not address the volume error, which is phase noise not bias |
| sweep the octree origin before meshing | grid origin already refuted for the flow bias |
| close coverage from 95.55% to 100% | 99.86% on layerable surface already; the rest cannot take a layer |
| add a fourth layer | the L+2 margin is 2.0%; a 4th layer breaks the fit rule |
| more cells across the channel | +55.4% cells for 0.03 points of Nusselt |
| reduce the stretch to cut non-orthogonality | scaleY is what buys the 3x cell saving; the angle is predicted, not pathological |

Also measured and ruled out: **scaleY is not a volume lever.** At pitch 8 with scaleY 1/2/3/4/6
the volume errors are +0.122%, -0.238%, -0.053%, -0.713%, -0.563% — all inside 0.8%, no trend.
(This is a different question from the already-refuted scaleY 4, which failed on time and cells.)

## 6. Not verified, and labelled as such

One agent argues the volume error is **zero-mean lattice phase noise** rather than a bias — that
-1.651%, +2.627% and -0.508% are 0.48, 0.76 and 0.15 standard deviations of one distribution set
by the coarsest cell touching the boundary. That proposal was **refuted** in verification, so the
framing is recorded here as unconfirmed, not adopted. If true it would mean the volume figure
needs an error bar rather than a fix; it should be settled before anyone acts on the -1.651%.
