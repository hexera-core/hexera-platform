# Pitch-8 cold plate mesh: quality ranked section by section

Written 2026-11-08 against `cbox8` (the three-region split mesh) and `bl8` (the layered coolant that
actually ships). Every number here comes out of a log in `bl8_meta.tar.gz` or `cbox8_meta.tar.gz`,
and where I did arithmetic I have left the inputs in so it can be checked.

## What I am grading against

We sell the mesh. The customer runs the solver. So "quality" here is not "did the physics come out
right", it is "can they run it, and are the numbers I gave them about it true". That is seven
conditions:

1. every region closed, no leaks
2. the coolant is one connected circuit, not several
3. the conjugate interfaces match face for face, `mappedWall` on both sides
4. inlet and outlet are separate named patches
5. `checkMesh` does not fail a criterion the solver will trip on
6. all cell volumes positive, no negative pyramids
7. the geometry it represents is their CAD within a tolerance I can state

Nothing below is marked down for "no solver has been run". That is their half of the job.

## Scorecard

| | section | verdict |
|---|---|---|
| 1 | Closure and topology | **Great** |
| 2 | Coolant is one circuit | **Great** |
| 3 | Conjugate interface conformality | **Great** |
| 4 | Cell type composition | **Great** |
| 5 | Port split | **Great** |
| 6 | Grading efficiency | **Great** |
| 7 | Unlayered geometric quality | **Good** |
| 8 | Boundary layer coverage | **Good** |
| 9 | Geometric fidelity against the CAD | **Good** |
| 10 | Channel resolution and its thermal cost | **Good** |
| 11 | Non-manifold interface edges | **Good** |
| 12 | Headroom to the label ceiling | **Good** |
| 13 | Meshing time | **Good** |
| 14 | Layered coolant quality | **Bad** |
| 15 | The chip, and what it is bonded to | **Bad** |
| 16 | Patches pointing at a region we do not ship | **Really Bad** |
| 17 | Quality criteria that were never evaluated | **Bad** |
| 18 | Delivery time, the serial tail | **Bad** |
| 19 | The published record | **Really Bad** |

Six Great, seven Good, four Bad, two Really Bad. One of the Really Bads is not in the mesh at all,
it is in what I wrote about the mesh. The other is: 2,706,383 faces of the delivered mesh reference
a region that is not in the delivery.

## What ships

| region | cells | share |
|---|---|---|
| solid, the plate | 199,394,482 | 62.77% |
| coolant, layered | 102,536,327 | 32.28% |
| chip | 15,726,384 | 4.95% |
| **delivered** | **317,657,193** | 100% |

| | |
|---|---|
| air, built then discarded | 32,601,168 |
| background box, all four zones | 303,025,475 |
| the same three regions with the **unlayered** coolant | 270,424,307 |

270,424,307 is the number I published. It is the three delivered regions with the coolant before
layers. The layers add 47,232,886 cells and what leaves here is 317,657,193.

---

## 1. Closure and topology: Great

| check | fluid (layered) | solid | chip |
|---|---|---|---|
| boundary openness | 4.12e-15 | 3.27e-15 | 1.23e-16 |
| max cell openness | 3.51e-16 | 4.38e-16 | 3.41e-16 |
| boundary definition | OK | OK | OK |
| cell to face addressing | OK | OK | OK |
| point usage | OK | OK | OK |
| upper triangular ordering | OK | OK | OK |
| face vertices | OK | OK | OK |
| face pyramids | OK | OK | OK |
| min cell volume | 4.72e-16 | 1.42e-14 | 1.02e-12 |

Openness is the check that matters most and it is the one I trust, because it cannot be fooled. If
`splitMeshRegions` had dropped a single face anywhere, the region would not close and openness would
not be 1e-15. Every region closes. Every cell volume is positive. No negative pyramids.

The face and point totals reconcile exactly across two independent tools, which is worth showing
because `checkMesh -parallel` reads high and it is easy to quote the wrong one:

```
checkMesh -parallel, layered coolant
    faces           317,886,236      <- sums local faces, so processor faces counted twice
    internal faces  301,035,882      <- excludes processor faces entirely
    boundary patches   16,622,416    <- 16,619,777 + 1,331 + 1,308

snappyHexMesh, same mesh
    faces           317,772,267      <- global, de-duplicated
```

317,886,236 minus 317,772,267 is 113,969, and 317,886,236 minus (301,035,882 + 16,622,416) is
227,938, which is exactly twice 113,969. So there are 113,969 faces on processor boundaries, each
counted twice by the parallel run and not at all in its internal-face line. Put them back:

```
301,035,882 + 113,969 + 16,622,416 = 317,772,267
```

which is snappy's number to the digit. The authoritative mesh is therefore 102,536,327 cells,
317,772,267 faces, 301,149,851 of them internal, 112,948,037 points.

## 2. Coolant is one circuit: Great

```
Number of regions: 1 (OK).
```

on the layered coolant. This is condition two of seven and it is the one that has bitten us before:
a fluid region that comes out in several pieces cannot be solved as one circuit, and we shipped a
four-piece fluid once before finding it was our own STL. Here it is one piece after layers, which is
the state that leaves the building.

`fluid_to_solid` also reports `ok (non-closed singly connected)` and the whole boundary reports
`ok (closed singly connected)`, so the coolant is a single closed surface with two holes in it, the
holes being the ports. That is exactly the topology a flow path should have.

## 3. Conjugate interface conformality: Great

| patch | faces |
|---|---|
| `fluid_to_solid` | 16,619,777 |
| `solid_to_fluid` | 16,619,777 |
| `solid_to_chip` | 270,816 |
| `chip_to_solid` | 270,816 |

Both sides of both couplings, counted independently by separate `checkMesh` runs on separate
regions, agree to the face. Both are typed `mappedWall`. That is a conformal interface, not an
interpolated one, so heat crosses it without a mapping error.

The boundary face budget closes too:

| region | patches | total |
|---|---|---|
| fluid | 16,619,777 + 1,331 + 1,308 | 16,622,416 |
| solid | 16,619,777 + 1,213,523 + 270,816 | 18,104,116 |
| chip | 1,492,860 + 270,816 | 1,763,676 |
| | | **36,490,208** |

Worth noting what `splitMeshRegions` prints and why I do not quote it. Its own interface table says
17,306,761 faces for fluid-to-solid. That counts internal faces between zones summed over
processors, so a face sitting on a processor boundary is counted twice and the table reads 4.1%
high. The reconstructed boundary files are the authority and they say 16,619,777.

## 4. Cell type composition: Great

| type | cells | share |
|---|---|---|
| hexahedra | 101,191,815 | 98.689% |
| polyhedra | 1,344,512 | 1.311% |
| prisms | 0 | |
| wedges | 0 | |
| pyramids | 0 | |
| tet wedges | 0 | |
| tetrahedra | 0 | |

Faces per cell 6.03613, against 6.0 for a pure hex mesh.

Zero tetrahedra is the line I would put in front of a customer. A tet in a conjugate heat transfer
mesh is where the gradient reconstruction goes soft, and there are none. The 1.311% polyhedra are
the refinement transitions and the layer terminations, and their face counts cluster where you would
expect: 1,025,690 of them have 9 faces, which is one hex with one face split four ways at a 2:1
octree transition.

## 5. Port split: Great

| | faces |
|---|---|
| `port_a` | 1,331 |
| `port_b` | 1,308 |
| sum | 2,639 |
| `fluid_to_air` before the split | 2,639 |

Exactly. The ports are the former coolant-to-air contact, cut in two, nothing added and nothing
lost. `createPatch` then reported

```
Removed zero-sized patch fluid_to_air type mappedWall at position 1
```

so the delivered coolant has three patches and no vestigial empty one. Both ports are typed `patch`,
not `mappedWall`, so a boundary condition can be set on them.

They are deliberately not named inlet and outlet. Their STEP named six solids, two of them
`Outlet:1` and `Inlet:1`, and by exclusion `port_a` is the outlet and `port_b` the inlet. I will not
stamp that on a patch name on the strength of an exclusion argument, because `port_a` reading as the
inlet is the one failure here that would silently invert their whole solve.

## 6. Grading efficiency: Great

Finest cell 402.525 um over 2^4 = 25.1578 um, so 1.59299e-14 m3 isotropic. Delivered volume is the
three measured region volumes:

```
solid   2.280570e-04
fluid   5.280580e-05
chip    1.602610e-05
        -------------
        2.968889e-04 m3

uniform at 25.1578 um:  2.968889e-04 / 1.59299e-14 = 18,637,200,000 cells
delivered:                                              317,657,193 cells
                                                              ratio 58.7x
```

Against the unlayered 270,424,307 the ratio is 68.9x.

Both figures are lower than the 77.2x I published. The domain volume I used then was 3.3005e-04 m3,
which is the plate envelope plus the coolant, and the envelope already contains the coolant, so it
double counted. Corrected, the mesh is 58.7x smaller than uniform at its own finest cell, not 77.2x.
Still the right order, still the thing that makes the job possible, but the published number read
about 1.3x high.

## 7. Unlayered geometric quality: Good

| | fluid | solid | chip | bar |
|---|---|---|---|---|
| non-orthogonality max | 47.9213 | 50.5129 | 0.00241 | 65 |
| non-orthogonality average | 6.00697 | 3.48983 | 5.79e-06 | |
| skewness max | 1.64555 | 1.64487 | 4.11e-05 | 4 |
| aspect ratio max | 3.73923 | 3.75705 | 1.00008 | |
| verdict | Mesh OK | Mesh OK | Mesh OK | |

Every maximum is inside its bar with room. Non-orthogonality peaks at 50.5129 against a bar of 65,
and the average is 3.49 in the plate. Skewness peaks at 1.65 against a bar of 4.

The peak has an exact cause, which is why I am not treating it as a defect. A 2:1 octree transition
under the anisotropic stretch gives `atan(scaleY/3)`, which at scaleY 3 is 45.00 degrees, and the
measured maxima of 47.92 and 50.51 bracket it. It is the stretch acting on refinement transitions,
behaving as predicted. There are zero faces over 40 degrees anywhere in the microchannel field
itself.

Good and not Great because of section 16. These three runs were `checkMesh -parallel -constant
-region <name>` with no `-meshQuality`, so five of the ten quality criteria were never evaluated on
them at all.

## 8. Boundary layer coverage: Good

| | requested | achieved |
|---|---|---|
| layers | 3 | 2.84 average |
| near-wall thickness | 4.15e-06 m | |
| overall thickness | 1.51e-05 m | 1.43e-05 m, 94.6% |
| faces extruded | 16,619,777 | 15,880,692, 95.553% |
| cells added | 49,859,331 | 47,232,886, 94.7323% |

The extrusion converged cleanly over ten outer iterations, and the convergence is the part I would
show a customer:

| iteration | faces extruded | removed this pass |
|---|---|---|
| 0 | 97.1958% | 340,361 |
| 1 | 96.2613% | 46,013 |
| 2 | 95.7636% | 30,066 |
| 3 | 95.6596% | 6,136 |
| 4 | 95.6079% | 4,142 |
| 5 | 95.5823% | 1,914 |
| 6 | 95.5695% | 1,073 |
| 7 | 95.5624% | 771 |
| 8 | 95.5570% | 560 |
| 9 | 95.5530% | 468 |

468 faces still moving on the last pass out of 16.6 million, so it had settled. 95.553% is over the
whole extrusion patch; on the surface that can actually take a layer it is 99.8579%, and 77.6% of
the shortfall sits geographically outside the plate block.

## 9. Geometric fidelity against the CAD: Good

| | CAD | mesh | error |
|---|---|---|---|
| coolant | 53,692.2192 mm3 | 52,805.7576 mm3 | **-1.651%** |
| chip | 16,294.85 mm3 | 16,026.10 mm3 | **-1.649%** |
| plate solid | 222,672.1 mm3 | 228,057.0 mm3 | **+2.418%** |

The coolant figure is a mean inward wall offset of 14.67 um, which is 0.583 of a background cell,
spread over 60,410.4513 mm2 of CAD surface. The coolant and the chip are small by the same 1.65%,
which is what you would expect from a lattice that cuts both of them, and the plate picks up most of
what they lose because the three partition the same box.

Chip contact area, which is what the thermal path actually depends on:

```
270,816 faces at the chip's measured cell pitch
cell volume 1.019045e-12 m3  ->  pitch 100.630 um
270,816 x (100.630e-6)^2  =  2,742.45 mm2
CAD                           2,744.50 mm2
                                  -0.07%
```

Half a cell. For the quantity that carries the heat out of the die this is as good as a staircase
can be.

Good and not Great because -1.65% is a real loss of coolant volume and it does not divide out. It is
stated here rather than buried, which is the whole point.

## 10. Channel resolution and its thermal cost: Good

The channel is 100.631 um and it is resolved by 4 cells, not 5. That was the open question in this
mesh and it is now computed rather than argued.

| | 5 cells, pitch 10 | 4 cells, pitch 8 |
|---|---|---|
| fully developed Nusselt error | +0.535% | **+0.679%** |
| mean Nu, Re 100, over the 12.8 mm run | | **+1.41%** |
| mean Nu, Re 226 | | **+2.40%** |
| mean Nu, Re 1000 | | **+4.48%** |
| length-weighted RMS local Nu error, Re 226 | 4.47% | 5.98%, 1.34x |

The solver was validated against every published pitch-10 figure before being used at 4 cells.

4 cells is 1.25x to 1.34x worse than 5 on local metrics, not a different regime. Going back to pitch
10 for this costs +55.4% cells and 5.79 h against 1.08 h, to buy about 0.03 points of mean Nusselt.
The lattices are commensurate, 4 x 25.1578 = 5 x 20.12625 exactly, so both produce the identical
100.631 um channel and the choice is purely how many cells sit inside it.

## 11. Non-manifold interface edges: Good

On the unlayered mesh, both sides of the conjugate interface report the same thing:

```
fluid_to_solid 16619777 16665955  multiply connected (shared edge)
solid_to_fluid 16619777 16666561  multiply connected (shared edge)
Detected 20 bad edges (possibly relevant for finite-area)
  <<Writing 40 conflicting points to set nonManifoldPoints
```

20 edges and 40 points, the same count from two independent runs, which is consistent with it being
one shared surface rather than two separate faults.

After the layer pass:

```
fluid_to_solid 16619777 16652644  ok (non-closed singly connected)
           ".*" 16622416 16655121  ok (closed singly connected)
```

The layer pass removed them. Point count on the interface went from 16,665,955 to 16,652,644, which
is 13,311 fewer, consistent with the pinch points being merged out. So the defect existed, it was
counted, and the mesh that ships does not have it. Good rather than Great because it was fixed by a
side effect of a stage run for another reason, not by anything I did on purpose.

## 12. Headroom to the label ceiling: Good

The binary is `label=32`, so the hard wall is the 2^31 face label, not memory. 767M cells sits at
107.9% of it. Practically the ceiling is 600M to 650M cells.

317,657,193 delivered is about half of that. One more doubling of resolution is available and no
more. Worth knowing that the 700M cell figure Corintis have quoted elsewhere is within 2% of
overflowing a `label=32` build, so if that is where this is going it needs `WM_LABEL_SIZE=64`
decided before the mesh, not after.

## 13. Meshing time: Good

| stage | rc | seconds |
|---|---|---|
| `cartesianMesh` | 0 | 401 |
| `topoSet`, four zones | 0 | 229 |
| `splitMeshRegions` | 0 | 306 |
| region dicts | 0 | 1 |
| `checkMesh` fluid | 0 | 46 |
| `checkMesh` solid | 0 | 127 |
| `checkMesh` chip | 0 | 12 |
| **to a checked three-region mesh** | | **1,122 = 18 min 42 s** |
| copy case for layers | 0 | 2 |
| `snappyHexMesh` addLayers | 0 | 2,624 |
| `checkMesh` layered | 0 | 125 |
| **to the delivered mesh** | | **3,873 = 1 h 4 min 33 s** |

8 ranks x 12 threads. `cartesianMesh` reports `ExecutionTime = 1578.52 s ClockTime = 349 s`, so a
4.52x speedup on 8 ranks, which is in line with everything else we have measured on cfMesh: it
saturates at about four cores and MPI buys nothing beyond that.

Inside the layer stage: layers added in 2,521.87 s, mesh written in 31.06 s, 2,602.18 s total.

Three failed layer attempts cost 216 s before the dict was right (17 + 8 + 7 + 68 + 47 + 69). I have
left them in the table above as the record but excluded them from the total, because the recipe that
ships does not contain them.

1.08 h for a 317.7M cell two-region conjugate mesh with a boundary layer. For reference the same
mesh the long way was 10,796 s to a checked mesh plus 10,038 s for the layer pass, and the earliest
comparable plate was 138,476 s for `cartesianMesh` alone.

## 14. Layered coolant quality: Bad

This is the one real defect.

```
Failed 1 mesh checks.
```

| criterion | bar | faces over it |
|---|---|---|
| non-orthogonality | > 65 deg | **2,348** |
| interpolation weights | < 0.02 | **405** |
| determinant | < 0.001 | **117** |
| face twist | < 0.02 | **55** |
| tet quality | < 1e-30 | **6** |
| volume ratio of neighbours | < 0.01 | **5** |
| face pyramid volume | < 1e-20 | 0 |
| concavity | > 80 deg | 0 |
| skewness | > 4 internal, 20 boundary | 0 |
| **total** | | **2,936** |

Also `*Number of severely non-orthogonal (> 70 degrees) faces: 2096`, max non-orthogonality 76.6676
against an average of 7.85233.

2,936 of 102,536,327 cells is 28.6 per million. It is 8.12x better than `bl3p`, which had 23,833.
It is not zero and the mesh does not pass clean.

2,348 of the 2,936, which is 80%, are the single criterion non-orthogonality over 65, and 405 more
are interpolation weights. 80% of one cause is not a scattered mesh failure.

### Where they are

`reconstructParMesh` did not carry `meshQualityFaces` or `nonOrthoFaces` into the archive, so I
recomputed them from the reconstructed ASCII mesh using OpenFOAM's own algorithms and gated the
result against what `checkMesh` printed:

| gate | checkMesh | recomputed |
|---|---|---|
| total volume | 5.28057e-05 m3 | 5.280574e-05 |
| min cell volume | 4.72424e-16 | 4.724238e-16 |
| max cell volume | 2.19804e-10 | 2.198042e-10 |
| max non-orthogonality | 76.6676 deg | **76.6676** |
| faces over 70 deg | 2,096 | **2,096** |
| faces over 65 deg | 2,348 | 2,349 |

The one extra face over 65 is a face sitting within rounding of exactly 65 degrees: `checkMesh`
compares cosines, I compared the angle. Everything else reproduces to the printed digit, so the
2,349 face positions are good.

**They are on the plenum wall, at the two z ends of the anisotropic stretch box, and they are not in
the channels.**

| group | faces | share | angle min | angle max | thinner cell | within 1.2 mm of a z box face |
|---|---|---|---|---|---|---|
| coarse wall, level +2 | 2,094 | 89.14% | 65.057 | **76.668** | 4.05 um | **96.9%** |
| channel wall, level +4 | 232 | 9.88% | 65.001 | 71.805 | 1.92 um | 7.8% |
| other | 23 | 0.98% | 65.362 | 69.470 | 2.76 um | 8.7% |

96.68% of all 2,349 lie within 1.2 mm of a face of the anisotropic refinement box, and 87.3% of
them on its two z faces specifically:

| | |
|---|---|
| within 1.2 mm of the z lower box face, +0.016734 | 1,034, 44.02% |
| within 1.2 mm of the z upper box face, +0.068072 | 1,016, 43.25% |
| within 1.2 mm of either x box face | 80, 3.41% |
| within 1.2 mm of either y box face | 157, 6.68% |

They collapse onto two planes. 43.12% sit at z = +0.0679 and 42.78% at z = +0.0160, which are the
box's own z limits, and no face outside the box is further than 1.95 background cells from it.

The angle is one tight spike, not scatter:

| | faces | share |
|---|---|---|
| 73.1 deg | 1,010 | 43.00% |
| 72.5 deg | 708 | 30.14% |
| 73.3 deg | 270 | 11.49% |
| the 72 to 74 group | 2,051 | 87.31% |

Mean of that group 72.9230 degrees, spread 72.2595 to 73.8063. One mechanism, not a sick mesh.

### What the mechanism is

The shared face carries the full base area of a layer cell, 3.0380e-08 m2, which is 100.63 x 301.89
um: a level-2 cell face stretched 3x in y. So the two cells are consecutive cells in the layer
stack, and the thinner of them is 4.05 um, against a requested `firstLayerThickness` of 4.15e-06.
The angle then says the two cell centres are offset sideways by

```
4.05 um x tan(72.923 deg) = 13.51 um
```

on a cell 301.89 um wide. That is a 4.5% lateral drift of the stack, and it reads as 73 degrees only
because the step between the centres is 4 um. A thin layer turns a mild sideways drift into a large
angle. On the channel wall the same drift is 4.21 um on a 75.47 um wide cell, and the angle tops out
at 71.8 degrees.

Two other things fall out of the measurement. The neighbour volume ratio is 1.86 across every group,
against an `expansionRatio` of 1.2 in the dict, so the stack is not grading as asked where it goes
wrong. And `nSmoothSurfaceNormals 1` with `nSmoothNormals 3` is exactly the knob that produces a
lateral drift of the extrusion direction, which we already know not to do over a staircase.

### What this changes

The channel walls carry 232 of the 2,936 faces. That is 7.9% of the defect, 2.3 per million cells,
and none of the worst ones: the channel-wall group stops at 71.805 degrees, below the 72.9 spike and
well below the 76.668 maximum. **The mesh is clean where the heat transfer happens.** The defect is
on the distribution volume wall, in the band where the octree grades down from the channel field,
at the two ends of the stretch region.

It also kills the fix I was going to propose. This is not a layer-thickness problem to be reprofiled
and it has nothing to do with the staircase on the channel walls. The two candidates it does point
at are splitting `fluid_to_solid` so that layers are requested on the channel surface and not on the
plenum wall, which removes 89% of the defect and a large number of cells that are doing nothing, and
padding the anisotropic box in z so its faces do not land on wetted surface at all.

One honest limit. I localised the 2,349 non-orthogonality faces. The other 588 faces in error are
the five remaining criteria and I did not compute those, so I am assuming rather than measuring that
the 405 interpolation-weight faces are the same faces. A sheared thin layer gives bad interpolation
weights for the same reason it gives a bad angle, but I have not checked it.

Max aspect ratio in this region is 322.161, against 3.74 before layers. That is the boundary layer
and `checkMesh` passes it as OK. It is not a defect, it is what a 4.15 um near-wall cell on a
25.16 um pitch looks like. Min volume 4.72e-16 m3 clears the 1e-20 bar by four orders of magnitude,
which is the whole reason layers worked here at all.

`snappyHexMesh`'s own final count was 2,888 illegal faces with 2,291 over the non-orthogonality bar,
against `checkMesh`'s 2,936 and 2,348. The two tools disagree by 2%, which is decomposition
handling, not a discrepancy worth chasing.

## 15. The chip, and what it is bonded to: Bad

```
*Number of regions: 2
The mesh has multiple regions which are not connected by any face.
  <<Writing region 0 with 12518256 cells to cellSet region0
  <<Writing region 1 with 3208128 cells to cellSet region1
```

`checkMesh` still printed `Mesh OK`, because a region count is a starred warning and not a failure.
That is exactly the kind of thing that ships unnoticed, and it did.

### The two regions are correct

`chip.stl` genuinely contains two disjoint bodies, and both are lattice-aligned rectangular slabs:

| body | volume in the CAD | size | cells across the thin direction |
|---|---|---|---|
| 2 | 12,987.260 mm3 | 70.000 x 2.360 x 78.615 mm | 23.45 |
| 1 | 3,307.595 mm3 | 49.900 x 1.119 x 59.235 mm | 11.12 |
| sum | 16,294.855 mm3 | | |

That sum is the expected chip volume to the digit. The mesh's two regions map onto them by volume:
12,518,256 cells is 12,756.9 mm3 against body 2's 12,987.260 (-1.774%), and 3,208,128 cells is
3,269.3 mm3 against body 1's 3,307.595 (-1.158%). Both errors are just the fractional cell in the
thin direction: 11 whole cells of 11.12 is -1.08%, 23 of 23.45 is -1.92%.

So the mesh is right about the topology. **What is wrong is what the two bodies are connected to.**

### Only one of them is bonded to anything

Measured off the decomposed chip mesh, every face of `chip_to_solid` sits on a single plane:

| patch | faces | area | y range |
|---|---|---|---|
| `chip_to_solid` | 270,816 | 2,742.46 mm2 | **+0.02011 to +0.02011** |
| `chip_to_air` | 1,492,860 | 15,117.67 mm2 | +0.01658 to +0.02011 |

y = +0.02011 is body 1's top face and nothing else. Body 1's top is 2,955.83 mm2 and 2,742.46 of it
is bonded, 92.8%, the rest trimmed where the plate does not cover its full z footprint.

**Body 2 touches nothing but air.** It is 12,987 mm3, 78% of the chip volume and 12.5 million of
the 15.7 million chip cells, and in the delivered mesh it has no conduction path to anything. The
whole surface budget closes on that reading: body 1 and body 2 are 17,863.46 mm2 of box surface
between them, the mesh reports 17,860.13 mm2 across the two patches, and the `chip_to_air` y
histogram lands where the two bodies' faces are:

| y | faces | what it is |
|---|---|---|
| +0.0166 | 547,228 | body 2 bottom, 5,503 mm2 predicts 543,500 |
| +0.0189 | 544,272 | body 2 top, facing the gap |
| +0.0190 | 293,816 | body 1 bottom, facing the gap, 2,955.83 mm2 predicts 291,885 |
| +0.0201 | 23,000 | the unbonded 213.37 mm2 of body 1's top |

### The 70 um gap, which is the real finding

Body 2's top is at y = +0.01892 and body 1's bottom at +0.01899. The gap is **70 um**, and both of
its faces are `chip_to_air`, so in our mesh the gap is air. 70 um is **0.70 of a 100.631 um cell**,
so a uniform lattice at this resolution cannot put a single cell in it.

The stack, read off the mesh, is: plate / body 1, 1.119 mm / 70 um gap / body 2, 2.360 mm / air.
That is the shape of a spreader bonded under a cold plate with a die beneath it. If that 70 um is a
bond line, then the thing carrying the die's heat into the plate is modelled here as air that we
discard, and the die has nowhere to send it. If it is genuinely an air gap, the mesh is right and
body 2 is a thermally separate part.

**That is the customer's answer, not ours**, which is why this is not being remeshed on a guess. But
two things are ours. We shipped a mesh in which 78% of the chip volume cannot receive heat and did
not say so. And at 100.631 um we could not represent that gap even if they told us tomorrow what is
in it: representing it needs 25.158 um across the band, which is 2.78 cells, and that costs about
+9M cells with an anisotropic y band or +92M isotropic.

### The chip is not surface-fitted at all

Worth stating plainly because it explains every number above. `cfMesh`'s domain is `domain.stl`;
`chip.stl` appears only as a refinement source and then as a `topoSet surfaceToCell` selector. So
the chip region is the set of whole background cells that fall inside the STL, with no snapping and
no surface fitting. Hence non-orthogonality 0.00241 degrees, aspect ratio 1.00008, and every cell
volume between 1.01902e-12 and 1.01907e-12.

For two lattice-aligned rectangular slabs that is close to exact, and it shows: the contact area is
2,742.45 mm2 against 2,744.50 in the CAD, which is -0.07%, half a cell, and the total surface is
within 0.024%. Uniform is also the physically right choice here, because a solid has no boundary
layer: the near-wall packing in a flow mesh resolves gradients that advection creates, and a
conduction field has none. The place a solid does need resolution is across a thin layer with a
steep through-thickness gradient, and that is precisely the 70 um gap.

## 16. Patches pointing at a region we do not ship: Really Bad

The delivery is three regions: `fluid`, `solid`, `chip`. The `air` zone, 32,601,168 cells, is built
by `topoSet` and then discarded. But the boundary files still say:

```
    chip_to_air
    {
        type            mappedWall;
        nFaces          1492860;
        sampleRegion    air;
        samplePatch     air_to_chip;
    }
```

and `solid_to_air` says the same for its 1,213,523 faces. So **2,706,383 faces of the delivered
mesh are typed `mappedWall` against a region that is not in the delivery.** `chtMultiRegionFoam`
constructs those mappings in order to exchange temperatures across them, and it cannot construct a
mapping to a region that does not exist.

`checkMesh` and `snappyHexMesh` do not care, which is how it got this far: neither builds the
mapping, they only need the topology. That is why every quality number in this document is still
valid. It is the solver setup that breaks, and the solver setup is the thing we are selling.

The fix is to retype those patches `wall`, which is also what they physically are: the outside of
the part, where the customer chooses an adiabatic or a convective condition. It is one key per
patch. The 2.3% of this that showed up as a `checkMesh` return code was recorded on 2026-11-07 and
closed by retyping on a check-time **copy**, which fixed the check and left the delivery alone.

Really Bad because this is the one thing on this page that stops the case being run at all, it was
avoidable, and the near-miss was already in the record.

## 17. Quality criteria that were never evaluated: Bad

The three region checks were:

```
Exec : checkMesh -parallel -constant -region fluid
Exec : checkMesh -parallel -constant -region solid
Exec : checkMesh -parallel -constant -region chip
```

No `-meshQuality`. So on the plate, the chip and the unlayered coolant, these five criteria were
never evaluated at all:

| criterion | evaluated on the unlayered regions |
|---|---|
| non-orthogonality | yes, by maximum |
| skewness | yes, by maximum |
| face pyramid volume | no |
| face-decomposition tet quality | no |
| interpolation weights | no |
| volume ratio of neighbour cells | no |
| face twist | no |
| determinant | no |

I have been writing "0 faces over the bar" about this mesh. For non-orthogonality and skewness that
is sound, because the reported maximum is below the bar and a maximum below a bar implies nothing
exceeds it. For the other five it is not a measurement, it is an absence of one. 199 million cells
in the plate have never been checked for determinant or face twist.

This is cheap to close. A `checkMesh -meshQuality` pass on the three regions is 185 s of compute on
the numbers in section 13. It has simply never been run.

## 18. Delivery time, the serial tail: Bad

| stage | seconds |
|---|---|
| `reconstructParMesh`, layered coolant | 1,105 |
| tar layered coolant, 3,631,095,151 B | 935 |
| upload layered coolant | 58 |
| tar solid, 8,415,218,220 B | 1,560 |
| upload solid | 92 |
| tar chip, 615,631,194 B | 124 |
| upload chip | 8 |
| tar fluid unlayered, 2,519,826,412 B | 444 |
| upload fluid | 21 |
| **total** | **4,347 = 1 h 12 min 27 s** |

| | seconds | share |
|---|---|---|
| meshing, checks included | 3,873 | 47.1% |
| reconstruct, tar, upload | 4,347 | 52.9% |
| **wall clock end to end** | **8,220** | **2.28 h** |

More than half the wall clock is packaging. `reconstructParMesh` and `tar` are single threaded on a
box with 32 cores available, so 4,347 s of a 2.28 h job runs on one of them. The tars alone are
3,063 s to produce 15.2 GB, which is 5.0 MB/s, and that is gzip on one core.

This is Bad and it is also the cheapest thing on this page to fix. `tar` piped through `pigz`
across the cores available would take the compression from 3,063 s to roughly a tenth of that. And
the reconstruct may not be needed at all: we already build the boundary index and the quality
sidecar straight off the decomposed mesh in 13 s and 9 s, so 1,105 s of reconstruct exists only to
make one tarball that a customer may prefer decomposed anyway.

## 19. The published record: Really Bad

Three numbers I published about this mesh were wrong, and I found all three only because something
else went looking.

| published | actual | what happened |
|---|---|---|
| 471 faces in error | **2,936** | I quoted the tail of a six-digit number |
| coolant 4.17% smaller | **-1.651% vs CAD** | I quoted a mesh-to-mesh ratio as a fidelity error |
| graded 77.2x below uniform | **58.7x** | the domain volume double counted the coolant |
| 270,424,307 cells delivered | **317,657,193** | the headline counted the unlayered coolant |

The first is the worst. `bl8/check.log` ends `<<Writing 2936 faces in error to set meshQualityFaces`
and its own breakdown sums to exactly that. The only `471` anywhere in the bl8 logs sits inside
`Detected 604471 illegal faces` in `snappy.log`, which is a different stage counting a different
thing. The same slip put zeroes in the table against determinant and non-orthogonality, which are
117 and 2,348. So "50x fewer faces in error" became "8.12x" and "4.6 per million cells" became
"28.6 per million".

The second reversed a conclusion. I called -4.17% "the strongest argument for pitch 10". Against the
CAD, cbox8 is 1.651% small and the mesh it replaced was 2.627% large, so cbox8 is 1.59x closer to
the customer's geometry. It is an argument against pitch 10.

Why this is the Really Bad row and not a footnote. What we sell is a mesh plus a set of honest
numbers about it. The mesh is good. Four published numbers about it were not, and two of them were
wrong in the direction that flattered us. A customer cannot check our mesh, which is the entire
reason they are buying it, so the numbers are the product as much as the cells are. Everything in
this document has its inputs shown for that reason.

---

## What to do next, in order of value per hour

Fix run 1 is in flight on `mesh-fix1` as of 2026-11-08 and covers items 1 to 4. None of it is a
remesh.

1. **Retype the air-facing patches to `wall`.** Section 16. One key per patch, and it is the only
   item on this page that stops the case running at all. The corrected regions go to
   `mesh/cbox8fixed/` and the originals are left alone.
2. **Run `checkMesh -meshQuality` on the three unlayered regions.** Section 17. 185 s. Also run on
   the chip *before* the retype, so the retype is shown to be what moves the return code rather
   than assumed to be.
3. **Three layer variants, measured.** Section 14. The mechanism says the angle is
   `atan(drift/thickness)` with drift about 4.5% of the cell width, so V1 makes the thickness track
   the cell size (`relativeSizes true`, predicted about 15 degrees everywhere), V2 removes the
   normal smoothing that creates the drift, V3 does both. Whichever reports fewest faces in error
   without losing coverage wins. The patch split is held in reserve: 95.689% of the interface is
   already level +4, so it would remove the defect but save only about 2M cells, not the large
   number I first claimed.
4. **`pigz` the tars and skip the reconstruct entirely.** Section 18.

Still open after that run:

5. **Ask what is in the 70 um gap between the two chip bodies.** Section 15. This is a question for
   the customer and it gates everything else about the chip. If it is a bond line, body 2 needs a
   conduction path and the gap needs 25.158 um across it, which is about +9M cells with an
   anisotropic y band. If it is air, the mesh is already right and only the documentation is
   missing.
6. **Localise the remaining 588 faces in error**, the five criteria other than non-orthogonality.
   Cheap now that the reader exists.

Not on this list: a new mesh. 25 proposals were analysed on 2026-11-08, 10 were adversarially
verified, 3 survived and none of the three changes the mesh.
