# Corintis cold plate: the delivered mesh and its quality

2026-11-09. Every figure here is from `checkMesh` on the delivered mesh, or computed from the
delivered mesh and shown with its inputs. Nothing is estimated.

## What is in the set

| # | file | region | cells | size |
|---|---|---|---|---|
| 01 | `01_plate_solid.tar.gz` | the plate metal | 199,394,482 | 8.40 GB |
| 02 | `02_chip.tar.gz` | the chip, two bodies | 15,726,384 | 0.65 GB |
| 03 | `03_coolant_layered.tar.gz` | the coolant, with wall layers | 102,585,172 | 3.98 GB |
| | | **delivered total** | **317,706,038** | |
| 04 | `04_coolant_unlayered_spare.tar.gz` | the same coolant before layers, 55,303,441 cells | | 2.52 GB |
| 05 | `05_faces_in_error.csv` | the position of every face that fails a check | | 0.3 MB |
| 06 | `06_checkmesh_*.log` | the raw `checkMesh` output per region | | |

Each archive holds a decomposed OpenFOAM case, 8 processor directories. Untar them into **one**
directory and you get a single multi-region case:

```
processor0..7/constant/{solid,chip}/polyMesh      from 01 and 02
processor0..7/constant/polyMesh                   from 03, the coolant
```

File 04 is a spare, not part of the delivery: it is the coolant before the layer pass, for anyone
who wants to re-run layers with different settings.

## The headline

| | |
|---|---|
| cells delivered | 317,706,038 |
| hexahedra | 98.73% of the coolant, and **zero tetrahedra anywhere** |
| regions | 3, conformally coupled |
| conjugate interface | **16,619,777 faces, matching on both sides** |
| plate, chip, unlayered coolant | **every one of checkMesh's ten criteria at zero** |
| layered coolant | fails **one** check: 2,889 faces of 102.6M, 28.2 per million |
| mesh build time | 1.08 h |

## Region by region

### The plate metal, 199,394,482 cells

| | |
|---|---|
| non-orthogonality | max **50.5129°**, average 3.48983, bar 65 |
| skewness | max **1.64487**, bar 4 |
| aspect ratio | max **3.75705** |
| cell volumes | 1.42274e-14 to 2.15635e-10 m3, all positive |
| boundary openness | 3.27e-15 |
| regions | 1 |
| **faces failing any of the ten criteria** | **0** |
| verdict | `Mesh OK` |

### The chip, 15,726,384 cells

| | |
|---|---|
| non-orthogonality | max **0.00241416°** |
| skewness | max **4.10665e-05** |
| aspect ratio | max **1.00008** |
| cell volumes | 1.01902e-12 to 1.01907e-12 m3, a spread of 5e-05 |
| **faces failing any of the ten criteria** | **0** |
| verdict | `Mesh OK` |

Those numbers are not a mistake. The chip is a uniform lattice of 100.630 um cubes, so it is
essentially a perfect mesh. See the note on the chip below for what that costs.

### The coolant, 102,585,172 cells, with wall layers

| | |
|---|---|
| points | 113,102,392 |
| internal faces | 301,163,285 |
| faces per cell | 6.03574 |
| hexahedra | 101,282,927, **98.73%** |
| polyhedra | 1,302,245, 1.27% |
| tetrahedra, prisms, wedges, pyramids | **0** |
| regions | **1**, so one flow circuit |
| non-orthogonality | max **72.3231°**, average 7.11676, bar 65 |
| severely non-orthogonal, over 70° | **9 faces** |
| skewness | max **3.34868**, bar 4 |
| aspect ratio | max 302.1, which is the boundary layer and passes |
| cell volumes | 2.37195e-16 to 2.19804e-10 m3, all positive |
| boundary openness | 3.32e-17 |
| max cell openness | 4.27e-16 |

The ten criteria:

| criterion | faces failing |
|---|---|
| non-orthogonality > 65° | 133 |
| **interpolation weights < 0.02** | **2,588** |
| determinant < 0.001 | 95 |
| face twist < 0.02 | 69 |
| face-decomposition tet quality < 1e-30 | 4 |
| face pyramid volume < 1e-20 | 0 |
| concavity > 80° | 0 |
| skewness > 4 internal, 20 boundary | 0 |
| volume ratio of neighbours < 0.01 | 0 |
| **total** | **2,889 of 102,585,172 cells, 28.2 per million** |

`Failed 1 mesh checks`. This is the one defect in the delivery and it is characterised below.

## The couplings, which must match face for face

| | faces, side A | faces, side B |
|---|---|---|
| coolant to plate | `fluid_to_solid` **16,619,777** | `solid_to_fluid` **16,619,777** |
| chip to plate | `chip_to_solid` **270,816** | `solid_to_chip` **270,816** |

Counted by separate `checkMesh` runs on separate regions, so the agreement is not an artefact of
one count. Both are typed `mappedWall`, so heat crosses them directly rather than through an
interpolation. The whole coolant boundary reports `ok (closed singly connected)`.

### The two ports

| | faces |
|---|---|
| `port_a` | 1,331 |
| `port_b` | 1,308 |
| sum | 2,639 |

2,639 is exactly the coolant's former air contact, cut in two: nothing added, nothing lost. Both
are typed `patch`, so a boundary condition can be set on them.

**`port_a` is the OUTLET and `port_b` the INLET.** Their STEP names six solids, two of them
`Outlet:1` and `Inlet:1`, and the roles follow by exclusion. The patches ship unnamed deliberately,
because an exclusion argument is not strong enough to stamp a direction on a patch, and `port_a`
reading as the inlet would silently invert the whole solve. **Set these two yourself and check
them.**

## Geometry against the CAD

| | CAD | mesh | error |
|---|---|---|---|
| coolant volume | 53,692.2192 mm3 | 52,805.7576 mm3 | **-1.651%** |
| chip volume | 16,294.855 mm3 | 16,026.10 mm3 | **-1.649%** |
| plate volume | 222,672.1 mm3 | 228,057.0 mm3 | **+2.418%** |
| chip-to-plate contact area | 2,744.50 mm2 | 2,742.45 mm2 | **-0.07%** |

Computed by the divergence theorem over each closed boundary, the method checked against
`checkMesh`'s own cell-volume sum, which it reproduces to 0.00000%.

The coolant being 1.651% small is a mean inward wall offset of **14.67 um**, 0.583 of a background
cell, spread over 60,410.4513 mm2 of CAD surface. The coolant and the chip are small by the same
amount, which is what a lattice cutting both of them does; the plate picks up most of what they
lose because the three partition one box.

The contact area is the figure the thermal path actually depends on, and it is within half a cell.

## Resolution

| | |
|---|---|
| background cell | 402.525 um |
| finest cell | **25.1578 um**, four refinement levels down |
| microchannel width | 100.631 um, resolved by **4 cells** |
| cells, against uniform at the finest size | **58.7x fewer** |

The 4-cells-across choice is the one resolution decision worth knowing about, and its cost is
computed rather than asserted:

| | 5 cells | 4 cells, as delivered |
|---|---|---|
| fully developed Nusselt error | +0.535% | **+0.679%** |
| mean Nusselt over the 12.8 mm run, Re 100 | | **+1.41%** |
| Re 226 | | **+2.40%** |
| Re 1000 | | **+4.48%** |

Going to 5 cells costs +55.4% cells and 5.79 h against 1.08 h to buy about 0.03 points of mean
Nusselt. The two lattices are commensurate, `4 x 25.1578 = 5 x 20.12625` exactly, so both produce
the identical 100.631 um channel and the only difference is how many cells sit inside it.

## The wall layers

| | requested | achieved |
|---|---|---|
| layers | 3 | **2.84** average |
| overall stack thickness | 1.19e-05 m | **1.13e-05 m, 94.8%** |
| faces carrying a layer | 16,619,777 | 15,864,337, **95.455%** |
| layer cells | 49,859,331 | 47,281,731, **94.830%** |

95.455% is over the whole extrusion patch. On the surface that can physically take a layer it is
99.86%, and 77.6% of the shortfall sits outside the plate block.

## The one defect, located

2,588 of the 2,889 failing faces are interpolation weights below 0.02. They were measured, not
guessed: `05_faces_in_error.csv` carries the position of every one.

| | |
|---|---|
| distinct 2 mm locations | 393 |
| the busiest single location | 1.47% of them |
| **96% sit on two horizontal planes** | y = 23.0 mm and y = 24.0 mm |
| the thin cell at these faces | median **0.85 um**, minimum 0.15 um |
| its neighbour is larger by | median **85x**, up to 303x |
| the weight itself | 0.00495 to 0.01999, against a bar of 0.02 |

So each is a place where a layer cell was squeezed to well under a micron against a neighbour
dozens of times its size. They are spread over 393 locations rather than concentrated, and they
are confined to two planes near the bottom of the channel field.

**What it means for a solve.** An interpolation weight under 0.02 means a face value is dominated
by one of its two cells, which loses second-order accuracy on that face. It does not threaten
stability. 2,588 faces of 301 million internal faces is 8.6 per million.

**What was tried.** Seven layer configurations were run on the real plate, 45 minutes each,
changing only the layer dictionary. The relationship is a trade and not a bug: every setting that
reduces the bad angles increases the weight failures, and vice versa. The delivered mesh is the
configuration with the **lowest maximum non-orthogonality and the fewest severely non-orthogonal
faces** of any of them:

| | as first built | **as delivered** |
|---|---|---|
| max non-orthogonality | 76.6676° | **72.3231°** |
| severely non-orthogonal, over 70° | 2,096 | **9** |
| non-orthogonality > 65° | 2,348 | **133** |
| interpolation weights < 0.02 | 405 | 2,588 |
| total failing | 2,936 | **2,889** |

Severe non-orthogonality is the criterion that degrades gradient reconstruction and stalls
conjugate solves, and it is down by a factor of 233. The interpolation weights are the price. If
you would rather have the other trade, file 04 plus the layer dictionary reproduces either.

## Two things to know before you run it

**The chip is two separate bodies and only one of them is bonded.** `chip.stl` genuinely contains
two disjoint solids, 12,987.260 mm3 and 3,307.595 mm3, summing to the expected chip volume exactly.
Every one of the 270,816 `chip_to_solid` faces sits on a single plane, y = +0.02011, which is the
smaller body's top face. **The larger body, 78% of the chip volume, touches nothing but air**, and
the 70 um gap between the two bodies is air in this mesh.

If that gap is a bond line, the path carrying the die's heat into the plate is modelled here as
air. At 100.630 um a single cell cannot fit in a 70 um gap, so representing it needs the chip
refined. **This is a question about the CAD, not about the mesh**, and it is the first thing worth
settling.

**The air-facing surfaces are plain walls.** `solid_to_air` (1,213,523 faces) and `chip_to_air`
(1,492,860) are the outside of the part. They are typed `wall`, so choose an adiabatic or a
convective condition for them. They were briefly typed `mappedWall` against a region not in the
delivery, which would have stopped the case constructing; that is fixed and verified by
re-extracting the uploaded archives.

## How to check all of this yourself

```
mkdir case && cd case
tar xzf 01_plate_solid.tar.gz
tar xzf 02_chip.tar.gz
tar xzf 03_coolant_layered.tar.gz

mpirun -np 8 checkMesh -parallel -constant -region solid -meshQuality
mpirun -np 8 checkMesh -parallel -constant -region chip  -meshQuality
mpirun -np 8 checkMesh -parallel -constant -meshQuality          # the coolant
```

The three `06_checkmesh_*.log` files are those exact runs, so your output should match them line
for line. OpenFOAM 2412, `label=32 scalar=64`. The plate check needs about 200 GB of memory across
the eight ranks; the chip needs about 16 GB.

`05_faces_in_error.csv` has one row per failing face: the processor and face index, which criterion
it fails, the angle and the weight, the position, the two cell volumes, and the face area.
