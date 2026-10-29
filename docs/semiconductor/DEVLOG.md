# Semiconductor devlog

What I changed, when, and what each change measured. The README says how the thing works today.
This says how it got there and what it cost to find out.

Everything here is on `rehaan/semiconductors`. Nothing has been merged to `main`.

## Where it stands, 2026-10-27

| | state |
|---|---|
| engine | `cfmesh_conjugate`, registered in the catalog, 8 gates, 11 criteria |
| delivery evidence | T4, native to terminal. Job `67161f7d-2c8c-4938-a350-a2d1ca85e451`, 2026-10-26 |
| not yet exercised | T5, a hosted job with a real intake and a real visual review |
| biggest mesh built | 81.6M cells, 74.2 GB, 100-channel part |
| tests | 5,845 pass, 3 skip, nothing failing |
| deployed to | `dev-rehaan` in the `hexera-dev` project |

## What it does on real parts

| part | read | meshed |
|---|---|---|
| `cold_plate_2ch` (2 roofed channels, 2.0 x 4.0 x 1.0 mm) | coolant exact, 0.960000 mm3 against 0.960000 | 41,616 cells, all 8 gates, 9.95% delivered coolant error |
| `manifold_two_level` | yes | 1,790,335 cells, fluid 569,305 in 2, solid 1,221,030, interface 179,655 faces both sides, coolant 0.538% from CAD |
| lidded plates (3 of them) | all 3 | each short by exactly 2 edge channels |
| pin-fin plate | no coolant | 504 of 526 candidate caps dropped, 0 of 505 dropped on parts that work |
| open-groove plates (4 of them) | no coolant | not meshable without a lid |

The split is 4 of 10 parts in the corpus read their coolant without help. The 6 that do not are all
the same shape: a plate with its channels open at the top, because I rebuilt them from published
papers and the papers draw the groove, not the cover.

## The log

### 2026-10-21, a conjugate mesh out of cfMesh at all

cfMesh cannot mesh multiple regions. The route that works is to mesh ONE manifold domain, the whole
block including the metal, then cut it:

1. an ASCII multi-solid STL of the bounding box, coolant surface inside it
2. `cartesianMesh` on the whole block
3. `topoSet` with `surfaceToCell` and `useSurfaceOrientation true` tags the coolant cells
4. `splitMeshRegions -cellZonesOnly -overwrite` cuts two conformal regions along faces that already
   exist
5. `constant/regionProperties` names them

The interface is conformal because the split never creates a face. It only decides which side of an
existing one each cell is on.

The staircase cost, measured on the unit cell: refining does not monotonically reduce error. 7 cells
across came out 14 times worse than 6. The error is in where the cut lands relative to the channel
wall, not in how fine the cells are.

### 2026-10-22, a solver, so a mesh can be judged

Meshing alone says nothing about whether the mesh is right. I added an external solve and a verdict:

| finding | number |
|---|---|
| the energy balance was perfect on a mesh 27% wrong | so the balance is not the test. The port area is |
| the verdict was reading the transient | it stopped at an iteration cap and called it converged |
| a workspace remembered the last run's state | two solves in one directory, and the later one wins |
| a hundred identical channels do average | the wavy channel converges and the straight ones do not, and that is the thing that says why |
| `--lid` was adding metres | the flag takes millimetres |

Five of these came out of an independent review, and every one of them was silent. The run finished,
wrote a report, and the report was wrong.

Richardson extrapolation and the GCI went in so a mesh's answer carries a band instead of a shrug.

### 2026-10-24 to 2026-10-25, the reader

A cold plate is a perforated plate, not a solid body in a wind tunnel, and the intake was telling
cold plate owners to go away. This stretch was all reading, not meshing.

| change | what it fixed |
|---|---|
| one solid with channels through it gives up its coolant | the base case did not work at all |
| the coolant leaves the extractor as one closed surface with nothing declared | a cold plate arrives with no declaration |
| a flange's bore is its opening | a bolt flange read as a passage |
| a sieve is not a cold plate | a perforated face needs the other end of its channels, or it is a grille |
| a channel mouth is not thrown away for being small | the mouth is 0.3 mm on a 100 mm plate |
| an inverted coolant surface is refused | the normals came out pointing in |
| when the coolant cannot be found, say which of the reasons it is | the note was one sentence for six different failures |
| a preposition stopped picking the mesher | `_PAST_A_BODY` matched "aero" inside other words |

A car went into the repository at this point as a regression guard, and it found an aero gap on its
first run.

### 2026-10-25, the engine moves into a bundle

The conjugate mesher moved into `engines/cfmesh_conjugate/` with cfMesh itself untouched. 21 files.
The host writes a plan, the container runs it, the case is scanned before OpenFOAM touches it rather
than after.

### 2026-10-26, the engine row

A full row: `spec.py`, gates, criteria, a review surface, a viewer, a deliverable manifest. One run
end to end from a customer-shaped STEP to a manifest every gate reads.

Two defects the first real runs found:

**A unit.** The extraction was called with a scale of 1.0 on a STEP that declares millimetres, so a
100 mm channel came back holding 4.56 m3 of coolant. Every number after it was off by a thousand and
consistently so, which is why nothing in the chain could notice. The driver now reads the unit
through the same reader the rest of the platform uses, then checks that the coolant's own extent sits
inside the part it was cut out of. That is what a factor of a thousand looks like from the outside.

**One quantity had two bars, and they disagreed.** The `coolant_volume` gate allowed 19.89% while the
criterion had a fixed 5.0% gating bar. The tier caught it at 9.95%. The gating bar is now
`coolant_error_over_tol`, which is the error divided by what the resolution actually allows, and the
bar is 1.0.

**A pin's top caps nothing.** 504 of 526 candidate caps on the pin-fin plate were tops of pins, and
capping them hid a whole chamber. A cap with metal on both sides is now dropped. 0 of 505 dropped on
parts that already worked.

### 2026-10-27, registration

I had held `spec.py` and the roster edits out of git, reading the delivery-evidence rule as
forbidding a committed engine that no job had delivered. That reading was wrong. The rule is about a
CLAIM, it is enforced by tests, and the tests run on any branch. What they require is an honest
entry in `validation_evidence.json`.

Registering it found three defects that were not reachable while the bundle was inert:

| defect | what it did |
|---|---|
| `reads_source_formats` declared the STEP family | that field means "I read the UPLOADED file itself", which only the snap grid does. `capability._source_read_form` asks whether ANY engine claims a format, so this changed the admitted geometry form for every STEP upload in the product, a car included |
| the runner hand-built its result dict | five keys short of every other engine, `signalled` and `signal` and `stage` among them |
| `RC_NO_PLAN` was -2 | a negative return code means "killed by signal N" to the shared seam, so a workspace with no plan reported as killed by SIGINT and a real out-of-memory kill reported as a plain refusal. The two were swapped |

The roster surface is 17 files. Three of them are deliberate gates rather than lists that drifted,
and the rest are rosters.

### 2026-10-27, the first customer CAD

Corintis sent a cold plate and a chip model. Everything before this I built myself from published
papers, so this is the first part I did not author.

| file | what it is |
|---|---|
| `ColdPlateAssembly_CLEAN.step` | 15.9 MB, AP214, millimetres, 6 solids: top lid, board cooling circuit, I/O splitter, microfluidic core, inlet fitting, outlet fitting. 177.5 x 41.8 x 101.0 mm, 8845 planar faces |
| `chip.ecxml` | Icepak neutral XML, 57 objects, 33 materials, 70 x 78.6 x 3.57 mm. No solution domain, and every power is 0 W |

I ran the production reader on it cold, with nothing tuned. It read the unit from the file, found
52.14 ml of coolant over 2 passages with no leaks, proposed three circular ports where its first
guess had been five, and put a seed inside the fluid. Shown six pictures of it, the naming step
said "liquid-cooled cold plate with internal channels", flow internal, kind solid-assembly,
confidence 0.98, real length 0.18 m against a true 0.1775.

So the reading is right. Five things between that and a mesh were not.

#### One line was 84% of the split

`metal_on_both_sides` built its own solid classifiers per candidate cap: 764 caps x 6 solids =
4584 constructions over 9226 faces, 291.0 s of a 347.5 s extraction. All six together take 0.21 s
to build, they are only ever read, and `_split` has always hoisted them.

| | before | after |
|---|---|---|
| extraction | 347.5 s | 141.5 s |
| child wall | 360.3 s | 162.9 s |
| the answer | 6 parts, 58 caps, 70 regions, 1 pocket, no leaks, two passages | identical on every number |

The budget it was being measured against was 120 s, which nothing imposed: `git log -S` shows it
added in one commit with no reasoning beside it, when the slowest part anyone had run was 62.6 s.
Nothing above it is close. The scout's own Celery soft limit is sized from the file, 900 s + 60 s
per MB, so 1853 s for this upload. Now 300 s, priced off the one bound the work actually has:
`extract` refuses anything over MAX_FACES = 20000 outright, and this file at 9226 faces costs
162.9 s.

#### A timeout read as "this part has no coolant"

That is how the slow function did its damage. `run_bounded` returns `{"error"}`, `_summary` copies
`found` only when the key is there, and both readers downstream take one boolean, so a timeout and
a measured absence arrived identically. `_kinds_on_offer` then withholds solid-assembly, the naming
step's correct answer is rejected for not being on the list, and the conjugate mesher is never
offered. `fold` now says which of the two it was. `run_bounded` also stops throwing the child's
stderr away on the failing paths, which is why none of this was explicable from a production log.

#### The engine ruled itself out of the part it exists for

`cfmesh_conjugate` declared `n_solids >= 2 -> outside`. This plate is six solids, so the
recommender placed it OUTSIDE by ENG-1 and recommended the snap grid, which is for electronics
models, for a STEP cold plate.

The solid count was never the question. A brazed or diffusion-bonded plate is a base and a lid, and
the engine's own confirmation wording had been saying so all along. What separates the three
multi-region engines is whether the fluid is a body in the file or the space cut out of the metal.
That is now a declared turf fact, `coolant_inside`, carried from the extraction's own answer.

| case | turf | recommended |
|---|---|---|
| cold plate, coolant cut out of the metal | home | cfmesh_conjugate |
| cold plate, extraction found nothing | outside | snapgrid |
| cold plate, the check has not run | neutral | snapgrid |
| chip stack, 19 bodies, no fluid inside | outside | snapgrid |

There is no `closed` row any more. It said "a solid that is not closed bounds no space", but
`closed` is read off a welded tessellation while `coolant_inside` is read off the faces. This plate
came back closed=False from two skipped null triangulations while the split cut 52.14 ml out of it
with no leaks, and almost any real assembly with fittings reads the same way.

#### The mesher ran, and then refused its own mesh

rc=0 in 102.3 s, 1,852,814 cells, fluid 364,105 and solid 1,488,709, interface 172,356 faces both
sides, `constant/regionProperties` written. Then: "fluid is in 226 disconnected piece(s), expected
2". `checkMesh` on the fluid region says what those are:

| | cells | what it is |
|---|---|---|
| region 1 | 255,144 | the main passage, part A |
| region 2 | 98,044 | the main passage, part B |
| region 0 | 9,966 | the second coolant space, matching its 1,497 mm3 |
| regions 3-225 | 1 to 3 each, 951 in all | specks, 0.26% of the fluid |

So 223 of the 226 are one-cell noise and the real fault is that the main passage came out in two.

#### The sizing step could not see a third of the part

The cell size follows from the narrowest passage, which is read as an inscribed-radius field at the
wall's own vertices. A SHORT neck carries vertices on its two rims alone, each on a 90-degree edge,
where the chord reads the neighbour it opens into rather than the neck. `engines/passage` already
carried the answer for exactly this - an orifice plate's 26 mm bore read as its 53 mm pipe - behind
a `read_sharp_edges` flag the cold-plate path was not setting.

| reading | narrowest |
|---|---|
| as production read it | 2.6525 mm |
| with the sharp edges | 0.0980 mm |

Confirmed independently by ray-casting all 52,334 triangles into the fluid and taking the far wall:
area-weighted p1 0.0979 mm against the sharp-edge reading's 0.0983. Two methods sharing no code,
agreeing to three decimals. **33.0% of that coolant's wall area is thinner than the 0.53 mm cell
the old number chose.**

The mean hides it completely, which is why it was not caught by eye: 4V/A over the whole coolant is
3.5 mm, because the thin layer is 2% of the volume spread over a third of the area. A mean is the
wrong statistic for a cell size, which has to suit the smallest thing the mesh must hold together.

#### What that part actually costs, which is the 300M question with a part attached

| | |
|---|---|
| true narrowest | 0.098 mm |
| block | 177.5 x 41.8 x 101.0 mm = 750,086 mm3 |
| 5 cells across it | 0.0196 mm cells, background 0.157 mm |
| cells | ~195M |
| memory at 909 B/cell | 177 GB |
| the platform's hard limit | 8M cells |
| Cloud Run | 32 GiB |

At the platform's own 13-cell rule it is 3.4 billion cells, which is not a target. At 5 cells
across its microchannels a real microfluidic cold plate is a 200M-cell job, which is a big-memory
VM and not Cloud Run. The scaling argument earlier in this log said the same thing; this is the
first time a customer file has said it.

#### The ports are inside the block

The last one, and it is a missing capability rather than a defect. The case takes its inlet and
outlet from the block's six named faces, which works while the coolant's mouths lie ON those faces.
Eight of the nine parts here do, because a channel milled across a plate ends flush with the side
it was cut on. This one does not: its mouths are fitting bores and the bounding box is set by the
fittings' own bodies, so the coolant stops 2 mm inside the block on every side and the mesher
refuses with "it is sealed inside the metal".

The mouths were measured all along and nothing carried them out of the extraction; the surface
record now does, and `engines/cfmesh_conjugate/mouths.py` turns them into stubs. A mouth with air
ahead of it is extended along its own axis until it crosses a block face, which is what anyone
setting up a case adds at an inlet anyway so the profile has length to form in. A mouth that opens
into another part gets nothing, because the stub would be a passage bored through metal the
customer did not draw.

On the real part:

| mouth | | |
|---|---|---|
| 1, 11.8 mm | refused | opens into the I/O splitter rather than into air |
| 2, 8.0 mm | stub to `xmax`, 20.07 mm | adds 1009.02 mm3 |
| 3, 8.0 mm | stub to `xmax`, 30.68 mm | adds 1542.22 mm3 |

THE VOLUME CHECK EARNED ITS PLACE ON ITS FIRST RUN. I wrote `added_volume_agrees` because a stub is
a cylinder whose volume is known in closed form, so the join can be checked rather than assumed,
and the first run said the two stubs enclosed **-840.735 mm3** against the 2551.247 they claim. The
tube's two triangles were wound inwards. A closed surface that encloses the wrong volume raises no
open, non-manifold or inconsistent edge, so nothing else in the chain would ever have noticed. The
winding is now derived in the docstring rather than guessed, and it measures 2522.204 mm3, the
1.1% being a 24-sided tube falling short of its circle.

### And the part still is not solvable, for a reason that is Corintis's to answer

With mouth 1 sealed, passage 1 has one open end and passage 2 has one open end. Neither can flow,
and no stub can invent the connection. The question is whether mouth 1 connects through the I/O
splitter, and if it does, whether that part's bore can come as fluid. That belongs in the intake,
and the extraction already says almost the right sentence: "opening 1 opens into another part whose
inside reaches the outside some other way (a gap in it?): check it on the picture".

So the honest state of this part is: Hexera reads it correctly, recommends the right engine, sizes
it correctly, meshes it in 102 s, and tells the customer exactly what it needs from them before the
mesh can be solved. That is further than any part of this package has gone on CAD nobody here
authored.

## What I tried and dropped

**Closing an open-top groove in software.** Nine proposals, none survived. Box caps close all four
failing plates exactly, 260.0 mm3 against 259.9998 declared on the Corintis baseline, and the same
caps give the Ahmed body 2 passages and 6.19e6 mm3 of "coolant", which `write_surface` accepts.

Every fluid-side guard I tried fails. The decisive one: requiring two or more coplanar faces closes
none of the four, and turns a finned heat sink into 10 passages and 59,318 mm3 of room air. Fins
fused onto a base leave 12 coplanar faces. Grooves cut into a slab leave one.

An open-groove cold plate and a finned heat sink are the same shape. This is not a geometry problem.
The platform's answer, which is to ask for the part with its lid on, is the right one.

**Chasing 13 cells across.** The platform's own rule is 13 cells across the narrowest passage. On a
chip-scale part that is hundreds of millions of cells. The staircase measurement says refining is not
monotonic anyway, so the fixed ladder in `budget.py` runs 4 to 8 and steps down to fit the budget.

## Open

| item | state |
|---|---|
| T5 delivery | a hosted job with a real intake and a real visual review. Not run |
| the accuracy promise | what error the product commits to on a conjugate mesh. Rehaan's call, not made |
| the engine recommender | still recommends ECXML-only snap grid for a STEP cold plate |
| `snappy_multiregion` | disowns single-solid plates |
| the car-path test | cannot see its own failures |
| 300M cells | the path is a big-memory VM, parallel post-processing, or a periodic unit cell. See below |

### 300M cells, what it would take

| | 81.6M, measured | 300M, scaled |
|---|---|---|
| memory for the finished mesh | 74.2 GB | 272.7 GB |
| wall clock | ~1.2 h | 4.3 h |
| Cloud Run ceiling | | 32 GiB memory, 4.0 h timeout |

Cloud Run cannot do it on either axis. 85% of that time is serial post-processing, `topoSet` testing
every cell centre and `splitMeshRegions` writing two full meshes. cfMesh itself saturates around 4
cores at 2.5x and MPI buys nothing, and `mpi-4` changes the mesh.

So: a big-memory VM, parallelising the post-processing, or meshing a periodic unit cell and
multiplying. The third is the only one that does not need new hardware.

**Corrected 2026-10-28.** "MPI buys nothing" is right about SPEED and wrong about the ceiling, which
is a different question. cfMesh MPI makes each process's octree addressing smaller, and that is what
lifts the 32-bit limit: the same case that aborts serially at 202,709,195 boxes built across 8
processes in 1397 s. The binding constraint after that is not the mesher. See below.

## 2026-10-28, the first customer part, and what the mesher will not build

| | |
|---|---|
| part | Corintis `ColdPlateAssembly_CLEAN.step`, the first real customer CAD |
| biggest mesh built | 102,630,992 cells, end to end, 2.23 cells across the 0.098 mm slots |
| previous best | 99,172,391 cells, 1.03 cells across the same slots, coolant in 367 pieces |
| ceiling found | cartesianMesh aborts above about 179M octree boxes per process, on any machine |
| ceiling lifted | cfMesh MPI. 202,709,195 cells built across 8 processes, the same case that aborts serially |
| commits | `18d64faa`, `a82c86c4` |

### What the 102,630,992-cell run delivered

| | |
|---|---|
| fluid | 22,062,782 cells in 19 pieces |
| solid | 80,568,210 cells in 1 piece |
| interface | 13,022,400 faces on both sides, conformal |
| coolant volume | 0.41% from the CAD, against a 5.0% tolerance |
| checkMesh | no fatal class in either region |
| mesher | 6286.1 s (1.75 h), peak 80.46 GB, 784 B/cell |
| finalize | ok |

19 pieces against 367 on the previous best, which is the reading that decides whether the mesh is
usable, and it is still not the 2 the part has. Memory came out at 784 B/cell against the 748.1 the
budget predicts, a 5% miss.

GETTING TO 2 PIECES NEEDS THE PARALLEL PATH, and the arithmetic says so rather than a guess. The
slots want about 4 cells across, which is one more octree level: a span of 4, a band depth of 16.81,
and 21,712.67 mm2 at a 0.0220 mm cell is about 754 million cells. That is past the 178,956,970 a
single process can address, and it is not close. One process cannot do it at any resolution that
holds those channels together.

### The part has two sizes of passage and the mesher was told about one

| | width | wetted area | share of block |
|---|---|---|---|
| manifold channels | 2.11 mm | 38,813.50 mm2 | |
| microchannel slots | 0.0982 mm | 21,712.67 mm2 | 6.0% |

The width distribution has a trough in it. The two fullest octaves hold 12,041 and 1,802 points and
the gap between them holds 54, so the split is the part's own and not a number I picked. Run over
ten other parts in the corpus, every one reads as a single scale.

Sizing from the narrowest passage everywhere prices the whole 60,526 mm2 at a slot-sized cell, which
estimates about three billion cells. Holding the manifold at what it needs and spending the rest on
the slots is what makes the part affordable. `cad/scales.py` measures it, writes the thin walls out
as their own surface, and `budget.plan_two_scales` prices the two bands separately.

### A band, not a box

| | cells |
|---|---|
| `objectRefinements`, type box, over the thin layer | 456,206,329 predicted, 478,648,313 built |
| a refinement band round the same triangles | 224,431,537 predicted |

A box fills its own volume at the fine size. The thin layer is a third of the wetted area and about
2% of the volume, so its bounding box is almost all metal and every one of those cells was paid for.
The box run died on it.

### The cost is the octree span, not the cell size

| background | span | octree boxes | outcome |
|---|---|---|---|
| 1.40822 mm | 5 | 204,543,984 | aborted |
| 0.35206 mm | 3 | 102,152,624 | built, 3060 s, peak 185 GB |

Same part, same thin surface, same 0.044007 mm cell. Only the background moved. Two extra levels of
grading cost 2.4x the cells at identical resolution, because the levels between a refined surface
and the background are grading shells of real cells.

The old depth fit read `depth = 6.90 * mm ** -0.341`, from two readings 2.6x apart in cell size that
both sat at a span of 3. With the span fixed, a finer cell is a wider span, so it could not separate
them. Extrapolated to the cell this part needs it predicted 20.0 cells of depth where the run
measured 6.9. Three things followed and all three are fixed:

| | was | now |
|---|---|---|
| the planner's preference | a coarse background, believing depth free | the shallowest octree that fits |
| a refusal's price | the deepest octree, 4,601,986 cells on this plate | the cheapest, 2,049,690 against a 2,000,000 limit |
| what it refused | configurations that build | |

The new line anchors at 11.86, the largest of the three span-3 readings rather than their mean. The
mean under-predicted the 17,447,384-cell calibration run by 3.8%, and under-predicting sizes a
machine for less than the run needs.

### The 32-bit ceiling, and that MPI lifts it

| octree boxes | outcome |
|---|---|
| 99,218,211 | completed, 99,172,391 cells, 2530 s, peak 74.61 GB |
| 102,152,624 | completed, 3060 s, peak 185 GB |
| 204,543,984 | aborted after 302 s, peak 77.55 GB |
| 479,119,929 | aborted after 277 s, peak 50.20 GB |

Always the same abort, in `meshOctreeAddressing::createNodeLabels`:

    --> FOAM FATAL ERROR: Negative size requested.
        From Foam::Module::LongList<T, Offset>::allocateSize(Foam::label)
                                   [with T = int; int Offset = 19; Foam::label = int]

This OpenFOAM is `linux64GccDPInt32Opt`, the only platform directory in the image. About twelve
32-bit labels per octree box, so the ceiling is 2^31 / 12 = 178,956,970 boxes. That figure fits
every row above. Priced in memory the dead runs looked affordable on any 100 GiB machine, so
`mesh_machine` would have sent them to a 961 GiB box to fail there identically and dearer.

`budget.OCTREE_BOXES_PROVEN_OK` carries the largest count proven to build, and the driver refuses
above it before a machine is chosen. The guard sits after the `CELL_HARD_LIMIT` check because they
are different questions: that limit is about the machine, this ceiling is not.

### Six things that were wrong and are not now

| | what it was |
|---|---|
| the refinement zones | `render_meshdict` took them, `build()` never passed them. A 78-minute run on a 251 GB machine rebuilt an identical 99,172,391-cell mesh at an identical 74.61 GB peak and exited 0 |
| a dry clamping bore | counted as coolant. 381.703 mm3 on `buried_mouths`, 6.1% of its coolant, and the Class 1500 spool fixture's sixteen bolt holes at 32.7% of its bore |
| the gate row | never ran on a mesh `finalize` had rejected. On the customer mesh none of the eight gates executed, and the run reported as a mesher that stopped before writing one |
| the refinement thickness | inert. Two runs at 6 and 4 of its own cells gave 202,660,138 and 202,709,195 boxes |
| `refuse_for_octree` | reachable from nowhere |
| my own watcher | reported a run finished while `splitMeshRegions` ran at 99.9% CPU. `pgrep -x A -x B` honours one pattern |

The dry bore is separated by shape, not size. A duct is longer than it is wide and a bore is not,
measured as volume over (mouth area x widest mouth diameter). Over 21 reads the two kinds are a
factor of five apart at the nearest: 0.33 and 0.68 for the bores, 3.31 and up for every real
passage. It applies only to a passage that is not the largest, because a flange is legitimately a
short fat bore.

### Open

| item | state |
|---|---|
| the parallel chain | cfMesh MPI builds the mesh. `conjugate.py` then reads `ws/constant/<region>/polyMesh/boundary`, which a parallel split never writes, so a good mesh reads as having no interface and no ports |
| the delivery | the workspace rides home as one archive capped at 512 MiB. A 300M-cell mesh is far past that even in binary |
| the quota | 32 CPUs all-regions with about 20 on CI runners. Only an 8-core box is creatable, and mesh-bench already is one |
| the depth model | one controlled pair, one part. The span term is 4.95 cells per level and the scatter at a fixed span is 6.9 to 11.86 |
| the test corpus | all ten generated parts read as one scale. None of them reproduces the difficulty of the customer part |

## 2026-10-29, the console opens the mesh

| | before | after |
|---|---|---|
| viewer payload | 555.8 MB of base64, coolant side alone | 18.7 MB, whole case |
| what is drawn | nothing, it never got that far | 636,713 triangles of 13,504,416 delivered faces |
| resolution drawn | | merged at 2.04 mm, stated in the payload and on the status line |
| payload built in | | 115.9 s, peak 6.14 GiB, on the box holding the 47 GB workspace |
| the API serves it in | | 0.36 s |

The mesh built on 2026-10-28 was on disk and checkMesh passed on both regions, and nothing
downstream could open it. Three limits, each measured on that workspace rather than guessed at.

| step | what happened | the limit |
|---|---|---|
| export for review | `VTK/fluid` and `VTK/solid` created at 21:05:37, the report written at 21:10:38, both directories empty | the 300 s timeout in `export_volume_vtk` |
| ship it home | the coolant side alone packs to 555.8 MB | the 512 MiB workspace archive cap |
| draw it | 13,022,400 faces handed to a browser with no budget applied | 400,000 triangles, which is what the INPUT skin path has always held itself to |

The input skin has had a budget since it was written. The delivered mesh had none:
`polymesh_response` base64-packs every face it is given. `render/polymesh_budget.py` gives it one.
It clusters on the compact (points, polys) pair rather than on a triangle soup, so it sorts 13
million rows where `skin_mesh.simplified` would sort 78 million, and the coolant interface reduces
in about 50 s.

### The payload says what it is showing

`fit_to_budget` returns a `reduced` block and `polymesh_response` carries it through:

    {"delivered_faces": 13504416, "drawn_triangles": 636713, "resolution_m": 0.0020381, ...}

The viewer's status line reads the delivered counts out of it and ends with "drawn merged at
2.04 mm (636,713 of 13,504,416 faces on screen)". Without that the console printed the drawn count
as the delivered one, which tells a reader their mesh is twenty times coarser than it is.

`delivered_faces` counts the mesh's own faces and `drawn_triangles` counts triangles. The first
version summed the fan triangulation into both and reported 27,020,802 delivered for a boundary of
13,504,416 faces.

### The metal was hiding the coolant

The solid region's every patch except the coupling is a face of the block the part was cut from, so
a viewer that opens them shown draws six grey rectangles with the coolant sealed inside. That is
what the first load looked like. `conjugate_viewer` now sets `encloses_domain` on them, which
`render/mesh_facts` turns into HIDDEN, and the viewer re-frames on what is left.

### How it got into the console

The payload is built where the mesh is, which the pipeline does not do: `artifact_uploader` builds
it on the worker, after the workspace has travelled home, and this workspace cannot travel. The
Cloud Run client sizes the return caps from a cell count clamped to 2 x CELL_HARD_LIMIT = 16M
(`adapters/mesh_execution/cloud_run_client.py:413-416`), which gives 12.8 GB against tens of GB. So
for now the payload is built on mesh-bench by the product's own code, 18.7 MB travels, and a job
row plus one `viewer_data` artifact are registered by hand against the local stack.

### Open

| item | state |
|---|---|
| the channels are not visible | the coolant merges at 0.41 mm and the slots are 0.098 mm. A whole-part payload cannot show them at any budget. The answer is a second level: the three cuts `cfmesh_conjugate/slices.py` already names, shipped at full resolution beside the preview |
| the quality heatmap | not attached on a reduced payload, because it is per face. It was not attaching on this engine anyway: `_paint` looks up bare patch names against region-prefixed ones |
| the part roster | lists the manifest's bare patch names and the viewer's region-prefixed ones, 15 entities where 9 would do |
| the other polyMesh engines | snapgrid and snappy_multiregion pack unbudgeted too. Neither delivers at this size today |
| a real run | cannot reach the console at this size at all: `mesh_paths.surface` is a required render artifact and the review needs it |

## 2026-10-29, cells shaped like the feature, and the readings the customer asked for

Corintis answered three questions on 2026-10-29. The one that turns into work is the third: they
want to see "mesh count, quality metrics to know if the mesh is of good quality, number of
boundary layers in the fluid, min and max cell sizes, cell types and count per type, highlight of
poor-quality cells". Four of those six were already being measured on every run and thrown away.

| reading | before | now, on their own delivered mesh |
|---|---|---|
| mesh count | cells only | 102,630,992 cells, 324,194,634 faces, 118,939,386 points, per region |
| cell types | hexahedra and polyhedra of seven | 100,629,029 hexahedra and 2,001,963 polyhedra |
| min and max cell size | nothing | 41.71 um .. 370.24 um, a ratio of 8.9 |
| poor-quality cells | nothing | 28,132 with a small determinant |
| quality metrics | the worse of the two regions | per region |
| boundary layers | none, and still none | |

`checkMesh` prints all of it on every run. `_CM` had regexes for two cell types out of seven and no
sizes at all, and `read_region` kept six fields of what it was handed. The run that produced the
numbers above cost 204.9 s on the coolant and 584.5 s on the metal, and had cost that every time.
Its output is now kept verbatim beside the mesh as well, so a reading nobody thought to parse is a
re-parse rather than a two-hour re-measure.

### The per-region split was hiding the answer

| | coolant | metal |
|---|---|---|
| cells | 22,062,782 | 80,568,210 |
| non-orthogonality, max | 16.92 | 22.71 |
| cells with a small determinant | 200 | 27,932 |

Collapsed with `max()`, as the manifest did, that reads as one mesh with 28,132 bad cells. It is
two meshes, and 99.3% of the bad cells are in the half that is not being solved for flow.

### A prefix nobody matched

`_paint` asks `face_quality` to colour the delivered boundary and hands it the payload's patch
names, which this engine writes as `fluid/fluid_to_solid` so that the two regions' identically
named block faces cannot replace one another. The polyMesh on disk knows only `fluid_to_solid`.
Nothing matched, `per_patch` came out empty, `quality_fields` returned None, and the engine whose
whole question is WHICH SIDE is bad has never drawn a heatmap. Nothing raised; a missing heatmap
is swallowed by design.

The same mismatch doubled the part roster. A declared `ymin` found no actor and was listed
un-renderable; `solid/ymin` was appended beside it as a part of its own. Fifteen entries for nine
boundaries, with the pickable half and the drawable half being different rows.

### A picture that times out takes the run with it

| | |
|---|---|
| VTK directories created | 21:05:37 |
| report written | 21:10:38 |
| the timeout | 300 s |

`review_surface.build_region_msh` converts foamToVTK's output into the msh that
`mesh_paths.surface` names, and that artifact is `required=True`. So a fixed 300 s ceiling, from
when meshes were small, is why a mesh that took 1.75 hours to build cannot reach a verdict. It now
follows the cell count like `CHECK_TIMEOUT_S` beside it, and says so loudly when it does not
finish.

### Cells shaped like the feature they resolve

An octree cell is a cube. The customer's slots are 0.098 mm wide, run tens of mm in plane, and are
1.9 mm tall with a constant cross-section through that height, so resolving the width to five cells
also buys about ninety-seven cells through a height where a dozen would do.

cfMesh answers this with `anisotropicSources`, which ships in its own ship5415Octree tutorial. I
measured the convention rather than assuming it, on a 10 mm box at 1 mm cells:

| | cells | largest cell volume |
|---|---|---|
| plain | 896 | 1.256e-9 |
| scaleY 4.0 | 320 | 5.142e-9 |
| scaleY 0.25 | 3,776 | 4.344e-10 |

So a scale above one stretches along the axis and the saving follows the factor. Guessing it the
other way quadruples a mesh meant to be quartered.

THE LICENCE IS GEOMETRIC, not a setting. A stretch is free only along an axis the shape is extruded
along; anywhere else it smears the features the mesh exists to resolve. `cad/scales.prismatic_axis`
measures it as the area-weighted mean of the normal's component along each axis, which for an
extrusion is the share of the surface that is cap. On the customer's own files:

| surface | reading |
|---|---|
| `fluid_fine.stl`, the microchannels | y at 0.0694 |
| `fluid.stl`, the whole coolant | no prismatic axis |

which is the right answer both times: the channel field is an extrusion and the manifold loop bends.

### What that buys, priced on the real geometry

| across the slots | cell | stretch | cells | fits 100-700M | one process |
|---|---|---|---|---|---|
| 2.23 | 44.01 um | none | 102,630,992 delivered | | yes |
| 4.46 | 22.00 um | none | 966,955,439 | no | 4.3x over |
| 4.46 | 22.00 um | 6x | 181,673,384 | yes | yes |
| 8.93 | 11.00 um | 8x | 634,530,269 | yes | 2.8x over, needs 3 ranks |
| 8.93 | 11.00 um | none | 4,903,923,343 | no | no |
| 17.85 | 5.50 um | 8x | 3,019,246,411 | no | no |

The stretch doubles the resolution across the slots for FEWER cells than the unstretched plan, and
4.46 clears the four-cell floor below which a passage stops holding together. The rung above is
inside the band and gated on the parallel chain alone, so `plan_two_scales` now takes a rank count
and a refusal says how many ranks the configuration it had to reject would have taken.

`FINE_ACROSS_CAP` moved from 8 to 10 because the ladder is discrete: the octree halves, so on
0.0982 mm slots the reachable rungs are 4.46 and 8.93, and a cap of exactly 8 refused the rung that
meets the published guidance by 0.93 of a cell.

### Open

| item | state |
|---|---|
| boundary layers | still none. `generateBoundaryLayers` is in the image and `nLayers` / `thicknessRatio` are keywords cartesianMesh reads, so this is wiring, not a capability |
| the heatmap on a big mesh | the name fix lands it for anything under the 40M-face cap; the customer's coolant alone is 73M. `checkMesh -writeAllFields` writes nonOrthoAngle, skewness, aspectRatio and cellShapes as volFields, and `-writeSets` writes the bad faces as a surface, which is the cheaper route than re-deriving in numpy |
| the corpus | a two-scale part is being built (`devtools/semiconductor/geometry/two_scale_plate.py`): 24 x 16 x 3 mm, a 2.1 mm manifold ring feeding 22 slots of 0.098 mm on the customer's own 0.545 mm pitch, in sealed and ported variants. It builds and its volumes close exactly on the block; the width histogram does not split yet at 944 triangles and is being re-read on a subdivided surface |
| NCC | `createNonConformalCouples` is absent from this build, so non-conformal coupling here means `nearestPatchFaceAMI`, which cfd.direct measures as non-conservative and Cheung (TUM 2016) measured at a 3.8% energy imbalance. Conformal stays the plan for the plate |
| `pureZoneMixture` | present in this build, which is what makes one coarse chip region with per-material cell zones the answer for an ECXML of up to 2000 solids rather than 2000 regions |

### A sealed coolant was refused for the wrong reason

`ConjugateReport.flowable` has been on the report since the engine was written, with a docstring
saying what it is for: "with neither, nothing fixes its pressure and no solver will run". No gate
read it. So the customer's sealed mesh cleared all eight mesh gates and was refused further down
for a patch-name contract mismatch, which is true and sends the reader to look at their patch
names when the problem is that their coolant is a closed box.

| | |
|---|---|
| their fluid region's patches | `fluid_to_solid`, and nothing else |
| the plan's ports | inlet '' and outlet '', port_walls [] |
| what the row said | the patch contract did not match |

There is now a `flowable` gate, and a sealed part that never asked for ports still passes it -
`murray.py` builds a tree sealed inside the metal deliberately and it is the hardest topology in
the corpus. What is refused is a part that asked for an inlet and an outlet and did not get them.

The port is named TWICE and that caught me: `ports` carries the block face the coolant leaves by
(`ymin`), while the delivered patch wears the user's name (`coolant_in`), because renameBoundary
runs before the split. My first version looked up the face name among the delivered patches,
found nothing, and refused the canonical deliverable fixture. The existing row test caught it.

### The floor that admitted 2.23 cells across

`FINE_ACROSS_FLOOR` was 2. The comment above it has said since it was written that four is the
floor at which a passage holds together, and the planner enforced two, which is how the customer
part came to be planned at 2.23 across and delivered in 19 pieces.

It was two because four was unaffordable: 4.46 across prices at 966,955,439 cells unstretched.
With the stretch the same resolution is 181,673,384, so the floor is now four and the
configuration that built is refused - which is the behaviour that was wanted all along.

### Also

The delivered interface AREA is now carried per patch, having been computed and discarded on
every read. On a conjugate mesh that number is the heat-transfer area, and it is what staircasing
takes away without changing a single face count: 48,430.54 mm2 delivered against 60,588.07 mm2 of
wetted CAD on the customer plate, a 20.07% deficit invisible from a face count.

## 2026-10-29, past one process, and a cold plate that reads as one

Two things were blocking the sizes this engine exists for, and they turned out to be unrelated.

### The ceiling was an int, not a machine

The Corintis plate at 22.00 um with its cells stretched 6x along the channels prices at
181,673,384 cells. It was launched, and it died 231 s in:

    --> FOAM FATAL ERROR: Negative size requested.
        From void Foam::Module::LongList<T, Offset>::allocateSize(Foam::label)
        #4  Foam::Module::meshOctreeAddressing::createNodeLabels() const

On a box with 251 GB of memory and 402 GB of free scratch. `budget.py` had priced that boundary
since before any of this - twelve 32-bit labels a box, so (2^31 - 1) / 12 = 178,956,970 boxes a
process - and `refuse_for_octree` has named the way past it in words all along. Nothing drove it,
so every configuration above the ceiling was refused at planning time and the one that slipped
through aborted. 181,673,384 is 2.7 million over.

The ceiling is PER PROCESS, which is the whole point: each rank octrees its own share.

| | |
|---|---|
| `preparePar` | cfMesh's own, not `decomposePar`: there is no mesh yet to decompose. Reads `system/decomposeParDict`, lays out one `processorN` per rank |
| `mpirun -np N cartesianMesh -parallel` | each rank meshes its own subdomain |
| `reconstructParMesh -constant` | back to one mesh in `constant/polyMesh` |
| then the chain as it was | `topoSet`, `splitMeshRegions`, the port cut, `checkMesh` |

ONLY THE MESHER GOES PARALLEL. The ceiling belongs to cfMesh's octree addressing and nothing
downstream shares it, so the mesh is merged the moment it exists and the cell-zone split, the
two-region check and where the ports are cut are the same code on the same kind of mesh they have
always run on. Making five stages parallel to get past a ceiling one of them has would have put
all five on a path nothing has measured.

Proven first on the ported two-scale plate at 0.5 mm, 8 ranks: 2,317,572 cells in each processor
directory in 68.29 s, and the merged mesh came back from checkMesh as

    points 6,220,725   faces 18,420,912   internal faces 18,181,584   cells 6,100,416   Mesh OK.

### Three flags, every one of them measured

| flag | what happens without it |
|---|---|
| `--allow-run-as-root` | the mesh container runs as root and Open MPI refuses to start |
| `--oversubscribe` | Open MPI counts the slots it believes it has, not the cores the box has. `nproc` said 8 inside the container and mpirun still refused 8 ranks: "Either request fewer slots for your application, or make more slots available for use" |
| `-x MPI_BUFFER_SIZE=1000000000` | cfMesh's octree load balancing sends boxes with `MPI_Bsend`, into a buffer the application attached beforehand. OpenFOAM attaches controlDict's `mpiBufferSize`, 20 MB. The 272M build got four rebalances in and died on the fifth after 40 s with `MPI_ERR_BUFFER: invalid buffer pointer`. Setting the variable in the launching shell is not enough - each rank reads its own environment at Pstream init, so it has to be exported |

The same chain at 6.1M cells never came near any of the three.

### What the box can hold, which is the binding limit now

At 748.1 bytes a cell, the rungs on the customer plate's own plan (background 352.06 um, broad 1
level, fine 4 levels, so 22.00 um at the slots):

| stretch | cells | ranks the ceiling demands | peak | fits 251 GB |
|---|---|---|---|---|
| 1 | 775,728,436 | 5 | 540.9 GiB | no |
| 2 | 397,710,991 | 3 | 277.5 GiB | no |
| 3 | 271,705,176 | 2 | 189.7 GiB | yes |
| 4 | 208,702,268 | 2 | 145.8 GiB | yes |
| 6 | 145,699,361 | 1 | 101.9 GiB | yes |
| 8 | 114,197,907 | 1 | 80.0 GiB | yes |

The ceiling and the memory are different limits and they bind in different places: stretch 4 needs
two ranks for the ceiling while fitting one box's memory twice over.

### A cold plate was being read as a car

The other half of this was nothing to do with size. Uploaded to the running stack, a 24 x 16 x 3
mm plate with two 1.2 mm bores through its lid came back from the stage as

    input_kind solid-body   flow external
    opening  inlet   1.35 mm across at (18, 13.9, 3) mm
    opening  outlet  1.35 mm across at (6, 2.05, 3) mm
    note: the space inside the part is 415 mm3, open at 2 places

Two openings named inlet and outlet, with their diameters and their positions, beside a reading
that says the fluid flows AROUND the part. Nothing asks the user which it is: `flow_question`
returns "" the moment there are two ports. Accepting what was drawn produced "No openings: the
fluid flows around the whole body" - `patches_from` returns no patches at all for external flow -
and the intake then refused, correctly, to mesh a conjugate case on a geometry with no coolant in
it.

The scout is not wrong to read a plate as box-like; a 24 x 16 x 3 mm block IS shaped like a body
in a flow (flat_share 1.32, fill 0.64). What was wrong is serving that reading as the default
AFTER the split had cut 414.6 mm3 of coolant out of the part and found it open at both ends.

`fold`'s rule for one solid - "THE SPLIT ADDS WHAT IT LEARNED AND OVERRULES NOTHING" - is about
the OPENINGS, which is what the pipe tee lost when the split replaced its three correct ports with
"the space inside the parts". Those stay the scout's. The flow is a different question and the
split has just answered it, so one solid whose coolant was found open at two or more places now
reads `internal` / `solid-assembly`, which is the kind `purposes.py` describes in as many words.

With that answered the whole chain runs with nobody steering it: upload, stage, confirm, intake,
`cfmesh_conjugate` proposed over the snap grid and snappy multi-region, the coolant conditions
asked for, and a job queued. It then stopped where it should:

    conjugate: refusing to start - this part does not fit: 4 cells across its narrowest passage at
    3 octree levels is about 28,444,758 cells against a limit of 8,000,000

`CELL_HARD_LIMIT` is already environment-overridable and its comment says what it is: the Cloud Run
feasibility limit, 8 vCPU / 32 GiB / ~50 min. It is a statement about where the mesh is built, not
a number to argue with - and the 100 to 700 million band this engine is for needs the box, not the
limit, raised.

### The case around the mesh, with the numbers left out

A mesh is not a case. chtMultiRegionFoam will not start without a field file per region naming
every patch, a thermophysical model per region, and the coupled condition on the interface - and
all of that is STRUCTURE, which the mesh determines: which regions exist, what the patches are
called, which patch is the interface, which are the ports. None of it is a physical choice.

The physical choices are not ours and are not guessed at. `solver_case.py` writes
`<<HEXERA_FILL_THIS_IN>>` where the flow rate, the inlet temperature, the outlet pressure, the
coolant and the metal go, and that token is one OpenFOAM cannot parse: an unfinished case stops on
the first file that still has one. A plausible default is the worse failure, because 1 m/s of water
at 20 C solves, publishes a peak temperature, and is wrong about the part.

Two things in it were wrong first and are worth recording.

| | |
|---|---|
| the comment ate the terminator | `internalField uniform <<...>> // initial temperature;` puts the semicolon inside the comment, so the entry has no end. The comment goes after it |
| an outlet was an inlet | the first version treated both as one "port": both ends came back with a fixed inlet temperature and a prescribed flow rate, which is a case with no way out and two conflicting flow rates. Where each condition goes is not symmetric - the velocity is prescribed at one end and the pressure at the other |

### Boundary layers, which this engine will not have

Corintis asked for the number of boundary layers in the fluid. The answer is none, by construction.

A conjugate interface is conformal because both regions are made of the same cells from the same
octree: every face on the coolant side IS the face on the metal side. An extruded prism layer is a
new cell inserted on ONE side of a patch, so a stack on `fluid_to_solid` leaves every one of its
faces without a partner - the single property this whole approach exists to guarantee.

cfMesh could not do it here anyway. `boundaryLayers.patchBoundaryLayers` names patches, and at
meshing time the coolant's wall is not a patch: it is interior to one manifold block, and the patch
is made by `splitMeshRegions` afterwards. `generateBoundaryLayers -region fluid` would run on the
delivered mesh, which is exactly the thing that must not be run on it.

What the mesh has instead is graded octree refinement toward the wall, which gives matched cells on
both sides and is the conformal equivalent of a layer stack. The manifest now carries those
numbers - `near_wall_levels`, `near_wall_cell_m`, `near_wall_layers` at zero - because near-wall
resolution is asked about on every run and a reader needs the number that exists.

### Where the poor cells are, not how many

The readings have carried a COUNT of poor cells since they were widened: 28,132 on the customer
plate, 27,932 of them in the metal. A count says a customer has a problem without saying where,
and "highlight of poor-quality cells" was the sixth thing they asked for.

checkMesh has already collected those cells into sets by the time it prints the count. `-writeSets
vtk` is the flag that writes them out, and it costs nothing on a good mesh: the sets ARE the cells
that failed a check, so a mesh that passes writes no files (measured on the 8-rank 6,100,416-cell
build - "Mesh OK." and nothing written). On a bad one the set is small by definition, 28,132 out of
102.6 million, so it is drawable at full resolution whatever the mesh's size - which a heatmap over
the whole boundary is not.

ONE FOLDER PER REGION, because checkMesh writes them by the set's own name and nothing else, so
the metal's run writes `skewFaces.vtk` over the coolant's. On a conjugate mesh that is the whole
measurement lost: one file called skewFaces cannot be both 27,932 cells and 200.

### A file of several parts, drawn as several parts

A thermal model has always been drawn as its parts - one block per part under its own name and
colour, and the console has always known how to draw that. A STEP assembly got none of it. The
customer's package is six solids and arrived on the stage as one grey shape, so a customer checking
that we had read their assembly could not see whether we had found their parts at all.

`cad/solid_parts.py` returns the same payload from a STEP. Named from the file where the file names
them, `Part_k` where it does not, and each part carries its own extent and volume - because "is
this my part?" is the question the stage exists to answer and a name does not answer it. Measured
on a three-solid STEP of 10x8x3, 6x6x6 and 20x2x2 mm boxes: 240.0, 216.0 and 80.0 mm3, every
volume exact.

MESHED ONCE, DRAWN TWICE. A big STEP takes 10-25 s to tessellate, and the welded skin and the
separate parts are the same triangles, so `write_view_stl` was split: `view_shape` meshes and
returns the shape, `write_meshed_stl` writes it. One solid gets none of this and is drawn exactly
as before, which is every part in the corpus.

### A mouth already on a face could not get a port

The port cutter was built for the plate fed and drained through its lid and could not be reached
from it. `render_port_patch_dicts` cuts a patch out of a block face so two ports can share one -
and the only way in was `stubs_for`, which returns a stub for a mouth that stops INSIDE the metal.
A mouth already ON its face needs no stub, so it got none, so it got no port row, so no patch was
cut. Driven through the console end to end:

    conjugate: meshing without named ports - the coolant reaches no opposite pair of block faces
    ... It reaches zmax. Name a pair yourself with --inlet/--outlet, or cut ports into the coolant
    geometry ...

Every clause is true and the part is still unsolvable. "Cut ports into the coolant geometry" is
advice to go and edit the CAD when the engine can cut them itself.

A flush mouth is now a Stub of zero length - nothing to draw, nothing added to the coolant, only a
patch to cut - and it is emitted ONLY when the faces reached hold no opposite pair, which is
exactly the case `plan_ports` refuses. Eight of the nine parts in the package take their ports from
renamed faces on an opposite pair and are untouched; so is a bore that stops inside the metal.

### Two sentences that said the wrong thing

| | |
|---|---|
| the machine | "It needs about 107 GiB, more than the Cloud Run mesh job's 32 GiB, so it runs on a n2-highmem-8 VM (64 GiB)." Nothing was wrong with the choice: a VM gets a real disk sized from `workspace_gib`, so the files it writes are not in its memory, and only Cloud Run holds both. The sentence quoted process-plus-files beside a machine chosen on process alone, so a correct decision read as a doomed one |
| two mouths on one face | said "the only way two of them on one face can carry different names", which does not cover one on zmax and one on xmax - the same problem for the same reason |

### 336 million cells, and what the merge costs

The Corintis plate at 22.00 um with its cells stretched 3x, eight ranks on mesh-bench (8 vCPU,
251 GB):

| | |
|---|---|
| octree boxes | 336,223,062, against a per-process ceiling of 178,956,970 |
| cartesianMesh | ExecutionTime 1744.7 s, ClockTime 2374 s |
| peak memory | 212 GB of 251, swap never touched (64 GB of it was there as insurance) |
| per rank | 23 to 27 GB, 8 ranks |
| octree nodes | 43,153,353 in one rank, where "Resizing points!" is the step that overflowed serially |

THE MERGE IS THE EXPENSIVE HALF, and most of what it spends is wasted here.
`reconstructParMesh` writes the merged mesh first - that part finished and `constant/` came to
69 GB - and then walks the processor directories again writing `pointProcAddressing`,
`faceProcAddressing`, `cellProcAddressing` and `boundaryProcAddressing` into each one. Those are
the maps between the decomposed and the merged mesh, for carrying FIELDS across. A mesh-only run
has no fields, and `build` deletes the processor directories as soon as the merge exits, so every
byte of it is written to be thrown away. It took longer than the mesher.

There is no flag to skip it: `-addressing-only` is the opposite option, writing the addressing
without the mesh. What does skip it is `redistributePar -reconstruct -overwrite`, which merges in
parallel and writes no addressing at all. Not swapped in yet - the serial path is the one that has
been measured end to end - but that is the next thing to measure on this part.

### The ladder offered an engine that cannot read the file

Driving the coupon plate end to end, the conjugate engine crashed, and:

    node_engine_fallback: cfmesh_conjugate could not mesh this shape (engine: engine_crashed) -
    attempt 2 moves to snapgrid, which delivers the same approved mesh

    ValueError: no ECXML model beside source.step - the snap-grid mesher reads the ECXML file
    itself

snapgrid reads ECXML and nothing else, and `EngineSpec.admit` has refused the mismatch since
snapgrid was written. `ladder` calls `admit`. The check fires only when the facts NAME the
upload's format, that name lived in `surface_analysis`, and `surface_analysis` comes from STAGING
the surface - which is exactly what the wrong format breaks. snapgrid's `tessellate_to_stl` threw
inside `prepare_surface`, `geometry_admission` swallowed it as "a staging hiccup is a system
issue - defer, never reject", and the run reached the builder and threw again, as a crash rather
than a refusal.

The format is a property of the FILE. `engine_fallback.source_format` reads it off the snapshot's
own name, so the check fires before anything is staged: a STEP now ladders
`cfmesh_conjugate, snappy_multiregion` and an ECXML still ladders snapgrid. Read off the RAW
mapping rather than through `GeometrySourceRef`, which raises on a partial snapshot - and this
code runs after an engine has already failed, where an exception takes the fallback with it.

### Two openings, and they were the wrong two

The coupon's brief came out as

    the `inlet` patch, which is 7.82 mm across and located near (4, 3, 0) mm, and exits through
    the `outlet` patch, which is 7.71 mm across and located near (4, 3, 2) mm

The coupon is 8 x 6 x 2 mm and its bores are 0.8 mm. 7.82 and 7.71 mm are the equal-area
diameters of the plate's two LARGEST FLAT FACES, top and bottom.

`map_openings` replaces the face reading with the inside-space map's mouths only when the faces
gave FEWER THAN TWO openings. On the full plate the faces gave none, so the two bores were
promoted and the stage showed them at 1.35 mm. On the coupon the faces gave two - zmin and zmax,
both rim-free flats on an extremity - so it returned early and the bores were never looked at.

The inside-space map had them: `has_inside` true, one passage, two mouths. The count of face
openings is not what should decide between the two readings - whether a face is where the coolant
LEAVES is, and the map is the thing that knows where the mouths are.

So the faces keep their openings when they LINE UP with the mapped mouths, within twice a mouth's
own width, and are replaced when they do not. A pipe whose two ends genuinely are faces keeps
them, measured exactly off the B-rep, which is the point of preferring faces at all; the coupon's
two big flats do not line up with anything and are put aside, with the reason said. Re-read after
the change:

    input_kind solid-assembly   flow internal
    openings   inlet 0.64 mm2   outlet 0.64 mm2        (a 0.8 mm bore, not a 48 mm2 face)
    note: the part's faces gave 2 opening(s) that are not at the mouths of its inside passage,
          so the map's mouths are proposed instead

### The engine was never in the image that runs it

The whole chain reached the mesher and then:

    rc=-4: [DISPATCH] unknown engine 'cfmesh_conjugate' - known: ['cfmesh', 'gmsh', 'snappy',
    'snappy_multiregion', 'vmtk']. The mesh image is likely stale; redeploy

It is not stale, it is from before. The Cloud Run job `hexera-mesh` pins
`sha256:0c46c0757da9...`, pushed 2026-09-13, and this engine was written in October. So every
conjugate job driven through the console has been failing at dispatch for as long as the engine has
existed, and the bench runs never saw it because the bench mounts the source over the image with
PYTHONPATH.

`make mesh-deploy COMPONENTS=images` is the authority for the job's image and it is the wrong
instrument here twice over: it PROMOTES a digest from `deploy/output/release.json` rather than
building one, and `hexera-mesh` is the job Areen's main uses.

So the semi stack has its own:

| | |
|---|---|
| image | the branch's wheel installed over `mesh:ff752ca1df02` - the SAME native floor, a new Python package |
| why not the `mesh` target | it builds OpenFOAM 2412 from the ESI apt repository, vmtk through conda and fTetWild from source: ~2 GB, slow, and the Dockerfile calls the ESI mirror "the least reliable thing in this build". None of it moved on this branch |
| `USER root` for the install | as the image's own user pip says "Defaulting to user installation because normal site-packages is not writeable" and the wheel lands in the build user's home, which is not necessarily the user Cloud Run runs as |
| `--no-deps` | nothing under requirements/ moved, so resolving the tree again could only move a pin the floor was built against |
| the job | `hexera-mesh-semi`, rendered from the project's own `deploy/gcp/cloud-run/mesh-job.yaml`, digest-pinned, same service account, maxRetries 0 |
| 32Gi, not 16Gi | `deploy/gcp/generated.prod.env` says `MESH_MEMORY=32Gi` and `settings/policy.CLOUD_RUN_GIB` prices against 32. The live `hexera-mesh` is at 16Gi, so the code has been sizing jobs against twice the memory that job has |

NOTHING OF THE SHARED DEPLOYMENT IS TOUCHED, and that was checked rather than assumed: a tag in
the same repository cannot reach a job that pins a digest, and `hexera-mesh` was read back
afterwards still on `sha256:0c46c075...`.

The image was verified before it was pushed - `cfmesh_conjugate` in `all_engine_names()`, the
octree ceiling and the MPI flags importable, the solver case's blank token present - because an
image whose only symptom is "unknown engine" is one nobody checks until a customer's job dies on
it.

### Two sentences about ports, both of them false at the time they were printed

Driving the coupon through the console, one run said all three of these:

    conjugate: meshing without named ports - the coolant reaches no opposite pair of block faces
    conjugate: 2 mouth(s) already lie on a block face ... so they are CUT out of their faces
    conjugate: 2 stub(s) added 0.0000 mm3; coolant_in and coolant_out are cut out of zmax
    conjugate: the mesh has no named inlet or outlet: the coolant reaches no opposite pair ...

The first and the last are the same claim, printed either side of the thing that disproves it. The
first was logged inside the `except` around `plan_ports`, forty lines before the mouths are read -
so it was written before the answer existed. It is now carried and said once, at the end, where
`port_patches` is known, and it is not said at all when the ports were cut.

### One flush mouth is not a flow path

The first version of the flush-mouth rule cut a port out of any face the mouths reached when no
opposite pair was there - including a part with ONE flush mouth. `port_rows` already says what is
wrong with that ("a solve needs two"), and an inlet with no outlet is as unsolvable cut as uncut,
so the cut was work and risk for nothing. The bar is BOTH_ENDS, and the row that caught it was an
existing one: `test_a_mouth_already_flush_with_a_face_is_left_alone`.

### The split does not overrule a wall

The cooled-solid reading was set for every one-solid part whose coolant the split found, and
`test_one_solid_keeps_the_reading_it_had` caught the pipe tee at once. A part the scout read as
`pipe_wall` is metal AROUND a fluid and its ordinary case is the fluid alone; reading it as
`solid-assembly` would impose a two-region case on the commonest internal-flow part there is. The
stage already OFFERS the cooled-solid kind for it when mouths were found (`_kinds_on_offer`), which
is the right strength for a part that could be either.

`single_solid` - a solid body with coolant found inside it - is the cold plate, and that one is a
conjugate case whatever else is true.

### One seam, because the tests fake it

Splitting `write_view_stl` into `view_shape` and `write_meshed_stl` split the seam four of the
geometry check's own tests patch: they replace the drawing step with a box so the check can run on
a file that is not really CAD, and with two functions to patch the real reader ran and refused the
fake ("the file could not be read as CAD (geometry.step)"). `draw_view` is the one seam - the skin
written and the meshed shape handed back - so a test that fakes drawing has one thing to fake.

### 336,666,582 cells, split in two

The Corintis plate at 22.00 um with its cells stretched 3x, eight ranks on mesh-bench, the whole
chain through to the split:

| | cells | points | faces |
|---|---|---|---|
| fluid | 66,764,096 | 80,080,797 | 213,315,900 |
| solid | 269,902,486 | 286,222,005 | 826,279,918 |
| total | **336,666,582** | | |

Against 336,223,062 octree boxes, so BOXES_PER_CELL at 1.0 is right to 0.13% on a part at this
size - the figure was fitted on a 99M-box run.

THE INTERFACE IS 21,637,041 FACES ON EACH SIDE. Equal, as it has been on every part this engine
has meshed, and that is the measurement the whole approach exists to produce. It holds at 336
million.

| stage | |
|---|---|
| preparePar | 8 processor directories |
| cartesianMesh, 8 ranks | ExecutionTime 1744.7 s, ClockTime 2374 s, peak 212 GB of 251 |
| reconstructParMesh | ~75 min, and most of it the per-rank addressing nothing reads (see above) |
| topoSet | read the merged 42.6 GB `faces` file in ONE process and finished |
| splitMeshRegions | ~78 min, 199 GB peak |

TOPOSET AND THE SPLIT RAN SERIALLY ON 336 MILLION CELLS AND NEITHER OVERFLOWED, which was the open
question behind "reconstructParMesh ... is the step to skip": the octree's twelve labels per box do
not have an equivalent in the merged polyMesh, whose faces are written and read as a stream rather
than indexed through one flat array. So reconstruct-then-serial is a real path at this size, and
not only the parallel-all-the-way one.

THE DISK IS THE LIMIT NOW, not the labels and not the memory. ASCII polyMesh at 336M cells is 71.6
GB merged (`faces` alone is 42.6 GB) and the two regions are another 44 GB beside it. The eight
processor copies were deleted by hand mid-run to keep the split's write from running out - which is
what `build` does at that point anyway, and the reason it does.

### Four fifths of it is air

The solid region is 269,902,486 of those 336,666,582 cells: 80%. The block is the part's bounding
box, 750,015 mm3, and the plate itself is 276,324 mm3 - so most of those cells are the air around a
cold plate, meshed as metal, carrying its thermal mass into a conjugate solve.

Measured at scale this is no longer an efficiency note. The same coolant resolution on the part's
own fused skin is about 130 million cells rather than 336 million, which is the difference between
a mesh that needs eight ranks and one that fits a single process.

What stands in the way is not the mesher: cfMesh fills a closed surface, and the part's skin is one.
It is that `plan_ports` names an inlet and an outlet from the BLOCK's six faces, and a part meshed
to its own skin has none. The port CUTTER is the mechanism that does not need them - it narrows a
face to a box round a mouth - and after this session's work every mouth on a real skin is flush with
the boundary by construction, which is the case it now handles. So the two pieces exist; what is
missing is the reading that says which patch a cut port is cut OUT of when there is no block.
