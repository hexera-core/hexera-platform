# Plan for the true mesh run, Corintis cold plate

Draft, 2026-10-30. Nothing runs until this is agreed.

## What has gone wrong twice

| run | cells | checkMesh | coolant pieces | expected |
|---|---|---|---|---|
| ws_par300 | 336,666,582 | Mesh OK both | 5 | 2 |
| ws_par247 | 182,269,914 | Mesh OK both | **13** | 2 |

Both passed every quality bar and neither would solve. checkMesh does not check connectivity, so
a mesh that cannot run passes everything the mesher asks of it.

Worth saying plainly: the engine's own report caught this. ws_par300's report says `ok: false`
with the exact problem written out. I drove the stages by hand and went round the gate. The engine
was right and I was not listening to it.

## Two separate faults, found by measuring their CAD

**Fault 1, the geometry: the outlet is orphaned.**

The stack, measured off their STEP:

| solid | y (thickness), mm |
|---|---|
| Microfluidics_Core | 20.110 .. 23.110 |
| Splits (the I/O Splitter) | 23.110 .. 24.110 |
| Board_cooling | 24.110 .. 33.446 |
| Top_Lid | 33.446 .. 40.446 |
| Inlet and Outlet fittings | 33.946 .. 61.946 |

The extracted coolant is 2 pieces. The small one is 29.47 x 24.73 x 11.69 mm at x 60.21..89.68,
z 69.68..81.37, which is the **Outlet** fitting's footprint. The **Inlet's** bore is already part
of the main circuit, so the two sides are not being treated alike.

    outlet bore bottom   y = 33.946
    board circuit top    y = 33.446
    gap                      0.5797 mm   at (60.25, 33.70, 76.67) mm

That gap is NOT metal. The midpoint is inside Top_Lid's bounding box and outside the Top_Lid
solid, so it is a real void and a real flow path. The extraction missed it on the outlet side and
caught the identical thing on the inlet side, which is the control.

Not a design problem and not a question for Corintis. An extraction miss at one known coordinate.

**Fault 2, the resolution: the slots pinch shut.**

Both plans sized cells off `narrowest_m` = 2.652 mm, the stale pre-fix reading. The real narrowest
passage is 0.098 mm, in the microfluidic core. Cells came out at 69.8 um at the wall, which cannot
hold a 98 um slot open, so the slots close and the circuit breaks into pieces.

## Why the obvious fix is wrong

A refinement BOX over the fine region. Measured, the box is 59.0 x 11.8 x 50.8 mm, and filled with
24.5 um cells that is **3,715 M cells** - because the box is 98% copper fin and 2% coolant.
Refining a volume is the wrong instrument for a surface feature.

## The plan

**Split the coolant surface into two named regions by feature size and give each its own cell
size.** A CAD tessellator puts small triangles on small features, so triangle size reads local
feature size directly. No guessing which part of the part is which.

| region | what it is | cell size | why |
|---|---|---|---|
| `coolant_fine` | the microfluidic core's slots | 24.5 um | 4 across a 98 um slot |
| `coolant_coarse` | the serpentine and the manifolds | 250 um | 8 across a 2.11 mm passage |
| background | everything else | 500 um | the block |

The fine region, from the finest decile of triangles (edge <= 0.1874 mm, 5,237 of 52,334), with
2 mm of margin:

    x  -8.57 ..  54.42 mm
    y  19.61 ..  35.45 mm
    z  15.14 ..  69.91 mm

which is the Microfluidics_Core's own footprint (x -8.58..54.42, z 14.41..69.41), measured
independently. Two different methods agreeing is the reason to trust it.

## What it should cost

Fine wall area is 19,984 mm2 of the coolant's 60,588. A band 3 cells either side at 24.5 um is
2,938 mm3:

| | volume | cell | cells |
|---|---|---|---|
| band around the fine walls | 2,938 mm3 | 24.5 um | 200 M |
| everything else | 557,426 mm3 | 250 um | 36 M |
| | | | **~236 M** |

Inside the 100 M to 700 M band. At the 715 B/cell the 336 M run actually used that is 169 GB
against mesh-bench's 251 GB, comfortable. About 4 hours, by the 336 M run's 4.94 h.

## Order of work

0. **Re-extract the coolant so the outlet bore joins the circuit.** Verify with the same
   connectivity test: the CAD coolant must come back as **1 piece**. Seconds to check, and it is
   the proof. Nothing else starts until this passes.
1. Split the coolant surface into the two named regions.
2. Write the meshDict with per-region cell sizes.
3. Mesh.
4. Gates below.

## Gates, in this order

Nothing proceeds past a failed gate.

1. **coolant pieces == 2** - the thing that has failed twice, checked first and alone
2. checkMesh `Mesh OK` on both regions
3. interface faces equal on both sides
4. cells across the narrowest passage >= 4, measured not assumed
5. the six readings Corintis asked for, all present

## Known limits, to state in the delivery rather than have them found

- **4 cells across the 98 um slots is the low end.** It fixes connectivity and is coarse for heat
  transfer accuracy. 8 across is 1.7 B cells and out of budget. If they need accuracy in the core
  that wants a periodic unit cell of the core at high resolution, as a second mesh.
- **Boundary layers: 0.** Being investigated separately. cfMesh cannot extrude layers onto an
  interface that is internal at mesh time, which is what split-after-meshing makes it. Corintis
  asked for the layer count by name, so a zero needs either a route to a non-zero or an argument
  good enough to put in writing.

## Open, needs a decision

- the deliverable: the plate alone, or plate + chip as three regions
- whether to also build the high-resolution unit cell
- whether the mesh ships with a demonstration solve on assumed conditions, clearly labelled
