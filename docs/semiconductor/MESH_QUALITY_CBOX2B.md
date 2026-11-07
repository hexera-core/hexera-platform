# How good is cbox2b, the layered coupled mesh

Measured 7 November 2026 on `cbox2b`, on the five STLs it was built from, and on the layered fluid
region `bl3p`. Every number is read off a run that finished or computed from the mesh itself. Where
a figure corrects something I published earlier, including earlier today, it says so and gives both.

The quality figures come from per-face sidecars, not from `checkMesh`'s summary lines. `checkMesh`
reports maxima, and a maximum over 25.7M faces cannot tell one bad face from one in a thousand.

## Verdict

| dimension | grade | the number behind it |
|---|---|---|
| conditioning | **great** | 0 faces in error on all 9 criteria, all three regions, before layers |
| conjugate interface | **great** | 25,685,941 faces matched one to one, max centroid gap 0.00085 cells |
| connectivity | **fine** | both ports in the same piece, proved; the 2 stray cells are 0.0000024% |
| channel fidelity | **pretty good** | median width +0.18% on CAD, but conductance spread 2.75x against the part's 1.89x |
| resolution for CHT | **okayish** | 5 cells across a channel; flow +8.0%, Nusselt +2.2%, wall shear exact |
| layers | **a defect, not a feature** | they moved Nusselt from +2.2% to **-4.0%** |
| cell budget | **poor** | the solid is 74% of the mesh and carries almost no physics |

## 1. What the mesh is

| region | cells | faces | points | pieces |
|---|---|---|---|---|
| fluid | 82,529,515 | 263,003,681 | 98,268,345 | 3, see section 4 |
| solid | 327,126,204 | 1,002,231,588 | 347,702,143 | 1 |
| chip | 31,012,960 | 94,416,838 | 32,394,165 | 2, by design |
| air | dropped | | | |
| **live total** | **440,668,679** | | | |

`cbox2b` as built was 504,715,948 cells, so the far field air region was 64,047,269 of them, 12.7%.

Patches, which are the whole boundary condition surface the solver will see:

| region | patch | faces |
|---|---|---|
| fluid | `fluid_to_solid` | 25,685,941 |
| fluid | `port_a` | 2,092 |
| fluid | `port_b` | 2,048 |
| solid | `solid_to_fluid` | 25,685,941 |
| solid | `solid_to_chip` | 424,080 |
| solid | `solid_to_air` | 1,904,636 |
| chip | `chip_to_solid` | 424,080 |
| chip | `chip_to_air` | 2,331,836 |

The coolant has no `fluid_to_air` patch. It is sealed except for the two ports, which is what it
should be and is worth stating because it is the one topological property that would silently void
a flow solve.

Background cell 20.1263 um in x and z, 60.379 um in y. The y cell is 3x coarser because the mesh was
built on geometry scaled down 3x in y and then scaled back, which is the `scaleY 3` setting that took
the mesher from 38.5 hours to 17 minutes. The aspect ratio reads back as exactly 3.000, which is the
confirmation that the back-scale did what it was supposed to.

## 2. Conditioning

Per boundary face, all three regions, before layers:

| region | non-ortho p50 | p99 | p99.99 | max | skew max | aspect mean | aspect max | faces > 65 deg |
|---|---|---|---|---|---|---|---|---|
| fluid | 0.000 | 2.646 | 45.805 | 49.451 | 1.657 | 3.000 | 3.660 | **0** |
| solid | 0.000 | 1.930 | 45.564 | 48.285 | 1.654 | 2.887 | 3.660 | **0** |
| chip | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.001 | **0** |

`Mesh OK` on all three, and 0 faces in error on every one of the nine criteria. The chip region is
exactly zero on every measure because it is an unrefined block of uniform hexahedra.

The median non-orthogonality is 0.000 on the fluid and the solid. That is not a rounding artefact:
the overwhelming majority of faces on a split octree mesh are perfectly orthogonal, and the
distribution is a spike at zero with a thin tail. The tail is what matters, and p99.99 at 45.8
degrees against a 65 degree bar means it never gets close.

Where the tail sits matters more than its height. The worst 1% of fluid interface faces cluster in
y 24 to 34 mm, on a patch that spans y 21.6 to 58.6 mm, and the worst 25 faces sit in a single spot
at y 33.32 to 33.47 mm, z 65.4 to 66.4 mm. So the bad faces are localised rather than spread through
the channel array, and they are localised away from the chip footprint.

## 3. The conjugate interface

| | |
|---|---|
| `fluid_to_solid` faces | 25,685,941 |
| `solid_to_fluid` faces | 25,685,941 |
| matched | 25,685,941, **100.000%**, one to one |
| max centroid gap | 1.707e-8 m = **0.00085 cells** |
| mesh wetted area | 65,411.2219 mm2 |

The interface is exactly conformal. This took two attempts to establish. The first matched centroids
by sorting on coordinates quantised to 1 nm, but the centroids are float32, which at coordinates of
order 0.1 m resolves to about 6 nm, so the sort keys sat below the precision of the data and
identical faces sorted into different orders. That produced a reported 8,652,419 gaps over 1 nm,
which measured my own method and nothing about the mesh. Replacing it with a spatial hash at half a
cell, probing all 27 neighbouring buckets, gives the result above. **Discard the 8.6M figure.**

## 4. Connectivity, and why the warnings are cosmetic

`checkMesh` reports the fluid region in 3 pieces and the chip in 2. Neither is a defect, and I
proved both rather than arguing them.

The fluid pieces are 82,529,513 cells, 1 cell and 1 cell. The question that matters for a cold plate
is whether the inlet and the outlet are in the same piece, because if they are not then nothing can
flow between them. Taking the owner cell of every port face and intersecting with each piece:

| set | cells | in piece 0 | in piece 1 | in piece 2 |
|---|---|---|---|---|
| `port_a` owners | 1,924 | **1,924** | 0 | 0 |
| `port_b` owners | 1,927 | **1,927** | 0 | 0 |

Both ports are wholly inside piece 0. The two strays are 0.0000024% of the region and touch no port.

The chip's 2 pieces are 6,388,480 and 24,624,480 cells, and `chip.stl` is itself two closed shells.
The chip is two separate bodies in the customer's CAD, so the mesh is right and there is nothing to
fix.

## 5. Geometric fidelity

The five surfaces the mesh was built from:

| surface | triangles | closed | pieces | area mm2 | volume mm3 |
|---|---|---|---|---|---|
| `fluid.stl` | 52,466 | yes | 1 | 60,410.4513 | 53,692.2192 |
| `chip.stl` | 96 | yes | 2 | 17,863.5581 | 16,294.8547 |
| `block.stl` | 27,696 | yes | 1 | 47,955.4164 | 276,364.2873 |
| `fluid_fine.stl` | 32,453 | **no, 111 open edges** | 1 | 21,712.6296 | meaningless |

`fluid.stl` being closed and in one piece confirms `cbox2b` was built on the repaired coolant
surface, not the two-piece one that was missing 132 triangles. `fluid_fine.stl` is open, but it is
only a refinement region, so openness costs accuracy in where refinement lands and not in the
geometry itself. It should still be repaired.

| | CAD | mesh | error |
|---|---|---|---|
| wetted area | 60,410.4513 mm2 | 65,411.2219 mm2 | **+8.28%** |
| coolant volume | 53,692.2192 mm3 | 55,102.6 mm3 | **+2.63%** |

The area excess is the staircase: a stepped surface is longer than the smooth one it approximates,
and +8.28% on a 20 um cell against 100 um features is the expected size.

The volume excess is a defect I cannot yet explain, but I can now say where it is **not**. Spread
over the wetted area, +1,410.4 mm3 implies a mean outward offset of 23.3 um, about 1.16 cells. A
uniform one-cell outward move on both walls would make a 100 um channel 40% wider, and the channels
measure +0.18%. So the excess volume is not in the microchannels. It is in the plenums, headers and
distribution passages, which are large enough to absorb it without a measurable width error.

### Channel width, measured the same way on both sides

Earlier figures for this were computed against an assumed 100.000 um nominal, which is not what the
part is built to. These come from casting rays along the pitch axis through `fluid.stl` and reading
coolant runs off the surface crossings, which is the identical definition used on the mesh, with the
identical filter discarding any scanline that does not cross the array at least 20 times.

| | CAD | mesh | error |
|---|---|---|---|
| channel p5 | 98.295 um | 100.598 um | +2.34% |
| channel **p50** | **100.446 um** | **100.630 um** | **+0.18%** |
| channel p95 | 121.487 um | 140.900 um | +15.98% |
| channel CV | 12.38% | 18.94% | |
| conductance spread, w^3 p95/p5 | **1.89x** | **2.75x** | mesh adds 1.45x |
| fin p50 | 101.255 um | 100.630 um | -0.62% |
| implied pitch | 201.702 um | 201.263 um | -0.22% |

The mesh population is purely discrete, because the interface sits on octree cell faces and a
channel has to be a whole number of cells:

| width | cells | share of wall area | flow vs nominal |
|---|---|---|---|
| 80.5 um | 4 | 1.85% | 0.51x |
| **100.6 to 100.7 um** | **5** | **79.91%** | **1.00x** |
| 120.7 to 120.8 um | 6 | 10.79% | 1.73x |
| 140.8 to 140.9 um | 7 | 3.17% | 2.74x |
| 161.0 um | 8 | 1.45% | 4.10x |
| 181.1 um | 9 | 0.81% | 5.83x |

Against the delivered `cbox2` mesh, which put 36.1% of channels at 88.014 um, 52.7% at 110.017 um
and 6.4% at 132.021 um, with **nothing** at the nominal width and a 3.38x conductance spread, this
is a large improvement. 79.91% now sits on nominal and the three-way split is gone. 88 um does not
appear anywhere in the distribution.

Two defects remain, and they are not the one I expected:

- **The mesh cannot represent the CAD's narrow channels at all.** The CAD's p1 is 97.97 um and its
  p5 is 98.295 um. The nearest widths the lattice admits are 80.5 um and 100.6 um, so every channel
  below 100 um is rounded up to 100.6 and passes about 8% more flow than it should. The mesh's p5 is
  pinned at exactly 5 cells. There is no lattice width between 4 and 5 cells.
- **The pitch is not the cause of the off-nominal tail.** Ten cells is 201.263 um against a CAD pitch
  of 201.702 um, which is 0.22% short, drifting 0.44 um per channel and so one whole cell only after
  about 46 channels. That is too slow to produce 10.79% at six cells. The tail is a phase effect:
  a channel 5.0 cells wide captures 5 cells or 6 depending on where its two walls fall relative to
  cell centres. I have not established the exact distribution of that phase, and I am not going to
  assert a mechanism I have not measured.

Consequently **changing `maxCellSize` cannot fix this.** The cell is already within 0.22% of a tenth
of the CAD pitch. The levers are one more refinement level, which the 32-bit label wall blocks, or
aligning the grid origin to the array, which is free and untested.

## 6. What the resolution costs

The channel is 100.630 um wide by 1509.0 um tall, aspect ratio 15.00, Dh 188.678 um, sitting on
967.6 um of base metal. Width carries 5 cells, height carries 25. At 15:1 the velocity profile is
essentially one dimensional across the short dimension, so 5 cells is the governing resolution.

These are solved, not estimated. The same discretisation OpenFOAM uses, cell-centred finite volume
with a one-sided wall gradient over the half cell, was solved on the real cell distribution and
compared against the analytic result. The method reproduces the textbook parallel-plate Nusselt
number of 8.2353 to 0.002% at 200 cells, which is what licenses the rest of the table.

| grid across the channel | flow rate | wall shear | Nusselt |
|---|---|---|---|
| 5 uniform cells, as shipped | **+8.00%** | **exact** | **+2.18%** |
| plus layers as built, 16.7 um in 2 cells, 3.43 um sliver | +5.28% | exact | **-4.02%** |
| plus layers with no sliver, 3 graded cells | +5.18% | exact | -4.41% |
| 10 uniform cells | +2.00% | exact | +0.67% |
| 20 uniform cells | +0.50% | exact | +0.17% |
| 200 uniform cells, reference | +0.005% | exact | +0.002% |

Three results, in order of how much they change what we do.

**Wall shear is exact at every resolution.** The discrete solution of fully developed flow is the
exact parabola plus a constant offset, and the offset cancels in the one-sided wall gradient. This
also disposes of a claim in my own earlier notes that the mesh ran "9% low on Nusselt". That was
wrong in sign and in subject: the error is not in the wall gradient at all, and where it does land
it is positive.

**Flow rate is over-predicted by exactly 2/N squared**, so +8.0% at 5 cells, and the fit is exact at
every N tested. It is identical in every channel, so it shifts the total hydraulic resistance and
does not redistribute flow between channels. The channel-to-channel maldistribution is the 2.75x
conductance spread in section 5, which is a separate and larger effect.

**Nusselt is over-predicted by 2.18% without layers.** Which is small, and better than the layered
mesh manages.

## 7. The layered mesh, and why the layers are a defect

`bl3p` is the fluid region with 3 boundary layers requested on `fluid_to_solid`.

| | serial, as first run | parallel, 16 ranks |
|---|---|---|
| wall clock | 52,021 s = 14.4 h, one core | **4,714 s = 1.31 h** |
| passes | 34 | 10 |
| faces extruded | 23,046,797 of 25,685,941 = 89.77% | 23,300,896 of 25,685,941 = **90.71%** |
| illegal faces at exit | 4,022 | 23,833 |

11.0x faster, with slightly better coverage, at 5.9x the illegal faces. The illegal count is the
price of cutting `nLayerIter` from 50 to 10 and `nRelaxedIter` from 20 to 3, and it is a price worth
paying: 23,833 illegal faces out of 446,465,029 is 0.053 ppm.

The result:

| | |
|---|---|
| cells | 143,236,900, up from 82,529,515 |
| cells added | 60,707,385 of 77,057,823 requested, 78.78% |
| faces | 446,465,029 |
| layers achieved | 2.36 of 3 requested |
| total layer thickness | 1.67e-05 m, 75.9% of target |
| total volume | 5.51026e-05 m3, **identical to pre-layer** |
| max aspect ratio | 345.071 |
| non-orthogonality | max 77.6263, average 6.87631 |
| max skewness | 3.475 |
| min cell volume | 3.14951e-16, down from 2.09433e-14 |
| verdict | `Mesh OK`, then **Failed 1 mesh check** |

The volume being identical to seven figures is the check that layers only subdivide existing cells
and never move a wall. It passed.

Faces in error, 23,814 of 446,465,029:

| criterion | faces |
|---|---|
| interpolation weights < 0.02 | **16,363** |
| non-orthogonality > 65 deg | 4,917 |
| cells with determinant < 0.001 | 1,124 |
| volume ratio of neighbours < 0.01 | 1,054 |
| face twist < 0.02 | 352 |
| face-decomposition tet quality < 1e-30 | 4 |

The dominant mode, interpolation weight below 0.02, means one cell is at least 49 times its
neighbour. That is the signature of the sliver: the layer stack fills 16.7 um of a 20.126 um cell
and leaves a 3.43 um remnant wedged against a full-size cell, a 5.87x jump on average and far worse
where the layer nearly filled the cell.

**But the sliver is not the real problem.** Removing it entirely, by grading 3 layers to fill the
first cell exactly, makes Nusselt slightly worse still, -4.41% against -4.02%. The problem is the
layers themselves. On a mesh whose wall gradient was already exact, moving cells to the wall while
leaving only 3 coarse cells to carry the core degrades the bulk temperature that Nusselt divides by.
The layer pass bought 2.7 points of flow accuracy and cost 6.2 points of Nusselt.

That verdict is conditional on the flow being thermally developed, and the condition is tight:

| | |
|---|---|
| Dh | 188.678 um |
| longest straight channel run | 12.8 mm = 67.8 Dh |
| thermal entrance length exceeds that run above | **Re 226** at Pr 6.0, Re 316 at Pr 4.3 |

At Re 200 the thermal entrance length is already 11.3 mm of the 12.8 mm available. So below roughly
Re 226 the flow is thermally developed over most of its run and layers hurt; above it the flow is
developing everywhere, the near-wall gradient is steep and transient, and layers help. **We do not
know the operating flow rate**, and that single unknown decides whether the layer pass belongs in
the chain at all.

## 8. What I corrected while doing this

| claim | status | replacement |
|---|---|---|
| 8,652,419 interface gaps over 1 nm | **wrong, my method** | 100% matched, max gap 1.707e-8 m |
| channel width +0.63% | **wrong, assumed a 100.000 um part** | +0.18% on the CAD median of 100.446 um |
| channel width -0.074% | **wrong, used the wall-spacing fundamental as the width** | as above |
| mesh runs 9% low on Nusselt | **wrong in sign and subject** | +2.18% high without layers, wall shear exact |
| the dead air region causes the `-allGeometry` fatal | **refuted** | `chip_to_solid` being a `mappedWall` whose `sampleRegion` is not loaded |
| 55.8% flow CV inside the array | **measurement artefact** | plenum crossings; 18.94% with the array filter |
| wetted area +8.29% | superseded | +8.28%, against a surface that is actually closed |

## 9. The `-allGeometry` gap, closed

`checkMesh -allGeometry` has failed on every conjugate mesh we have ever built, which means
`mesh_ok` has never once been true and a whole class of geometry checks has never run. Two arms on a
copy of the chip region:

| arm | change | result |
|---|---|---|
| A | nothing | rc=1 after 610 s, FOAM fatal |
| B | `chip_to_solid` retyped `mappedWall` to `wall` | **rc=0 after 624 s, `Mesh OK`** |

Arm B passes every check the flag adds: patch topology ok on both patches, face interpolation weight
check OK, face volume ratio minimum 0.998759 and average 0.999859. The cause is that checking one
region alone cannot resolve a `mappedWall` patch's `sampleRegion`, which is not loaded. The cost of
the fix is 2.3%. This should go into the build.

## 10. What is still open

- **The operating flow rate and fluid.** Section 7 cannot be resolved without it, and every error
  estimate in section 6 scales with it.
- **The +2.63% coolant volume.** Narrowed to the plenums and headers, not explained.
- **The phase mechanism** behind 10.79% of channels landing one cell wide.
- **Interior cell quality.** Everything in section 2 is the boundary ring. The interior has never
  been measured directly.
- **A grid convergence study**, which needs a `WM_LABEL_SIZE=64` image to go finer than 5 cells.
- **`fluid_fine.stl`** is open on 111 edges and should be repaired.

## Where the artefacts are

| | |
|---|---|
| `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/cbox2b/` | `fluid.tar.gz` 3,860,506,550 B, `solid.tar.gz` 12,758,125,530 B, `chip.tar.gz` 1,194,225,496 B, `case.tar.gz` 1,486,935 B |
| `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/bl3p/` | `fluid_layered.tar.gz` 5,453,801,464 B from 33.81 GB, `logs.tar.gz` 8,044 B |

Both sets were verified by streaming back out of the bucket and walking the archive, because an
archive that has not been read is not a backup. The `cbox2b` set holds the pre-fix `mappedWall`
boundary files and needs the two corrected ones re-uploaded.
