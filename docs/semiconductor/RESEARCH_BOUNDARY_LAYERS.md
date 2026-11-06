# Boundary layers and grading on the conjugate plate: what 37 agents found

Research sweep run 2026-11-05. Source journal: `wf_f5fee2ef-012/journal.jsonl`, 37 result
records (16 research angles, 20 individually verified techniques, 1 synthesis).

The question put to the sweep was: is there genuinely no way to get boundary layers or near-wall
grading onto a two-region conjugate mesh of the Corintis cold plate, and is there a more
efficient approach than 413M uniform cells. The answer to the first half is no, there is a way,
and it was run end to end inside `hexera-mesh:2412` rather than read about. The answer to the
second half is yes, and it is not a better octree.

The sweep also demolished the premise it was given. That is recorded here as plainly as the
findings, because the premise was mine.

---

## 1. What was run, and how

| | |
|---|---|
| angles | 16 |
| verified techniques | 20 (9 refuted, 11 work with caveats) |
| synthesis records | 1 |
| angles that exhausted the shared web-search budget | 11 of 16 |
| image used for every experiment | `hexera-mesh:2412`, OpenFOAM-2412 patch=260127, `LSB;label=32;scalar=64`, `linux64GccDPInt32Opt` |
| independent verification passes on the layers route | 5 |
| `snappyHexMesh` addLayers-only runs across those passes | about 40 |

The shared WebSearch allowance was 200 calls across every agent in the run and it was exhausted
early. Eleven angles reported falling back to reading primary source and running experiments in
our own image instead. That produced stronger evidence than the searches would have: the layers
result is a measurement on our binary, not a forum claim.

### Domains that are hard-blocked from this environment

Worth writing into the research playbook, because this cost the sweep real time.

| blocked | how it fails |
|---|---|
| cfd-online.com | 403 to WebFetch and to curl with a browser UA; Cloudflare interstitial in the browser pane that did not clear; `?print=` and `printthread.php` variants also fail |
| sciencedirect.com | 403 on every article and on `/topics/` pages |
| nature.com | 303 to `idp.nature.com` |
| develop.openfoam.com | GitLab sign-in wall; announces the repos moved to `gitlab.com/openfoam/core/openfoam` |
| gitlab.onelab.info | Anubis bot protection, "Access Denied"; raw URLs redirect to sign-in |
| docs.lib.purdue.edu | 403, and served one PDF then switched to bot-detection HTML |
| link.springer.com, wiley, ASME, researchgate, SSRN, academia.edu | 403 or IdP redirect |
| mdpi.com article HTML | 403 (but `mdpi-res.com` PDFs work) |
| gitlab.com raw | works for one file then HTTP 429, Retry-After up to 3396 s |
| openfoam.com `/man/` pages | 404 for every mesh utility now |
| journal.ucas.ac.cn, etd.lib.metu.edu.tr | connection refused |

### Routes that do work

| route | note |
|---|---|
| `api.openfoam.com/2412` doxygen | reliable for headers and class pages; an offset read of a long source page can silently cross into a neighbouring class, so do not assert from a partial read |
| arXiv PDFs | fetch saves the file, then `pdftotext -layout` on the saved path |
| OSTI `/servlets/purl/` | works |
| cloning the source | the strongest route; `Community/integration-cfmesh` (project id 22) is fully readable anonymously via the GitLab API |
| EPFL Infoscience | the `/bitstreams/<uuid>/download` path rate-limits at 429 with Retry-After 900; the REST API route works (`/items/<uuid>/bundles` then `/bundles/<uuid>/bitstreams` then `/bitstreams/<id>/content`) |
| Google Patents XHR endpoint | how both Corintis patents were found after search budget ran out |
| `cfmesh.com/help` release notes | fetch directly, they answer what the marketing pages do not |
| running the thing in our own image | produced every number in section 3 |

PDFs generally come back as FlateDecode binary through WebFetch and yield nothing. Download, then
`pdftotext`. Several agents wasted calls concluding "no information found" on a PDF that extracts
perfectly.

---

## 2. The 700M ceiling is mine, and the sweep was briefed wrong

The synthesis repeatedly calls 700M cells "the customer's ceiling" and in one place quotes it back
as theirs. That is wrong, and it is my error, not the sweep's. `DEVLOG.md` line 3982 says it
plainly in my own words: the 11 um attempt "said it would cost 623M cells, inside Rehaan's 700M
ceiling." The figure is my instruction. Nothing in any Corintis publication, preprint or patent
states a cell count, an element count or a mesh-size limit, and the corintis-epfl angle checked
all four publications, the 300-page van Erp thesis, the POWERlab publication list and a Scholar
profile looking for one. Every "inside the 700M ceiling" statement in this document means inside
my own budget.

One coincidence makes it worth keeping anyway. Measured faces per cell on a hex mesh is 3.02
(5,566,220/1,840,000 and 11,961,548/3,959,680), so the 32-bit face-label ceiling of 2,147,483,647
faces lands at about 711M cells. My 700M number sits within 2% of overflowing this build. The
real ceiling on this image is roughly 600 to 650M cells.

---

## 3. Boundary layers: the route that works

`snappyHexMesh` run as a pure layer-adder on a mesh it did not build.

```
castellatedMesh false;
snap            false;
addLayers       true;
geometry        {}
```

Two variants both work. The one to run is the **post-split** one: after `splitMeshRegions`, on
the fluid region alone, keyed on the `fluid_to_solid` patch. The pre-split variant works too,
keyed on an internal faceZone whose name contains `_to_`, because `snappyHexMesh.C:1616` parses
that substring and synthesises the master/slave patch pair before the three mode switches at
L1725-1733 are even read. The log prints
`On faceZone X adding layers to master patch X only. Freezing points on slave patch X_slave`.

The pre-split variant is ruled out on memory, not capability: see the table below.

### Measured, on our own binary

| what | measured |
|---|---|
| peak RSS per output cell | 1.61 kB, by VmHWM polling |
| whole-mesh route at 500.1M output cells | about 805 GB peak. Impossible on anything we have |
| post-split fluid-region route (68.8M to 155.6M cells) | about 250 GB peak. Wants a 512 GB box, marginal on 256 GiB |
| coverage, clean runs | `Extruding N out of N faces (100%)` |
| cell types out | all hexahedra, 0 polyhedra on an all-hex input |
| `checkMesh` | Mesh OK |
| parallel | 4-way MPI gave byte-for-byte the serial result: 706,560 of 706,560 extruded, 2,119,680 added, unbalance 0.0097 |
| layer-addition time | 243.06 s of a 291.47 s pass on 706,560 extruded faces |
| extrapolated to 21,708,529 faces at 4 layers | about 3 to 6 h serial, 1 to 2 h on 8 to 16 ranks. The 2.60 h chain becomes 4 to 6 h |

### The conformal interface survives, and this was measured not assumed

Verifiers computed order-sensitive md5 hashes over patch-face point coordinates, plus independent
area integrals out of `points`/`faces`, rather than reading log lines.

| | before | after |
|---|---|---|
| faceZone faces | 5336 | 5336 |
| faceZone area | 1.844045802e-05 m2 | 1.844045346e-05 m2 (2.5e-7 percent) |
| post-split patch geometry hash, fluid side | `4601667372addbdc` | `4601667372addbdc` |
| post-split patch geometry hash, solid side | `0969234b17c5b1a5` | unchanged |
| post-split interface pairing | 667 == 667 across 8 interfaces, summing to 5336 | |
| total mesh volume, `refineWallLayer` 3 passes | 4.806709364e-11 | bit-identical at every pass |

The structural reason: layers shrink the interior and insert cells, they never move the extrusion
base. So our 21,708,529 == 21,708,529 `mappedWall` `nearestPatchFace` coupling and its zero
interpolation error are preserved exactly. Face count alone proves nothing, because it stays equal
even when geometry drifts. Integrate the areas and hash the coordinates.

### The layer ladder, measured

110 um slot, 4 layers, first 1 um, total 8 um, `relativeSizes false`:

| position | um |
|---|---|
| layer 1 | 0.978 |
| layer 2 | 1.456 |
| layer 3 | 2.166 |
| layer 4 | 3.224 |
| core | 18.871 |

Expansion ratio measured 1.4887 / 1.4876 / 1.4885 against a computed 1.4886, which is exactly
what `thicknessModel firstAndOverall` gives for 4 layers, first 1 um, total 8 um.

On the real 100.76 um channel at 3 layers, ER 1.5, `maxThicknessToMedialRatio 0.6`, 100% coverage,
22.0 um achieved (100% of target):

```
4.62 / 6.93 / 10.40 / 11.37 / 11.37 / 11.37 / 11.37 / 11.37 / 10.40 / 6.93 / 4.62 um
```

11 cells across instead of 4.6. Note what happened to the core: the medial-axis shrink compressed
the bulk cell from 20.15 um to 11.37 um as well as inserting the stack. First cell centre goes
from 11.0 um off the wall to 0.49 to 2.0 um depending on the recipe.

### Cost and quality

| | |
|---|---|
| 4 layers, fluid side only | +86.8M cells, 413.3M to 500.1M (71% of my ceiling, 1.510B faces = 70% of 2^31) |
| 2 layers, fluid side only, at measured 1.73 cells/face | +37.6M, to 450.9M |
| 4 layers both sides | +173.7M, to 586.9M. Measured worse: average non-orthogonality 16.03 against 12.78 one-sided |
| non-orthogonality on a staircased interface | max 0 to 48.8-53.3, average 0 to 7-17 |
| max skewness | about 0 to 1.75-2.84 |
| max aspect ratio | 3.70 to about 20-52 |
| hexahedra | stays 100% at full coverage; partial coverage is what manufactures polyhedra |

We lose "zero faces over 50 degrees". `COUPLED_MESH_DELIVERY.md:43` says the real bar is 70
degrees and our measured max is 51.46, so the 50-degree line was never true anyway. See section 11.

Do not try to buy the quality back with `maxNonOrtho 50`: measured, coverage collapses from 100%
to 46.2% and achieved thickness from 22 um to 4.98 um (22.7% of target). `maxNonOrtho 40` gives
the identical degraded result, so it saturates.

### Two findings that decide whether this works on our part

**A 2:1 refinement transition sitting ON the layer patch is fatal.** Measured: coverage collapsed
to 384 of 12,992 faces (2.96%), 0.043 of 4 layers, 0.485% of requested thickness, cause printed as
`Number of isolated points extrusion stopped : 3848`. Three rescue attempts (thinner absolute
layers, `relativeSizes true` with `finalAndExpansion 0.25`, 4 layers at 0.2) all went to 0 of
12,992 (0%). It is not a thickness-model problem. The same transition placed one cell AWAY from the
patch gave 100% coverage, 4 layers, area bit-identical, with 560 polyhedra present and
non-orthogonality unchanged at 54.74. So polyhedra and octree transitions in the mesh are harmless.
A transition on the extrusion patch is fatal.

Our wetted surface is 95.2% at 22 um, 3.5% at 176 um and 0.6% at 88 um, with a 4x cliff in 0.5 mm.
The interface therefore spans levels and this must be measured before anyone commits. Mitigation:
split the interface by cell level in `topoSet` into a fine faceZone and a coarse one, and layer
only the fine zone. A layer patch bordering a non-layer patch is fine, that configuration gave
100%.

**Fewer and thicker beats more and thinner**, the opposite of normal boundary-layer practice.

| request | coverage | layers achieved | polyhedra | non-ortho max |
|---|---|---|---|---|
| 6 layers, 1 um first, ratio 1.2 | 24.1% | 0.62 of 6 | 552 | 63.86 |
| 8 layers, 0.7 um first, ratio 1.25 | 84.5% | 1.71 of 8 | 1840 | |
| 4 layers, 8 um first, ratio 1.3 | 100% | 4 of 4, 24.8 um delivered (50.2% of request) | 0 | |

`maxThicknessToMedialRatio 0.3` caps the stack at 0.3 x 50.38 = 15.11 um per side in our channel.
Raise it to 0.6 for about 22 um. The practical ceiling in a 110 um slot is 12 to 25 um of stack
per side. Target 100% coverage: partial coverage is what produces the polyhedra and the worst
non-orthogonality.

One further trap, measured. On the post-split route, pinning the region's own side patches at
`nSurfaceLayers 0` gave 93.1% coverage with 92 polyhedra and non-orthogonality 62.09. Say nothing
about them and let them slide, and it is 100% and all-hex. Name the other *interface* patches at 0;
leave the ports and outer walls unnamed.

---

## 4. The `minVol 1e-13` trap, which is why all of this looked impossible

The shipped `/usr/lib/openfoam/openfoam2412/etc/caseDicts/meshQualityDict` has

```
minVol 1e-13;   // "Is absolute volume of cell pyramid. Set to a sensible fraction
                //  of the smallest cell volume expected."
```

It is an **absolute** face-pyramid volume. A 22.0035 um cube's face pyramids are
(1/3)(22e-6)^2(11e-6) = 1.78e-15 m3, 56x below it. So every face of a geometrically perfect mesh
is flagged purely on length scale. The break-even cell edge is about 84 um; we are at 22 um.

Reproduced on a 20x20x10 blockMesh at exactly 22.0 um, 4000 cells, addLayers-only:

```
faces with face pyramid volume < 1e-13 : 12800     (of 12800)
Extruding 0 out of 1600 faces (0%)
Added 0 out of 4800 cells (0%)
Finished meshing with 12800 illegal faces
checkMesh: Mesh OK
exit code 0
```

Silent, total, zero-layer failure, exit 0.

Ablation on the identical case:

| config | coverage |
|---|---|
| shipped defaults | 0% |
| `minVol 1e-20` alone | 100%, Mesh OK |
| `minVol` + `minTetQuality -1e30` | 100%, identical mesh |
| `minVol` + `minFaceWeight 0.005` | 100%, identical mesh |
| all three | 100%, identical mesh |
| `minVol 1e-18` | 100%, identical mesh |

`minVol` is necessary AND sufficient. Do **not** also set `minTetQuality -1e30`:
`tetrahedron::quality()` returns `mag()/(8/(9*sqrt(3))*pow3(circumRadius()) + ROOTVSMALL)`, which
is volume over length cubed, dimensionless and scale-invariant, so the shipped 1e-15 was never the
blocker at 22 um, and clearing it only disables inverted-cell detection. The only dimensional
entries in that file are `minVol` (enabled), `minArea` (-1, disabled) and `minEdgeLength`
(-1, disabled). Everything else is dimensionless.

Use `minVol 1e-20`, or principled, 1e-4 x smallest cell volume, which is about 1e-18 and also gave
100%.

**This voids our own QA evidence, independently of layers.** `checkMesh -meshQuality` with the
shipped dict would report all 47.7M faces of the mesh of record as failing. Any such report we hold
is worthless unless `minVol` was overridden, and I have not checked which of ours were. That is a
latent defect in the quality evidence, not in the mesh.

One angle reported needing `minFaceWeight 0.005` as well, to stop a monotone unravelling from 98%
to 0% over 14 iterations. That did not reproduce in the ablation above and appears specific to that
agent's geometry rather than to the 22 um scale. Related: a `relaxed { ... }` sub-dict in
`meshQualityControls` does nothing until `nRelaxedIter` iterations have passed, default 20, so an
unravelling that finishes in 14 never sees it. Easy to mistake for "relaxing the criteria does not
help". Leave `nRelaxedIter` at 20 once `minVol` is right.

---

## 5. Refuted routes. Do not re-attempt these

| route | why it is dead |
|---|---|
| cfMesh `boundaryLayers` during `cartesianMesh` | The v1.1 User Guide section 4.5 states it verbatim: layers "are extruded from the boundary faces of the volume mesh towards the interior, and cannot be extruded prior to the meshing process." `cartesianMeshGenerator.C` runs `generateBoundaryLayers()` as step 6 of 10, calling `boundaryLayers::addLayerForAllPatches()` against the *mesh's* patches, and our mesh has exactly one patch (the box STL) at that moment. Our `Cannot find any patch names matching coolant.*` is a Warning, not a Fatal, and the run finishes with exit 0 and a layer-free mesh. The guide also says thickness "is controlled by the cell size specified at the boundary", so even on a real patch cfMesh's layers are about 0.5 cell thick with no user control. |
| cfMesh `generateBoundaryLayers` post-split | Its `meshOptimisation()` is unconditional: `optimizeMeshFV(); optimizeLowQualityFaces(); untangleMeshFV(); optimizeBoundaryLayer(); untangleMeshFV();` with no `lockPoints`, no `lockCells`. `untangleMeshFV` builds `lockedPoints` only from `vertexLocation_ & LOCKED` and then runs an explicitly unconstrained boundary-vertex pass (`optimiseBoundarySurfaceLaplace()`, `optimiseBoundaryVolumeOptimizer(false)`); the assertion that boundary vertices were not moved is inside a commented-out `#ifdef DEBUGSmooth`. So it would silently drift the fluid interface off the solid interface: both sides still report 21,708,529 faces, `nearestPatchFace` still finds a nearest face, the solver still runs, and the conjugate flux is quietly non-conservative. Separately its layer-validity gate is all-or-nothing per patch (one face with layer id < 0 clears the whole patch) and our octree-transition polyhedra will fail it, and the thickness is uncontrollable, hard-wired to 0.5 x local cell size. |
| `snappyMultiRegionHeater` as a precedent | Read the v2412 dict. It ships `addLayers false;` and its entire `layers` block is `maxY { nSurfaceLayers 3; }`, an external box patch, nothing to do with its five cellZones. Its own `snapControls` carries the comment "Of limited use in this case since faceZone faces not handled". Anyone citing this tutorial for conjugate layers is wrong. |
| Non-conformal AMI coupling | Measured, at perfect alignment and matched cell size, with correct exact patch names: source sum(weights) min 0.931498 max 1.34494 average 1.0038; target min 0.599425 max 1.02459 average 0.990967. That is +34% / -40% local flux error replacing exactly zero across 21,708,529 matched faces. Cause measured: nearest-donor centre distance between the two independently meshed surfaces is p50 0.001 um, p90 5.534 um, max 10.840 um, half a cell, because cfMesh projects boundary points onto the surface and the two meshes see it from opposite sides. Coarsening the solid, the only reason to want this, made it worse: at 44.0070 um the weights go to 0.713365-1.94228, target min 0.492444, `checkMesh` "Failed 2 mesh checks", and the solid's hole boundary is geometrically wrong (19 um bbox overshoot) because a 102.01 um fin is 2.3 cells. |
| `createPatch autoPatch` to promote the internal interface | Exit 0, boundary file unchanged, no new patch, just `faceAreaWeightAMI` "Invalid normal for source face 0" warnings because it was AMI-ing the patch against itself. Forcing it with a faceSet of the internal faces hard-stops at `createPatch.C:419`: "Face 160 is not an external face of the mesh. This application can only repatch existing boundary faces." |
| cfMesh `workflowControls { stopAfter; restartFromLatestStep; }` to iterate layer settings | The step order in `cartesianMeshGenerator.C` is `boundaryLayerGeneration` BEFORE `meshOptimisation`, the reverse of the ordinal map in `workflowControls.C` and of the user guide table, so a `meshOptimisation` stop leaves only `boundaryLayerRefinement`. `polyMeshGen::write()` unconditionally `rm`s owner/neighbour/cells/boundary/pointZones/faceZones/cellZones/meshModifiers/parallelData/meshMetaDict and `rmDir`s `sets/` on every write, so any stop-restart cycle in a case that holds the zones destroys them. And `stopAfterCurrentStep()` throws a `std::string` that the constructor catches with `Info<< "Here"`, so a left-behind `stopAfter` yields a half-built mesh, the word "End", and exit 0. That is exactly why `stage_plate` is truncated. |
| `WM_LABEL_SIZE=64` rebuild | Refuted, and it is a regression. Our own `BUFFER_PROBE.txt` (5 Nov) already refuted the label-overflow diagnosis: the error is `MPI_ERR_BUFFER`, and `MPI_Buffer_attach(void*, int)` takes an int so `MPI_BUFFER_SIZE` caps near 2^31 BYTES, which `WM_LABEL_SIZE` cannot touch. The octree cube struct was compiled and measured at exactly 16 bytes at int32 and 32 at int64, so the rebuild HALVES the box ceiling: the currently working level-14 mesh needs 59,795,379 boxes, 0.96 GB at int32 and 1.91 GB at int64 against a 2.147 GB hard cap, an 11% margin on a mesh that works today. `ROOT_PROBE.txt`, same day, has the free alternative: halve the octree root from 0.360505 to 0.180253 m (the part is 177.500 mm in x) so microchannels +5 lands on level 14 instead of 15, giving 11.00 um at a box count that already works. |
| `refineBoundaryLayers` as the sub-22 um route | Not in `hexera-mesh:2412` at all. 312 binaries in `platforms/linux64GccDPInt32Opt/bin`, and it is not among them. |
| cfMesh `localRefinement` + `importSurfaceAsSubset` on the coolant surface | Verified no-op. `surfRefLevel_[triI]` is indexed by triangles of the DOMAIN surface, which is `domain.stl`, the box with two ports cut out, and it has no coolant triangles. `importSurfaceAsSubset` matches by proximity and normal, keeping a match only if `dSq <= 0.09*maxEdgeDSq`, so it creates a subset that exists and is empty, with "Could not find a matching triangle" warnings, and `localRefinement` then warns and skips. |
| `refineHexMesh` | Isotropic 8x, and `hexRef8` needs `cellLevel`/`pointLevel` files a cfMesh mesh does not have. |
| cfMesh `anisotropicSources` as a grading mechanism | See section 8. |

---

## 6. `refineWallLayer` and `refineMesh`: what each can and cannot do

Both are OpenFOAM mesh-manipulation utilities on `cellCuts`/`meshCutter`/`polyTopoChange`. Neither
touches the cfMesh octree, so both go below 22.0035 um on the existing label=32 image. That is the
single most useful true thing about them.

### `refineWallLayer`

Pure topological cutting. It cuts only edges running from a patch point to a non-patch point, so
the patch's own faces are never split and the conformal interface cannot drift. Measured: total
mesh volume bit-identical across three passes, `fluid_to_solid` and `solid_to_fluid` held at the
same count through one, two and three passes, patch area and area-weighted centroid identical to
9 significant figures on both sides. The lowest geometric risk option we have.

Arbitrary `edgeFraction`, so a real expansion ratio, which snappy's relative sizing does not give.
For m sub-layers in geometric ratio r, pass k of N = m-1 uses
`f_k = (sum_{i=0..m-k-1} r^i) / (sum_{i=0..m-k} r^i)`.

| recipe | layers off a 22.0035 um cell |
|---|---|
| two 0.5 passes | 5.5009 / 5.5009 / 11.0018 um, first cell centre 2.75 um |
| r=1.5, two passes (0.52632 then 0.4) | 4.6323 / 6.9485 / 10.4227 um |
| r=1.5, three passes (0.58462, 0.52632, 0.4) | 2.7081 / 4.0621 / 6.0932 / 9.1398 um |

Cost is exactly +1 cell per cut cell per pass. Measured interface-faces-per-cut-cell 1.13 to 1.22,
so 21,708,529 faces imply roughly 18.5 to 27.5M added cells per pass. Serial cost measured on a
scaling sweep: 64 us/cell and 1.92 GB per million cells, so pass 1 on the 68.8M fluid region is
about 1.2 h and 132 GB, pass 2 about 1.6 h and 170 GB. Fits a 256 GiB box at two passes.

Downgraded by two measurements.

**It does not reach all of the wetted area.** `cellCuts` takes at most one loop per cell, so a
staircase corner cell owning two interface faces on different axes gets one cut and the second face
keeps its full wall distance. Wall-normal distance to the first cell centre, in cell units, over
all 2772 interface faces of a faithful staircased proxy:

| | median | max |
|---|---|---|
| baseline | 0.500 | 1.500 |
| 1 pass | 0.457 | 1.479 |
| 2 passes | 0.316 | 1.419 |
| 3 passes | 0.158 (mean 0.265) | 1.388 |

After three passes 47.3% of wall faces reach 0.05-0.10, but 31.3% sit at 0.3-0.4, 3.2% at 0.4-0.6,
1.3% at 0.6-1.0 and **7.5% are still at 1.0-2.0, completely unrefined**. Spread in y1 is 26x,
against snappy's 2.75x. `-useSet` (faceSet `patchToFace` then cellSet `faceToCell option owner`)
removes 12% of the cells and the point-only refinements, but max non-orthogonality was identical
at 53.13010235 and max skewness slightly worse. It does not fix corners.

Quality cost is also front-loaded: one pass took the proxy from max non-orthogonality 0.00 to
53.13 and from 100% hex to 74.7% (804 prisms, 920 polyhedra). Pass 2: 53.17, skew 2.00, aspect
28.5. Pass 3: 59.85, aspect 57.0, and `checkMesh` FAILS with
`***Max skewness = 4.819277569, 2 highly skew faces detected`. Three passes is not purchasable.
Two is the budget. On a clean planar duct one pass gave only 13.63, so the damage is specifically
the staircase.

**The no `-region` trap.** Confirmed from source: `refineWallLayer.C` includes `createPolyMesh.H`,
not `createNamedPolyMesh.H`, and its only option header is `addOverwriteOption.H`. It is the only
utility of six checked (`refineMesh`, `snappyHexMesh`, `topoSet`, `checkMesh`, `splitMeshRegions`,
`reconstructParMesh` all have it) without `-region`. A forum recipe showing
`refineWallLayer -region FLUID` is from the openfoam.org fork, whose v13 source does use
`createSpecifiedPolyMesh.H`. Workaround: stage the region as its own case with
`constant/polyMesh` symlinked to `constant/fluid/polyMesh`. Symlink only the polyMesh directory,
never the case root, because a serial run calls `processorMeshes::removeFiles(mesh)` and would
delete your decomposition.

**The parallel-safety trap, and it is worse than it looks.** `cellCuts` has six constructors. Five
call `syncProc()` ("Adds cuts on other side of coupled boundaries"). The one `refineWallLayer`
calls, `(mesh, cutCells, meshVerts, meshEdges, meshEdgeWeights)`, does not. There is no
`argList::noParallel()`, so it will not refuse. Measured on 4 ranks: rc=0, no FatalError, and
`checkMesh -parallel -allTopology` reported "Mesh OK" with metrics bit-identical to serial. It
agreed by luck, because the cut test is purely local. The consistency check lives inside
`syncProc`, which is never called, so when it does disagree there is no error at all. Never run it
under `mpirun`, and never read a clean `checkMesh` as evidence that you can.

Two mechanics traps, both measured. A bare regex argument fails:
`refineWallLayer ... (fluid_to_.*)` dies with "Cannot find any patch or group names matching". It
needs inner double quotes, `'("fluid_to_.*")'`, or the literal name. And `topoSet::removeFiles(mesh)`
runs at the end of every pass and wipes `constant/polyMesh/sets`, so `topoSet` must be re-run
before every pass or pass 2 with `-useSet` dies on a missing set file.

A one-line patch swapping in a `syncProc`-calling constructor was proposed and **refuted**; see the
journal record. Do not attempt it.

### `refineMesh`

Parallel-safe, has `-region`, and the cut is an exact axis-snapped bisection.
`hexCellLooper::cut` calls `meshTools::cutDirToEdge` so the requested direction is snapped to one
of the hex's own three edge bundles, then `walkHex` sets `loopWeights[cutI] = 0.5` on every cut
edge. On Cartesian cells the cut plane is exactly axis-aligned through face midpoints, so
non-orthogonality stays at median 0.000 and skewness does not move. Only aspect ratio rises.

But `hexCellLooper` hard-codes weight 0.5, so it can only halve. No arbitrary ratio, no prismatic
constant-thickness layer, no choice of y1 outside powers of two of 22.0035 um. Dyadic Cartesian
grading, not boundary layers.

Three silent no-ops found in source, all worth a harness guard:

1. `directions.C` scans the `directions` word list setting `wantNormal`/`wantTan1`/`wantTan2` with
   no else branch. An unrecognised name is silently ignored. `directions (x)` or
   `directions (Normal)` yields zero direction fields, `refineAllDirs` loops zero times, and the
   mesh is written UNCHANGED with no error.
2. `refineMesh -all` takes precedence over `-dict` and is isotropic `hexRef8`, 8x cells.
3. A missing or mistyped `-dict` path falls through to refining all cells. No error, 8x mesh.

Two more: `multiDirRefinement` routes to `hexRef8` only when `useHexTopology` is true AND
`directions.size() == 3`, so always use one direction and keep `useHexTopology false`. And
`coordinateSystem patchLocal` with `useHexTopology` FatalErrors on any non-hex owning a patch face,
which our 1.4-2.1% octree-transition polyhedra guarantee; without it, it falls back to a MeshWave
propagation referenced to `faceNormals()[0]` of a single patch, which is meaningless on a branching
network.

Ordering is load-bearing: run `refineMesh` BEFORE `splitMeshRegions`. At that point it is one mesh,
`meshCutter` inherits `cellZones().whichZone(celli)`, and the split then manufactures the interface
from the same internal faces, so 1:1 is true by construction. Refining one region AFTER the split
splits staircase-step interface faces on the fluid side only and the 1:1 property dies.
`refineMesh` also does not update pre-existing cellSets, it writes a new `refinedCells` set plus a
`cellMap`, so reusing your own cellSet for pass 2 silently refines stale labels.

---

## 7. The physics finding that reframes the whole exercise

At Dh 187 um and Re 34 to 800 there is no boundary layer and no log layer. The thermal profile
fills the channel and the viscous scale IS the half-channel. y+ requires a friction velocity and a
log layer; neither exists. "Add boundary layers to fix Nu" was the wrong objective.

The thermal layer never exceeds the half-width, 50.4 um = 2.29 cells, anywhere, forever.
`delta_T = (9 alpha x / gamma)^(1/3)` at 1 m/s: 6.04 um at x = 10 um (0.27 cells), 10.32 um at
50 um (0.47), 13.01 um at 100 um (0.59), 22.24 um at 500 um (1.01), 28.02 um at 1000 um (1.27),
merging with the opposite wall at x = 5.81 mm. Slower flow gives a thicker layer at the same x, so
the fine low-Re branch generations are better resolved than the fast trunk.

**Aggressive wall clustering measurably WORSENS laminar Nusselt at matched cell count.** Measured
on a validated conjugate unit cell, conjugate Nu error at matched cell count across the fluid:

| distribution | cells across | Nu error |
|---|---|---|
| uniform | 4 (our mesh) | +4.95% |
| uniform | 6 | +3.12% |
| uniform | 8 | +2.02% |
| uniform | 12 | +1.01% |
| geometric r=1.3 | 8 | -1.32% |
| geometric r=1.6 | 8 | **-4.05%** |
| geometric r=1.6 | 6 | -4.16% |
| octree base4 + 1 level on outer cell | 6 | -3.60% |
| octree base4 + 2 levels on outer cell | 10 | -5.75% |
| **octree base6 + 1 level on outer cell** | **8** | **-0.57%** |
| octree base8 + 1 level on outer cell | 10 | +0.22% |
| octree base8 + 1 level on outer 2 | 12 | -0.35% |

r=1.6 at 8 cells is twice as bad as uniform 8. The error is distributed across the whole parabolic
profile, not concentrated at the wall: the profile fills the channel and the curvature lives in the
core. Starving the core to feed the wall trades one error for a bigger one. In fully developed flow
the profile has ZERO curvature at the wall and maximum curvature at the centreline, so clustering
takes cells from the only place the profile actually bends.

The one near-wall treatment that helped was MILD banding: base 6 plus one octree level in a
one-cell band gave -0.57%, a 3.5x error reduction at identical cell count and equivalent to uniform
16 to 20. That is ordinary local octree refinement, which cfMesh already does.

The solver behind these numbers reproduces Shah and London fRe to 0.04-0.10% and Nu_H1 to
0.01-0.04% across five aspect ratios (square duct: fRe 14.2185 against 14.2296, Nu_H1 3.6100
against 3.6102, Nu_T 2.9787 against 2.9787).

So: buy 2 to 4 modest layers, not 8 thin ones. They are the right fix for pressure drop and for
the entry region downstream of every junction. They are not the Nusselt fix I assumed.

The instinct is wrong in print too. A published microchannel heat-sink paper (arXiv:2106.11752)
stacks 15 prismatic layers with growth 1.15 and a 1.5 um first cell on a 1 mm x 5 mm laminar
channel at Re < 2000 and justifies it with "maximum wall y+ is below 0.3", while noting in the same
breath that this is a turbulent criterion.

---

## 8. The 2/N^2 law governs FLOW RATE, not Nusselt

Derived in closed form and confirmed to five decimals. For 1D plane Poiseuille, cell-centred FV
with the standard `(d/2)` one-sided wall stencil gives exactly `w_i = w_exact(y_i) + d^2/8`, hence

```
u_m,FV / u_m,exact = 1 + 2/N^2     EXACTLY
```

Verified numerically: N=1 ratio 3.00000, N=2 1.50000, N=4 1.12500, N=5 1.08000, N=10 1.02000,
N=64 1.00049. That is the origin of "2/N^2". It is the flow rate.

Nusselt is OVER-predicted at 4.6 cells, not under, because no-slip forces `u = 0` at the wall, so
`alpha d2T/dn2 = u dT/dx` gives `d2T/dn2|wall = 0` identically, the leading term of the two-point
wall-gradient error vanishes, and the error becomes O(h^3).

### The correct error budget

| quantity | at 4.579 cells across | direction |
|---|---|---|
| total chip-to-coolant dT | -1.38% | robust to copper caps of 50/200/500 um and bases of 50/200 um |
| conjugate Nu | +4.37% | HIGH |
| convective dT | -4.18% | |
| Nu_H1 (non-conjugate) | +4.2% | HIGH |
| Nu_H2 | +19.7% | HIGH |
| fRe | -10.1% | so pressure drop is about 10% LOW |
| flow rate at given dp | +11.3% | |
| per-channel hydraulic resistance scatter | 66.9% today | grid artefact, see section 9 |
| staircase wetted area | +7.8% overall, +11.6% on the channels | geometric, unfixable by refinement |

Four independent solves agree the fully developed error is between -2.6% and +4.5% depending on
thermal boundary condition. Observed order of convergence: fRe p = 1.78/1.92/2.00/2.10 over
4 to 8 to 16 to 32 to 64; Nu_H1 p = 1.29/1.76/1.96/2.05; Nu_H2 p = 1.40/1.46/1.43/1.33, slower
because of corner singularities.

So "the mesh is 9% low on Nu" was wrong in sign, in magnitude and in subject. It had the right
magnitude attached to the wrong quantity.

### The cancellation, which has a hard consequence

The staircase area bias partially cancels the Nu error: 1.116 x 0.988 is about +10% on Q = h.A.dT.
Adding layers alone moves Q from roughly +10% to roughly -5%, so correcting the gradient without
correcting the area makes the integrated answer WORSE. Nobody should fix one of these without the
other. The implied mean wall misalignment is only about 7.1 degrees from the +11.6% channel figure
(4.6 degrees from the +7.8% overall), so the network is mostly axis-aligned and the staircase is
mild in ANGLE, but the area bias is real.

### The error I cannot yet size

Entry-region Nu error is +10 to +20% at x* = 0.0025-0.005 and **non-monotone below 8 cells**, so a
grid study run at fully developed conditions falsely passes and Richardson/GCI is invalid there.

Segment-averaged Nu error at 4.6 cells, which is what sets total heat removed over a segment of
that length, at 1 m/s:

| segment | error |
|---|---|
| 0.07 mm | about -43% |
| 0.24 mm | about -25% |
| 0.73 mm | about -10% |
| 2.45 mm | about -1% |
| 4.90 mm | about 0% |
| 12.2 mm and beyond | within 0.3% for every mesh from 4 cells up |

The slot's thermal entrance length is 1.4 to 5.7 mm (L*th = 0.01155 for H1, 0.00798 for T, which
reproduce Shah and London's tabulated parallel-plate 0.0115/0.0079). The familiar `0.05 Re Pr Dh`
rule is the CIRCULAR-TUBE rule (L*th 0.0430/0.0335) and overestimates this slot by 4.3x to 6.3x.
It would have told us the flow never develops on a 55 mm plate when the truth is a few millimetres.

A branching plate restarts the thermal layer at every junction, so the whole question reduces to
the junction-to-junction segment length distribution, **which we have never measured**. Segments at
or above 2.5 mm and the mesh is fine. Segments near 0.25 mm and we are 40% low on heat removed.
Discount any restart enhancement by about two thirds: Xu et al. (IJHMT 48:1662-1674) measured
1.27x gross where ideal Graetz predicts 1.82x, with net enhancement 1.202 falling to 1.01 and
pressure drop DOWN 27%.

### And the whole budget is dwarfed by what we do not know

Flow rate, coolant and power map are unknown. The thermal boundary condition alone spans Nu 2.907
(H2, uniform flux on all four walls, capped by corner hot spots) to 7.214 (H1, peripherally
isothermal) for this exact aspect ratio, a factor of 2.5. Uniform solid temperature against uniform
flux is up to 23%. Copper at k_Cu/k_water = 667 pins us at the H1 limit, which is the
steepest-gradient, least forgiving case, so the fluid side is the only side that matters. But no
mesh-error argument is meaningful against a 2.5x boundary-condition ambiguity. Any quoted Nu must
name its boundary condition. Correct targets for the true 1:14.89 aspect ratio (alpha* = 0.06718):
fRe_Dh 22.014, Nu_H1 7.214, Nu_T 6.377, Nu_H2 2.907. Note Nu_H2 does NOT approach 8.235 at high
aspect ratio; corner hot spots cap it near 2.91.

---

## 9. The efficiency levers

### Lever 1: commensurability. Free, and the best finding in the sweep

cfMesh's finest cell is `maxCellSize / 2^L`, so the cell size is a free parameter we have been
setting carelessly. The sweep predicted that a non-integer pitch in cells makes the 4-or-5-cell
snapping phase drift down the array.

**That prediction has since been measured and it is recorded in `MESH_QUANTISATION.md`.** The
measured numbers supersede the sweep's estimates, because the sweep worked from a 100.76 um
nominal width and 202.77 um pitch while the measurement found 100.04 um median and 201.26 um
pitch:

| | sweep estimate | measured (`MESH_QUANTISATION.md`) |
|---|---|---|
| CAD channel width | 100.76 um | median 100.04 um, mean 100.7 um, one population, CV 0.89% in the core |
| widths present in the mesh | bimodal 88.014 / 110.017 | 88.014 (36.1%), 110.017 (52.7%), 132.021 (6.4%) |
| w^3 conductance spread p95/p5 | 1.95x between two channels | 3.38x, against CAD's 1.09x |
| pitch-8 `maxCellSize` | 405.540 um | **402.525 um**, dx 25.1578, channel exactly 4 cells at 100.631 um, +0.59% |
| pitch-10 `maxCellSize` | 324.432 um | **322.020 um**, dx 20.1263, channel exactly 5 cells at 100.631 um, +0.59% |
| pitch-8 cell count | 270-311M | about 276M, 33% fewer than today |
| pitch-10 cell count | 487-528M | about 540M, 8.5% finer linear resolution than today |

Use the measured numbers. Both options are the same octree depth as today, so neither carries any
new label or buffer risk. Both replace the three-way split with one width everywhere and zero
scatter.

One correction to the sweep worth keeping: it originally proposed making the CHANNEL commensurate
rather than the PITCH. That leaves the pitch at a non-integer number of cells, so the phase still
drifts over 200 channels and the alternation comes back. Pitch commensurability is the correct
target, because channel and fin are each almost exactly half the pitch (measured CV 0.40% on the
fin, 0.46% on the pitch), so an even number of cells per pitch splits evenly into both.

Caveat on scope: Corintis's own published optimised designs carry channel widths from 90 to 371 um
(Glacierware reports min 90.37, mode 134.74, mean 186.12, max 370.93 um). Commensurability may only
apply to the uniform trunk array, with the branch region quoted separately.

### Lever 2: anisotropy. Real, large, and cfMesh cannot express it

The mesh is isotropic while the physics is anisotropic by 20 to 100x. Across the channel the length
scale is 50 um. Along it, the thermal entrance length is 1.4 to 5.7 mm and the gradient scale is
millimetres. Three independent estimates converge:

| configuration | fluid cells | 2/N^2 flow error | against today |
|---|---|---|---|
| 4.58 across, 22.0 um, 22.0 um axial, nz 22.7 (today) | 68.8M | 9.53% | 1.00x |
| 16 across at 6.30 um, 22.0 um axial, nz 22.7 | 240.2M | 0.78% | 0.29x (naive refine, worse) |
| 16 across at 6.30 um, 80 um axial, nz 22.7 | 66.1M | 0.78% | 1.04x (free 12x error cut) |
| 16 across at 6.30 um, 80 um axial, nz 10 | 29.1M | 0.78% | 2.36x |
| 24 across at 4.20 um, 80 um axial, nz 10 | 43.6M | 0.35% | 1.58x |
| 32 across at 3.15 um, 120 um axial, nz 10 | 38.8M | 0.20% | 1.77x |

Whole mesh with anisotropic in-plane plus 10 graded z layers: 169.3M, 2.4x lighter. A separate
estimate per channel gives 22 um across / 66 um along the height / 110 um axial at 0.067x the
isotropic cost, and 16 across at 6.30 um with 110 um axial at 0.23x, which scales to about 95M.

Spanwise cell size barely matters, so the `scaleY 3` stretch is nearly free numerically: at 4 cells
across, cell aspect 1 to 12 moves fRe only 19.517 to 18.906, a 3% spread over a 12x change. The
numerical case for more stretch exists and the operational case kills it: `scaleY 6` took 38.5 h
against 17 min at `scaleY 3`.

**cfMesh cannot express the in-plane anisotropy on a branching network.** `anisotropicSources` has
only two primitives: Box (centre, lengthX/Y/Z, scaleX/Y/Z over the whole coordinate range) and
Plane (origin, normal, scalingDistance, scalingFactor, a clamped LINEAR ramp with a single factor
over the whole slab, read in `planeScaling.C`). The guide states: "It is possible to use more than
one anisotropic source in a single meshing job, with a restriction that their zone of influence
must not overlap. In case their zones of influence overlap, the mesher stops and reports the
error." With channels interleaved at a 201.26 um pitch running in many in-plane directions there is
no non-overlapping set of sources. Our `scaleY 3` is the whole of what that feature gives us.

Inside cfMesh the only reachable anisotropic lever is post-hoc single-direction `refineMesh` on a
cellSet, which works on the axis-aligned trunk runs and not on the branches. Before committing to
it, histogram the `fluid_to_solid` face normals into x/y/z bins weighted by AREA: if 85% or more of
wetted area is within 30 degrees of +/-x the technique applies to the trunk, and below about 60%
the coarsen-the-base variant is dead.

### Lever 3: the solid. Mostly REFUTED on our part, by our own generator

The physics says the solid is nearly free to coarsen:

| | |
|---|---|
| Biot on the fin half-thickness | 0.0016 to 0.0041, 25 to 60x below the 0.1 thermally-thin threshold |
| k_Cu / k_water | 667, past the 500 threshold where Nu stops depending on solid conductivity at all |
| fluid-side Dirichlet weight `nbrKDelta/(nbrKDelta+myKDelta)` | 0.99850 at 22 um both sides |
| the same weight with the solid at 220 um | 0.98522 |
| temperature error across one solid near-wall cell, as a fraction of wall-to-bulk dT | 0.053% at 22 um, 0.212% at 88 um, 0.529% at 220 um, independent of q |
| peripheral wall conduction parameter K_p | 182 to 364, against a threshold of 4 for "effectively infinite" |

So the solid contributes 0.15% of the interface stiffness, and coarsening it 10x costs 0.53% of the
wall-to-bulk temperature budget. One octree level on solid plus chip would be 365M to 106M, 3.4x
lighter.

**But the sweep refuted most of that saving on our part, from our own script.**
`devtools/semiconductor/couple_box.sh` lines 128-165 already emit four `surfaceMeshRefinement`
entries, every one on an internal surface, because `surfaceFile` is the box `domain.stl`:
`plate_skin` +1, `coolant` +1, `microchannels` +4 at 8.8014e-05 thickness, `chip` +2 at 0.0036. So
"cfMesh can refine on an internal conjugate surface" was settled empirically on this exact part at
413M cells, not in theory. The mechanism is `refineBoxesIntersectingSurfaces()` in
`meshOctreeCreatorAdjustOctreeToSurface.C`, which loads each entry's own `surfaceFile` and tests
`intersectsTriangleExact` per candidate leaf, with no patch lookup and no inside/outside test. That
is exactly why it works where `boundaryLayers` failed.

And `cbox2_checkMesh.par.solid.log` says the solid is 2.22054e-4 m3 in 271,759,108 cells, mean cell
8.17e-13 m3, against a fine cell of about 3.0e-14. A uniform 22 um solid of that volume would be
6.95 BILLION cells. **The solid is already graded by about 25x.** The brief's premise, that the
solid is 271.8M "because the whole band sits at 22 um", is wrong. Same on the fluid: 5.34194e-05 m3
in 68.8M cells, but the microchannels are only about 2,270 mm3 of that, so about 96% of the coolant
volume is plenum already carried at background size.

What is actually recoverable:

| item | saving |
|---|---|
| the CHIP band, `additionalRefinementLevels 2; refinementThickness 0.0036;`: an 88 um band 3.6 mm deep around about 12,000 mm2, reaching 3.6 mm into the plate under the chip | about 63M cells. Tighten to 3.6e-4 and recover about 55-57M |
| tightening the microchannels band from 8.8014e-05 to 4.4007e-05 | about 8.4M cells, and only above and below the slab where the bands do not already overlap |
| geometry-locked floor | about 141M cells. Fins at 102 um between channels at 100 um, with both walls' refinement bands overlapping inside every fin, twice over |
| irreducible halo | about 660 um, from the one-ring-per-level minimum in `refineSelectedBoxesAndAdditionalLayers` |

Total identifiable saving about 65M of 413M, 1.19x, not 3.4x, and dominated by the chip band. The
script's own `LCHIP2` comment already suspected it.

Do not go below two rings of the finest cell on the microchannels band: one ring puts a 22 to 44 um
transition on the cell adjacent to the conjugate interface, which is both a quality cost and
exactly the condition that collapses layer coverage (section 3).

One source-confirmed trap on all of this: `refineBoxesIntersectingSurfaces` transforms the
refinement surface through `surfaceMeshGeometryModification` when `anisotropicSources` is present,
and the thickness is divided by a single scalar leaf size, so every `refinementThickness` is
measured in the `scaleY`-3 STRETCHED space, not in metres. Inside the anisotropic box the band is
about 3x deeper in the channel-height direction than the number suggests.

Coarsening the solid further requires a non-conformal interface, which measured +34% / -40% local
flux error. Refuse it.

---

## 10. The Gmsh 2.5D swept route: the strategic alternative

This part is prismatic in z (vertical walls, constant 1499.9 um depth), which makes it textbook
sweepable, and the literature is unanimous that this is how the class should be meshed.

A Gmsh 4.15.2 route was PROVEN on the real dimensions, including a branching T-junction: two 2D
`BoundaryLayer` size fields on the shared wall curves, each listing the other material's surface in
`ExcludedSurfacesList`, both registered with `setAsBoundaryLayer`, then a graded `Extrude ... Layers`
through the channel height.

| measured | value |
|---|---|
| node offsets into fluid | 2.00, 4.60, 7.98, 12.37, 18.09, 25.51 um |
| node offsets into solid | 2.00, 4.60, 7.98, 12.37, 18.09, 20.51 um |
| one field alone | 2.00 um fluid side, 23.1 um solid side. BOTH fields are required |
| branching T-junction (OCC BooleanFragments, 11 wall curves) | same ladder both sides; 304 quads + 209 tris; triangle quality min 0.0081 at the reentrant corner, mean 0.7746 |
| extruded to 3D | 2 volumes, 11 surfaces (the interface lateral surface created ONCE and reused), 7959 nodes, **0 duplicated coordinates**, so conformal by construction |
| elements | 4480 hex + 5120 prism, zero tets, zero pyramids |
| 5-channel/6-fin array, hfar 0.20 mm, nz 14, wall 2.0 um | 10,137 3D cells per mm2 of footprint |
| equivalent band of our mesh | about 46,900 cells per mm2 |
| MSH2 ASCII size | 102.0 bytes per 3D cell |

4.6x lighter with an 11x finer wall cell, and it is the only route that kills the staircase outright
rather than reducing it, because the walls follow the CAD curves. Two Physical Volumes that
`gmshToFoam` turns into cellZones for `splitMeshRegions`.

Hard constraints, all verified:

- `gmshToFoam` reads ASCII MSH2 only. `readMeshFormat` does
  `if (asciiFlag != 0) FatalIOError << "Can only read ascii msh files."`, and `readPoints`/`readCells`
  parse the MSH2 block layout, not MSH 4.1 `entityBlocks`. Element types are hard-coded to
  tri/quad/tet/hex/prism/pyramid. So `Mesh.MshFileVersion=2.2`, `Mesh.Binary=0`, first order only,
  no polyhedra.
- It is serial, with a reported failure at about 400M elements (about 30 GB, 128 GB box,
  "Attempt to get back from bad stream") that worked "once I got under 80 M". At 102 bytes per 3D
  cell a 60M-cell mesh is about a 6 GB file, which is feasible and is exactly where this route lands.
- Wall-cell aspect ratio goes to 50 to 100.
- `FanPointsList` at junction corners is not optional. One reentrant triangle came out at quality
  0.0081.
- The extrusion forces one in-plane mesh through the whole stack, so base and lid copper inherit the
  in-plane spacing. The manifold and the chip must be separate volumes. Estimated about 12.8M cells
  for 1 mm of base with 6 graded z layers, against about 92M at our current resolution.
- Precondition not yet proven: the channels must be genuinely prismatic in z. Section the STEP at
  10%, 50% and 90% of the channel depth and compare cross-sections. Corintis's own patent
  EP4591205A1 claims 1 to 10 degrees of DRIE sidewall taper, so if they taper the extrusion needs a
  scaling law.

Do NOT build anything on Gmsh's 3D boundary-layer machinery. The 2D `BoundaryLayer` field is
explicitly 2D only (`BoundaryLayerField::setupFor2d` in `src/mesh/Field.cpp` counts adjacent faces
per BL curve, skipping extruded entities and `ExcludedSurfacesList` members, and errors if more than
one remains; those two skips are precisely why the 2.5D trick works). The released 4.15.2
`Plugin(BoundaryLayer)` 3D path ships but is inert: measured 45 nodes / 84 tri / 100 tet in, 57
nodes / 84 tri / 8 quad / 100 tet out, zero prisms, with "Winslow untangler 2D: average triangle
area is negative" printed. Its 2D path on the same build works perfectly (548 tri to 548 tri + 312
quad). The working 3D code exists only in git master, is AGPL inside a GPL2 project, and dates from
2026.

Also dead: anisotropic 3D metric fields (MMG3D is an experimental remesher, BAMG is 2D only), hybrid
prism+tet under the parallel mesher (pyramids require `Algorithm3D=1`, the serial one), and all-hex
by subdivision (`SubdivisionAlgorithm=2` with `Recombine3DAll=1` multiplies cell count and wrecks
quality). Isotropic size-field grading as a substitute for layers is arithmetically dead: a 2 um
first cell with isotropic cells implies about 6e9 wall triangles on our wetted area.

---

## 11. The reframing: nobody meshes a whole plate

| mesh | cells |
|---|---|
| ours (`cbox2`) | 413,273,323 |
| largest microchannel conjugate mesh found anywhere | 24M, a single channel at 200 cells/Dh (Municchi et al., arXiv:2203.09305) |
| device-level microchannel meshes | 1 to 12M |
| unit cells | 40k to 500k |
| the "done properly" validated conjugate benchmark (UNM/Sandia, arXiv:2106.11752) | 3,835,800 total, 2.43M fluid, 15 prismatic layers at 1.15, first cell 1.5 um |
| whole TPMS two-fluid heat exchanger | 1,365,178 |
| pore-resolved micro-CT monoliths on 8,000 cores | about 200M |
| lattice/TPMS CHT industrial norm | 50 to 150M |
| grid-independent 3D cold plates in recent papers | 2.02M and 4.72M |
| best-resolved manifold-microchannel unit cell | 655,360 |

Ours is about 17x the largest published and about 1000x typical. Our linear resolution is 8.58
cells per Dh against a published floor of 50 and a typical 65 to 100. Our first cell is 21.8% of
the channel width against 0.15% in the validated UNM/Sandia conjugate case, about 145x coarser in
relative near-wall terms.

The reason nobody publishes at our resolution is that nobody meshes a whole plate. Literature
resolution is unreachable on the full plate by isotropic Cartesian refinement: 50 cells/Dh is
3.74 um, 204x our cell count, about 84 billion cells. Even two octree levels is 26.4 billion.

And the closest-matched published grid study used uniform cubic hexahedra with **no boundary layers
and no grading**, at 50/100/200 cells per Dh (0.4/3/24M cells), and reported "The three curves
overlap and differences are below 1%" for single-phase Nu at all three. The about 15% Nu
underprediction on their coarsest mesh was for TWO-PHASE bubble growth, not single phase.

Corintis themselves state in their own methods that a full 3D model of the network "would involve
tens of millions of degrees of freedom, making the simulation computationally expensive, and the
optimization unfeasible", and design with a 2.5D depth-averaged two-layer FEM model on linear
triangles with an assumed parabolic out-of-plane profile. For final verification they "construct a
body-fitted mesh based on the black and white image of the generated geometry", a planar
unstructured mesh of the 2D channel pattern. EPFL's own 3D work (van Erp thesis TH9480) models ONE
channel as a unit cell with symmetry boundaries at 7e5 DOF in COMSOL, and when it needs the chip
temperature field it replaces the entire fluid domain with a solid layer of effective conductivity
"to avoid the need for solving the fluid domain."

Published error of that model class against full 3D is 7.27 to 24.8% on Nusselt and 12.6 to 16.3%
on inlet velocity (arXiv:2510.18882, Table 2). Yan et al. 2019 measured pseudo-3D against 3D
conjugate at Re 47/185/573: objective mean T 0.85 / 5.35 / 13.27%, and MAX TEMPERATURE 5.34 / 8.10
/ 18.72%.

So our 413M-cell mesh, even at 4.6 cells across, is plausibly more accurate than the tool that
designed the part. That is the useful framing, and it is a better one than "we added boundary
layers".

Nothing in this lineage publishes a single 3D cell count, a cells-across-the-channel figure, a 3D
conjugate grid-convergence study, or any mention of boundary-layer or inflation meshing. The word
"boundary layer" appears in the thesis only as physics, never as a meshing instruction. There is no
published rule of the form "N cells across a channel for laminar heat transfer", and no published
rule of the form "solid cells = X x fluid cells at a CHT interface". Both were looked for directly
and both had to be derived.

---

## 12. What I should have known

Eight things, and several are in files we already own.

1. **cfMesh's own user guide states the boundary-layer limitation verbatim in section 4.5**, and
   that guide ships INSIDE the `integration-cfmesh` repo we clone. Our
   `Cannot find any patch names matching coolant.*` is the documented design, not a bug and not a
   syntax problem. I treated a documented behaviour as a wall and stopped the inquiry there.

2. **Our own QA has been meaningless and I did not notice.** `minVol 1e-13` is 56x above what a
   22 um cell produces. Any `checkMesh -meshQuality` report built with the shipped dict is
   worthless.

3. **I have been misquoting our own work.** The brief says "zero faces over 50 deg out of 47.7M".
   `COUPLED_MESH_DELIVERY.md:43` says the bar is 70 degrees and our measured max is 51.46, so there
   is at least one face over 50. I have been overstating a headline number to myself.

4. **The 22 um floor is not a label overflow, and our own probe said so on 5 November.**
   `BUFFER_PROBE.txt`: "The first explanation, an octree box-count overflow at level 15, is
   REFUTED ... And the error is MPI_ERR_BUFFER, the attached Bsend buffer having insufficient
   space." And `ROOT_PROBE.txt`, same day, already had the free alternative. I had both the
   refutation and the fix on disk and was still planning the 64-bit rebuild.

5. **700M cells and the 32-bit face-label ceiling are the same number.** At 3.02 faces per cell,
   2^31 faces is about 711M cells. My own figure sits within 2% of overflowing this build. The real
   ceiling is roughly 600 to 650M.

6. **`snappyHexMesh`, `refineWallLayer` and `refineMesh` have been in `hexera-mesh:2412` the entire
   time.** The shipped annotated dict says it in one line, at
   `etc/caseDicts/annotated/snappyHexMeshDict:681`: "Per final patch or faceZone (so not geometry!)
   the layer information". ESI ships a tutorial, `mesh/snappyHexMesh/addLayersToFaceZone`, that does
   exactly what we needed. Our institutional memory says "layers do not work here" because of two
   real failures on disk, `t3work/C2/log.snappyHexMesh` at 28.7% coverage and `t3work/LM/log.layers`
   finishing with 439 illegal faces, but both were the ordinary castellate+snap+addLayers route on
   snapped geometry, which is a different operation from addLayers-only on an existing Cartesian
   mesh. I generalised from the wrong experiment.

7. **Our own generator already refutes the brief's premise about the solid.** See section 9,
   lever 3.

8. **y+ is meaningless here and I reached for a turbulent instinct.** The measured truth is that
   aggressive wall clustering makes laminar Nu WORSE at matched cell count, and one hour with a 1D
   solver would have found that before commissioning sixteen research angles.

---

## 13. Ranked actions, as the sweep left them, with current status

| rank | action | status |
|---|---|---|
| 1 | Histogram per-channel WIDTH and PITCH in cells on the mesh we already have | **DONE.** `MESH_QUANTISATION.md`. Confirmed worse than predicted: three widths, 3.38x conductance spread, not bimodal with 1.95x |
| 2 | Retune `maxCellSize` for pitch commensurability (402.525 or 322.020 um) and tighten the chip band from 3.6 mm to 0.36 mm | open. Two dict numbers, one re-mesh. The only action that improves fidelity and reduces cell count at once |
| 3 | Retire the "9% low on Nu" claim internally and publish the measured error budget in its place | open. Section 8 is the budget |
| 4 | Run `snappyHexMesh` addLayers-only on the SPLIT fluid region, `minVol 1e-20`, 2 to 4 layers on `fluid_to_solid`, absolute sizing | open, gated on the one-hour subset test below |
| 5 | Measure the junction-to-junction segment length distribution from `PlateAndChip.step` against the 1.4 to 5.7 mm thermal entrance length | open. An afternoon, no meshing, no solver. Swings the answer by 40 percentage points |
| 6 | Fix `system/meshQualityDict` and re-run `checkMesh`; correct every document quoting a 50-degree bar to the real 70 | open. Precondition for rank 4, same `minVol` value |
| 7 | Cancel the `WM_LABEL_SIZE=64` rebuild; halve the octree root instead if sub-22 um octree resolution is ever wanted | decision. Note 11.00 um across the microchannel band is roughly 1.38 billion cells, about 2x my ceiling, so 11 um on the full plate is not deliverable by any route |
| 8 | Keep `refineWallLayer` (two passes, 0.52632 then 0.4, serial, staged as its own case) as the zero-geometric-risk fallback if rank 4's coverage test fails | fallback only |
| 9 | Take the CF-MESH+ 14-day trial and aim it at exactly ONE thing: the staircase | optional. Not layers, we can already do layers free. The one thing no open-source tool in the sweep gives us is a surface-fitted internal conjugate interface. Release notes 3.1 confirm "Layer properties can be specified separately on each side of a multi-domain interface", so the capability is named, not inferred. But a GUI session is needed to author setups, which breaks an automated pipeline, and 4.1's "Writting mesh to a disk in the ascii format when integer overflow would occur" says they are still on 32-bit labels, so it does not lift our resolution floor |
| 10 | Prototype the Gmsh 2.5D swept route | weeks, strategic. Section 10 |

### The one-hour test that gates rank 4

Carve two roughly 2M-cell subsets of the real fluid region with `topoSet` and `subsetMesh`: one
straddling the 4x refinement cliff at x about 55 mm including a branching junction, one entirely
inside the 22 um zone. Run the dict on each. At 1.61 kB/cell that is about 3 GB and about 5 minutes
each, laptop-class, no new mesh and no VM.

Read four lines:

1. `Extruding N out of M faces (P%)`. P = 100, ship it. P 60 to 99, usable, expect polyhedra.
   P under 30, the level transition is on the patch: split the interface by cell level in `topoSet`
   and layer only the fine zone.
2. Interface `nFaces` AND integrated area unchanged to 1e-6 relative. Anything else and 1:1 is
   lost. Stop.
3. `checkMesh` Mesh OK with nothing over 70 degrees.
4. Zero polyhedra on an all-hex input.

**Kill criterion:** if the in-zone block itself comes in under 60%, the branching junctions are
defeating the medial axis and this route is dead for our part.

### Three assertions to put in the pipeline permanently

Every failure in this family exits 0.

```
grep -c "faces with face pyramid volume <"   # must be 0
grep -c "does not match any patch"           # must be 0
grep -c "Cannot find any patch names matching"  # must be 0
# plus an explicit comparison of the two interface patch face counts
# plus: no stopAfter key may survive in a dict a pipeline reads;
#       gate on meshMetaDict lastStep, not on exit code
```

---

## 14. What is still unmeasured

Stated explicitly, because several of the numbers above are estimates and should not be quoted as
measurements.

| open | note |
|---|---|
| layer coverage on OUR interface | The only genuinely open variable in the layers route. Our interface spans cell levels, branches so channel width varies and junctions have no clean medial axis, and carries 87 non-manifold edges and 174 non-manifold points. `snappyLayerDriver` has explicit machinery there (it printed "dupNonManifoldPoints : Found : 4032 non-manifold points" and zeroes their displacement), and a staircase with non-manifold points still hit 100%, but those 174 points will get zero displacement and a few hundred faces will lose coverage locally. Bounded, not fatal, not measured |
| peak RSS at production scale | 1.61 kB/cell is measured on a 1.84M to 3.96M cell run. The extrapolation to 155.6M output cells (about 250 GB) is linear and unvalidated |
| non-orthogonality on the real part after layers | 48.8 to 53.3 was measured on proxies whose staircase ramp is about 24.6 degrees (32.6% area inflation) against our measured 11.6% on the channels (about 7 degrees). The real tail should be milder but it will not be zero. Re-measure, do not assume |
| whether the 70-degree bar survives | Layer-induced non-orthogonality was 48.79 to 48.88 in every clean run and 59.8 to 63.9 only in partial-coverage failures, always below 70, and in the test where a transition pre-existed the max was unchanged at 54.74. Plausible, not proven on our mesh |
| the junction-to-junction segment length distribution | Never measured. It is the difference between about -1% and about -43% on total heat removed. The Corintis channels are NOT following the Bejan constructal law (width is near-constant), so segment lengths must be measured, not inferred |
| whether the channels are prismatic in z | Our own 1499.9 um measurement makes it likely. Not proven. Blocks the whole Gmsh route |
| `nearestPatchFaceAMI` region-to-region in v2412 | `develop.openfoam.com` issue #1982 ("BUG in nearestPatchFaceAMI between regions (v2012)") could not be read. Treat as unverified. We are refusing the non-conformal route anyway |
| implicit inter-region energy coupling (`useImplicit`) | Could not establish whether it requires matching faces. The relevant tutorial to read in the image is `$FOAM_TUTORIALS/basic/chtMultiRegionFoam/2DImplicitCyclic` |
| `generateBoundaryLayers` under `-parallel` | The utility's own .C has no decomposition and no Pstream calls. Unproven. We are refusing the utility anyway |
| which of our existing `checkMesh -meshQuality` reports used the shipped `minVol` | Not audited. Until it is, treat all of them as void |
| Shah and London's finite-wall-conductivity (H4) tables | Not available online, the 1978 monograph is not accessible, and the Lyczkowski OCR from OSTI lost Equation (7a) so the exact definition of their wall conduction parameter K could not be recovered. The numeric K_p = 182 to 364 is indicative; the conclusion K_p >> 4 is robust to any plausible definition |
| two bug reports relevant to the faceZone route | develop.openfoam.com #3151 (`locationsInMesh` with a faceZone in `refinementSurfaces` producing wrong cellZones) and #3302 (buffer layer always created on a baffle regardless of `addBufferLayers`). Both second-hand from search extracts; the tracker now demands sign-in. #3151 does not affect us because our faceZone comes from `topoSet`, not from `locationsInMesh` + `refinementSurfaces` |

### Two minor code defects spotted while reading, recorded so they are not lost

- `snappyLayerDriver.C`: the parallel load-balancing weights for faceZone layers use
  `cellWeights[cellIDs[i]] += numLayers[mpi];` inside the `if (numLayers[spi] > 0)` branch. The
  master's layer count is used where the slave's should be. A balance-quality bug, not a
  correctness bug, but at 21.7M interface faces it could skew decomposition for a slave-side-only
  run.
- `workflowControls.C`: `populateWorkflowSteps()` assigns `meshOptimisation=32` and
  `boundaryLayerGeneration=64`, i.e. the ordinal map and the user guide table are in the reverse
  order from the executed call order in `cartesianMeshGenerator.C::generateMesh()`. The map is only
  consumed by `isStepCompleted()`, which nothing in the flow calls. Trust the call order.

### Other v2412 behaviours found and worth knowing

- Layer addition across a `cyclic` patch FOAM-FATALs, new and reproducible:
  `polyTopoChange::getFaceOrder` at `polyTopoChange.C:876`, "Did not determine new position for
  face ... This is usually caused by not specifying a patch for a boundary face", exit 134. Do not
  put cyclic or probably symmetry patches on a box domain where the layered faceZone terminates.
- `snapControls` is fully parsed even with `snap false`. A missing `nRelaxIter` there is a FOAM
  FATAL IO ERROR.
- Parallel snappy aborts in `fvMeshDistribute::findNonEmptyPatch` with "Cannot find a patch which
  is not of type empty, mapped or coupled" on a region whose only patches are mapped or coupled.
  Our fluid region has the ports, so it is safe. A fully enclosed chip region would not be.
- `reconstructParMesh -overwrite` returns exit 1 (usage error) in this build.
- `checkMesh -allRegions -allGeometry` did NOT fatal on an AMI-coupled mesh; it completed and wrote
  weights to `postProcessing/<region>/checkMesh/<patch>-src_constant.vtp`. So our long-standing
  `-allGeometry` FOAM-FATAL is specific to `splitMeshRegions`' `mappedWall` with
  `sampleMode nearestPatchFace`, not to `mappedPatchBase` in general. That is a lead on getting
  `mesh_ok` True on a conjugate mesh and it is tracked separately.

---

## 15. Key sources

Primary, read directly:

- `hexera-mesh:2412`, direct execution, about 40 `snappyHexMesh` addLayers-only runs across five
  independent verification passes
- `/usr/lib/openfoam/openfoam2412/etc/caseDicts/annotated/snappyHexMeshDict`, 902 lines:
  `mergePatchFaces` L36, `faceZoneControls` L483, the "or faceZone" line L681,
  `maxThicknessToMedialRatio` L778, `nBufferCellsNoExtrude` L822
- `/usr/lib/openfoam/openfoam2412/etc/caseDicts/meshQualityDict`
- v2412 `snappyLayerDriver.C` (`dupFaceZonePoints` L4826, `mapFaceZonePoints` L5244,
  `mergeFaceZonePoints` L5315, one-sided freeze L4781-4818, `deltaNLayers` L4950, point retirement
  L5118-5130), `snappyHexMesh.C` L1616, `layerParameters.C` L397 and L534-545,
  `medialAxisMeshMover.C` L1516, `refineWallLayer.C`, `cellCuts.C`/`.H`, `hexCellLooper.C`,
  `meshCutter.C`, `directions.C`, `multiDirRefinement.C`, `AMIInterpolation`,
  `faceAreaWeightAMI`, `mappedPatchBase.H`, `turbulentTemperatureRadCoupledMixed`
- cfMesh v1.1 User Guide section 4.5, extracted from the PDF shipped inside `integration-cfmesh`
- `integration-cfmesh` main @ 3ff8555 (18 Dec 2024, the v2412 line):
  `meshOctreeCreatorCreateOctreeBoxes.C` L409-430, `boundaryLayers.C` L548 and L654-662,
  `refineBoundaryLayersFunctions.C` L684-712, `boundaryLayersCreateVertices.C`,
  `polyMeshGen.C` L123-150, `generateBoundaryLayers.C`,
  `detectBoundaryLayersFunctions.C` L300-323 and L476-499
- Gmsh 4.15.2, direct experiment, plus `src/mesh/Field.cpp` `BoundaryLayerField::setupFor2d`

Ours:

- `~/audit_dl/BUFFER_PROBE.txt` and `~/audit_dl/ROOT_PROBE.txt` (both 5 Nov)
- `devtools/semiconductor/couple_box.sh` L128-165
- `~/audit_dl/cbox2_RESULT.txt`, `cbox2_checkMesh.par.solid.log`
- `docs/semiconductor/COUPLED_MESH_DELIVERY.md`, `docs/semiconductor/MESH_QUANTISATION.md`,
  `DEVLOG.md` L3982
- `~/t3work/C2/log.snappyHexMesh`, `~/t3work/LM/log.layers`

External, verified:

- Municchi, El Mellas, Matar, Magnini, arXiv:2203.09305 (OpenFOAM v2106 conjugate microchannel,
  Dh 229 um, uniform cubic hexahedra, no layers, no grading, 50/100/200 cells per Dh)
- Corintis Glacierware, arXiv:2408.15024 (THERMINIC 2024)
- Corintis patent EP4591205A1 (van Erp, Salazar de Troya, Boutsikakis)
- arXiv:2510.18882 Table 2 (two-layer Darcy-Forchheimer against full-scale resolved)
- van Erp et al., Nature 585:211-216 (2020) and van Erp EPFL thesis TH9480
- Jaiman, Jiao, Geubelle, Loth, IJNME 64:2014-2038 (2005), grid-mismatch function
  `gmm = max(nint(h1/h2)(h2/h1), h2/h1) - 1`, exactly zero at integer ratios
- Xu et al., IJHMT 48:1662-1674 (2005), interrupted microchannels
- LLNL/Colorado State SiC microchannels, LLNL-JRNL-844626
- Shah and London rectangular-duct correlations at alpha* = 0.06718
