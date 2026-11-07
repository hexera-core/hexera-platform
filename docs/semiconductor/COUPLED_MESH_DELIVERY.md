# Coupled plate + chip mesh: what it is, what I checked, what to watch

Built 2026-11-05 on `corintis-farfield`. Label `cbox2`. One mesh, four zones, three delivered.

## What it is

| region | cells | what it is |
|---|---|---|
| fluid | 68,806,800 | the coolant |
| solid | 271,759,108 | the plate metal |
| chip | 24,173,140 | the chip, which is two disjoint bodies |
| air | 48,534,275 | split off, **not delivered** |
| **total** | **413,273,323** | |

Cell sizes: background 352.056 um, plate skin and coolant plenums 176 um, chip 88 um, microchannels
22 um, with a 3x stretch in y over the microchannel band only.

The chip and the plate were cut from the same block, so the contact between them is a conformal
interface the mesher built, not two meshes placed against each other.

## The interfaces

| between | faces | both sides agree |
|---|---|---|
| fluid - solid | 21,708,529 | yes |
| air - chip | 1,956,532 | n/a, air not delivered |
| air - solid | 1,589,463 | n/a |
| **chip - solid** | **353,184** | yes |
| air - fluid | 3,465 | n/a, these are the two ports |

Read these off each region's own `boundary` file. **Do not use splitMeshRegions' "Sizes of
interfaces" table**: it overcounts three of the five, by 13,997 faces in total.

## What I checked

| check | result |
|---|---|
| the four zones partition the domain | exact, and splitMeshRegions accepted it |
| the four region volumes sum to the box | **-0.007%** |
| each region against its CAD surface | within a half-cell staircase budget, all four |
| `checkMesh` per region | **Mesh OK** on all four |
| `Boundary openness` after the merge | OK to 1e-12 on all three delivered |
| worst non-orthogonality | 51.46, and **zero faces over the 70 degree bar** |
| chip-to-plate contact area | 2,735.93 mm2 against the CAD's 2,744.50, **-0.31%**, about half a cell |
| the contact, cut live at a point | `chip_to_solid` 404, `solid_to_chip` 404 |
| the coolant walls, cut live | 26,664 / 26,664 and 14,903 / 14,903 |

For comparison, the plate we shipped before had non-orthogonality 84.08 with 41,700 faces over the
bar. This one has none.

## Two things to watch

**The main coupling is non-manifold.** `checkMesh` calls `fluid_to_solid` / `solid_to_fluid`
"multiply connected (shared edge)", with **87 bad edges** and **174 non-manifold points**, in both
the parallel and the serial pass. It still prints "Mesh OK", because patch topology is a warning
rather than a failure. 87 edges out of 21.7 million faces, but it is there and it is not a
decomposition artefact.

**The two ports are named but not assigned.** They are separate patches now, `port_a` and `port_b`,
so each can take its own boundary condition. They are not called inlet and outlet because Corintis
has not said which is which.

| patch | faces | mouth |
|---|---|---|
| port_a | 1,759 | x 89.34 mm |
| port_b | 1,706 | x 99.88 mm |

Checked geometrically rather than by face count: a 2 mm cut at x 89.34 returns only `port_a`, a cut
at x 99.88 only `port_b`, and a 20 mm cut between them sees both. So the faces went to the right
mouths.

## Which port is the inlet

Corintis' own CAD answers this and I had missed it. `ColdPlateAssembly_CLEAN.step` is an assembly of
six named solids, and two of them are called `Outlet:1` and `Inlet:1`:

| STEP solid | x range, mm | z centre, mm |
|---|---|---|
| Microfluidics_Core:1 | -8.577 .. 54.424 | 41.910 |
| Top_Lid:1 | -61.577 .. 115.924 | 41.910 |
| **Outlet:1** | **57.169 .. 89.829** | **75.868** |
| Splits:1 | -8.577 .. 54.424 | 41.910 |
| Board_cooling:1 | -61.577 .. 115.924 | 41.910 |
| **Inlet:1** | **67.669 .. 100.228** | **42.137** |

Measured off the delivered mesh, through the viewer rather than from any figure quoted earlier:

| mesh patch | x range, mm | z centre, mm | lands on |
|---|---|---|---|
| port_a | 88.960 .. 89.644 | 77.727 | **Outlet:1** |
| port_b | 99.697 .. 100.052 | 43.536 | **Inlet:1** |

The frames are the same frame: the plate's outer surface in the mesh runs x -61.555 .. 115.903 and
z -8.610 .. 92.429 against the STEP assembly's -61.577 .. 115.924 and -8.590 .. 92.410, agreeing to
0.022 mm, which is a ninth of one skin cell.

**The pairing is forced, not fitted.** port_b sits at x 99.697 .. 100.052, which is 9.868 mm wholly
outside `Outlet:1`'s x range, so it cannot be the outlet whatever anchor you measure from. The same
holds the other way: port_a at 88.960 .. 89.644 is 8.025 mm outside `Inlet:1`'s range. Neither
assignment is close to its alternative.

**So why are the patches still called port_a and port_b.** Because what I measured is a
correspondence to a named solid, not a direction of flow. Every statement of direction here comes
from Corintis: the two component names in a STEP exported 2025-08-29, and the red and blue hoses on
slide 3 of the alignment deck of 2026-10-28. The deck is a second human act and not a render of the
file - the STEP carries no red or blue, its only colour is `Copper - Raw` and both fittings are
"Steel - Satin" - but it is the same organisation, and it may simply be reading the same part names
back. Renaming would convert their unverified label into a boundary condition we assert, baked into
`polyMesh/boundary` where a swap made at authoring time would be permanent and invisible.

**One line from you settles it and I will rename them in minutes.** It is a label, not a remesh: no
face moves and no count changes.

Until then, do not assume `port_a` is the inlet because it is first. On your own CAD's naming it is
the outlet.

## Before you solve it

I retyped three patches that `splitMeshRegions` left pointing at the air region, which is not
delivered. Without this the mesh passes every check and will not run: a `mappedWall` resolves its
sample region through the object registry and fails to find `air`.

| patch | was | now |
|---|---|---|
| fluid_to_air | mappedWall, sampleRegion air | **patch** |
| solid_to_air | mappedWall, sampleRegion air | **wall** |
| chip_to_air | mappedWall, sampleRegion air | **wall** |

The ports become `patch` and not `wall` on purpose. A wall there seals the cold plate.

The originals are kept beside each boundary file as `boundary.mappedwall`, and the three corrected
files are published as `cbox2_boundary_retyped.tar.gz`, 3 KB, so an archive can be fixed without
unpacking 38 GB.

`regionProperties` groups them as `fluid (fluid)` and `solid (solid chip)`, which is what
`chtMultiRegionFoam` expects. The mesh ships reconstructed, so `decomposePar` it for a parallel run.

## Resolution, and why it stops here

22.0035 um in the microchannels is the floor with the current mesh image, and it is the tool rather
than the machine. OpenFOAM in that image is built with **32-bit labels** (`Arch: label=32`), and one
more refinement level overflows them in two different places: in parallel the octree's load balancer
computes a negative destination rank, and in serial the octree's node addressing asks for a negative
allocation. No rank count, buffer size, domain box or serial run gets past it.

**The `WM_LABEL_SIZE=64` fix this paragraph used to recommend is refuted.** Our own
`BUFFER_PROBE.txt` of 5 November, recorded at `DEVLOG.md:4649`, shows the parallel failure is
`MPI_ERR_BUFFER`, and `MPI_Buffer_attach` takes an `int` that `WM_LABEL_SIZE` cannot widen, so a
64-bit rebuild is a regression rather than an unlock. The floor is real; the stated cause and the
stated remedy were both wrong and no remedy is currently known.

It is not under-resolving anything on this part. The narrowest facing-wall gap anywhere on the
coolant surface is **99.03 um**, which gets 4.5 cells, and the coolant comes out as one connected
flow path in two independent meshes. An earlier run at 94 um cells split it into five pieces, so 22
um is doing real work.

## Where it all is

| | |
|---|---|
| the mesh, by region | `gs://dev-rehaan-artifacts-224734058693/corintis/farfield/cbox2_{fluid,solid,chip}.tar.gz` |
| the retyped boundary files | `.../cbox2_boundary_retyped.tar.gz` |
| the viewer sidecar, 2.1 GB | `.../cbox2_sidecar.tar.gz` |
| the run's own report and logs | `.../cbox2_RESULT.txt`, `cbox2_logs.tar.gz` |
| the plate on its own, for comparison | `.../plate_clean_{fluid,solid}.tar.gz` |
| to look at it | `http://localhost:8787/cbox2` |

Two numbers in the run's own `cbox2_RESULT.txt` are wrong and are corrected above: it heads a
section "=== cells" with 411,910,655, which is cfMesh's count before the boundary was added, and its
"=== integrity" section prints six `FOAM FATAL` lines and then captions them "nothing above = no
crash". Those fatals are `redistributePar` failing on two regions, which `reconstructParMesh`
recovered.
