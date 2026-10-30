# What I did by hand today that the engine should do for itself

2026-10-30. Everything below I ran manually to work out why two meshes of Corintis's plate would
not solve. None of it needed judgement. All of it is arithmetic the engine already has the inputs
for, and had it run any of the first three, it would have caught the fault before a five hour run
instead of after two of them.

## The one that would have saved both runs

**Count the connected pieces of the EXTRACTED COOLANT, before meshing.**

The plan already writes `expect_fluid_pieces: 2`. The report already counts the pieces in the
finished MESH and sets `ok: false` when they disagree. Nobody counts the pieces in the CAD
extraction, which is the input.

    extracted coolant       2 pieces     <- never checked
    plan expects            2 pieces
    mesh came out          13 pieces     <- checked, after 4.9 hours

The second of those two is a disconnected Outlet bore, 0.58 mm from the circuit with void between
them. That is a fault in the extraction and it is visible in under a second:

    con = surface.connectivity(); len(unique(con.cell_data["RegionId"]))

If the extraction's own piece count disagrees with the plan's expectation, the plan is being built
on a coolant that is not the coolant. Stop there.

## Make the geometry facts the engine already reads into something it reasons with

The reader gets all of this today and then drops it.

| it already has | it does not do |
|---|---|
| the six solids and their names from the STEP | order them into a stack and notice the layers |
| each solid's bbox | see that Core / Splits / Board / Lid are flush, 0 gaps |
| the fitting solids | check each port's bore reaches the circuit |

The whole outlet diagnosis was: read the names, sort the solids by their thickness axis, see which
solid the orphan piece sits inside, and test whether the gap between it and the circuit is metal or
void. Four steps, all of them arithmetic over data the reader already produced.

The void test is the one worth writing down, because it tells extraction misses apart from design:

    midpoint of the gap inside the solid  ->  a real wall, the spaces are meant to be separate
    midpoint of the gap NOT in any solid  ->  an extraction miss, and the flow path is real

## Stop describing the part with one number

`narrowest_m` is a single scalar for the whole part, and both failed plans sized every cell in the
mesh off it. On a part with two scales 27x apart that number cannot be right for both:

    narrowest_m used          2.652 mm    (the stale pre-fix reading)
    narrowest passage really  0.098 mm    (the microfluidic core)
    manifold passage          2.112 mm

What the planner needs is a feature-size FIELD, not a scalar, so refinement can go where the fine
features are. The cheapest honest version costs nothing extra: a CAD tessellator already puts small
triangles on small features, so the triangle edge length at each point IS a local feature size.
Taking the finest decile of the coolant surface gave a box within a millimetre of the
Microfluidics_Core's own footprint, by two independent routes.

That field is also what lets the surface be split into named regions with their own cell sizes,
which is the whole fix. A planner that can only say one cell size cannot express the right mesh
for this part.

## Verify the fix you already shipped is in the path that runs

The sizing fix that makes `narrowest_passage_m` read 0.0980 mm instead of 2.6525 mm exists. Both
of the plans that failed carry 2.652 mm. Either those plans predate it or the fixed reading is not
reaching the planner. Worth a direct check rather than an assumption - a fix that is not on the
path that runs is not a fix.

## Smaller ones, same morning

- **Gate on connectivity, do not just record it.** `ok: false` with the reason written out is
  correct and it did not stop anything, because the stages can be driven round it. A delivery that
  would not solve should not be deliverable.
- **Report cells across the narrowest passage** as a delivered reading. It is the number that
  decides whether a passage survives meshing, and it is one division.
- **`write_index` had no caller for a day.** Separately recorded, but the same shape of mistake:
  code that is correct, tested, measured, and never reached by the product.

## What this adds up to

Three checks - pieces in the extraction, each port's bore connected, cells across the narrowest
passage - would have turned nine hours of meshing into two failures caught in seconds. None of
them needs a new measurement. They need the engine to look at what it has already measured.
