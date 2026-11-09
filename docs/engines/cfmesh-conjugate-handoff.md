# End to end conjugate meshing: what exists, what is missing, where to start

Written 2026-11-08 for whoever takes this on. The goal is Hexera generating, for any geometry, a
mesh like the one we delivered on the Corintis cold plate. This is what you are inheriting, what is
genuinely missing, and the order I would do it in.

Every claim here has a file path or a number behind it. Where I am not sure, I say so.

## The short version

| | state | where |
|---|---|---|
| a conjugate meshing engine in the platform | **built and registered** | `src/meshpipeline/engines/cfmesh_conjugate/` |
| a big-box execution path | **built and wired** | `contracts/mesh_machine.py`, `adapters/mesh_execution/gce_vm.py` |
| the recipe that built our example mesh | **in git since today** | `devtools/semiconductor/recipe/` |
| boundary layers in the engine | **not started**, two named blockers | `finalize.py:383` |
| the engine proven at our scale | **not done**, 41,616 cells against 317M | `engines/validation_evidence.json` |
| the agents actually driving it | **not done**, graph stubbed | same file, tier line |

Three things to build. One is hard, one is a scale run, one is integration. The infrastructure under
all three already exists, so none of this is a rewrite.

## 1. What is already in the platform

**The engine is real, not a script.** `src/meshpipeline/engines/cfmesh_conjugate/`, 23 modules,
7,587 lines. The big ones are `conjugate.py` (1,586), `budget.py` (694), `driver.py` (611),
`finalize.py` (586), `foam_exec.py` (483), `native.py` (449), `mouths.py` (449).

It is wired into engine selection, not bolted on the side: `dispatch.py`, `manifest.py`,
`purposes.py`, `fitness.py`, `home_turf.py`, `return_scope.py`, `verdict_limits.py`,
`pipeline/engine_fallback.py`, `pipeline/executor.py`, `settings/inventory.py`,
`settings/runtime.py`.

**What it already does.** Cuts the coolant out of the CAD, builds the zones, splits the regions,
makes the conformal `mappedWall` interface match face for face on both sides, applies cfMesh's
anisotropic stretch, plans a cell budget over two scales, runs in parallel, and checks itself
through eight declared gates.

**Its registered delivery**, from `engines/validation_evidence.json`, job
`67161f7d-2c8c-4938-a350-a2d1ca85e451`, 2026-10-26: `cold_plate_2ch.step`, one solid 2.0 x 4.0 x
1.0 mm with two roofed 0.3 x 0.4 mm channels. The coolant came out of the CAD exactly, 0.960000 mm3
against 0.960000. 41,616 cells in 2 regions, interface 2,992 faces both sides, max non-orthogonality
2.9352. All 8 gates passed, mesh and bundle and viewer data all delivered.

**The big-box path exists.** `src/meshpipeline/contracts/mesh_machine.py` has a ladder:

```
n2-highmem-8    64 GiB     8 cores
n2-highmem-16  128 GiB    16
n2-highmem-32  256 GiB    32
n2-highmem-64  512 GiB    64     <- the ceiling
```

`choose(process_gib=, workspace_gib=)` picks from it and flags `over` when the need exceeds the
ceiling. `adapters/mesh_execution/gce_vm.py` then runs the mesh job's own container on that VM with
the same image, service account and exchange contract as the Cloud Run path, and the VM deletes
itself. **n2-highmem-64 is the exact box I rented by hand for our mesh**, so the substrate is
adequate. Nobody has walked the engine up the ladder, that is all.

## 2. The three gaps

### Gap 1: the engine adds no boundary layers. This is the hard one.

`finalize.py:383` says `NEAR_WALL_LAYERS = 0`, and the comment above it is the most useful thing in
the file. The obstacle is mechanical, not a missing feature:

> `boundaryLayers.patchBoundaryLayers` names PATCHES, and at meshing time the coolant's wall is not
> a patch. It is interior to one manifold block, and the patch is created by `splitMeshRegions`
> afterwards. So layers here mean a second pass over the delivered mesh, not a key in the meshDict.

That is exactly how we did it by hand: cfMesh builds the mesh, `splitMeshRegions` creates the
patches, and then a separate `snappyHexMesh -addLayers` pass puts layers on `fluid_to_solid`.

**Two repairs stand in the way, both already measured and written down in that comment.** cfMesh's
writer drops the `mappedWall` coupling keys, and `checkMesh` then flags upper-triangular ordering
and wants `renumberMesh`, which this repository has never run. Start there. They are specific.

**What I learned today that you should not have to rediscover.** I ran five layer variants on the
real plate, 45 minutes each, changing only the layer dictionary. Full table and logs in
`docs/semiconductor/MESH_QUALITY_RANKED.md` section 14 and `docs/semiconductor/fixruns/`. The
useful conclusions:

- The defect in our shipped coolant is 2,936 faces failing one mesh check, and 89% of the bad
  angles are on the coarse plenum wall within 1.2 mm of the ends of the anisotropic stretch box.
  Measured, not inferred: `devtools/semiconductor/badfaces.cpp` recomputes them and gates against
  checkMesh's own printed maxima.
- The angle is `atan(drift / thickness)` where the drift scales with the cell WIDTH, about 4.5% of
  it. So making the layer thickness track the local cell size collapses the angle. That took
  severely non-orthogonal faces from 2,096 to 10.
- Every route to fixing the angle costs interpolation weight instead, either by thinning the stack
  or by terminating it. Do not expect a free win.
- Asking for layers on only part of a patch is worse than it sounds. Splitting the interface and
  layering just the channel wall got the best geometry of all five, maximum non-orthogonality
  67.2138 and zero severely non-orthogonal faces, but coverage fell to 91.90% and the stack filled
  only 77.80% of the requested cells, and that shortfall is on the channel wall, which is the only
  place the layers exist for.

### Gap 2: the engine has never produced a mesh at our scale

Its registered delivery is 41,616 cells. Our plate is 317,657,193. That is about 7,600x.

The gap is not the substrate, which is built. It is that nobody has run the engine big, so the
surprises at scale are all still ahead. The ones I hit by hand, so you can look for them:

- **The 2^31 face label is the real ceiling, not memory.** The build is `label=32`. 767M cells sits
  at 107.9% of it. Practically the ceiling is 600M to 650M cells. If a customer asks for 700M,
  decide `WM_LABEL_SIZE=64` before the mesh, not after.
- **`writeFormat` must be `ascii`.** Binary is 1.3 to 1.8x slower on every stage AND unreadable to
  the boundary index, the viewer and the quality sidecar. The readers refuse it by design and the
  refusal lands only after the whole chain has succeeded.
- **The serial tail will eat you.** Our first delivery spent 4,347 s of a 2.28 h job on one core,
  reconstructing and gzipping. `pigz` plus skipping the reconstruct took that to 87 s. There is a
  `parallel_gzip.py` in the platform already.
- **Do not reconstruct if you do not have to.** The boundary index and the quality sidecar both
  build straight off the decomposed mesh, 13 s and 9 s on a 270M cell mesh.

### Gap 3: tier T4, not T5

The evidence file says it in its own words:

> T4 (native-to-terminal: the real engine and mesh image, the real executor gates and the
> production terminal chain against real Postgres/Redis/MinIO, with the graph stubbed and the
> reviewer a deterministic PASS, no model calls). **T5 pending a hosted job with a real intake and a
> real visual review.**

So the plumbing has been exercised end to end but the agents have never driven it. Unchanged since
26 October.

## 3. The order I would do it in

1. **Walk the engine up the machine ladder.** Take the registered 41,616 cell case and run the same
   engine on something 100x bigger, then 1,000x. You will find the scale bugs with a case you
   already trust rather than with a customer's part. Cheap, and it de-risks everything after it.
2. **Fix the two named layer repairs.** The coupling keys and `renumberMesh`. Then add the layer
   pass as a second stage after `splitMeshRegions`, which is where the code comment says it has to
   go. Use the dictionary that wins in section 14 of the quality doc rather than starting from
   scratch.
3. **Take it T4 to T5.** A real intake, a real geometry read, a real visual review on one case.
4. **Then the hard problem below.**

## 4. The thing that is actually hard, and is not in any of the above

Our example was hand tuned. The numbers in `devtools/semiconductor/recipe/inputs/meshDict` are
mine, for this plate: a 402.525 um background cell, four refinement levels at the channels, two at
the chip, one at the coolant surface and the plate skin, and a stretch of 3 in one direction over
the microchannel band only.

**End to end for any geometry means deriving those numbers from the geometry, not reading them from
our file.** That is the real product problem and it is a different problem from running the chain.
`budget.py` (694 lines) and `plan.py` (338) are where that reasoning already lives, and
`budget.plan_two_scales` is the function that decides sizing today, so start by reading what it
already infers and what it still takes on faith.

Two measured things that should inform it:

- **Commensurate lattices matter more than fineness.** `4 x 25.1578 = 5 x 20.12625` exactly, so a
  coarser lattice gave the IDENTICAL 100.631 um channel at 4 cells across instead of 5, for 270M
  cells instead of 440M and 65 minutes instead of hours. The right question is not "how fine" but
  "which pitch divides the feature".
- **The thermal cost of 4 cells instead of 5 is +0.679% on developed Nusselt**, and the mean error
  over the run is +1.41% at Re 100, +2.40% at Re 226 and +4.48% at Re 1000. That is the kind of
  number the sizing step should be able to predict before it commits.

## 5. What the example mesh actually is

317,657,193 cells: plate 199,394,482, coolant 102,536,327 layered, chip 15,726,384. The conjugate
interface matches face for face at 16,619,777 on both sides, counted by separate checkMesh runs on
separate regions. 98.689% hexahedra and **zero tetrahedra**. 1.08 h to the delivered mesh and 87 s
to package it.

**Read `docs/semiconductor/MESH_QUALITY_RANKED.md` before quoting any figure from it.** It ranks
nineteen aspects Great to Really Bad with the inputs left in so they can be checked, and it is
honest about what is wrong, including four numbers I published about this mesh that were wrong and
are corrected there.

The one open defect: the layered coolant fails one mesh check, 2,936 faces of 102 million. That is
the thing section 14 is about and the thing the five variants were trying to fix.

## 6. Where everything lives

| | |
|---|---|
| the engine | `src/meshpipeline/engines/cfmesh_conjugate/` |
| machine sizing, big-box path | `src/meshpipeline/contracts/mesh_machine.py`, `adapters/mesh_execution/gce_vm.py` |
| the recipe, drivers, dictionaries, STLs | `devtools/semiconductor/recipe/` |
| the honest quality record | `docs/semiconductor/MESH_QUALITY_RANKED.md` |
| raw logs from every layer variant | `docs/semiconductor/fixruns/` |
| analysis tools I wrote for this | `devtools/semiconductor/badfaces.cpp`, `parbad2.cpp`, `chanset.cpp`, `chipcontact.cpp`, `patcharea.cpp`, `stl_bodies.py` |
| the delivered mesh itself | `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/cbox8fixed/` and `.../mesh/bl8/` |

The mesh is not in git and should not be. The bucket paths are in the quality doc.

Branch from `rehaan/semiconductors`, not from main. Main does not have the conjugate engine.

## 7. Four traps, each of which cost me hours

- **`createPatch` in OpenFOAM 2412 rejects `-constant`.** Pass `-overwrite` alone. When I passed it,
  the utility aborted, the patch split silently did not happen, and the layer pass downstream then
  reported "Finished meshing without any errors" in 143 s having added **no layers at all**. A
  silent success is the worst failure mode there is. Gate on the thing you asked for existing.
- **`-np` must equal the processor count** of the decomposed mesh, or FOAM aborts.
- **Never run a box without `--max-run-duration` and `--instance-termination-action DELETE`.**
  Restarting such a box re-runs its startup script, which has silently relaunched a 96-core
  experiment in this project before. Write a sentinel first.
- **A self-deleting box takes its mesh with it.** I lost two experimental meshes that way and can
  no longer answer questions about either. Archive the mesh, or run the analysis on the box, before
  you let it die.
