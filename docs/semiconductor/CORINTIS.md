# Corintis: the account, their part, and what we still do not know

Every other document in this folder is about our mesh. This one is about them. Written because
their side of the engagement existed only scattered through a 4,758 line devlog, which is no use
to anyone picking this up.

Each fact below is marked by where it comes from. **(them)** is something they told us or that is
in a file they sent. **(measured)** is something we measured off their files ourselves.
**(published)** is from their own public work, found in research, not from them directly. The
distinction matters because we have twice treated our own number as theirs.

This is an internal engineering document, like everything else in this folder. Nothing here is
written to be sent anywhere.

## Who they are

EPFL spinout. They build branching microchannel cold plates for semiconductor cooling: the
coolant path is a topology-optimised tree rather than a set of parallel channels, so flow splits
and recombines many times across the plate. **(published)**

They design with a 2.5D depth-averaged two-layer FEM model on linear triangles, assuming a
parabolic out-of-plane profile. They say in their own methods that a full 3D model of the network
"would involve tens of millions of degrees of freedom, making the simulation computationally
expensive, and the optimization unfeasible". Published error for that class of model against full
3D is 7.27 to 24.8% on Nusselt and 12.6 to 16.3% on inlet velocity. **(published)**

That last paragraph is the commercial position in one line: they cannot currently simulate the
part they designed. We can.

## Their part

`PlateAndChip.step`, sent August 2025. 6 plate solids and 19 chip solids, pinned to our rebuilt
domain by a 0.005% volume match. **(them, then measured)**

| | | |
|---|---|---|
| plate extent in x | 177.500 mm | measured |
| channel width | 100.04 um, core CV 0.89% | measured |
| fin width | 101.25 um | measured |
| channel+fin pitch | 201.263 um, core CV 0.46% | measured |
| channel depth | 1499.9 um, constant | measured |
| named port solids | `Inlet:1` and `Outlet:1` | them |
| chip power map | every `powerDissipation` is zero | them |

Their published optimised designs carry channel widths from 90 to 371 um, so the uniform array we
measured is probably one region of a family, not the whole design space. **(published)**

Worth knowing: the array is uniform to half a percent. That is what makes the fix in
[MESH_QUANTISATION.md](MESH_QUANTISATION.md) free, and it may not hold on their next part.

## The six readings they asked for by name

This is the spec the work was built against, in their words. **(them)**

> the mesh count, quality metrics to know if the mesh is of good quality, number of boundary
> layers in the fluid, min and max cell sizes, cell types and count per type, highlight of
> poor-quality cells

| # | what they asked | where our answer is | state |
|---|---|---|---|
| 1 | mesh count | viewer and [COUPLED_MESH_DELIVERY.md](COUPLED_MESH_DELIVERY.md) | measured |
| 2 | quality metrics | [MESH_QUALITY_ASSESSMENT.md](MESH_QUALITY_ASSESSMENT.md) | measured, but see the bar below |
| 3 | boundary layers in the fluid | zero. **The reason we recorded was wrong, see below** | needs restating |
| 4 | min and max cell sizes | viewer | measured |
| 5 | cell types and count per type | viewer | measured |
| 6 | highlight of poor-quality cells | viewer, `POOR_CELLS` is its own part kind | measured, clickable |

All six render in a browser. Three of them had been measured on every run and displayed on none
until late October, because `render/mesh_facts` keeps an allow-list and a metric left off it never
reaches a reader however faithfully it was measured.

On reading 6: their plate had **28,132 poor cells, 27,932 of them in the metal**. A count without
a location says there is a problem without saying where, so the cells are written out as sets and
drawn flat and hot, at full resolution. **(measured)**

## Three things we have recorded that are wrong

**Reading 3, boundary layers.** We recorded zero, by construction, on the grounds that a prism
layer inserted on one side of a conformal interface leaves every face without its partner. **That
reasoning is refuted.** Measured on 6 November: `snappyHexMesh` run as a pure layer adder on the
split fluid region adds graded layers and the interface face count does not move, because layers
shrink the interior and insert cells rather than moving the extrusion base.
`MESH_QUALITY_ASSESSMENT` section 5 says "cfMesh cannot put them there", which is true about
cfMesh and the wrong conclusion to draw. Where reading 3 actually stands: zero in the mesh we
built, achievable on it without a remesh, and deliberately not done yet because doing it alone
makes the integrated heat number worse.

**The quality bar.** We have written "zero faces over 50 degrees out of 47.7M". Our own delivery
note puts the bar at 70, and our measured max is 51.46, so there is at least one face over 50. We
were overstating a number to ourselves. Separately, the shipped `meshQualityDict` has
`minVol 1e-13` against the 1.8e-15 a 22 um cell produces, so any `checkMesh -meshQuality` report
built with the shipped dict flagged all 47.7M faces as failing and is worthless.

**The 700M ceiling is Rehaan's, not theirs.** DEVLOG line 3982 says so plainly: "inside Rehaan's
700M ceiling". I have twice written it up as the customer's constraint, including in the brief I
gave the research sweep, which then produced a recommendation built on that premise. If Corintis
have never stated a cell ceiling, the premise was mine. **Worth Rehaan confirming, because it is
load-bearing in the sweep's conclusions.**

Our real ceiling is about 600 to 650M on this image regardless: at a measured 3.02 faces per cell,
2^31 faces is roughly 711M cells, so the 32-bit face label is the binding limit. **(measured)**

## What the earlier documents in this folder do not reflect

All three findings below postdate the delivery note and the quality assessment, so anything built
from those two alone is out of date.

- The channel quantisation defect. The grid rounds every 100 um channel to 88, 110 or 132 um, so
  two channels the CAD made identical come out exactly (5/4)^3 = 1.953x apart on flow. On a part whose purpose is
  controlling flow split, this is the finding that matters most.
- The measured error budget: chip-to-coolant dT about 1.4% low, Nusselt about 4% high, pressure
  drop about 10% low, staircase wetted area 7.8% high overall and 11.6% on the channels.
- That boundary layers are reachable on the mesh as built, and why they are not the first move.

## What is still unknown

Inputs the mesh and any solve depend on, which nothing in our files settles. Carried from
[MESH_PLAN.md](MESH_PLAN.md):

1. Conduction or conjugate.
2. Flow rate and coolant composition.
3. Which solver.
4. Where the chip bonds to the plate, and in what orientation.
5. Whether a chip power map exists. Every `powerDissipation` in the file is zero.

Added since:

6. **Which port is the inlet.** Their STEP names two solids `Inlet:1` and `Outlet:1`, and we
   established by exclusion that our `port_a` is at `Outlet:1` and `port_b` at `Inlet:1`. The
   patches are named `port_a` and `port_b` and were deliberately not renamed, because a solid
   called `Inlet` is a designer's label on a fitting and not a flow boundary condition, so
   renaming would turn their unverified label into one we assert. The mapping is recorded in the
   delivery note.
7. **Which quantity the mesh is for.** It changes which mesh is right. For junction temperature we
   are within about 1.4% today. For pressure drop we are about 10% low. For per-channel flow
   split, the quantisation defect dominates everything else.
8. **The thermal boundary condition.** The four candidates span Nusselt 2.907 to 7.214 for this
   aspect ratio, a factor of 2.5, which is larger than every mesh error above put together. Any
   Nusselt number recorded anywhere has to name its BC or it means nothing.

Their published work mentions a 150 mbar pressure drop constraint. **(published, not from them)**
A single straight 100.76 x 1499.9 um channel 55 mm long would need 1.94 bar at 1 m/s, so the real
per-channel path must be a few millimetres, which is consistent with a branching tree. Do not
record the 150 mbar as a requirement they gave us; we read it in a paper.

## Where everything is

| | |
|---|---|
| their CAD and the clean STL set | `~/clean_stl` on the laptop, and archived into `dev-rehaan` |
| the mesh we built, 413.27M cells | `cbox2`, archived; the live box is deleted |
| what makes the viewer work without the mesh | the boundary index sidecar, about 3 GB, `~/corintis_view/sidecar` |
| the newest surviving plate | `ws_shell`, 270.5M cells. The 328M plate does not exist |
| full history | [DEVLOG.md](DEVLOG.md), chronological, later entries correct earlier ones |

## Reading order for someone new

1. This document.
2. [COUPLED_MESH_DELIVERY.md](COUPLED_MESH_DELIVERY.md), what the mesh is and what was checked.
3. [MESH_QUALITY_ASSESSMENT.md](MESH_QUALITY_ASSESSMENT.md), how good it is. Note its section 5
   conclusion is marked superseded.
4. [MESH_QUANTISATION.md](MESH_QUANTISATION.md), the defect found afterwards, and the free fix.
5. The last entry of [DEVLOG.md](DEVLOG.md) for the plan that came out of all of it.
