# Plan: the Corintis deliverable

Draft 3, 2026-10-30. The deliverable is their PLATE and their CHIP, meshed and coupled.

## The end goal

One conjugate mesh of the cold plate and the chip: the plate's real geometry with copper that is
copper and not air, both passage scales resolved, about 6 cells across the 98 um slots; the chip
as a structured stack with its 9 materials as zones and its 25 heat-source regions as selectable
faces, so their power map drops straight on. Both viewable together or separately, with the six
readings they asked for, zoomable to real cells.

## What actually went wrong, and it is one thing

Three faults were on this page yesterday. Two of them were me.

| | verdict |
|---|---|
| the outlet bore is severed from the circuit | **not real** - that was a stale STL on the bench; the live extraction gives ONE connected piece, 0 open edges, 0 non-manifold edges |
| the plans sized every cell off `narrowest_m` = 2.652 mm | **real, and already fixed** - the live reading is 0.09797 mm |
| we meshed a bounding box, so 60% of our "copper" was air | **real, and mine** - the engine's own gate says use the part's skin for this part |

So there is **one root cause**: both failed runs were driven from hand-built plans made before the
sizing fix, on the wrong domain. The extraction is fine, the sizing is fine, and the two-scale
machinery is fine - `cad/scales`, `plan_two_scales`, `fine_surfaces` and `stretch_zone` all exist,
are tested, and were simply never run on this part through the driver.

## THE PLATE

### Domain

The part's own skin (`domain.stl`, 27,696 facets), not the bounding box.

| | mm3 |
|---|---|
| bounding box | 750,015 |
| the solid region we produced with it | 694,854 |
| the actual metal | 276,372 |

418,482 mm3 of air was being modelled as copper. `use_shell_domain` already returns True here
(share_of_box 0.3685 against a 0.6 bar). This is correctness, not an optimisation.

### Resolution, from the engine's own planner

Measured by asking `plan_two_scales` what it builds at budgets it has never been given - every
refusal of this part has been against `CELL_HARD_LIMIT` = 8,000,000, the hosted path's ceiling.

| budget | broad cell | fine cell | cells across the 98 um slots | estimate |
|---|---|---|---|---|
| 8M / 100M / 236M | - | - | REFUSED | - |
| **400M** | 528 um | 16.5 um | **5.95** | **334M** |
| 700M | 422 um | 13.2 um | 7.44 | 695M |

**Without the y-stretch it refuses at every budget**, so the anisotropic cells are what make this
part meshable at all, not a nicety. 400M is the one to build: 695M needs about 480 GiB and
mesh-bench has 256, while 334M lands where the 336M run actually peaked.

### The thing that failed last time, and the lever for it

The fitted run already had the right plan shape - fine surface at 4 levels, `anisotropicSources`
scaleY 6 - and still ran 4 h 06 m without converging. Surface untangling reached 0 bad faces; the
`meshOptimisation` phase then oscillated between 5,283 and 9,523 bad faces for hours.

Its meshDict carries **no `meshQualitySettings` and no `workflowControls`**, so it ran cfMesh
defaults with that phase unbounded. Our engine writes neither key. cfMesh has both:

    workflowControls { stopAfter <stage>; }     restartFromLatestStep
    templateGeneration -> surfaceTopology -> surfaceProjection -> patchAssignment
      -> edgeExtraction -> boundaryLayerGeneration -> meshOptimisation -> boundaryLayerRefinement

So: stop before `meshOptimisation`, look at the mesh, and only pay for optimisation if checkMesh
says we need it - which is a resume, not a restart. That is also most of the answer to "can five
hours be less": on the 336M run the actual meshing was 14% of the time.

### Gates, in order

1. the solid region's volume is about 276,372 mm3, not the box's 750,015
2. both declared ports are in the same connected coolant piece, with at least one in and one out
   (a piece COUNT tests nothing: 1 piece can still have a dead end, and the engine's
   `expect_fluid_pieces` is set from what the extraction found, so it checks its own answer)
3. checkMesh `Mesh OK` on both regions
4. interface faces equal on both sides
5. cells across the narrowest passage >= 4, measured off the mesh, not derived from the plan
6. the six readings present

## THE CHIP

Equal half of the deliverable, and cheap. Measured off `chip.ecxml`: **17 solid blocks, 9
materials, 25 2D heat sources, 70 x 78.62 x 3.55 mm**, thinnest layer **10 um** (interposer
metallization).

| layer | thickness |
|---|---|
| interposer metallization (Cu) | 10 um |
| wafer bond | 50 um |
| interposer (Si) | 60 um |
| chip solder, TIM | 300 um |
| mold, ASIC, 8x HBM | 699 um |
| chip substrate | 2,060 um |

### Why it is NOT an octree job

| | cells |
|---|---|
| isotropic cubes, 3 across the 10 um layer | 527,000 M |
| structured and anisotropic | **23 M** |

22,000x. It is a stack of boxes, so it wants a structured hex mesh matching those boundaries.
Forcing it through cfMesh would drag the cell size down everywhere near that 10 um layer.

### How it is built

    a NON-UNIFORM cartesian grid whose lines include every block edge and every
    source edge, in all three axes

42 boxes (17 solids + 25 sources) give at most ~84 lines per axis, then fill to the target size.
This is what Icepak itself does, which is why their model will drop straight on. It costs nothing
over the 23M, because the alignment is in WHERE the lines go, not how many.

That gives, exactly rather than approximately:

- every layer interface on a cell face, so no cell straddles two materials
- every source rectangle on cell edges, so the power footprint is not stair-stepped
- the 25 sources as **faceZones**, selectable
- the 9 materials as **cellZones**, carrying their orthotropic axes (17 entries are orthotropic)
- in-plane grading: fine under the ASIC and HBM footprints, coarse at the substrate edges

Plus a manifest mapping our zone names to theirs, so they do not have to guess.

### What blocks it

Registration, and nothing else. The two files are in different coordinate frames and the chip
(70 x 78.62 mm) is larger than the Microfluidics_Core footprint (63 x 55 mm), so which face it
bonds to and in what orientation is a question for them. The mesh does not change when they
answer - only its transform does - so it can be built standalone and placed later.

## Coupling the two

Meshed as regions of one case and coupled at the flat bond plane through `mappedWall`, which is
what the conjugate engine already uses for fluid/solid. The viewer already shows regions
individually or together with a Restore full mesh control, so chip-alone, plate-alone and both is
existing behaviour, not new work.

## Boundary layers

Zero in the slots, and the reason we currently print is measurably false.

- measured: `generateBoundaryLayers` with `optimiseLayer 0` kept all 14,477 interface faces
  identical on both sides to 4e-18 m, 99.97% coverage. Layers do not break conformality.
- what actually stops them: a prism stack does not fit in a 98 um channel at any affordable
  resolution, and quality goes to the bar (non-ortho 13.82 -> 64.95 against 65).
- near-wall resolution comes from cells across the channel, which is the same lever as the fine
  surface. For a channel this narrow, across-the-channel IS near-wall.
- **layers on the manifolds only** (`coolant_coarse`, 67% of the wetted area, room for a stack)
  are a worthwhile second run and would let us report a real count. Needs two repairs measured
  today: cfMesh's writer drops the mappedWall coupling keys, and checkMesh then flags
  upper-triangular ordering wanting `renumberMesh`, which this repo has never run.

## Delivery

OpenFOAM polyMesh plus the VTK export, Fluent or CGNS on request. It is the only format that
carries the regions and the conformal interface without a second pass over 334M cells.

## Product fixes

| | state |
|---|---|
| their question "number of boundary layers" read as a demand to build them, cutting the conjugate engine's fitness by 30% | **FIXED**, pinned with their own sentence, 587 pipeline tests pass |
| interface area vs wetted CAD area measured and then filtered out, understating their heat transfer surface by 20% | to do |
| cells-across reported from the plan rather than measured off the mesh | to do |
| the extraction record labels millimetres as `_m3` and `_m2`, out by 1e9 | to do |
| nothing counts the extracted coolant's pieces before meshing | to do |

## Ask Corintis

1. conduction or conjugate
2. flow rate and coolant composition
3. which solver
4. where the chip bonds to the plate and in what orientation
5. whether the chip power map is coming (every `powerDissipation` is zero)

## Order of work

1. Plate: run the driver on the shell domain at 400M, stopping before `meshOptimisation`.
2. Check it, then decide whether to pay for optimisation.
3. Gates, in order.
4. Viewer.
5. Chip: build standalone now, place when they answer registration.
6. Couple, and show both.
7. Second run: layers on the manifolds.
