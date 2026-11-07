# How good is cbox2b, the layered coupled mesh

Measured 7 November 2026 on `cbox2b`, on the five STLs it was built from, and on the layered fluid
region `bl3p`. Every number is read off a run that finished or computed from the mesh itself. Where
a figure corrects something I published earlier, including earlier today, it says so and gives both.

The quality figures come from per-face sidecars, not from `checkMesh`'s summary lines. `checkMesh`
reports maxima, and a maximum over 25.7M faces cannot tell one bad face from one in a thousand.

## Verdict

| dimension | grade | the number behind it |
|---|---|---|
| conditioning | **great** | 0 faces in error on all 9 criteria, all three regions, before layers |
| conjugate interface | **great** | 25,685,941 faces matched one to one, max centroid gap 0.00085 cells |
| connectivity | **fine** | both ports in the same piece, proved; the 2 stray cells are 0.0000024% |
| channel fidelity | **okayish** | median width +0.18% on CAD, but quantisation passes 10.1% too much flow and doubles the off-nominal share |
| resolution for CHT | **okayish** | 5 cells across a channel; flow +8.0%, wall shear exact, Nusselt +0.54% developed. Local error up to 43-90% in the thermal entrance, see section 7 |
| layers | **a defect, not a feature** | they moved Nusselt from +2.2% to **-4.0%** |
| cell budget | **poor** | the solid is 74% of the mesh and carries almost no physics |

## 1. What the mesh is

| region | cells | faces | points | pieces |
|---|---|---|---|---|
| fluid | 82,529,515 | 263,003,681 | 98,268,345 | 3, see section 4 |
| solid | 327,126,204 | 1,002,231,588 | 347,702,143 | 1 |
| chip | 31,012,960 | 94,416,838 | 32,394,165 | 2, by design |
| air | dropped | | | |
| **live total** | **440,668,679** | | | |

`cbox2b` as built was 504,715,948 cells, so the far field air region was 64,047,269 of them, 12.7%.

Patches, which are the whole boundary condition surface the solver will see:

| region | patch | faces |
|---|---|---|
| fluid | `fluid_to_solid` | 25,685,941 |
| fluid | `port_a` | 2,092 |
| fluid | `port_b` | 2,048 |
| solid | `solid_to_fluid` | 25,685,941 |
| solid | `solid_to_chip` | 424,080 |
| solid | `solid_to_air` | 1,904,636 |
| chip | `chip_to_solid` | 424,080 |
| chip | `chip_to_air` | 2,331,836 |

The coolant has no `fluid_to_air` patch. It is sealed except for the two ports, which is what it
should be and is worth stating because it is the one topological property that would silently void
a flow solve.

Background cell 20.1263 um in x and z, 60.379 um in y. The y cell is 3x coarser because the mesh was
built on geometry scaled down 3x in y and then scaled back, which is the `scaleY 3` setting that took
the mesher from 38.5 hours to 17 minutes. The aspect ratio reads back as exactly 3.000, which is the
confirmation that the back-scale did what it was supposed to.

## 2. Conditioning

Per boundary face, all three regions, before layers:

| region | non-ortho p50 | p99 | p99.99 | max | skew max | aspect mean | aspect max | faces > 65 deg |
|---|---|---|---|---|---|---|---|---|
| fluid | 0.000 | 2.646 | 45.805 | 49.451 | 1.657 | 3.000 | 3.660 | **0** |
| solid | 0.000 | 1.930 | 45.564 | 48.285 | 1.654 | 2.887 | 3.660 | **0** |
| chip | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.001 | **0** |

`Mesh OK` on all three, and 0 faces in error on every one of the nine criteria. The chip region is
exactly zero on every measure because it is an unrefined block of uniform hexahedra.

The median non-orthogonality is 0.000 on the fluid and the solid. That is not a rounding artefact:
the overwhelming majority of faces on a split octree mesh are perfectly orthogonal, and the
distribution is a spike at zero with a thin tail. The tail is what matters, and p99.99 at 45.8
degrees against a 65 degree bar means it never gets close.

Where the tail sits matters more than its height. The worst 1% of fluid interface faces cluster in
y 24 to 34 mm, on a patch that spans y 21.6 to 58.6 mm, and the worst 25 faces sit in a single spot
at y 33.32 to 33.47 mm, z 65.4 to 66.4 mm. So the bad faces are localised rather than spread through
the channel array, and they are localised away from the chip footprint.

## 3. The conjugate interface

| | |
|---|---|
| `fluid_to_solid` faces | 25,685,941 |
| `solid_to_fluid` faces | 25,685,941 |
| matched | 25,685,941, **100.000%**, one to one |
| max centroid gap | 1.707e-8 m = **0.00085 cells** |
| mesh wetted area | 65,411.2219 mm2 |

The interface is exactly conformal. This took two attempts to establish. The first matched centroids
by sorting on coordinates quantised to 1 nm, but the centroids are float32, which at coordinates of
order 0.1 m resolves to about 6 nm, so the sort keys sat below the precision of the data and
identical faces sorted into different orders. That produced a reported 8,652,419 gaps over 1 nm,
which measured my own method and nothing about the mesh. Replacing it with a spatial hash at half a
cell, probing all 27 neighbouring buckets, gives the result above. **Discard the 8.6M figure.**

## 4. Connectivity, and why the warnings are cosmetic

`checkMesh` reports the fluid region in 3 pieces and the chip in 2. Neither is a defect, and I
proved both rather than arguing them.

The fluid pieces are 82,529,513 cells, 1 cell and 1 cell. The question that matters for a cold plate
is whether the inlet and the outlet are in the same piece, because if they are not then nothing can
flow between them. Taking the owner cell of every port face and intersecting with each piece:

| set | cells | in piece 0 | in piece 1 | in piece 2 |
|---|---|---|---|---|
| `port_a` owners | 1,924 | **1,924** | 0 | 0 |
| `port_b` owners | 1,927 | **1,927** | 0 | 0 |

Both ports are wholly inside piece 0. The two strays are 0.0000024% of the region and touch no port.

The chip's 2 pieces are 6,388,480 and 24,624,480 cells, and `chip.stl` is itself two closed shells.
The chip is two separate bodies in the customer's CAD, so the mesh is right and there is nothing to
fix.

## 5. Geometric fidelity

The five surfaces the mesh was built from:

| surface | triangles | closed | pieces | area mm2 | volume mm3 |
|---|---|---|---|---|---|
| `fluid.stl` | 52,466 | yes | 1 | 60,410.4513 | 53,692.2192 |
| `chip.stl` | 96 | yes | 2 | 17,863.5581 | 16,294.8547 |
| `block.stl` | 27,696 | yes | 1 | 47,955.4164 | 276,364.2873 |
| `fluid_fine.stl` | 32,453 | **no, 111 open edges** | 1 | 21,712.6296 | meaningless |

`fluid.stl` being closed and in one piece confirms `cbox2b` was built on the repaired coolant
surface, not the two-piece one that was missing 132 triangles. `fluid_fine.stl` is open, but it is
only a refinement region, so openness costs accuracy in where refinement lands and not in the
geometry itself. It should still be repaired.

| | CAD | mesh | error |
|---|---|---|---|
| wetted area | 60,410.4513 mm2 | 65,411.2219 mm2 | **+8.28%** |
| coolant volume | 53,692.2192 mm3 | 55,102.6 mm3 | **+2.63%** |

The area excess is the staircase: a stepped surface is longer than the smooth one it approximates,
and +8.28% on a 20 um cell against 100 um features is the expected size.

The volume excess is a defect I cannot yet explain, but I can now say where it is **not**. Spread
over the wetted area, +1,410.4 mm3 implies a mean outward offset of 23.3 um, about 1.16 cells. A
uniform one-cell outward move on both walls would make a 100 um channel 40% wider, and the channels
measure +0.18%. So the excess volume is not in the microchannels. It is in the plenums, headers and
distribution passages, which are large enough to absorb it without a measurable width error.

### Channel width, measured the same way on both sides

Earlier figures for this were computed against an assumed 100.000 um nominal, which is not what the
part is built to. These come from casting rays along the pitch axis through `fluid.stl` and reading
coolant runs off the surface crossings, which is the identical definition used on the mesh, with the
identical filter discarding any scanline that does not cross the array at least 20 times.

| | CAD | mesh | error |
|---|---|---|---|
| channel p5 | 98.295 um | 100.598 um | +2.34% |
| channel **p50** | **100.446 um** | **100.630 um** | **+0.18%** |
| channel p95 | 121.487 um | 140.900 um | +15.98% |
| channel CV | 12.38% | 18.94% | |
| fin p50 | 101.255 um | 100.630 um | -0.62% |
| **pitch** | **201.2625 um** | **201.263 um** | **-0.0002%** |

The pitch is worth dwelling on because `maxCellSize 322.020` was chosen to make ten cells equal it,
and it did: the CAD pitch measured wall-to-wall over two steps is 201.26253 um, which is 9.999977
cells. Core n=103,980, mean 201.403 um, sd 0.771 um, CV 0.383%. **The mesh pitch is right to two
parts in a million.** An earlier line in this document gave the CAD pitch as 201.702 um and the mesh
as 0.22% short; that came from adding the coolant median to the fin median, which is not a pitch
because it pairs quantities by rank rather than by position. Discard it.

The mesh population is purely discrete, because the interface sits on octree cell faces and a
channel has to be a whole number of cells. **So is the part's**, once it is binned the same way, and
putting the two side by side is the only comparison that separates what the mesh invents from what it
inherits:

| width | cells | CAD share | mesh share | CAD mean width | flow vs nominal |
|---|---|---|---|---|---|
| 80.5 um | 4 | **0.00%** | **1.85%** | nothing there | 0.51x |
| **100.6 um** | **5** | **90.87%** | **79.91%** | 100.495 | **1.00x** |
| 120.8 um | 6 | 6.77% | 10.79% | 119.335 | 1.73x |
| 140.9 um | 7 | 1.29% | 3.17% | 143.035 | 2.74x |
| 161.0 um | 8 | 0.45% | 1.45% | 158.756 | 4.10x |
| 181.1 um | 9 | **0.00%** | 0.81% | nothing there | 5.83x |
| 201.3 um | 10 | 0.11% | 0.00% | 205.055 | 8.00x |
| 221.4 um | 11 | 0.45% | 0.51% | 220.436 | 10.65x |

The part has a real tail. 6.77% of its channels are genuinely near 119 um, which are the wider
distribution channels, so the mesh is not inventing them. But it does not reproduce them faithfully
either:

| | CAD | mesh |
|---|---|---|
| on the 5-cell nominal | **90.87%** | **79.91%** |
| off nominal | 9.13% | **18.86%** |
| conductance, mean w^3 against a 5-cell channel | 1.1467 | **1.2620** |
| conductance CV | 86.94% | 75.46% |

**The mesh doubles the off-nominal share and passes 10.1% too much flow.** That net bias is the
figure that matters, and it is separate from and larger than the per-channel scatter. It is on top of
the +8.0% discretisation error in section 6, so the two compound to about +19%.

Per-channel, the error is the forced snapping within each bin:

| cells | CAD range forced onto one width | mesh width | width error p5 / p50 / p95 |
|---|---|---|---|
| 5 | 97.963 to 110.533 um | 100.632 um | -4.05% / +0.19% / +2.49% |
| 6 | 110.734 to 130.787 um | 120.758 um | -6.21% / +0.76% / +8.54% |
| 7 | 130.838 to 150.736 um | 140.884 um | -6.08% / -3.98% / +7.68% |

So an individual channel is wrong by -6.2% to +8.5% on width, which is -17% to +28% on the flow it
passes. That is the channel-to-channel maldistribution the solver will see.

Against the delivered `cbox2` mesh, which put 36.1% of channels at 88.014 um, 52.7% at 110.017 um
and 6.4% at 132.021 um, with **nothing** at the nominal width, this is still a large improvement.
79.91% now sits on nominal and 88 um does not appear anywhere in the new distribution.

Two defects remain, and neither is the one I expected:

- **The mesh invents narrow channels the part does not have.** The CAD has nothing at 4 cells at all,
  and its narrowest in-window run is 97.963 um. The mesh puts 1.85% of its channels at 80.5 um, a
  full cell narrower than anything in the part, which is a -20% width and -49% flow error on those
  channels. That is worse per channel than anything on the wide side.
- **Neither the pitch nor the grid origin can fix the tail.** The pitch is already exact to two parts
  in a million, so there is no drift to remove. And the phase of channel walls within a cell is
  **uniform**: binned into 20 bins across the 20.1263 um cell, the counts run 290 to 482 about a mean
  of 370, with no peak anywhere. A flat phase distribution means no single translation of the octree
  can put walls on cell faces; shifting the origin only permutes which channels land on 5 cells and
  which on 6, leaving the off-nominal fraction unchanged.

So the mechanism, finally, is this: **the part's own channel width spread straddles the cell-count
boundaries, and with uniform wall phase every channel near a boundary goes either way.** The CAD's
5-cell bin runs 97.963 to 110.533 um while 5 cells is 100.632 um, so a substantial part of that bin
sits above the boundary and rounds up. The part's 12.38% width CV against a 20.1263 um quantum is the
whole story, and the only lever that touches it is a smaller quantum.

That leaves exactly one real fix: one more refinement level in the channels, which halves the quantum
relative to the width spread. The 32-bit label wall blocks it until there is a `WM_LABEL_SIZE=64`
image.

## 6. What the resolution costs

The channel is 100.630 um wide by 1509.0 um tall, aspect ratio 15.00, Dh 188.678 um, sitting on
967.6 um of base metal. Width carries 5 cells, height carries 25. At 15:1 the velocity profile is
essentially one dimensional across the short dimension, so 5 cells is the governing resolution.

These are solved, not estimated. The same discretisation OpenFOAM uses, cell-centred finite volume
with a one-sided wall gradient over the half cell, was solved on the real cell distribution and
compared against the analytic result. The method reproduces the textbook parallel-plate Nusselt
number of 8.2353 to 0.002% at 200 cells, which is what licenses the rest of the table.

| grid across the channel | flow rate | wall shear | Nusselt |
|---|---|---|---|
| 5 uniform cells, as shipped | **+8.00%** | **exact** | **+0.54%** (was +2.18%, corrected in section 7) |
| plus layers as built, 16.7 um | +5.28% | exact | **-1.41%** (was -4.02%) |
| plus layers with no sliver, 3 graded cells | +5.18% | exact | -0.73% (was -4.41%) |
| 10 uniform cells | +2.00% | exact | +0.67% |
| 20 uniform cells | +0.50% | exact | +0.17% |
| 200 uniform cells, reference | +0.005% | exact | +0.002% |

Three results, in order of how much they change what we do.

**Wall shear is exact at every resolution.** The discrete solution of fully developed flow is the
exact parabola plus a constant offset, and the offset cancels in the one-sided wall gradient. This
also disposes of a claim in my own earlier notes that the mesh ran "9% low on Nusselt". That was
wrong in sign and in subject: the error is not in the wall gradient at all, and where it does land
it is positive.

**Flow rate is over-predicted by exactly 2/N squared**, so +8.0% at 5 cells, and the fit is exact at
every N tested. It is identical in every channel, so it shifts the total hydraulic resistance and
does not redistribute flow between channels. The channel-to-channel maldistribution is the 2.75x
conductance spread in section 5, which is a separate and larger effect.

**Nusselt is over-predicted by 0.54% without layers, in the fully developed limit.** (This read 2.18% and was wrong; **CORRECTED, see MESH_QUALITY_CBOX2B.md section 7.** The developing-flow local error is far larger, 43 to 90% in the thermal entrance.) Which is small, and better than the layered
mesh manages.

## 7. The layered mesh, and the Nusselt numbers I got wrong

**This section replaces an earlier version that reported the 5-cell mesh at +2.18% and the layered
mesh at -4.02%, a 6.2 point gap, and concluded from it that the layers are a defect. Both figures
are wrong, the gap is 1.27 points, and the conclusion reverses on every local metric. The reasoning
behind it was a correct result for momentum applied to energy, where it does not hold.**

### What was wrong, and why it looked right

The old argument: for fully developed laminar duct flow the cell-centred one-sided wall gradient is
exact at any resolution, because the discrete solution is the exact parabola plus a constant offset
and the offset cancels in the gradient. Therefore there is no wall gradient error for a boundary
layer to fix, and the layer stack can only take cells out of the core that the bulk temperature
needs.

**That is exactly true for momentum and false for energy.** Momentum has a uniform source, the
constant pressure gradient, so its exact solution is a parabola, and a second-order finite volume
scheme reproduces a parabola's cell averages exactly. Nothing is left but the flow-rate quadrature,
which is why the flow error is *exactly* +2/N squared with no fitted constant. The energy equation's
source is `rho cp u(y) dT/dz`, which is itself a parabola, so the exact temperature profile is a
**quartic**. A second-order scheme does not reproduce a quartic exactly, the first cell's value
carries a real error, and the one-sided wall gradient inherits it.

Measured. The discrete first-cell value against the exact profile's average over that same cell,
both extrapolated to the wall the same way:

| N | deviation of the discrete first cell |
|---|---|
| 5 | **-2.0172%** |
| 10 | -0.3627% |
| 20 | -0.0687% |

Not zero, and converging away at better than second order. So the wall gradient was never exact in
the energy problem, and the sentence that carried the old conclusion does not apply to it.

### The corrected figures

Solved in closed form rather than marched. The semi-discrete system is `M dT/dz = -D T + b`, so
expanding in the eigenvectors of `D phi = lambda M phi` gives the whole streamwise behaviour exactly
and leaves no streamwise discretisation error to argue about. Fully developed limit:

| grid across the 100.6315 um channel | Nu | error |
|---|---|---|
| 400 uniform, the reference | 8.23530 | 0.00% |
| **5 uniform cells, as shipped** | 8.27938 | **+0.54%** |
| 3 layers per wall, nominal 21.9925 um | 8.17536 | **-0.73%** |
| 3 layers, the 16.7 um actually achieved | 8.11941 | **-1.41%** |
| 2 layers per wall | 8.10862 | -1.54% |

Three checks, because this overturns a published number and one solver's word is not enough:

| check | result |
|---|---|
| Nu grid convergence, N = 5 to 1280 | observed order **2.0003**, error x N squared to **0.17647 = 3/17**, extrapolating to **140/17** |
| flow rate against the record's exactly +2/N squared | ratio **1.000000** at N = 5, 10, 20, 40, 80 |
| one wall heated, other adiabatic | **140/26** to four decimals |
| closed form against a brute-force march | agree to **0.000%** |

The flow check is the important one. It shares every piece of machinery with the energy solve except
the boundary condition, and it reproduces the record's own figure exactly, which puts the fault in
the energy boundary treatment of the old solver and nowhere else. A clean second-order sequence
converging to 3/17 cannot pass through 0.545, which is what +2.18% at N=5 would require.

**What survives unchanged:** flow rate +8.00% at 5 cells, and wall shear exact at any resolution.
Both are momentum results and both are confirmed above. Only the Nusselt figures move.

### The layer stack that actually exists

| | layers | overall thickness |
|---|---|---|
| requested by the dict | 3 | 2.2e-05 m |
| **achieved over all 25,685,941 faces** | **2.36** | **1.67e-05 m, 75.9% of target** |

Coverage was 90.71%, so **9.29% of the conjugate interface has no layers at all.** That matters more
than the average: on those faces the mesh is the plain 5-cell mesh, with the plain mesh's errors. A
90.71% covered layer mesh does not remove the worst-case local error, it removes it from 90.71% of
the wall. The `lay3 achieved` row above models the covered part and is not the patch as a whole.

### Developing flow, which the old section only reasoned about

The old section said the conclusion holds while the flow is thermally developed and that layers
start earning their cost above Re 226. That was an estimate from an entrance-length formula, not a
solution. Solving the Graetz problem on the same grids, fully developed velocity with developing
temperature, which is the right model at Pr 6 because the thermal entrance length is Pr times the
hydrodynamic one:

| Re | x*_exit | mesh mean | mesh worst local | lay3 ach mean | lay3 ach worst local |
|---|---|---|---|---|---|
| 10 | 1.0600 | +0.59% | **42.9%** | -1.41% | 2.1% |
| 100 | 0.1060 | +1.08% | **72.9%** | -1.39% | 6.5% |
| 226 | 0.0469 | +1.82% | **79.3%** | -1.37% | 6.5% |
| 1000 | 0.0106 | +4.67% | **87.4%** | -1.60% | 31.4% |
| 2000 | 0.0053 | +4.57% | **90.0%** | -1.88% | 44.3% |

And the crossover depends entirely on which metric is asked for:

| metric | crossover, plain mesh against the stack as built |
|---|---|
| mean Nusselt over the 12.8 mm run | **Re 152** |
| length-weighted RMS local error | **Re 23** |
| **worst local error** | **none anywhere in 1 to 1e6; layers win at every Reynolds number** |

Mean Nusselt is a generous metric for a coarse mesh, because a coarse mesh under-predicts Nusselt in
the entrance and over-predicts it downstream and the two cancel. At Re 1000 the plain mesh is -54.1%
at 0.05 mm and +8.5% at 3 mm, and those average to +4.7%. A cold plate with chip hot spots does not
average them: it feels the local wall temperature, and the entrance is where the coolant is coldest
and the flux highest.

### So what the layers actually are

Not a defect. A trade worth 5,843 s:

- **On mean Nusselt the plain mesh is adequate below Re 152** and better than any layered variant,
  though by 0.1 to 0.8 points rather than the 6.2 I published.
- **On any local measure the layered mesh is better at every Reynolds number tested**, and the gap
  widens with Re: 42.9% against 2.1% at Re 10, and 90.0% against 44.3% at Re 2000.
- **The time case for dropping them is untouched.** They cost 5,843 s and sub-one-hour is only
  reachable without them. That is a real engineering trade now, not a free win.
- **Fixing coverage is worth more than adding layers.** 9.29% of the interface carries the plain
  mesh's 43 to 90% local error no matter how good the other 90.71% is, so the worst-case number for
  the patch as a whole does not improve until that fraction does.

The customer's flow rate still matters, and the threshold is Re 152 rather than 226. But it no
longer decides the question on its own, because the metric decides it first.

## 8. What I corrected while doing this

| claim | status | replacement |
|---|---|---|
| 8,652,419 interface gaps over 1 nm | **wrong, my method** | 100% matched, max gap 1.707e-8 m |
| channel width +0.63% | **wrong, assumed a 100.000 um part** | +0.18% on the CAD median of 100.446 um |
| channel width -0.074% | **wrong, used the wall-spacing fundamental as the width** | as above |
| mesh runs 9% low on Nusselt | **wrong in sign and subject** | +0.54% high without layers developed, wall shear exact. My own +2.18% replacement was also wrong, see section 7 |
| the dead air region causes the `-allGeometry` fatal | **refuted** | `chip_to_solid` being a `mappedWall` whose `sampleRegion` is not loaded |
| 55.8% flow CV inside the array | **measurement artefact** | plenum crossings; 18.94% with the array filter |
| the mesh adds 1.45x of conductance spread, 1.89x to 2.75x | **wrong comparison, mixed bins** | the part has its own tail; the mesh doubles the off-nominal share from 9.13% to 18.86% and passes 10.1% too much flow |
| the mesh rounds sub-100 um channels up and passes 8% too much | **wrong direction** | it rounds 1.85% of them DOWN to 80.5 um, a width the part does not contain at all |
| wetted area +8.29% | superseded | +8.28%, against a surface that is actually closed |

## 9. The `-allGeometry` gap, closed

`checkMesh -allGeometry` has failed on every conjugate mesh we have ever built, which means
`mesh_ok` has never once been true and a whole class of geometry checks has never run. Two arms on a
copy of the chip region:

| arm | change | result |
|---|---|---|
| A | nothing | rc=1 after 610 s, FOAM fatal |
| B | `chip_to_solid` retyped `mappedWall` to `wall` | **rc=0 after 624 s, `Mesh OK`** |

Arm B passes every check the flag adds: patch topology ok on both patches, face interpolation weight
check OK, face volume ratio minimum 0.998759 and average 0.999859. The cause is that checking one
region alone cannot resolve a `mappedWall` patch's `sampleRegion`, which is not loaded. The cost of
the fix is 2.3%. This should go into the build.

## 10. What is still open

- **The operating flow rate and fluid.** Section 7 cannot be resolved without it, and every error
  estimate in section 6 scales with it.
- **The +2.63% coolant volume.** Narrowed to the plenums and headers, not explained.
- **The phase mechanism** behind 10.79% of channels landing one cell wide.
- **Interior cell quality.** Everything in section 2 is the boundary ring. The interior has never
  been measured directly.
- **A grid convergence study**, which needs a `WM_LABEL_SIZE=64` image to go finer than 5 cells.
- **`fluid_fine.stl`** is open on 111 edges and should be repaired.

## Where the artefacts are

| | |
|---|---|
| `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/cbox2b/` | `fluid.tar.gz` 3,860,506,550 B, `solid.tar.gz` 12,758,125,530 B, `chip.tar.gz` 1,194,225,496 B, `case.tar.gz` 1,486,935 B |
| `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/bl3p/` | `fluid_layered.tar.gz` 5,453,801,464 B from 33.81 GB, `logs.tar.gz` 8,044 B |

Both sets were verified by streaming back out of the bucket and walking the archive, because an
archive that has not been read is not a backup.

**And reading them back found a real defect in them.** The three `cbox2b` region archives were
streamed before the dead-region patches were retyped, so `solid.tar.gz` and `chip.tar.gz` hold
`solid_to_air` and `chip_to_air` as `mappedWall` with `sampleRegion air`. The air region was split
off during the build and deliberately never delivered, so those patches point at a region that does
not exist and nothing can resolve them. Anyone restoring the archive gets a mesh that fails
differently from the one on disk. `fluid.tar.gz` is clean, because the coolant has no air patch.

Corrected: `boundary.fluid`, `boundary.solid`, `boundary.chip` and a `README.txt` are now uploaded
beside the archives, and the README says to overwrite `<region>/polyMesh/boundary` after extracting.
Re-streaming 17.8 GB to correct 2 kB would not have been the fix.

The correct state, for the record, is four `mappedWall` patches each pointing at a region that IS
delivered, and the two dead air patches as plain walls:

| region | patch | type | sampleRegion |
|---|---|---|---|
| fluid | `fluid_to_solid` | `mappedWall` | solid |
| fluid | `port_a`, `port_b` | `patch` | |
| solid | `solid_to_fluid` | `mappedWall` | fluid |
| solid | `solid_to_chip` | `mappedWall` | chip |
| solid | `solid_to_air` | `wall` | |
| chip | `chip_to_solid` | `mappedWall` | solid |
| chip | `chip_to_air` | `wall` | |
