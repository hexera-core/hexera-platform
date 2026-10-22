# Semiconductor meshing

Conjugate solid/fluid meshes for chip cooling, on the engine the platform already uses.

## Why this exists

A chip-cooling answer is conjugate: the silicon and the coolant are solved together, exchanging
heat across the channel walls. That needs two mesh regions with a **conformal** interface, where
every face on one side has exactly one partner on the other.

cfMesh cannot produce that. It meshes manifold domains only and discards zone information.
snappyHexMesh can, through `locationsInMesh`, but carries a hard cell ceiling that a chip-scale job
walks straight into. So the engine that scales could not do conjugate work and the engine that
could do conjugate work would not scale.

## The approach

Never ask cfMesh for regions. Give it one manifold domain, the outer block of the whole part, and
let it build a single octree across metal and coolant alike. Then cut the finished mesh in two.

    block surface  ->  cartesianMesh  ->  topoSet (tag + name)  ->  splitMeshRegions  ->  2 regions

The interface is conformal **by construction**: both regions are the same cells from the same
octree, so the surface between them is a set of internal faces that were always there. Nothing is
reconstructed and there is no tolerance to tune. Meshing the two regions separately and stitching
their surfaces afterwards is the approach that fails.

## Running it

    python devtools/semiconductor/conjugate_part.py <solid.stl> <fluid.stl> \
        --cell 80e-6 --levels 3 --expect-fluid-mm3 6.311812 --expect-pieces 1

Both STLs must be in **metres**. The parts in `cad/semiconductor/` already are.

Add `--dry-run` to write the dictionaries and stop, which is how to see what a run would do without
paying for it.

OpenFOAM has to be on PATH, so this runs inside the mesh image:

    docker run --rm --user root -v "$PWD":/work -w /work --entrypoint bash hexera-mesh:2412 -c \
      'source /usr/lib/openfoam/openfoam2412/etc/bashrc; PYTHONPATH=/work/src python3 \
       devtools/semiconductor/conjugate_part.py cad/semiconductor/murray_branching_4gen_solid.stl \
       cad/semiconductor/murray_branching_4gen_fluid.stl --cell 120e-6 --levels 2 \
       --expect-fluid-mm3 6.311812 --expect-pieces 1 --workspace /tmp/conj'

## Why the expectations are worth passing

`--expect-fluid-mm3` is what proves the **right cells** were tagged. A conformal interface proves
the two sides share a surface and nothing about which cells ended up on which side: a split that
put half the metal in the coolant region would be perfectly conformal and completely wrong.

`--expect-pieces` is how many separate passages the part has - 1 for a connected network, N for N
channels that never touch. It catches a mesh too coarse to hold a passage together, which `checkMesh`
reports as "Mesh OK" and no solver will run. On a coarse build the coolant came back as 15 pieces:
one body of 5,386 cells and 14 islands of 24 to 32. Every geometric bar was green, and the solve
died in the preconditioner because an island has no face path to the pressure reference cell.

## Sizing

`engines/gap_sizing` owns resolution and holds every passage to `PASSAGE_CELLS_ACROSS`, with the
resolution_floor gate refusing anything below `PASSAGE_FLOOR_CELLS` (12). This path does not
second-guess it.

`engines/staircase_error` says what a resolution is **worth**, measured on real meshes rather than
cited from practice:

| cells across | volume error |
|---|---|
| 2 | 3.21% |
| 5 | 1.04% |
| 12 | 0.12% |

12 across measured 0.12%, so the floor the gate already enforced is now corroborated by measurement
and not only by citation.

## Where the time goes

Not the mesher, which is the thing worth knowing before promising a turnaround.

| | 100-channel part (81.6M cells) | branching part (1.2M cells) |
|---|---|---|
| cartesianMesh | 617 s | 9.3 s |
| everything after | over an hour | 18.0 s |
| post-processing share | >85% | 66% |

`topoSet` tests every cell centre against the coolant surface and `splitMeshRegions` writes two
full meshes. Both are serial. A conjugate mesh in thirty minutes is a post-processing problem, not
a meshing one. `ConjugateReport.seconds` carries the per-stage split so the attribution is
measured on every run rather than assumed.

## The test parts

`cad/semiconductor/` holds three, in metres, each verified against its source CAD before export:

| part | narrowest | fluid volume | what it stresses |
|---|---|---|---|
| `murray_branching_4gen` | 119 um | 6.3118 mm3 | one connected branching network, three-way junctions |
| `corintis_straight_baseline_100um` | 100 um | 260.0 mm3 | 100 channels that never touch each other |
| `embedded_silicon_D9_10um` | 10 um | 0.75 mm3 | 200 channels separated by 15 um fins |

None is a replica of any customer part and none should be presented as one.

## Measured results

All three produced conformal interfaces, 2026-10-21:

| part | interface faces | fluid volume vs CAD |
|---|---|---|
| murray | 1,065,510 both sides | +0.12% |
| corintis 100ch | 13,700,762 both sides | -1.04% |
| embedded D9 | 6,747,560 both sides | -3.21%, solid +0.05% |

On D9 the solid landing within 0.05% is what proves the 15 um fins survived rather than letting
neighbouring channels merge into one region.

A conjugate solve then ran on the murray mesh: `chtMultiRegionSimpleFoam`, 25 iterations, silicon at
148 W/m/K against water with 25% propylene glycol, chip face held at 350 K. The solid cooled to
348.3 K and the coolant rose to 349.9 K, so heat crossed the interface as intended.
