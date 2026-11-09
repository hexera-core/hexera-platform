# The pitch-8 cold plate mesh, everything needed to rebuild it

Added 2026-11-08 because none of this was in git. `couple_fast.sh`, the driver that actually
produces the mesh, the five STLs and all four dictionaries existed only in `~/rebuild_kit` on one
laptop and in the bucket. Anyone picking this up had no way to reproduce it from the repository.

## What is here

| | |
|---|---|
| `drivers/couple_fast.sh` | the whole chain: mesh, four zones, split, check. 18 min 42 s |
| `drivers/port_split.sh` | cuts the coolant's air contact into `port_a` and `port_b` |
| `drivers/layers_par.sh` | the boundary layer pass, in parallel |
| `drivers/layers_full.sh` | the earlier serial layer pass, kept because its header records why serial was wrong |
| `drivers/mkfast.sh` | builds the fast chain |
| `inputs/meshDict` | the refinement recipe and the anisotropic stretch box |
| `inputs/topoSetDict` | the port split selection |
| `inputs/createPatchDict` | the port patches |
| `inputs/controlDict`, `inputs/decomposeParDict` | `writeFormat ascii` matters, see below |
| `inputs/triSurface/*.stl` | the five surfaces, 5.4 MB |

The STLs are in git deliberately. They are 5.4 MB, they are the actual customer geometry the mesh
is built on, and a recipe without its geometry is not a recipe.

## How to run it

The image is `us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324`. The
drivers call Docker directly and expect the STLs at `/mnt/work` under the `cb_` names:
`cb_block.stl`, `cb_fluid.stl`, `cb_fluid_fine.stl`, `cb_chip_fit.stl`, `cb_domain_box.stl`.

`couple_fast.sh` takes `LAB BG LCOOL LFINE LCHIP LSKIN RANKS THREADS [LFINE2] [LCHIP2] [DOM]`.
The delivered mesh was:

```
couple_fast.sh cbox8 0.000402525 1 4 2 1 8 12
```

which is a 402.525 um background, coolant +1, microchannels +4, chip +2, plate skin +1, on 8 ranks
of 12 threads, with `scaleY 3` over the microchannel band only.

## Four things that will bite

**`writeFormat ascii`, not binary.** Binary is 1.3 to 1.8x slower on every stage and makes the mesh
unreadable to the boundary index, the viewer and the quality sidecar. The readers refuse it by
design and the refusal lands only after the whole chain has succeeded.

**`createPatch` does not accept `-constant`** in OpenFOAM 2412. Pass `-overwrite` alone. If you
pass `-constant` the utility aborts, and if the thing downstream was a layer pass it will report
"Finished meshing without any errors" having added no layers at all.

**`-np` must equal the processor count.** The archives are decomposed 8 ways. Re-decomposing a
300M cell mesh costs more than it saves.

**Do not run a box without `--max-run-duration` and `--instance-termination-action DELETE`.**
Restarting such a box re-runs its startup script, which has silently relaunched a 96-core
experiment in this project before. Every startup script here writes a sentinel first.

## What the mesh is

317,657,193 cells delivered: plate 199,394,482, coolant 102,536,327 layered, chip 15,726,384.
The conjugate interface matches face for face at 16,619,777 on both sides. 98.689% hexahedra and
zero tetrahedra. 1.08 h to the delivered mesh, 87 s to package it.

Read `docs/semiconductor/MESH_QUALITY_RANKED.md` before quoting any figure from it. It ranks
nineteen aspects of the mesh Great to Really Bad with the inputs left in, and the one open defect
is in the layered coolant.
