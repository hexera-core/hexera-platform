# The two viewers, how to join them, and how to show the whole mesh

Written 2026-11-08. Areen's viewer on `main` is the one that ships. Ours on
`rehaan/semiconductors` is a set of extras built to answer questions about one 317M cell mesh.
This is what each has, how ours should land on his rather than beside it, and what it would take
to show an entire mesh instead of a window into one.

## 1. His is ahead on structure, and it is not close

Both descend from the same viewer, and both still import the same `../core/format.js`,
`../api/endpoints.js`, `../api/client.js`, `./dispute.js` and `./config.js`. The divergence is
inside the viewer package.

| | main (Areen) | ours |
|---|---|---|
| modules | **16** | 9 |
| total lines | 5,670 | 4,795 |
| `viewer.js` | 1,875 | **2,429** |

Note the last row. **His whole package is bigger while his `viewer.js` is 554 lines smaller**,
because he decomposed it and we did not. What he pulled out:

| module | lines | what it is |
|---|---|---|
| `parts_view.js` | 401 | per-part view |
| `mesh_partial.js` | 301 | the mesh as it is BEING built, one frame per published pointer |
| `toolbar.js` | 264 | icons, segmented controls, axes, PNG snapshot, theme colours |
| `result_panel.js` | 259 | verdict, key numbers, hot spots, downloads |
| `intake_view.js` | 181 | |
| `scene/canvas.js` | 165 | mounting the render window |
| `scene/overlay.js` | 114 | |
| `scene/vtk_load.js` | 39 | the base64 to typed-array readers and `loadVtk` |

We have exactly one module he does not: `inspect.js`, 218 lines.

So the 966 lines we added this week went **into the monolith he has since broken up**. A
three-way merge of `viewer.js` will be miserable and will produce something worse than either
side. Do not do it.

## 2. What we actually have that he does not

Four capabilities, and they are the only things worth carrying over:

| ours | what it does | where it belongs in his tree |
|---|---|---|
| `inspect.js` | click a point, fetch the real cells there at full resolution, read a cell's own numbers | stays a module; swap its readers for `scene/vtk_load.js` and its chrome for `toolbar.js` |
| Follow mode | the cut tracks the camera and sizes its own window by a geometric bracket | new `follow.js`, bound through `toolbar.js` |
| `oldInternalFaces` handling | a cross-section's exposed interior faces render as an ordinary surface, so you can see **inside** the plate | his `parts_view.js`, or a new `section.js` |
| the edge-visibility cap | refuses `setEdgeVisibility` above a polygon count | **a bug fix for him**, see below |
| `patch_id` colouring | paints the surface by patch | probably subsumed by `parts_view.js`; check before porting |

He already has quality display: `quality_fields`, `non_ortho` and `skew` all appear in his
`result_panel.js` and `viewer.js`. So quality is not our contribution. What may still be ours is
painting those numbers **on the surface** rather than listing them in a panel, which is worth
checking before claiming.

### The edge cap is a defect in his copy right now

He calls `setEdgeVisibility` in three files (`viewer.js`, `geometry_stage.js`,
`mesh_partial.js`) with no guard. We measured it **losing the WebGL context at 1.9M faces** - the
canvas goes black and does not come back. His `mesh_partial.js` draws decimated frames so it is
probably safe today, but `viewer.js` on a delivered mesh is not. This is the one change I would
send him on its own, ahead of any merge, because it is small and it is a crash.

## 3. How to land ours on his

1. **Take his `viewer.js` wholesale.** Ours has nothing in it worth keeping that is not one of
   the four capabilities above.
2. **Port the four as modules against his seams** - `scene/vtk_load.js` for readers,
   `scene/canvas.js` for the render window, `toolbar.js` for controls, `core/state.js` for state.
3. **Send the edge cap separately and first**, as a fix rather than a feature.
4. **The platform-side half merges cleanly and is the real substance.** None of it touches his
   files: `render/boundary_index.py` (670), `render/boundary_quality.py` (580),
   `render/detail_window.py` (461), `render/index_surface.py` (222), `render/polymesh_budget.py`
   (219), `parallel_gzip.py` (250). That is the machinery that makes a 36M face boundary
   addressable at all, and it is additive.

## 4. Seeing the whole mesh

### Why it does not work today

The delivered set has **36,490,208 boundary faces**. Measured, not estimated:

| | |
|---|---|
| vtk.js draws | 3.81M triangles in 0.30 ms of CPU per frame, 55 fps |
| so rendering is not the limit | |
| VRAM is | about **92 B per delivered face** |
| 36.49M faces therefore need | **about 3.4 GB of VRAM** |
| `setEdgeVisibility` dies at | 1.9M faces |
| transport, binary vs base64 | 574 ms against 1,379 ms |

So the whole boundary at full resolution is out of reach of a browser, and will stay out of
reach. 3.4 GB is not a number that falls to a better loader.

### The approach I already refuted, and why it is not this one

I tried spatial tiling and killed it: 71.4% of the tiles came back empty, and full detail only
reached 4 mm of a 170 mm part. **That failure was specific to tiling at full resolution.** It
divides the mesh into boxes and shows you one box, so you never see the whole thing - which is
exactly the complaint.

A level-of-detail hierarchy is the opposite shape: it shows you **everything, coarsely**, and
refines only where you are looking. The whole mesh is on screen at all times.

### What the field actually does: cluster LOD

The mature technique is a DAG of small triangle clusters, each cluster a patch of the surface at
one detail level, with a cluster swapped for a coarser ancestor when its on-screen error stays
under about a pixel. This is what Nanite does and what `meshoptimizer` now builds directly:
`meshopt_buildClusters` plus `meshopt_simplify`, and zeux's September 2025 write-up covers
building the hierarchy at scale.

One gotcha from its README that would otherwise cost a day: **every cluster at every level must
share one quantization exponent**, or adjacent clusters at different levels leave visible gaps in
the surface.

For us that means:

- build the DAG **once, offline, on the box where the mesh already is**, not in the browser
- the browser walks the tree against the view frustum and a screen-space error target
- the whole boundary is always drawn; detail arrives where the camera is

### Why this is cheap for us specifically

We already have the three hard pieces:

| we have | what it gives the LOD build |
|---|---|
| `boundary_index.py` | the boundary surface extracted straight off the decomposed mesh, 13 s on 270M cells, no reconstruct |
| `polymesh_budget.py` | patch-wise reduction with a face budget, which is one level of the pyramid already |
| `detail_window.py` | full-resolution fetch for a region, which is the leaf of the tree |

So a cluster-LOD pyramid is a **generalisation of what exists**, not a new subsystem. The missing
pieces are the cluster builder, a manifest describing the tree, and a frustum walk in the client.

### The honest alternative

Server-side rendering - render on a GPU box and stream frames, the ParaView/trame model - removes
the client limit entirely and is what the VTK community recommends for data this size. It costs a
GPU per concurrent viewer and puts network latency between the mouse and the picture. I would
keep it as the fallback for the genuinely enormous case rather than the default.

## 5. Ideas worth adding that are not a merge

These come out of this week rather than from the viewer code, and the first is the one I would
build.

**Draw the bad cells.** Section 14 says the coolant has 2,936 faces failing one mesh check, and
I now have the position of every one of them in a CSV. In a panel that is a number nobody can
act on. In the viewer it is a cloud of markers you can fly to, and the question "is this defect
anywhere near the flow I care about" answers itself. We measured that 89% of them sit on the
plenum wall at the ends of the stretch box and only 232 are on channel-resolution wall - which
took me a C++ reader and an afternoon, and would have taken ten seconds of looking.

**Show the conjugate interface as its own surface.** It is the one thing in a CHT mesh that must
match face for face, and we currently prove it with two numbers that happen to agree
(16,619,777 both sides). Drawing it, coloured by which side it came from, makes a mismatch
visible instead of arithmetic.

**Label flow direction on the ports.** `port_a` is the outlet and `port_b` the inlet, forced by
exclusion from the customer's own STEP names. A viewer that shows two identical unnamed holes is
one mis-set boundary condition away from a silently inverted solve.

**A section plane that is a first-class control.** We can already render a cross-section as an
ordinary surface through `oldInternalFaces`. For a conjugate mesh the interesting thing is almost
always inside the metal, and a drag-a-plane control would make the whole interior reachable
without the detail window.

## 6. What I would not do

Merge `viewer.js`. Build tile streaming again. Write our own simplifier when `meshoptimizer`
exists and is a WASM build away.

---

Sources for the LOD section: [zeux.io, Billions of triangles in
minutes](https://zeux.io/2025/09/30/billions-of-triangles-in-minutes/) ·
[meshoptimizer](https://github.com/zeux/meshoptimizer) · [Cesium, hierarchical LOD with 3D
Tiles](https://cesium.com/blog/2026/04/27/3d-gaussian-splats-lod/) · [vtk.js on the
web](https://www.kitware.com/vtk-js-the-visualization-toolkit-on-the-web/) · [VTK discourse, web
rendering of large
datasets](https://discourse.vtk.org/t/confusion-with-tool-selection-vtk-vs-paraview-for-web-based-volume-rendering-of-large-data-sets/1384)
