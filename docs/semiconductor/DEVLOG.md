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
coolant and the metal go, and that token is not a NUMBER: an unfinished case stops on
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

### The box is not a surface you can simply swap

The metal region is 80% of a 336M-cell mesh and most of it is air, so the obvious move is to hand
cfMesh the part's own skin instead of its bounding box. Measured on the coupon before writing any
of it, same cell size both ways:

| surface | cells | fluid | solid | split |
|---|---|---|---|---|
| the bounding box | 1,500,000 | 495,506 | 1,004,494 | clean, `zmax` and `fluid_to_solid` on the coolant |
| the part's own skin | 1,037,018 | **256** | 1,036,762 | clean, and WRONG |

cfMesh meshes the part's skin perfectly happily - 1,037,018 cells, "Mesh OK.", 117,274 boxes
discarded outside it - and the coolant comes out as 256 cells instead of 495,506.

THE REASON IS THE SKIN ITSELF. A cold plate's skin is the boundary between the metal and the air
AND between the metal and the coolant: the channel walls are part of it. So the closed surface
bounds the METAL ALONE, and cfMesh fills exactly that. The coolant is on the other side of it - a
cavity, outside the domain - and was never meshed, so there are no shared faces to split along and
no conformal interface to have.

Which is what the box is FOR, and the engine's own header has said so from the first line: "Give
it ONE manifold domain - the outer block of the whole part - and let it build a single octree
across metal and coolant alike. Then cut the finished mesh in two." The box is not a lazy envelope.
It is the thing that makes both regions one mesh.

WHAT WOULD ACTUALLY WORK is the part's outer boundary with its cavities FILLED - the solid plus the
coolant as one closed volume - which is a smaller domain than the bbox without being the metal
alone. On the coupon that is no saving at all, because the coupon's outer shape IS a full
8 x 6 x 2 mm slab and filled-part equals box to within a rounding error. On the first customer's
plate it is the whole saving: the bbox is 750,015 mm3 because the FITTINGS stick out of it, the
part is 276,324 mm3, and the part plus its ~52,140 mm3 of coolant is about 328,000 mm3 - so 2.29x
less domain, and all of it air around the fittings rather than air inside the plate.

It cannot be taken off the skin by connectivity, either: the bores join the channel walls to the
outer surface, so a cold plate's skin is ONE connected shell and there is no inner piece to drop.
Capping the mouths first is what separates them, and `cad/cad_tessellate.tessellate_internal` and
`cad/bore_staging` already do exactly that for the hollow-wall case ("a cap over each WHOLE mouth,
so the cavity is sealed").

So the work is: cap the mouths, take the outer shell of what that closes, mesh THAT, and cut the
ports out of its own patch rather than out of a block face - which is the one piece that already
exists, because a cut port needs no six named faces. Not started. The measurement above is why it
is not a one-line change, and it cost twenty minutes instead of a day.

### The mesh was built and refused for what the ports were called

The coupon plate went the whole way through the console: the stage read it as a cooled solid with
its two 0.9 mm bores, the intake proposed the conjugate engine, the ports were cut out of the lid,
the mesher ran on the Cloud Run job, the workspace came home - and then

    failed_gate     patch_contract
    failure_detail  The mesh has no boundary called 'inlet' or 'outlet' (it has 'coolant_in' and
                    'coolant_out'); the mesh has 'coolant_in' and 'coolant_out', which you did
                    not approve, so the mesh does not match what you approved. This is our
                    mistake, not your geometry's.

Nothing was wrong with the mesh. `port_rows` names the first mouth it measured `coolant_in`, says
out loud that which end is fed is a guess, and its own docstring has said from the day it was
written that "A declared pair of names from intake overrides it". The one caller never passed
them. The gate was right and the message it wrote about itself was right.

BOUND BY POSITION, NOT BY ORDER. The declaration carries a `near_mm` for every port - it is what
the user confirmed on the stage - and the stubs carry the mouths the coolant's own surface has.
The two lists are measured by different code on different surfaces, so nothing promises they come
out in the same order, and an inlet and an outlet swapped is a case that solves and runs
backwards. `declared_for` matches each mouth to its nearest declared port within two mouth
diameters and takes that port's name AND its role.

ALL OR NOTHING. A declaration that places only some of the mouths is refused whole and the engine
keeps its own names, because half the patches under the user's names and half under ours is worse
than all of ours: a reader of the delivered case cannot tell which half is which.

On the run after the fix:

    conjugate: the ports are cut under the names declared at intake, matched to the mouths by
    position: inlet (inlet), outlet (outlet)
    conjugate: 2 stub(s) added 0.0000 mm3 to the coolant; inlet and outlet are cut out of zmax,
    0.903 mm and 0.903 mm across.

### What the 336M run actually delivered

| | |
|---|---|
| cells | 66,764,096 fluid + 269,902,486 solid = **336,666,582** |
| interface | 21,637,041 faces, both sides, equal |
| fluid volume | 5.51626e-5 m3 against 5.3661786893e-5 expected: 2.8% high, inside the 5% tolerance |
| fluid cells | 65,290,108 hexahedra + 1,473,988 polyhedra |
| cell sizes | 29.22 um .. 521.93 um |
| quality | max non-orthogonality 48.65 (avg 6.21), max skewness 1.66, 0 skew faces, 0 small-determinant cells, checkMesh OK on both regions |
| peak | 257,691,721,728 bytes = 240 GiB, on a 251 GB box |

AND IT IS REFUSED, by the engine's own verdict:

    fluid is in 5 disconnected piece(s), expected 2. Islands mean the cells are too coarse to hold
    the passage together; this mesh will not solve even though checkMesh calls it OK.

So the stretch of 3 along the channels is too far. The window is narrow and now bounded on both
sides from measurement: stretch 6 was refused for the per-process octree ceiling (181,673,384 cells
against 178,956,970) and stretch 3 fragments the coolant. With the ceiling lifted by the ranks,
stretch 6 is buildable and is the rung `plan_two_scales` itself picked as fitting, so it is the run
to do - not a coarser one.

### The mesher is the fast part

| stage | seconds |
|---|---|
| preparePar | 0.6 |
| cartesianMesh, 8 ranks | 2,399.6 |
| reconstructParMesh | 4,518.4 |
| topoSet | 2,240.2 |
| splitMeshRegions | 6,073.3 |
| checkMesh fluid | 654.3 |
| checkMesh solid | 1,903.3 |

Four hours and fifty minutes, of which the MESH is forty minutes. Everything after it runs in one
process on the merged mesh, and `reconstructParMesh` plus `splitMeshRegions` alone are 2.9 hours -
4.4x the mesher they are serving.

Two of those are addressable and both have been named already: the merge spends most of itself
writing per-rank addressing nothing reads (`redistributePar -reconstruct` writes none), and the
split's 6,073 s is a single process walking 336 million cells. The third, topoSet at 2,240 s, is
`surfaceToCell` testing every cell centre against the coolant surface, which has a -parallel of
its own.

So the parallel chain's next question is not whether the mesher scales - it does - but whether the
THREE SERIAL STAGES after it should stay serial. They were kept serial deliberately, because the
ceiling belongs to the octree and nothing downstream shares it, and that reasoning is still right
about correctness. It is wrong about time.

### A claim of mine that was not true

"a token OpenFOAM cannot parse", said of the solver case's blanks, in the module, its tests and
this log. Checked on 2026-10-29 by running every file the writer produces through `foamDictionary`
in the mesh image: all twelve parse, blanks and all. A dictionary is a stream of tokens and
`<<HEXERA_FILL_THIS_IN>>` is a legal word in one; `foamDictionary` even reads the coupled condition
straight back with the blank sitting in its `value`.

What refuses it is the READ. `Istream >> scalar` on a word is a FOAM FATAL ERROR, so the case stops
when a field is read rather than when it is parsed - later than claimed, still before anything is
solved, and all the mechanism needs. But the two are different claims and only one of them is
true, so the wording is now the true one everywhere it appears.

### The stretch does not save what the model says it saves

Two runs of the same plan at the same cell size, differing only in the stretch along the channels:

| stretch | estimate (margin off) | internal boxes BUILT | ratio |
|---|---|---|---|
| 3 | 271,705,176 | 336,223,062 | 1.24x |
| 6 | 145,699,361 | 279,621,370 | 1.92x |

`two_scale_estimate` divides the FINE band by the stretch factor and leaves the background and the
broad band alone, which is right in principle and said so in its own comment ("The background is
deliberately NOT divided ... that makes this an over-estimate, which is the safe direction"). It
is not an over-estimate here. It is 1.24x to 1.92x UNDER, and it gets worse the more the cells are
stretched, because the term it divides is not the one that dominates.

Fitting the two measured points to A + B/S:

    A + B/3 = 336,223,062       A = 223,000,000       the part no stretch touches
    A + B/6 = 279,621,370       B = 339,600,000       the fine band at S = 1

So about 223 MILLION CELLS OF THIS PART ARE NOT IN THE STRETCHED ZONE at this cell size, and no
stretch reaches them. At S = 8 the whole mesh is still 265M; the floor is A. Stretching from 3 to
6 - doubling it - took 17% off the box count, not half.

WHAT THIS CHANGES. The earlier measurement that "scale > 1 stretches along the axis and the cell
saving follows the factor" was taken on a 10 mm box whose every cell was inside the stretch zone,
and it is true of that box. On the customer's plate the stretch zone is the microchannel field and
the rest of the mesh - the manifold loop, the grading shells around it, the background filling the
block - is the 223M. The factor is right about the cells it reaches and says nothing about how many
of them there are.

It also explains the earlier arithmetic that looked wrong: a stretch of 6 was planned at
181,673,384 cells and died on the per-process octree ceiling at 231 s, which the ceiling explains
on its own - but the plan it died on was already an estimate 1.9x under what it would have built.
The configuration was never 181M. It was 280M.

AND IT MEANS THE BOX MATTERS MORE, NOT LESS. 223M cells that no stretch can reach is, by the
measurement two sections up, 80% metal - and most of THAT is air in the bounding box around the
fittings. The term the model cannot divide is the term the box fix removes.

### The review was reading the blank solver fields instead of the mesh

A conjugate mesh that passed every gate came back inconclusive, twice, on two independent runs:

| run | terminal status | what the review opened |
|---|---|---|
| 51d5481d | delivered with the review inconclusive (reviewer_evidence_missing) | `patches: ['solid']` |
| e4b9abfe (Cloud Run jwtzw) | same failure in the log | `XMLPolyDataReader(.../fluid/cr_ws_iewyhoz1_1/boundary/outlet.vtp) raised an error` |

The coolant region was built, measured and correct in both. The only trace was one warning:
`vtkXMLPolyDataReader: Error parsing input file`.

It was the solver case. `write_solver_case` writes the delivered case's time 0 with
`<<HEXERA_FILL_THIS_IN>>` where every physical value belongs. `foamToVTK`'s default time range is
every time, so `-constant` exported `constant` (the mesh), went on to export `0` (the blanks) and
died on them, leaving a half-written time directory with no `.vtm` beside it. `_newest_time` picked
that one, because it was the newest.

Measured in the mesh image on a 2x2x2 blockMesh with one volScalarField at time 0:

| flags | field at time 0 | rc | directories written |
|---|---|---|---|
| `-constant` | readable | 0 | `tc_0  tc_0.vtm  tc_1  tc_1.vtm` |
| `-constant -noZero` | readable | 0 | `tc_0  tc_0.vtm` |
| `-constant` | blank | 1 | `tc_0  tc_0.vtm  tc_1` |
| `-constant -noZero` | blank | 0 | `tc_0  tc_0.vtm` |

Row three is the delivered tree exactly: `cr_ws_tghpvgh4_0` with its `.vtm` and a 70,361-byte
`inlet.vtp` ending in `</VTKFile>`, and `cr_ws_tghpvgh4_1` with no `.vtm` and a 65,539-byte
`inlet.vtp` ending mid-base64.

Two fixes, and they are not the same fix. `-noZero` means the field time is never exported, so the
broken directory is never written. `prefer_finished` means a time directory with no `.vtm` loses to
one that has it, so a timeout or a full disk or a kill cannot put a truncated export in front of
the reader either. It prefers rather than requires: made a precondition the rule would cost a
readable export its surface, and two existing tests build exactly that tree.

A dropped region also says so now. `if not tags: continue` in `build_region_msh` is why nothing in
the log an operator reads contained the word "fluid".

### A two-region result never raised the archive ceiling

Live, on the coupon, after the mesher was taught to ask the right authority:

    return scope: The mesh was too large to send back whole within the transfer limit (763 MiB),
    so the volume export the reviewer slices was left out (330 MB)

763 MiB is `CELL_HARD_LIMIT x RESULT_BYTES_PER_CELL` with the cell count unread. The count was
there: 2,520,586 in the coolant and 4,061,885 in the metal. `result_extraction_limits` looks for it
under `quality.cells`, and a conjugate result has no `quality` block - the runner returns
`{"ok", "problems", "notes", "regions": {...}}`, because a two-region mesh has no single set of
quality numbers.

| result | archive ceiling |
|---|---|
| none | 763 MiB |
| `quality.cells` 6,707,503 | 1,279 MiB |
| `regions` 2,520,586 + 4,061,885 | 1,256 MiB (was 763) |
| `regions` unreadable | 763 MiB |
| `regions` 100 x 10^12 | 3,052 MiB, clamped at 2 x CELL_HARD_LIMIT |

The coupon now reaches a verdict instead of an evidence gap: two runs on the fixed ceiling came
back "delivered with the review's concerns listed - every gate passed and no finding makes it the
wrong problem", with `concerns=['passage_resolution']`.

### The heatmap skipped the worse half of the mesh

It measured the coolant alone, on the grounds that the metal is untouched background. The engine's
own checkMesh says the opposite about the coupon, in the same report:

| region | max non-ortho | avg | max skew | max aspect |
|---|---|---|---|---|
| fluid | 15.8181 | 1.51736 | 0.295526 | 1.61317 |
| solid | 26.9835 | 2.03453 | 0.513708 | 2.4443 |

The metal is worse on every one, and on the 336,666,582-cell plate it holds 27,932 of the 28,132
poor cells. Both regions are read now and merged into one `quality_fields`: arrays union, hotspots
concatenate, each metric keeps the worse max and `scale_to` and the sum of `n_over` and `n_faces`.

Two more things follow from the same place. The poor-cell sets are drawn, at full resolution, after
the drawing budget rather than inside it - `checkMesh -writeSets vtk` writes one `.vtp` per failed
check (measured: 4 cells of aspect ratio 2500 gave 18 points and 16 polys), so a few thousand faces
draw exactly whatever the rest of the mesh had to give up. And on a mesh whose cells were merged for
drawing the figures and the hotspots now ship without the arrays, where before both were dropped:
that plate's coolant side is 13,022,400 faces against a budget of 800,000, so every delivery of it
had no quality view of any kind.

### The box the mesher fills is three times the part, and the boolean that would fix it does not come off

Measured on the real customer assembly (`ColdPlateAssembly_CLEAN.step`, 6 solids, 15.9 MB):

| | volume | share of the bounding box |
|---|---|---|
| bounding box, 177.51 x 47.21 x 101.01 mm | 846,543 mm3 | 100% |
| the six solids | 222,662 mm3 | 26.3% |
| the coolant, 2 passages | 53,662 mm3 | 6.3% |
| metal + coolant | 276,324 mm3 | 32.6% |

The conjugate engine hands cfMesh one manifold volume and cuts the two regions out of it
afterwards, and the easy manifold is the cuboid. Two thirds of every cell in that mesh is air
outside the part, at the background size. By the stretch measurement above, 223 million of that
mesh's cells sit outside any stretch zone and no refinement setting reaches them; this is the term
that does. The box is 3.06x the domain.

THE PART ON ITS OWN IS NOT THE DOMAIN. A cold plate's boundary is one closed surface that runs
over the outside, dives in at the inlet, winds through the channels and comes back out at the
outlet, so a mesher handed it fills the METAL: measured on a part skin given to cfMesh, the fluid
region came back at 256 cells against 495,506. Fusing the coolant back in caps each mouth with the
coolant's own end face and leaves exactly the outer skin, enclosing metal and coolant together.

That works on the 2-channel cold plate fixture: 7.04 mm3 of metal and 0.96 of coolant fuse to
8.00 mm3 as one solid, one closed piece, no open rims. It is also the negative control, because
that plate is a plain cuboid and fills its own box - `share_of_box` 1.0, so there is nothing to
build.

ON THE CUSTOMER ASSEMBLY THE FUSE IS WRONG, three ways:

| glue mode | time | result |
|---|---|---|
| none | 816.6 s | -11.4752 mm3 in a 1,118 mm3 box |
| `BOPAlgo_GlueShift` | 142.2 s | identical, 5.7x faster |
| `BOPAlgo_GlueFull` | 81.9 s | 2 solids, 0.0 mm3, 24,310 mm3 box |

A 1,118 mm3 bounding box out of a part whose own box is 846,543: the result is a fragment. Glue is
a speed win and not a correctness fix - GlueShift stays because it gives the same answer 5.7x
sooner, and GlueFull asserts that nothing really intersects, which is too strong and hands back
the same shell twice with opposite orientations, hence the exact 0.0.

TWO REAL DEFECTS CAME OUT OF LOOKING. `BRepBuilderAPI_MakeSolid` does not require a closed shell,
so an open shell comes back as a "solid" measuring whatever its open surface happens to enclose:

    passage 0   9331 faces   1 shell   free edges 0   solid 52,144.6 mm3   boundary said 52,144.6
    passage 1     14 faces   1 shell   free edges 2   solid    933.4 mm3   boundary said  1,517.2

Fusing that second one is what took 346.8 s to produce a zero-volume bounding box on the first
attempt. A shell has to close now, and a passage that will not close is skipped and named. And
`write_domain` returned a bare None, which lost the reason: it runs in a time-boxed child process
whose stdout is read and thrown away unless it times out, so the first customer run came back
`"domain": null` after 1,001 s with nothing anywhere saying why.

WHERE IT STOPS. The gate refuses anything that is not one closed piece, so the engine keeps the
cuboid and nothing is worse than before. The route that does not go through a boolean is to take
the metal's own closed tessellation and flood-fill its outer skin with the mouth rims as barriers,
then add the caps. The rims are chains of mesh edges because a CAD tessellation respects face
boundaries, so that needs no tolerance against the 98 um walls - which is what defeats matching
the metal and coolant tessellations against each other, since the tessellation deflection at
1/2500 of a 210 mm diagonal is 0.084 mm, itself thicker than the walls. Not attempted.

### The three stages after the mesher are 4.4x the mesher, and they do not have to be serial

On the 336,666,582-cell customer build:

| stage | time | processes |
|---|---|---|
| cartesianMesh | 2,399.6 s | 8 ranks |
| reconstructParMesh | 4,518.4 s | 1 |
| topoSet | 2,240.2 s | 1 |
| splitMeshRegions | 6,073.3 s | 1 |

The mesher is 40 minutes of a 4h50 run. The three stages after it, all in one process, are 4.4x
that. They were serial on purpose: the ceiling that forces the mesher onto ranks is cfMesh's
octree addressing and nothing downstream shares it, so the block was merged the moment it existed.
Right about correctness, wrong about time.

The alternative is to stay decomposed to the end and merge the two REGIONS instead of the block:

    preparePar
    mpirun cartesianMesh    -parallel
    mpirun topoSet          -parallel
    mpirun splitMeshRegions -parallel
    reconstructParMesh -constant -allRegions

Measured both chains against each other on the coupon's own block and coolant, 4 ranks:

| chain | fluid cells | solid cells | interface faces | fluid max non-ortho | solid max non-ortho | checkMesh |
|---|---|---|---|---|---|---|
| serial, today | 29,812 | 29,262 | 22,534 | 16.2739 | 35.3268 | OK |
| parallel to the end | 29,813 | 29,261 | 22,534 | 16.2739 | 35.3442 | OK |

The interface is 22,534 faces on both sides in both chains, which is the conformality the whole
approach rests on, and the total is 59,074 either way. ONE CELL moves between the regions, because
`topoSet surfaceToCell` classifies a cell on a processor boundary differently from how it
classifies it with the mesh whole. That is a real difference and not a rounding - the parallel
chain is not bit-identical - and it is one cell in 59,074 on a boundary the two regions share
exactly.

TWO THINGS HAD TO BE HANDLED. `splitMeshRegions` writes each region's `system/<region>`
dictionaries beside the mesh it split, which when it runs decomposed is inside `processor0` and not
at the root - so the reconstructed case has `constant/fluid/polyMesh` and no
`system/fluid/fvSchemes`, and `checkMesh -region fluid` exits on the missing file. They are copied
up with `cp -rn`, no-clobber, because the port cut writes its own `topoSetDict` into the same
directory before the chain runs.

And the root block mesh is never reconstructed now. Nothing downstream wants it: the viewer refuses
to read it on purpose (it carries both regions' face counts added together, so a viewer pointed
there draws a mesh that was never delivered), and `foamToVTK -constant -noZero -noFaceZones
-allRegions` was measured on a case without it - rc 0, both regions' `internal.vtu`, 9 boundary
files, the same as with it.

On this coupon the parallel chain is slower, as expected: cartesianMesh 8 s serial against 52 s on
4 ranks, because 52,026 octree boxes is all MPI overhead. The figure that matters is the 12,831 s
of serial tail on the part that needs ranks, and that is not measured yet - it needs the big box.

### A reader who zooms in on a 98 um channel is shown merged cells

`polymesh_budget.fit_to_budget` clusters cells so a delivered mesh fits what a browser can carry.
The first customer plate delivers 13,022,400 boundary faces on the coolant alone against a budget
of 800,000 triangles, so about sixteen delivered faces become one drawn one. The payload says so,
and the zoom still lets a reader go right in - where the surface under their cursor is a merged
approximation of the cells rather than the cells. Every face count, size and quality figure they
are given is the mesh's own. The surface is not.

`render/detail_window.detail_at` is the answer, served at `/simulation/<job>/detail`: the same
reader, on the same delivered polyMesh, restricted to a ball around the point they asked about,
with nothing merged, and the count of what was in range beside it. Measured on the delivered
coupon (fluid 349,656 boundary faces, solid 375,594):

| ball | time | in range | returned |
|---|---|---|---|
| 0.3 mm, first ask | 8.05 s | 732 | 732, one patch |
| 0.3 mm, asked again | 0.04 s | 732 | 732 |
| 1.0 mm | 0.10 s | 23,223 | 23,223 over four patches |
| 4.0 mm | 0.51 s | 404,725 | 150,000, truncated |

Every patch's two buffers agreed with its declared face count with no index past its own points,
and `fluid_to_solid` and `solid_to_fluid` both came back 8,869 faces - the conjugate interface
showing up equal on both sides in the detail cut too.

Three things the measurements changed. It read the pre-split block: `splitMeshRegions` leaves it at
`constant/polyMesh` carrying every patch with the two regions' face counts added together, which is
why the viewer refuses to read it, and the first version of this did not - a 0.3 mm ball came back
with `zmax` 732 faces beside `fluid/inlet` 732, the first being a surface that was never delivered
standing in for the one that was. It was 28.1 s a request, because it computed a centroid per face
in a Python loop; one `np.add.reduceat` over the flattened connectivity took that to 8.0 s. And 8 s
is still not a click, so the compact form is kept - connectivity as one int64 array, points and
centroids as float64, about 70 MB for the coupon's two regions - keyed by the owner file's size and
mtime so a workspace rebuilt under the same path is read again.

What it refuses: a radius that is not a length, and a boundary past `MAX_SOURCE_FACES`, named,
because the read is the whole boundary (a face's position is not known until its points are, so
there is no way to seek to a neighbourhood) and a reader who asked for the coolant and was given
the metal would read the metal as the coolant. The customer plate's 13,022,400-face coolant side is
that case and needs an index built at delivery, which this does not do.

### The ceiling fix read two keys the result does not have

The commit that raised the archive ceiling for a two-region result was followed by a live run
reporting the same floor:

    mesh complete: {"rc": 0, ..., "cells": 6582471, ...,
                    "return_scope": {"ceiling_bytes": 800000000,
                                     "archive_bytes_est": 689484779,
                                     "left_out": [{"file": ".../internal.vtu",
                                                   "bytes": 330346080,
                                                   "archive_bytes_est": 101859643}]}}

The count was at the top, already added up: 6,582,471 is 2,520,586 of coolant plus 4,061,885 of
metal. `quality.cells` is a single-region engine's shape and `regions[*].cells` is the conjugate
RUNNER's shape, and by the time dispatch hands the result to `return_scope` it has been flattened
to a top-level `cells`. Neither key was there.

And the arithmetic is closer than it was written: 689,484,779 plus 101,859,643 is 791,344,422
against a ceiling of 800,000,000, so by the raw number it FITTED - the test asserting otherwise
failed and was right to. `return_scope.HEADROOM` is why it did not: the archive is kept to 0.9 of
the ceiling, making the real bar 720,000,000. With the count read the ceiling is 1,316,494,200 and
the bar 1,184,844,780, so the whole workspace rides home with 393 MB to spare.

### The viewer I fixed was not the viewer the stack serves

There are two copies of the viewer:

| file | bytes | who serves it |
|---|---|---|
| `ui/js/viewer/viewer.js` | 100,421 | the local stack and the API's /ui (docker-compose mounts ./ui at /srv/ui) |
| `apps/console/public/static/js/viewer/viewer.js` | 96,830 | the Next.js console |

Nothing builds one from the other and nothing checks they agree. After a full `make dev-build` and
`dev-up`, `/static/js/viewer/viewer.js` still carried the old code, because the change had gone
into the console copy. They have drifted 132 lines apart and `ui/` is ahead - it carries the
`drawn_as` handling for a multi-region engine, with a comment about this engine, that the console
copy does not have. Every conjugate fix has gone into `ui/` only.

### Every mesh image after the first shipped stale source under a current tag

The build script only built the wheel if one was not already there:

    [ -f dist/meshpipeline-0.1-py3-none-any.whl ] || { ... python -m build --wheel ... }

so once `dist/` held a wheel from any earlier run, every build after that installed THAT wheel and
tagged the image after the current commit. Measured: the image tagged `semi-32417451` had no
`NO_FIELD_TIME` and no `prefer_finished`, committed two and three commits before it.

Two live end-to-end runs reproduced bugs that were already fixed, and a good deal of diagnosis
went into a mystery that was a build cache. The third of them, job 575f76e6 on the stale image, is
also the clearest statement of what the time-0 export does:

    render backend (PyVista): loaded .../mesh.msh - patches: ['solid']
    render backend: loaded volume .../VTK/fluid/cr_ws_n41vtoe6_1/internal.vtu (0 cells)

Both halves of the reviewer's evidence came from `_1`, the time-0 directory. The boundary was the
truncated one, so the coolant's patches did not parse and the review opened on the metal alone.
And the VOLUME was the time-0 export too - **zero cells**. It was not that the volume was missing;
when it came home it was empty, and the reviewer sliced nothing. That is a third consequence of
the same bug, and it is why an earlier run reached a verdict on a volume that held no cells.

`dist/` is removed and the wheel built every time. The image's smoke test now looks for things the
newest commits added rather than only that the package imports:

    OK   export excludes the field time
    OK   a truncated export is not believed
    OK   the reported cell count raises the ceiling
    OK   the tail goes parallel

and exits "this image is STALE - the wheel does not match the source" if any is missing. A tag is
a promise about the source and the only way to keep it is to look for something that was not
there before. Both scripts moved out of a scratchpad into `devtools/semiconductor/`, because a
build whose only copy is in a session directory is one nobody else can reproduce.

### A correct conjugate mesh came back PASSED on a review that saw neither of its regions

Job 575f76e6, live, on the stale image:

    render backend (PyVista): loaded .../mesh.msh - patches: ['solid']
    render backend: loaded volume .../VTK/fluid/cr_ws_n41vtoe6_1/internal.vtu (0 cells)
    Reviewer: eligibility accepted - verdict=passed rounds=8 blocking=[] improve=[] concerns=[]
    Reviewer: verdict=PASS tool_calls=10

The mesh was fine: 2,520,586 fluid cells, 4,061,885 solid, both passing checkMesh, interface
346,456 faces on each side. The review saw the metal's boundary and nothing else, and sliced a
volume that held no cells, and passed it with no concerns.

WHAT THE VOLUME ACTUALLY WAS. Not empty. **Truncated.** 197,566,474 bytes whose own header
declared `NumberOfPoints='2777505' NumberOfCells='2520586'`, 6,718 bytes short of its closing
tags, ending mid-base64. The complete export beside it is 197,573,192 bytes and reads 2,520,586
cells. `pv.read` does not raise on the short one: it warns and returns `n_cells=0`. So the header
lies, there is no cheap check, and the read's own count is the only honest signal - which was
being logged and thrown away. `self._volume.n_cells` appeared exactly once in the codebase, inside
the log format string.

THE CHAIN, in the order it runs. Each link tests presence and the next one believes it.

| where | what it accepts |
|---|---|
| `review_surface.py:199` | publishes `mesh_paths.volume` on `is_file()` - no open, no count |
| `native.py:195` | `if groups:` publishes a two-region surface when one region made it |
| `review_session.py:105` | resolves the artifact on `is_file()` plus a 512-byte `<?xml` sniff |
| `render_inputs.py:76` | `has_volume = volume is not None` gates the region targets |
| `base.py:615` | drops REGION obligations on the manifest STRING while discovery uses the FILE |
| `eligibility.py:313` | "produced" is a string-identity test between two lists of ids |
| `backend.py:236` | reads `n_cells`, logs it, discards it |
| `backend.py:261` | `if sl.n_cells:` decides only whether to DRAW; the blank frame is returned |
| `render_runtime.py:276` | the tool text asserts the cut "exposed interior cells" regardless |
| `unified.py:255` | files `image_ok=True, covered=True` as LITERALS if a PNG path is non-empty |
| `review_session.py:214` | the only emptiness check in the whole path is the PNG's byte size |
| `spec.py:82` | the PATCH obligation is kind-only min 1, so one solid patch discharges "every renderable boundary patch" |

So "the interior was inspected" means "a PNG got written", and a blank 1280x960 frame clears the
byte-size screen by several KB. Eight rounds of blank cuts read as eight rounds of interior
inspection, and `concerns=[]` is the honest output of that ledger.

WHY THE TWO RUNS DIVERGED. 51d5481d lost its volume to pruning and the file was GONE, so
`has_volume` was False, the region ids were never advertised, and the string test found them "not
produced" - `reviewer_evidence_missing`, inconclusive, which is the honest outcome. 575f76e6 had
the file, so every presence test passed and the same blindness came out as a PASS. Absent evidence
refuses; empty evidence passes.

TWO CUTS CLOSE IT AT THE SOURCES.

A volume that reads as no cells is not a volume (`backend._load_volume`), and a cut that shows
nothing is a refusal and not a picture (`backend.inspect_region`, three ways: no readable volume, a
slice that raised, a slice of no cells). The reason rides in the return rather than on a new
property, so the backend's public surface stays the one
`test_the_public_surface_of_the_backend_is_unchanged_by_the_split` pins - and `_failed` carries no
image, which `_evidence` already turns into an empty `covers_target`, so the ledger cannot file it.
The adapter checks the RETURN before `last_shot_path`, because that path still holds the previous
command's screenshot and would otherwise be filed as this region's section.

And half a surface is not the surface (`build_region_msh`). The regions are read off the delivered
polyMesh rather than off the export, because the export is the thing that failed and asking it
which regions exist is asking it to grade itself. A workspace with no region directories - every
unit fixture that fakes a VTK tree - names no required regions and is published as before.

WHAT IS NOT CLOSED. The string-identity target test, the manifest-string/file asymmetry at
base.py:615, the kind-only PATCH obligation, and the hardcoded `image_ok=True` are reviewer
contract weaknesses that survive these two cuts. Nothing empty now reaches them, which is why the
specific hole is shut, but a different empty thing would find them again.

(Three of those four are closed in "The four gates that survived those two cuts", below. The
string-identity target test is still open, and that entry says what answering it would cost.)

### Eight ranks on eight cores spawned 84 threads and the mesher never left the smoother

A 279,621,370-cell build of the plate on mesh-bench, eight ranks, stretch 6 along the channels:

| | |
|---|---|
| threads | 8 ranks x 10 = 84, on 8 vCPU |
| CPU burnt | 180,053 s in 22,536 s elapsed = 8.0 cores, saturated throughout |
| where it got to | iteration 1 of the final smoother, 212,460 bad faces, no log line for 26 minutes |
| what it had left | 2 hours of its 8 hour bound, against a tail that needs longer than that |

Killed. For comparison a 98,973,937-cell build of the same part in ONE process is 640 s of
ClockTime.

cfMesh is built with OpenMP and its default is a thread per core it can SEE. In one process that
is right, and it is the whole reason a single-process build gets 2.5x out of four cores. Once
there are as many processes as cores it is exactly wrong: each rank still asks for eight threads,
gets one core's worth between them, and spends it on barriers.

`OMP_NUM_THREADS` is now `cores // ranks`, floored at one, exported with `-x` the same way
`MPI_BUFFER_SIZE` is. An assignment in front of mpirun sets the variable for mpirun and for
nothing it launches, which is the trap that already cost a build in cfMesh's load balancing. A box
with cores to spare still threads each rank, so the setting costs nothing where OpenMP was
helping, and the serial chain does not go through `mpi_command` at all.

The proof it is honoured is the ratio rather than a thread count: on the next run cartesianMesh
came back `ExecutionTime = 722.7 s  ClockTime = 727 s`, a ratio of 1.0, against 2530.64 / 640 =
3.95 for the single-process build of the same part.

THE RANK IMBALANCE IS REAL AND SEPARATE. Same measurement: 16,056 CPU-seconds on the lightest rank
against 29,520 on the heaviest, 1.8x, with scotch asked only to balance cell counts.

### The viewer had never been run in a browser, and three things were broken

Every test of the viewer is Python on the server side. Nothing in the tree runs `viewer.js`, and
the only JavaScript linter is `apps/console/eslint.config.mjs` whose first rule is
`ignores: ["public/static/**"]`, which is where one of the two copies lives. I opened the page on
the delivered coupon (job 18e2f4ed) and found three defects in an hour.

> CORRECTED, and the sentence to distrust is "nothing in the tree runs `viewer.js`". It does:
> `tests/ui/` drives the shipped page in a real Chrome over CDP, it fails rather than skips when no
> browser is on PATH, and five of its files were already driving this very file through
> `window._vdbg`. Chrome was not installed in this checkout, so the whole gate came back as ~142
> identical errors and I read it as noise. What was true is narrower: the gate did not cover the
> detail window, which is where all three of these defects were. See "A correction: the browser
> gate exists, and it is strict" and "The detail window is in the browser gate now" below. The
> linter half of the paragraph stands.

| what | where | effect |
|---|---|---|
| `tanHalf` called across closures | both copies of viewer.js | every click of Detail threw, always |
| the detail budget eaten first-come | render/detail_window.py | a truncated window showed one region |
| a metric painted under the detail | both copies of viewer.js | a legend over an uncoloured mesh |

THE FIRST ONE IS THE ONE THAT MATTERS. `tanHalf` was a const inside the heatmap's IIFE.
`radiusNow`, inside the detail window's IIFE, called it by name. Different closure, so the name
was never in its scope chain:

    ReferenceError: tanHalf is not defined
        at radiusNow (viewer.js:1112)

Not a race and not a load order. Every click, in every browser, since the feature was written. It
had a route, a bounded reader, a face budget, a button in the bar and thirty Python tests. Hoisted
to the scope both closures are in, next to the camera it reads, in both copies.

THE SECOND CAME OUT OF NOT BELIEVING A NUMBER. Measured on the same job, 4 mm window:

    faces_in_range 684660   returned 150000   truncated True
      fluid/fluid_to_solid   mappedWall   faces 150000
      (and nothing else)

The cap was spent walking the patches in the order they came, the coolant's interface used all of
it, and the metal side of that same interface was not in the answer. `truncated: true` said
something had been cut and not that a REGION had. The budget is shared out now by largest
remainder with a floor of one face per patch, the faces kept inside a patch are spread evenly
rather than taken off the front, and every patch says how much of itself is showing. Same window
afterwards: eight patches, both regions, `fluid_to_solid 72,386/330,400` and
`solid_to_fluid 72,386/330,400`.

THE THIRD IS AN INTERACTION. The colours go on the merged surface and the detail window hides
exactly that surface while it is open, so a metric painted with the detail up left a legend
reading "max 27.0 deg" over cells with no colour on them. The merged surface comes back when a
metric is painted, which is what any camera move already does.

`devtools/semiconductor/viewer_check.sh` checks the two things that do not need a browser, both
copies parse and both carry the entry points, and says plainly that it checks nothing else. The
copies are 87 differing lines apart now, down from 132.

### The face counts in the parts panel were the block's, not the delivery's

Found by reading the panel in a browser and not believing one number. Job 18e2f4ed, off its own
workspace:

| patch | block | delivered |
|---|---|---|
| xmin, xmax | 1,928 | 1,928 |
| ymin, ymax | 776 | 776 |
| zmin | 7,504 | 7,504 |
| zmax | 17,426 | 14,226 |
| inlet | absent | 1,600 |
| outlet | absent | 1,600 |
| fluid_to_solid | absent | 346,456 |
| solid_to_fluid | absent | 346,456 |

`_patch_face_counts` read `constant/polyMesh/boundary`, which on a multi-region delivery is the
PRE-SPLIT BLOCK sitting beside the regions. Two things happen to that block before the customer
gets anything: `splitMeshRegions` divides its faces between the regions, and the port cut carves
inlet and outlet out of a block face afterwards, on the coolant alone. So zmax was shown 3,200
faces too big, which is exactly the two ports, and the ports and the interface had no count at
all. The interface is the one patch a conjugate mesh is for.

AND AT THE SIZES THAT NEED RANKS THERE IS NO BLOCK TO READ. The parallel chain stays decomposed
through the split and reconstructs the two REGIONS, never the block, so `constant/polyMesh` does
not exist on a customer-sized conjugate build and this returned `{}`. Every count in the panel
would have been blank, on the runs that matter most.

Summed per BARE name across the delivered regions, because the manifest is keyed by the name the
user asked for at intake and two regions can legitimately reach the same block face, which
`mesh_facts` already says where it maps a declared patch to the surfaces that draw it. A
single-region mesh has no region directories, reads the one boundary file it always read, and is
unchanged.

### The six readings, and the two that were still not reaching the reader

Corintis asked for six things by name. Where each one stood when I opened the delivered coupon in
a browser:

| # | reading | where it was |
|---|---|---|
| 1 | mesh count | shown: 6,582,471 cells, 20,381,030 faces, 7,216,181 points |
| 2 | quality metrics | shown: max non-ortho 26.98 deg, skewness 0.51, aspect ratio 2.44 |
| 3 | boundary layers in the fluid | measured, in the manifest, filtered out before the viewer |
| 4 | min and max cell sizes | shown: 11.69 um to 160.5 um, ratio 13.73x |
| 5 | cell types and count per type | shown: 6,404,482 hexahedra (97%), 177,989 polyhedra (3%) |
| 6 | highlight of poor-quality cells | shipped as an ordinary wall, in a wall's colour |

READING 3 WENT THE WAY TWO OTHERS ALREADY HAD. `render/mesh_facts` keeps a list of the metrics
that may reach a payload, and says in its own comment what that list costs when something is left
off it: "a metric that is not here never reaches a reader however faithfully the mesher measured
it - which is how 'cell types and count per type' and 'min and max cell sizes' came to be measured
on every run and shown on none". The layer count was still being dropped there. Measured on the
coupon's own manifest:

    manifest quality : near_wall_layers 0, near_wall_levels 3, near_wall_cell_m 1.96e-05
    viewer payload   : none of the three

The answer is a deliberate zero and that is exactly why it cannot ship alone. A conjugate
interface is conformal because both regions are the same cells from the same octree; a prism layer
is a new cell inserted on ONE side of a patch, so a stack on the interface would leave every face
without its partner. The mesh has graded refinement toward the wall instead, so the three numbers
travel together and the reason is on the row:

    Boundary layers      0       none by design: an extruded layer sits on one side of a patch
                                 and would leave every interface face without its partner
    Cell at the wall  19.6 um    graded refinement instead: 3 levels from the background cell
    Interface faces  346,456     the same on both sides

The interface count came through with them. Equal counts on the two sides ARE the conformality,
and the parts panel could not show a face count for that patch at all.

READING 6 HAD NO CLIENT SIDE. The engine writes the poor-cell sets, moves them under
`poor_cells/<region>/`, reads them with pyvista and ships them typed `poorQuality`. Both copies of
viewer.js contained zero occurrences of that string, so a set of failing cells arrived as an
ordinary boundary patch, took the next colour in the palette and was listed between ymin and zmax.
`POOR_CELLS` is a part kind of its own now, on both sides, which is what mesh_facts asks for where
the kinds are declared: "Closed: the viewer groups by these, so a new kind is a deliberate
addition on both sides rather than a string that appears one day." They open visible, are never
treated as enclosing, and are drawn flat and hot rather than shaded so a few hundred cells inside
a mesh of millions read as a mark on it.

Not yet seen in a browser: the coupon has nothing wrong with it, n_over is 0 on all three metrics
at the engine's bars, so it writes no sets. The customer plate had 28,132 poor cells, 27,932 of
them in the metal.

AND THE CONSOLE'S COPY WAS THREE READINGS BEHIND, not one. It had no cell-size group, no size
ratio and no small-determinant count, so "min and max cell sizes" and part of "cell types" had
been fixed in the served viewer only. The `meshFacts` IIFE reads `surf`, `q`, `host` and `esc` and
nothing else, so it ported whole. 87 differing lines between the two copies, now 47.

### A customer run, driven from the page rather than from a script

Uploaded the coupon through the page itself and answered as a customer would. What it did:

| step | what happened |
|---|---|
| upload | drew the part, 4 s, "check it and press Proceed now" |
| named | "Liquid-cooled cold plate with internal channels", 26 s |
| openings | 2 found, 0.8 x 0.8 mm each, confidence 85% then 99% after the naming |
| proposed | solid-assembly, mm, internal flow, inlet and outlet, flow axis +x |
| unit | refused to proceed: 8 mm is small for what it had just named, so it asked |
| engine | conjugate recommended as best fit, with the single-solid heads-up on the snappy row |
| conditions | water at 20 C, 1 L/min, steady CHT, proposed |
| submitted | after a confirm, six exchanges in all |

Everything it proposed was right, and the one thing it stopped for was worth stopping for.

ONE DEFECT. Proceed refuses an unresolved unit and writes the question into the hint. Picking the
unit satisfies the check and the unit row redraws to "your choice, the part would be 8 mm long" -
and nothing cleared the hint, so the form went on saying "Pick the file's unit first" directly
under a row saying it had been picked. Cleared now when the unit is chosen, by the button and the
select alike, and only the unit's own hint: an opening that still has no size is a different
unanswered question.

### The parallel tail, measured, and the merge that was rebuilding the block

The chain was proven equivalent on the coupon in October but never timed at a size that needs
ranks. A 182,046,234-cell build of the plate on mesh-bench, 8 ranks, no stretch zones:

| stage | processes | wall |
|---|---|---|
| preparePar | 1 | 1 s |
| cartesianMesh | 8 ranks | 727 s |
| topoSet | 8 ranks | 348 s |
| splitMeshRegions | 8 ranks | 568 s |
| reconstructParMesh | 1 | see below |

The serial baseline is the 336,666,582-cell build: cartesianMesh 2,399.6 s on 8 ranks, then
reconstructParMesh 4,518.4 s, topoSet 2,240.2 s and splitMeshRegions 6,073.3 s in ONE process,
12,831.9 s of tail. Scaled per cell to this build's size that tail predicts 1,211 s of topoSet and
3,284 s of the split; measured on ranks they are 348 s and 568 s. Different builds, so this is a
rate and not a race, and the rate is what the chain was changed for.

THE MESHER IS ALSO A DIFFERENT NUMBER NOW and for a different reason: `ExecutionTime = 722.7 s
ClockTime = 727 s` is a ratio of 1.0, which is one OpenMP thread per rank. The same part in one
process reads 2530.64 / 640 = 3.95.

AND THE LAST STAGE WAS REBUILDING THE BLOCK. 25 minutes into `reconstructParMesh -constant
-allRegions`, 39 GB written into `constant/polyMesh` at the root and neither region produced. What
each processor directory held:

    constant/polyMesh   5.7 GB     the pre-split block
    constant/fluid      2.4 GB
    constant/solid      2.9 GB

`splitMeshRegions -cellZonesOnly -overwrite`, run decomposed, writes the regions BESIDE the base
mesh rather than replacing it. And `-allRegions` means, in its own help, "Use all regions in
regionProperties" - while build() writes `constant/regionProperties` at the end, after the merge.
No file, so the option falls back to merging every mesh it can find, and the block is bigger than
both regions together.

The trap is already written down in the same file, forty lines further on, for the next command
that meets it: "PER REGION, never `-allRegions`. `checkMesh -allRegions` reports the PRE-SPLIT
mesh". The coupon could not show it - at 59,074 cells the stage is seconds either way - so the
equivalence test that proved the chain correct could not also prove it quick.

`-regions '(fluid solid)'` now, measured on the same processor directories the killed run left,
which is the controlled comparison the disk could not afford on a second mesh.

### A correction: the browser gate exists, and it is strict

I wrote above that nothing in the tree runs `viewer.js`. That is wrong, and the way I found out is
worth keeping: `pytest tests/ui` here reports 142 errors, every one of them the same message, and
I had read that as noise.

`tests/ui/` drives the shipped page in a REAL Chrome over CDP. With no browser on PATH it fails
rather than skips, deliberately:

    No Chrome/Chromium on PATH. The UI gate validates the shipped page in a REAL browser, so this
    is a hard failure rather than a skip - a release must not pass with its browser validation
    quietly absent.

Five of its files import `openViewer` from `/static/js/viewer/viewer.js` and drive
`window._vdbg[job]` and `window._vdbg[job + ':heat']` directly - the same hooks I had been using by
hand all afternoon. Chrome simply is not installed in this WSL checkout, and the `chromium` apt
package is a snap wrapper that will not run here, which is why the gate's own message says to
install Google Chrome's .deb instead. I did, and ran it:

    143 passed, 2 warnings in 260.48s

Everything today's viewer work touched passes it: the hoisted camera helper, the poor-cell kind
and its colour, the At the wall group, the detail window closing when a metric is painted, the
console copy's ported meshFacts block, the unit hint, the coerced id.

WHAT IS ACTUALLY TRUE, and it is narrower and more useful than what I wrote. The gate does not
cover the DETAIL WINDOW: no file in tests/ui mentions `detailWindow`, `v-detail` or `radiusNow`.
That is precisely where the three defects were. A feature was added to a file that has a browser
gate, without a test in that gate, and the gate had no way to notice.

The linter half of what I wrote stands: `apps/console/eslint.config.mjs` ignores
`public/static/**`, which is where one of the two copies lives, and no linter would have caught a
name resolved in the wrong closure anyway.

### The detail window is in the browser gate now

The gate was never the problem, and the entry above has the shape of the mistake: a feature went
into a file that HAS a browser gate without a test in that gate, and the gate had no way to notice.
`tests/ui/` held no mention of `detailWindow`, `v-detail` or `radiusNow`, so all three of the
morning's defects were invisible to it and visible only to me opening the page.

`tests/ui/test_detail_window.py`, one test per defect, 3 passed in 9.9 s. I put each defect back
and ran the matching test against it, because a test that passes against the fix proves nothing on
its own:

| defect | what the test says with the defect put back |
|---|---|
| `tanHalf` across closures | `ReferenceError: tanHalf is not defined at radiusNow (viewer.js:1156)` |
| the budget eaten first-come | drawn patches `[fluid/inlet, fluid/outlet, fluid/wall]`, no solid side |
| a metric painted under the detail | `non_ortho` active, colour `[92,120,152]`, `hidden: 3`, detail still up |

THE THIRD ROW IS THE DEFECT WRITTEN OUT: the calm end of the scale painted on a merged surface,
three merged actors switched off, and the window still standing in front of them.

THE FIRST ROW IS WHY A CHECK FOR PAGE ERRORS WOULD NOT HAVE DONE IT. The ReferenceError was thrown
inside the click handler's own try/catch, which wrote it into the button's label and logged it with
`console.warn`, not `console.error`, so `assert_clean` sees nothing at all. The test reaches it two
ways: reading the radius the button would ask for, which is the call that threw, and counting the
request, which the defect never issued.

The payload is the backend's own, as the heatmap test's is. `detail_at` reads a real two-region
polyMesh, `MAX_RETURN_FACES` stands in at 8 so a ball holding 28 faces over six patches has to be
shared out, and the route's JSON shape now has a name - `detail_window.detail_response` - which the
route returns in one line and the test serves to the browser. A second hand-written copy of that
shape in the test would have agreed with the route only until one of them changed, and a client and
a route that agree only while someone reads them side by side is how the first defect shipped.

`_vdbg[job].detail()` gained `names` and `hidden`: which patches are drawn, and whether the merged
surface the window stands in front of is still off. Both were already locals of the window, and
both are what the three defects came down to.

I COULD NOT GET A CLEAN FULL-GATE NUMBER, and the reason belongs here because it is the same trap
as the first wrong version of this correction. The gate is 146 tests now. Run whole while another
session had this box on a meshing chain, it reported 110 passed, 1 failed and 35 errors - and all
36 non-passes were one message, `ConnectionClosedError: sent 1011 keepalive ping timeout`. The
session browser is a single long-lived CDP websocket; under load it misses a keepalive, and every
test after that errors at setup. Transport, not the page. Run one file per invocation and the
browser session is too short to time out:

| file | alone |
|---|---|
| test_detail_window | 3 passed |
| test_heatmap | 4 passed |
| test_browser | 2 passed |
| test_fresh_downloads | 7 passed |
| test_a_merged_view_says_it_is_merged | 3 passed |
| test_thermal_results | 2 passed |
| test_viewer_controls | 2 passed |

Every file that drives viewer.js passes on its own. The other way to get the same misleading pile
is `pytest tests/ui tests/unit/...` together: `from conftest import assert_clean` then resolves to
tests/unit/conftest.py and all 17 viewer files error on import. Both are in the script's header
now, because a wall of identical errors is what made me write that nothing here runs viewer.js.
### The four gates that survived those two cuts, and what each one measures now

The two cuts in "A correct conjugate mesh came back PASSED" closed the specific hole: nothing
empty reaches the reviewer's contract any more. The contract itself was untouched, and an
adversarial read of it found four places where a question about substance was answered with a
fact about presence. A different empty thing would have found all four.

| where | the test it was | the test it is |
|---|---|---|
| `cfmesh_conjugate/spec.py` | PATCH obligation kind-only, min 1 | `patch:fluid` AND `patch:solid`, named off checkMesh's own region rows |
| `engines/base.py` | an absent `mesh_paths.volume` STRING waived every region obligation | the waiver is the engine's to refuse, and this engine refuses it |
| `reviewer/unified.py` | `image_ok=True, covered=True` as literals beside a PNG path | read off the evidence: a kept frame, and two statements agreeing |
| `sandbox/review_session.py` | the PNG's byte size | the pixels |

ONE PATCH WAS NEVER EVERY PATCH. The obligation read
`TargetObligation(kind=PATCH)`, which is kind-only, and `required_count()` floors that at one.
Its provenance string said "every renderable boundary patch". So one inspection of one entity
discharged it, and that is exactly what 575f76e6 did: the surface carried the solid alone, the
reviewer toggled it, the obligation was satisfied, and the absent coolant boundary was never
asked about.

This engine is the cheap case and the obvious one. `review_surface.build_region_msh` writes ONE
physical group per REGION rather than one per patch, so the renderable entities ARE the two
bodies, and exact coverage costs two toggles out of a review's view budget. The ids come from
`quality.regions`, which is checkMesh's own per-region measurement of the delivered polyMesh, not
from the export and not from discovery: a surface that lost a region cannot satisfy the
obligation by not offering it, because `missing_target_obligations` then comes up short and the
review refuses before the model is asked.

THE OTHER FOUR ENGINES HAVE THE SAME GAP AND I LEFT IT. cfmesh, snappy, snappy_multiregion and
snapgrid all carry the identical kind-only PATCH obligation under the identical provenance
string. The difference is arithmetic: their entities are boundary patches rather than regions,
and snapgrid meshes up to 2,000 placed parts. Exact coverage there is tens to thousands of
inspections against a 64-image session cap and a 30-round budget, so widening it would end
reviews with no verdict on meshes that passed every gate, which is the failure this codebase has
already paid for twice. The right answer for those is a floor that is neither 1 nor N, and I do
not have the measurement to pick it. It is written down here rather than guessed at.

(Corrected in "The patch floor was one, and I said N was not payable", below. The arithmetic
holds and the conclusion did not follow from it: the obligation feeds two checks and only one of
them costs images.)

THE WAIVER, AND WHICH HALF OF IT IS DANGEROUS. `base.expected_target_obligations` dropped every
REGION obligation when `manifest["mesh_paths"]["volume"]` was falsy. That clause reads a CLAIM, a
string; the render session advertises region targets off the resolved FILE. Two authorities, and
they meet in exactly one place:

| claim | file | what happens |
|---|---|---|
| yes | no | the obligation stands, discovery cannot meet it, EXPECTED_TARGET_MISSING. The pruned volume on 51d5481d, and the honest outcome |
| yes | yes | compared at full strength |
| no | no | waived, and nothing downstream asks to see inside the mesh at all |

The third row is the one with no floor under it. For an engine whose interior is a VIEW of the
mesh the waiver is right, and demanding a slice there is what ended reviews on good meshes. For
this engine the interior IS the mesh: a conformal two-region split and one merged block agree on
cells, faces, non-ortho, skew and the interface count, and differ only in a cut through the
coolant. A review of the outside of one cannot tell it from the other. And `mesh_paths.volume`
goes absent on a real path, not a hypothetical one: `review_volume` returns None whenever the
fluid VTK export is missing, and the artifact is declared optional precisely so that does not
fail the mesh.

So the engine declares it. `EngineSpec.review_requires_the_interior` is False everywhere and True
for cfmesh_conjugate, and a conjugate run with no volume is now stated missing evidence rather
than a question nobody asked. DECLARED, not derived: all five region engines mark `region:*`
required, so reading the flag off the targets would delete the waiver for all of them at once.

IMAGE_OK AND COVERED WERE LITERALS AND ARE NOT ANY MORE.

`image_ok` is now `bool(ev.image_ref)`, which is a different sentence than it was yesterday
because of the row below it. `review_session._validate` clears `image_ref` for a frame that
escaped the sandbox, does not exist, is outside the size band, is past the view budget, or was
MEASURED to have nothing drawn in it. A surviving `image_ref` is a fact about pixels.

`covered` is the only independent pair the evidence carries: the entity the command ADDRESSED
(`target_ref`) and the entity the session claims it COVERED (`covers_target`). Every renderer in
the tree sets both, in three places (`sandbox/render_adapter`, `gmsh/review_renderer`,
`vmtk/review_renderer`), and a disagreement means the coverage claim is about something other
than what was rendered. `usable` is the AND of the two, so such a record is filed and cited as
neither. The invariant is pinned at the source by an AST scan rather than in the fakes, because
four test fakes were setting `covers_target` alone, which is a shape no session produces.

THE EMPTINESS CHECK IS ON THE PIXELS NOW. A blank 1280x960 PNG is several KB and clears the
byte-size band by a wide margin, which is how eight blank cuts read as eight interior
inspections. The measure is the share of pixels that are not the frame's most common colour,
with a floor of 1 in 10,000: on 1280x960 that is 122 pixels, and 122 pixels are not a mesh at any
zoom. It is not a quality bar. A legitimate view with the model small in frame is thousands of
pixels and passes.

The first version read the background off the top-left pixel, and a unit fixture broke it in the
first run: the fixture wrote its drawn pixels from index 0, one of them landed on the corner, and
the measure inverted. A frame whose model reaches the corner would have read as blank. The mode
over the whole frame assumes nothing about where the model sits, and that case is now its own
test.

The measure speaks only about frames it can decode. A file that Pillow cannot open falls through
as unmeasured, because a measurement that cannot be taken is not evidence of blankness, and the
checks that own those cases (missing, unreadable, out of band) are already above it.

It costs 118 ms on the worst frame I could build (1280x960, a 500x700 block of random colour, a
1,060,712-byte PNG, five runs), nearly all of it the decode. At the 64-image session cap that is
7.5 s across a review that runs for minutes.

THE REFUSAL. `inspect_region` was the one tool in the runtime whose text made a claim about what
the picture SHOWS: "This cut exposes the interior cells", said whether the renderer had produced
a section, a blank frame or nothing. Every other tool there answers a refusal by naming the way
out, and job 53bbce4b is on record for what the generic version costs. It now names the backend's
own reason, the other declared regions, and what to do if the interior cannot be seen at all, and
the success text says the cut was taken only because it passed through cells.

Also small: the order that demands an uninspected target named the PREFIXED id as the tool's
argument ("the slice tool with region 'region:across_the_flow'"), which can only be refused.
It names the bare entity now, and names toggle_patch for a patch, which matters because an
engine can oblige exact patch ids as of this commit.

WHAT I DID NOT CHANGE, AND THE QUESTION IT IS. `eligibility.missing_target_obligations` decides
"produced" by string identity between two lists of ids. No artifact, no geometry, no count. The
obvious objection is that a target can be produced and empty, which is the whole subject of this
entry.

I left it because the honest version is expensive in a way the others were not. A measured
quantity on an inspection target has to come from the backend, and the backend only knows it by
doing the work: the face count of a patch needs the patch loaded, and the cell count of a
region's cut needs the volume read and sliced at every declared region BEFORE the session opens.
On the Corintis plate that is a 197 MB read and two slices at session open, on every review,
including the ones where the reviewer never cuts. The cheap half of the same protection is
already in: the backend refuses a cut that passes through no cells, the surface is not published
holding one region of two, and a blank frame is measured. What string identity still lets through
is a target that loads, draws, and is empty of the thing it was supposed to show. Closing that
means paying the load up front, and the trade is worth measuring before it is made.

Also still open: the other four engines' patch obligations, above, and the undecodable frame.

tests/unit/agents/reviewer/test_the_gates_test_substance_and_not_presence.py, 22 tests, one per
claim above. tests/unit/agents/reviewer/test_render_runtime.py gained the empty-cut refusal.
tests/unit/agents/reviewer/test_review_evidence_follows_the_run.py had asserted the waiver for
every region engine; it now checks the two halves of the rule for opposite behaviour, with the
reason in the file.

### The whole chain, at scale, on the customer's own plate

`ColdPlateAssembly_CLEAN.step`, 177.5 x 41.84 x 101.0 mm, meshed on mesh-bench through the
parallel chain with both of today's fixes in it - one OpenMP thread per rank, and one merge per
region:

| stage | processes | wall |
|---|---|---|
| preparePar | 1 | 1 s |
| cartesianMesh | 8 ranks | 727 s |
| topoSet | 8 ranks | 348 s |
| splitMeshRegions | 8 ranks | 568 s |
| reconstructParMesh, fluid | 1 | 824 s |
| reconstructParMesh, solid | 1 | 1,169 s |

And what came out, checked:

| region | cells | checkMesh | max skewness |
|---|---|---|---|
| fluid | 75,162,351 | Mesh OK | 0.704509 |
| solid | 107,107,563 | Mesh OK | 0.691260 |

182,269,914 cells. The interface is **13,181,685 faces on each side**, which is the conformality
the whole split-after-meshing approach rests on, measured at scale on the part it was built for
rather than on a coupon.

WHAT THIS RUN IS NOT. Its plan declares no ports, so the coolant is a sealed region with the
interface and nothing else - a bench measurement of the chain, not a delivery. A delivered mesh
needs the mouths cut and named, which is the pipeline's own path.

THE SERIAL BASELINE for the same part was 12,831.9 s of tail behind a 2,399.6 s mesher. The tail
here is 2,909 s behind a 727 s mesher, at 54% of the cells.

### The domain was in the split all along

The box the mesher fills is three times the part, and the boolean that would fix it does not come
off on the one assembly that needs it. That entry ended "Not attempted", with a route written down
that goes through the tessellation. The route turned out to be shorter than that.

`_split` classifies every region of space as outside, metal or space - that is how it finds the
coolant at all. So metal plus coolant is "every region that is not outside", and its boundary is
every face of those regions that two of them do not share: the part's outer skin, with each port
mouth closed by its own cap. `_boundary` has always been able to take the boundary of a SET of
regions; it was only ever asked for one passage at a time. The answer was computed on every run
and thrown away.

Measured on `ColdPlateAssembly_CLEAN.step` - Corintis's own assembly, 6 solids, 2 passages, 70
regions, 53,661.8 mm3 of coolant - which is the part the boolean cannot do:

| | |
|---|---|
| domain faces | 348 |
| sewn | 2 shells, one degenerate (1 free edge, 0 mm3), one CLOSED |
| closed shell | 276,372.4 mm3 |
| the cuboid the engine meshes today | 750,014.9 mm3 |
| ratio | 2.71x less volume to fill |

276,372.4 against the 276,324 the fuse gives on the fixture it does work on is the same answer to
four figures. Same domain, by a route with no boolean and no tolerance - which is what matters on
a part whose walls are 98 um and whose tessellation deflection is 84.

NO MATCHING ANYWHERE, which is the whole reason this works where the other routes do not. The
faces come from one split of one shape, so they already share their edges. The degenerate shell is
expected and is skipped exactly as an open passage boundary always was; the largest CLOSED shell
is the domain.

The fuse stays as the fallback: an extraction from before this carries no boundary and fuses as it
did, and a sew that raises returns None rather than failing a path that already worked.

WHAT IT IS WORTH is already written down in `cfmesh_conjugate/conjugate.py`, next to the patch
name a shell domain would use: "the cuboid is 846,543 mm3 and metal plus coolant is 274,807 mm3,
so 67.5% of a box-domain mesh's cells are air."

AND THE CONSUMER SIDE IS ALREADY BUILT. `OUTER_PATCH = "wall_outer"`, `is_domain_face`, and
`mouths.shell_stubs` - which cuts the ports out of the shell instead of a block face, is
documented against this exact assembly, and has NO CALLERS. `_child` already handles
`domain_path`. The driver writes `block_surface_stl(lo, hi)` unconditionally and never asks. One
branch there is what connects it.

### The mesher fills the part now, and everything it needed was already there

The entry above gets the domain out of the split. This connects it, and the connecting turned out
to be two edits, because the consumer side had been written and left uncalled:

| what | where | callers before today |
|---|---|---|
| `OUTER_PATCH = "wall_outer"` | conjugate.py | the name only |
| `is_domain_face` | conjugate.py | accepts it already |
| `mouths.shell_stubs` | mouths.py | NONE |
| `domain_path` handling | fluid_volume._child | nothing asked |
| the record and its logging | fluid_volume.surface_bounded | nothing asked |

The driver wrote `block_surface_stl(lo, hi)` unconditionally. It asks for the domain now, on the
extraction it is already paying for - one extraction, two answers, since asking twice would double
the slowest step in a build.

On `ColdPlateAssembly_CLEAN.step`:

| | |
|---|---|
| ok | true |
| solid_name | wall_outer |
| triangles | 27,696 |
| volume | 276,372.39 mm3 |
| box | 750,014.89 mm3 |
| share_of_box | 0.3685 against BOX_WORTH_KEEPING 0.60 |
| metal | 222,661.97 mm3 |
| fluid | 53,661.79 mm3 |

ONE DECISION IN ONE PLACE. `use_shell_domain` sits beside the patch name it decides about and the
driver asks it rather than doing the arithmetic inline. It is false for everything that is not a
measurement - no domain, one that did not come out, a part that fills its own box, a missing
share, and a share that is a STRING, which `float()` would have taken. My own test caught me
writing that one: a record that went through something which stringified it is not a measurement
to act on.

THE PORTS ARE CUT WHERE THEY ALREADY ARE. On a box the mouths are inside it and must be stubbed
out to a face first, which is why both of that plate's fittings land on xmax and a face can carry
one name. On a shell they are on the surface already, so each is a zero-length stub and a patch
cut in place.

NOT YET SEEN IN A LIVE BUILD, and it changes the surface cfMesh is handed, so it wants one.

### A budget that does not scale with the file it is given

Running their plate through the page while two extractions of the same file were racing each
other, the check came back:

    the fluid inside the parts could not be found (finding the fluid inside the parts took over
    300 s); this is a limit of the check and not something measured about the part

and the part read as `solid-body` in an internal flow - a solid in a flow, not a cold plate with
coolant. The note is honest and the fallback is the inside-space map, which still found 4 mouths.
But the job that would have been built from it is the wrong job.

Today's timeout was mine: `extract` on that file is 141.5 s idle and measured 420.8 s while I had
a second extraction of it running. So the 300 s stands on one contended reading and I have not
moved it.

Worth writing down anyway: everything around this budget scales with the file and this one does
not. The scout's own Celery soft limit is 900 s + 60 s per MB, which is 1,853 s for that 15.9 MB
upload, and `geometry_progress` states the principle - "Liveness, not a budget". The extraction is
the one step priced by a flat wall clock, and its stated justification is that the work is bounded
by MAX_FACES = 20,000 - while the measurement behind the 300 s is a 9,226-face file at 141.5 s.
A file at the face limit is 2.2x the faces on a superlinear split.

### The customer's plate failed the budget every time, and the budget was priced off a reading that does not reproduce

Every run of `ColdPlateAssembly_CLEAN.step` through the page came back the same way:

    the fluid inside the parts could not be found (finding the fluid inside the parts took over
    300 s); this is a limit of the check and not something measured about the part

and a liquid-cooled cold plate read as `solid-body` in a flow. The note is honest, the fallback to
the inside-space map still finds 4 mouths, and the job that would be built from it is the wrong
one. The flagship file, the engine built for it, and the wrong answer every time.

I CALLED IT MY OWN LOAD FIRST, AND IT WAS NOT. Measured twice, same file, same checkout, idle box:

| | |
|---|---|
| load the STEP | 31.3 s |
| candidate_caps | 116.6 s, 58 candidates |
| extract, run 1 | 359.2 s |
| extract, run 2 | 359.0 s |
| every `_boundary` walk | 16.1 s, 4.5% |
| the domain walk alone | 11.0 s, 69 regions, 348 faces |
| CHECK_BUDGET_S | 300 s |

Two things to say about that.

The first is a reading error of mine worth writing down: I thought a worker was pinning two cores
through all of this, because `ps %cpu` showed 147-173% long after its task had finished. `ps %cpu`
is CPU time over the process's LIFETIME, not what it is doing now; `docker stats` said 0.21% and
was right. Two of my earlier timings really were contended - I had two extractions of the same
file racing - but the box was idle for these.

The second is that my own domain walk, which I suspected first because every timing today had it
in, is 11.0 s of 359. Not it.

The 300 s came from "that customer assembly at 9226 faces now measures 141.5 s in `extract` and
162.9 s of child wall". It does not hold on this box. 900 s now, and the reasoning is that nothing
else around this is a flat number: the scout's own Celery soft limit is 900 s + 60 s per MB, which
is 1,853 s for that 15.9 MB upload, and `geometry_progress` states the principle outright,
"Liveness, not a budget". 900 leaves that enclosing limit in charge for a file this size, is 2.5x
the measurement rather than 0.84x it, and the work is still bounded by MAX_FACES = 20,000.

STAGE_BUDGET_S goes 600 to 1,200 with it, because the builder's extraction splits the same space
and then writes a surface out of it - two now, when the domain is worth building - so it cannot be
the tighter of the two. A test already pinned that relationship, which is what caught the change.

AND THE DOMAIN WALK IS OPT-IN. It covers every region that is not outside, where a passage covers
only its own, and the check has no use for its 348 faces. 11 s is not the timeout, but it is 11 s
on the one path with a wall clock.

WHAT WOULD BE BETTER THAN EITHER NUMBER is not spending it. 116.6 s of the 359 is
`candidate_caps`. That is the next thing to measure into.

## 2026-10-29, later: the gate under the budget, their second file, and the box I have been paying for

Raising the budget got their plate through the check. It still came out wrong, one line further on.

| | |
|---|---|
| split | 52,144.56 mm3 of coolant, 3 openings, 379.5 s |
| scout | single_solid, solid-body, EXTERNAL, confidence 0.70 (box_like) |
| naming | "cold plate with internal coolant passages", solid-assembly, internal, 0.62 |
| proposal | solid-body, EXTERNAL |

`_proposal` hears the naming only when its confidence clears the scout's. 0.62 against 0.70, so
the block was skipped whole and both the kind and the flow stayed the flat-face reading. External
flow means `patches_from` returns no openings, and the intake then correctly refuses a conjugate
case on a geometry with no coolant in it. The customer is told their cold plate is a solid body in
a stream.

The principle was already in that file, forty lines down, in `_kinds_on_offer`'s own words: coolant
actually found inside the part outranks the flow the measuring step guessed. That decides what may
be named. It was never applied to whether the naming is heard at all. The scout's 0.70 is its own
flat-face number, set for a body it calls `box_like`, which a brazed cold plate is, before any
split has run.

So a measurement now beats that comparison, and only for the one kind the measurement is about.
`solid-assembly` is on offer only when the split found coolant and there are two or more mouths,
so naming it was already evidence-gated twice. Nothing else changes: no split result and the
scout's reading stands exactly as it did, which is what keeps a car body from being read as a cold
plate. Seven tests over the numbers that plate produced, the two negatives included.

### Their second file

The chip ECXML, read with our own reader, no changes:

| | |
|---|---|
| objects | 57 |
| materials | 33 |
| elements ignored | 0 |
| notes | 0 |
| total declared power | 0.000 W, across all 44 powerDissipation |
| solutionDomain | absent |
| footprint | 70 x 78.6 x 3.571 mm |

It reads clean. It also declares no heat load at all, so there is nothing of that sort to carry
and I am not inventing one.

What looked like the prize was two monitor points, named `inlet` and `outlet`, at (51, 0, 30.785)
and (51, 53.5, 30.785) mm. The customer naming their own ports. The geometry check finds three
openings and has to work out the roles, so a name would settle it.

It does not. Those two are 53.500 mm apart, and a separation does not change under any rotation,
reflection or shift of the axes, so I tested it against the plate's own openings. Of the 58
candidate caps on their plate I took the 28 largest:

| | |
|---|---|
| pairs within 1 mm of 53.500 mm | 13 |
| best pair | 53.515 mm, off by 0.015 |
| what that pair is | a 9,194 mm2 face against a 165 mm2 one |

53.5 mm is a commonplace distance on a part 177.5 x 41.8 x 101.0 mm. The best match by separation
pairs a large internal cap with a port, and a matcher built on position would have returned it
with no sign that anything was wrong. The names in that file are in the Icepak model's own frame,
around its own simplified manifold, and they cannot be put on this plate by geometry.

AND I SHOULD NOT HAVE GUESSED WHICH FACES WERE THE PORTS. I wrote above that the two 165.13 mm2
faces, 35.102 mm apart, were the ones a person would pick. The check has since run that plate to
the end and found its own, off the inside-space map's mouths, and it chose neither of them:

| opening | role | area | at (mm) | diameter |
|---|---|---|---|---|
| inlet | inlet | 109.36 mm2 | 65.92, 33.45, 75.41 | 11.8 mm |
| outlet_1 | outlet | 50.27 mm2 | 99.88, 54.68, 43.31 | 8.0 mm |
| outlet_2 | outlet | 50.27 mm2 | 89.34, 54.68, 77.39 | 8.0 mm |

Those three are in my own cap measurement to three decimals - 109.359, 50.265, 50.265 mm2 at the
same coordinates - so the hand measurement and the product agree about what is there. Only my
reading of which ones mattered was wrong. The real inlet-to-outlet separations are 51.326 mm and
31.672 mm, so the nearest is 2.174 mm off the file's 53.500 and it is not even among the thirteen
pairs that come within 1 mm of it. The conclusion stands and is stronger than when I guessed.

The door I would have had to open is already shut, and shut for the right reason: `_joined_assembly`
refuses a file whose format has a check mode of its own, with "a MODEL is not a part that failed to
be CAD". An ECXML cannot be stapled onto a STEP assembly by position. It should stay that way.

### The box

The bench already has a big one of their plate, from before the fitted domain existed:

| | |
|---|---|
| cells | 336,223,062 |
| cartesianMesh | 1,744.7 s execution, 2,374 s clock, 8 ranks |
| fluid | 66,764,096, Mesh OK, non-orthogonality max 48.65 |
| solid | 269,902,486, Mesh OK, non-orthogonality max 50.22 |
| block.stl | 3,493 bytes |

3,493 bytes is twelve triangles. Every run on that plate so far, all three workspaces, has filled
the cuboid. The domain measures 276,372 mm3 of a 750,015 mm3 box, share_of_box 0.3685 against a
BOX_WORTH_KEEPING of 0.6, so the fitted skin will be used from now on and it is 2.71x less volume
to fill. Most of those 269.9M solid cells are air.

## 2026-10-30: their plate went all the way to the engine, and two things killed the turn on the way

Their plate now reads correctly, end to end, on the real file through the page:

| | |
|---|---|
| upload | 15,883,539 bytes, AP214, 6 solids, 9,226 faces |
| split | 58 caps, 70 regions, 384.3 s |
| coolant | 52,144.56 mm3 through 2 openings, plus 1,517.23 mm3 behind 1 |
| pockets left out | 1, no leaks |
| read as | copper cold plate with internal water channels, solid-assembly, internal |
| openings | 3: inlet 109.36 mm2 d 11.8 mm, two outlets 50.27 mm2 d 8.0 mm |
| confirmed | stored with their positions and diameters intact |
| engine | cfmesh_conjugate |

The three openings are the same faces, to three decimals, that I measured by hand off
`candidate_caps` before any of this ran. The product and the hand measurement agree about what is
on the part.

### I overclaimed the gate fix and the record says so

Yesterday's fix lets measured coolant beat the scout's confidence. I said this run proved it. It
did not:

| | |
|---|---|
| scout confidence, input_kind | 0.70 |
| the naming's confidence | 0.93 |

0.93 clears 0.70, so the ordinary comparison opened the gate on its own and the measured-coolant
clause was never needed. The earlier run that produced 0.62 is the one the fix is for, and it is
unit-tested on those numbers, but today's run is consistent with either path and proves neither. An
adversarial read of the same code said the same thing independently, which is the only reason I
caught it.

### The API worker was killed for reading their CAD, and the log called it a death

The turn straight after the customer presses Proceed, three runs out of three: the worker handling
it is gone and the customer gets an empty reply. The log says

    INFO:     Waiting for child process [10]
    INFO:     Child process [10] died

There was no crash. uvicorn 0.32.1's multi-worker supervisor pings each worker over a pipe every
0.5 s and allows 5 s for an answer; answering needs the GIL, OCP does not release it while
OpenCASCADE reads and tessellates, and a worker that cannot answer is SIGKILLed with the comment
"process is hung, kill it".

| | |
|---|---|
| uvicorn --workers 2, what we ship | SIGKILLed 11 to 15 s in, 3 of 3 |
| uvicorn --workers 1, no supervisor | HTTP 200 in 44 s |
| a plain python process | exit 0, same answer |

faulthandler was armed for the two-worker run and printed nothing, which rules out every catchable
fatal signal, and the container never ran out of memory: 596 MB peak of 15.48 GiB with 12.3 GB
free. A ping nobody could answer is the only thing that fits all three rows.

So `measure_file` runs in a child now, whose GIL is its own, on the one path that runs inside the
API. This is not a new idea here: `fluid_volume.run_bounded` already does it for the split and says
why, and the lease keeper's file opens with the same lesson from 25 October. `measure_file` was the
one left inside. The loop's own lateness is the measure of it: `rank_in_time` has a 15 s budget and
fired at 34 s before the change and at 17.5 s after, so the block went from 19 s to 2.5 s.

IT IS NOT ALL OF IT. The worker still dies, now at 34 s instead of 15, so there is a second hold
after the engine is confirmed and the ports are restored. Not found yet.

### The naming gave up waiting and told nobody

| | |
|---|---|
| the naming waited | 1,016 s (its soft limit 1,076 less a minute) |
| the scout needed | 1,303.9 s |
| so it named the preview at | 1,022.2 s |
| and the customer saw | a finished check, 0 openings, no reason, for 5 min 16 s |

`preview_facts` builds those facts with `openings: []` and `confidence: {"input_kind": 0.0}` on
purpose and says so, "with no confidence in that". So the served proposal could not offer
solid-assembly, had no mouths and gave no reason. `_wait_for_scout` recorded its give-up only when
the stored record was missing or pending, never when it was a preview, which is the one case that
reaches a customer. It records it now.

I have not changed what that window serves. A record going out as `ready` while it says the part is
still being measured is a contract between the check and the page, and whether Proceed should be
offered on a preview is a product call, not a bug to fix quietly.

### One thing I nearly broke

MESH_THREADS is 8 and the geometry worker's container is held to 4 CPUs, so the pinned environment
oversubscribes by two. Clamping it looked free. It is not: this module's own opening says thread
count changes cfMesh's output, with 82.4 / 84.3 / 85.6 non-orthogonality on three identical runs,
so clamping would make the same input give a different mesh and would put the machine back inside
the pinned set. It stays as it is.

OpenCASCADE's own pool is a separate matter: it sizes itself from the HOST and no environment
variable reaches it, so inside that same 4-CPU container it launches 32 threads a job. The hook to
pin it has existed in `fluid_volume.main` since it was written, reading `task["threads"]`, and
nothing has ever set it. Whether setting it is worth anything is being measured, and the same run
says whether thread count changes the split's answer, which decides whether it is safe at all.

### The thread cap I was sure about, measured and dropped

The geometry worker's container is held to 4 CPUs (`NanoCpus=4e9`). Inside it `nproc` and
`sched_getaffinity` both say 32, `determinism.job_cpus()` correctly says 4, and OpenCASCADE's
default pool launches 32 threads a job because it sizes itself from the host and no environment
variable reaches it. Both of the operations that dominate ask for that pool: `fluid_volume` sets
`SetRunParallel(True)` on the volume maker in `_split` and on the fuse. Eight times
oversubscribed, on the one path with a wall clock. I was confident.

Same file, same container, one arm after the other so neither contended:

| | 32 threads, as it is | 4 threads, the cgroup's own |
|---|---|---|
| load | 32.9 s | 35.0 s |
| candidate_caps | 119.4 s | 147.7 s |
| extract, whole | 436.9 s | 443.2 s |
| caps | 58 | 58 |
| passages | 2 | 2 |
| regions | 70 | 70 |

Capping it is 1.4% slower overall and 23.7% slower on cap detection. So it is not a cost worth
paying attention to and the hook stays unused. `fluid_volume.main` has read `task["threads"]` since
it was written and nothing has ever set it; that remains true on purpose now rather than by
accident.

TWO THINGS THAT COME WITH IT. The first is that I had already written down that this
oversubscription explained the same file taking 379.5 s in one run and 711.1 s in another. It does
not explain anything: capping the threads moves neither number. The spread is real - 359.0, 359.2,
384.3, 436.9 and 711.1 s across runs of one file - and the threads are not its cause. The second is
that thread count does not change what the split finds here: identical caps, passages and regions
in both arms. That is worth having, because it is the opposite of what this module records about
cfMesh, whose thread count does change its output.
### The patch floor was one, and I said N was not payable. It was the wrong question

I left the kind-only PATCH obligation on cfmesh, snappy, snappy_multiregion and snapgrid in the
entry above, with the reason that exact coverage of a 2,000-part board is not payable against a
64-image session. That is true and it is not the question. An obligation feeds two checks, and
they cost completely different things.

| check | what it asks | what it costs |
|---|---|---|
| `eligibility.missing_target_obligations` | is every named target among the ones discovery found | a set comparison, before the model is asked. Nothing |
| `eligibility._check_obligation` | has every named target been inspected | one image each, out of 64 |

The first is the check that catches an entity the mesh HAS and the review surface LOST, which is
the whole shape of 575f76e6. It is free at any N. The second is the expensive one. They read the
same `exact_ids`, so naming every entity priced the free check at the expensive one's rate, and
the only way out was to name nothing and ask for one of any kind.

`TargetObligation.inspect_at_least` splits them. Every entity is named, so none can go missing
unnoticed; the inspection floor is what the budget affords.

WHAT THE RENDERABLE ENTITIES ACTUALLY ARE, which I had not read before writing that paragraph:

| engine | one renderable entity per | built by |
|---|---|---|
| cfmesh | named solid in the staged triSurface, i.e. the body's own patches | `read_stl_solids` → `build_review_msh` |
| snappy | the same, body only: the far field is in the blockMesh boundary, never in the triSurface | `review_surface_is_body_only` |
| snappy_multiregion | REGION | `constant/triSurface/<region>.stl` |
| snapgrid | placed PART | the named solids in `input.stl` |
| cfmesh_conjugate | REGION | `review_surface.build_region_msh` |

So two of the four are small by construction and two are one-per-body. The counts that have
actually been delivered:

| | entities | source |
|---|---|---|
| cfMesh / snappy | 1 to 3 | every patch roster in the tree: body + farfield, inlet + outlet + wall, sym + farfield + car_wall |
| snappy_multiregion | 2 | job bb5c9179, plate + derived air. 19-solid as1 is a stress fixture |
| snapgrid | 9 | job f8542f71, set_top_box.ecxml. 2,000 is a generated board |
| cfmesh_conjugate | 2 | always |

THE BUDGET, read off the code rather than guessed:

    MAX_IMAGES_PER_SESSION                                   64
    the opening view                                         -1
    the declared interior cuts (3 on the conjugate plate,
    at most 4 on a board: results.HOT_PARTS + 1)             -4
                                                            ---
    left for the reviewer                                    59

An inspection is one image (`toggle_patch`) and two when the reviewer frames it first with the
`go_to_coordinates` line the prompt hands it per patch, which is what the prompt steers and what
`_check_obligation` already says is not an inspection on its own. So a floor of k costs about 2k
of those 59. A floor that eats the budget leaves nothing to look twice with, and a review that
cannot look twice at what it found is not a review. A third of 59 is 19 images, about ten
targets.

**PATCH_INSPECTION_FLOOR = 10**, and `inspect_at_least = min(N, 10)`. Against the counts above
that is exact coverage on every job that has ever been delivered - 1, 2, 3 and 9 are all under
it - and a sample only on the two stress cases, where it is ten instead of one.

THE CAP IT HAS TO RESPECT, which the first version did not. `expand_patch_targets` stops at
`MAX_PATCHES = 512`, so on the generated 2,000-part board discovery offers 512 targets and no
more. Naming all 2,000 would have ended every such review as EXPECTED_TARGET_MISSING on a mesh
that is fine - the free check is only free if it names things that can be produced. The
truncation is the renderer's own: `patch_names()` is sorted and `expand_patch_targets` takes them
in order, so the first 512 sorted names are exactly the ids it will offer, which a test
re-derives from both sides rather than asserting.

WHAT A SAMPLED OBLIGATION SAYS WHEN IT IS NOT MET. Not the roster. Listing 1,990 outstanding ids
would BE the correction message and the reviewer would read it as an order to inspect all of
them. It gets the two counts, how many more are needed, and eight names to start from.

THE RESIDUAL, and it is the same shape as the one this closes. The names come from
`manifest["patches"]`, which is the review surface's own map, so for the two body-patch engines
the production check reads the export - the thing that fails. Total loss is still caught (no
renderable entity means no patch target, and empty discovery refuses the review); partial loss is
not. The region engines have a second, independent reading in checkMesh's per-region rows, and
cfmesh_conjugate uses it; cfMesh and snappy have none, because they deliberately render a SUBSET
of the delivered boundary and nothing declares which subset. `patch_face_counts` is the
independent authority and naming every patch with faces would oblige snappy's far field, which no
session can show. Closing it needs the engine to declare which of its delivered boundaries its
review surface is supposed to carry. None of them declares it today.

Also noted and not fixed: PATCH VIEWS writes one `go_to_coordinates` line per patch into the
system prompt, so the 2,000-part board puts 2,000 of them there. That is a prompt-size defect,
not an obligation one, and it is upstream of everything here.

tests/unit/agents/reviewer/test_the_patch_floor_is_what_the_budget_affords.py, 56 tests: the
arithmetic, every delivered count held to exact coverage, the two stress counts sampling at ten,
the free check still refusing a lost entity at N=300, the cap agreeing with the renderer on both
sides, and the correction message that must not become a roster.
`test_obligation_corrections` had a four-attribute stand-in for `TargetObligation` and now uses
the real one, because the stand-in could only report an AttributeError from inside the thing it
was testing.

The end-to-end check was already there and I had not looked: test_reviewer_assurance_matrix
drives the whole review loop per engine over manifests that carry real `patches` maps, so
snappy, cfMesh and snappy_multiregion now run it with NAMED patch obligations - every entity
named, every one inspected, verdict PASS - where they used to run it with the kind-only one.

### I blamed the machine for the gate not finishing, and it is one slow test

Correcting myself in the entry above, which says the full gate's pile of identical websocket
errors came of another session having this box on a meshing chain. It does not. Run again with the
box quiet - load 2.84 across 32 cores, the other session's mesher finished - it broke in the same
place:

| run | order | result |
|---|---|---|
| under load | random | 110 passed, 1 failed, 35 errors |
| quiet | random | 85 passed, 1 failed, 60 errors |
| quiet | `-p no:randomly` | 132 passed, 1 failed, 13 errors |

WHAT IT ACTUALLY IS. `test_thermal_results.py` runs a real mesher, `mesh_ecxml`, inside the test
process. That holds the interpreter long enough for the session browser's CDP websocket to miss a
keepalive ping, and it closes with `sent 1011 keepalive ping timeout`. The file takes about 110 s
where every other viewer file takes 7 to 13, and it sits on the edge of the timeout: it passes
when it reaches the end, and dies around 51 s when it does not. Because the `browser` fixture is
session-scoped, every test scheduled after that one errors at setup with the same message and no
other symptom.

In deterministic order it is second from last, so it strands 13 tests and the run reports 132. In
a random order it lands anywhere, which is why the figure moved between 85 and 132, and why a run
last week saw 143 pass: the order was kind. A whole-suite count is not the gate's verdict on the
page, and the clean 146 I went looking for was never going to exist in one invocation.

IT IS NOT ANYTHING I CHANGED, and I checked rather than assumed. Four runs of that file alone, two
with the served viewer.js as it is and two with the copy from before the detail-window hook went
in:

| served viewer.js | run 1 | run 2 |
|---|---|---|
| with the hook | failed, socket died at 51 s | 2 passed, 110.4 s |
| before the hook | 2 passed, 103.1 s | failed, socket died at 111 s |

One pass and one failure on each side. The flake is the fixture and the mesher, not the page.

Every other file passes run on its own: 144 of 146 over 30 files, and the two outstanding are that
file's, both of which have been seen to pass. One browser session per file is how to get a real
number until the thermal test stops running a mesher in-process or the fixture stops being shared.

THE LESSON IS THE ONE THIS FILE KEEPS TEACHING ME. A wall of identical errors out of tests/ui has
now had three separate causes - no Chrome installed, the wrong conftest collected, and this - and
each time I reached first for the explanation that needed no measurement. Twice it was wrong.

### A 336 million cell mesh passed both checkMesh runs and would not have solved

The big run landed. 336,666,582 cells across the two regions, both halves Mesh OK, and the report
says `ok: false`.

| | cells | max non-ortho | max skew | max aspect | pieces |
|---|---|---|---|---|---|
| fluid | 66,764,096 | 48.65 | 1.659 | 3.786 | 5 |
| solid | 269,902,486 | 50.22 | 1.653 | 4.222 | 1 |

Interface 21,637,041 faces, the same on both sides. Peak 240 GiB. Stage times:

| stage | s |
|---|---|
| preparePar | 0.6 |
| cartesianMesh | 2,399.6 |
| topoSet | 2,240.2 |
| splitMeshRegions | 6,073.3 |
| reconstructParMesh | 4,518.4 |
| checkMesh fluid | 654.3 |
| checkMesh solid | 1,903.3 |
| total | 17,789.7 (4.94 h) |

THE FLUID CAME OUT IN FIVE PIECES AND SHOULD BE TWO. The two are the inlet and outlet manifolds.
The other three are islands, which means the coolant circuit pinched off somewhere and the mesh
has passages that do not connect to the ports they are fed from. checkMesh does not look for this
and says nothing about it, so a mesh that cannot solve passes every check the mesher runs.

WHY IT PINCHED. The plan sized the cells off the narrowest passage it measured, 2.112 mm, and got
12 cells across it at a 0.3521 mm background with one level of refinement. The fluid came out at
93.84 um mean. But this part has a second scale: the measurement on it reads passages at
0.0982 mm and `thin_gap_m` 4.1e-05, which is 41 um. A 93.84 um cell cannot hold a 41 um gap open,
so the fine branches close and the circuit splits. The two scales are the same thing the engine
refuses two-scale parts for; the refusal is right and this run went round it by being driven
stage by stage on the bench instead of through the engine.

The workspace is gone. I have the report and the stage logs, not the mesh.

### The fitted skin is a net loss on this part and I stopped the run

Both runs on 8 ranks, 0.3521 mm background, 1 level, only the block changed.

| | cartesianMesh | cells |
|---|---|---|
| box | 2,399.6 s | 336,223,062 |
| the part's own skin | 14,794 s and not finished | 269,650,437 |

So 6.2x the time on the stage that dominates, to mesh 19.8% fewer cells, and it was still going.
I killed it at 4 h 06 m. Eight ranks at 99% CPU and 162 GiB resident for that whole time.

WHERE IT GOES WRONG IS NOT THE SURFACE. Surface untangling converges to 0 bad faces on the fitted
skin, same as the box. The volume smoothing phase after it is where the bad faces appear, and they
do not clear: 1,362 at first, up to 9,523, 5,283 when I stopped it. The skin is 27,696 triangles
and 36.9% of the box volume, so the geometry it has to conform to is not the problem either.

THE GATE CURRENTLY PICKS THIS PATH FOR THE CUSTOMER'S PART. `use_shell_domain` compares
`share_of_box` 0.3685 against BOX_WORTH_KEEPING 0.6 and returns True, so a production run of
ColdPlateAssembly_CLEAN.step fills the skin rather than the box. On the only part either has been
measured on, that is the slower and worse of the two by a wide margin.

I am not flipping the gate on this. The fitted run never finished, so I have "6.2x and still
going" and not "finishes and is worse", and a gate that decides which domain to mesh for every
part deserves a completed comparison. What the measurement does say is that volume share is not
the thing to decide it on, because the cell saving it predicts is off by a factor of three and the
time cost does not appear in it at all.

### I built the index and never called it, and three reviewers found that before I did

I put a boundary index in `render/boundary_index.py`, wired `detail_window` to use it, measured it
on the customer plate, and reported that the detail window now works at any size. Then I had the
design for the next piece attacked from three angles, and all three opened with the same sentence:
`write_index` has no caller in `src/`.

    $ grep -rn "write_index" src/ --include=*.py
    src/meshpipeline/render/boundary_index.py:259:def write_index(...)
    src/meshpipeline/render/boundary_index.py:590:    "has_index", "index_dir", "open_index", "write_index"]

Its own definition and its own `__all__`. Every index that existed, I had written by hand on the
bench. In production the reader fell through to the parse on every mesh, and the parse refuses
anything over 2,000,000 faces, so every detail request on this plate still refused. The mechanism
to answer them sat one call away and nothing made the call.

It is wired now, in `native._boundary_indexes`, beside the review aids and under their rule: built
where the mesh is, recorded per region including when it failed, and unable to fail the run.

WHAT I SHOULD HAVE DONE is run that grep myself. I measured the pass, I measured the query, I drove
the route and the shipped page, and the one thing I did not check was whether anything in the
product would ever reach it. Each of those measurements was real and none of them was the question.

### What the review did to the heatmap design, which was worse than wrong

The design was: compute the same per-face quality the viewer shows today, in the worker, reduce it
to one value per boundary face, store it in the index, serve colours from an mmap. Three lenses,
all three refuted, all three fatal, and on different grounds.

**It is not a boundary pass.** `owner_cell_max` is a maximum over a cell's internal faces taken
from both sides, so it needs owner, neighbour, every internal face's area vector and every cell's
centre. The index gets away with 105 s and 623 MB precisely because it touches none of that: it
counts past 221,872,725 internal records without parsing them and never opens `owner`. Priced on
this mesh's own counts:

| region | cells | faces | resident floor for the pass |
|---|---|---|---|
| fluid | 75,162,351 | 235,054,410 | 25.6 GB (36.1 GB at the rate the cap was measured at) |
| solid | 107,107,563 | 332,328,927 | 36.3 GB (51.1 GB) |

Against a 16 GB worker container and the 8 GiB that MAX_FACES_FOR_FIELDS was actually priced
against. I was off by about 60x. And the parse alone exceeds it before a metric exists: `_body` is
`read_bytes()` and `_read_faces` runs it twice, which is 18.2 GB of bytes objects on the fluid's
9.1 GB faces file. There is no RLIMIT_AS on that path, so the failure is a kernel kill of the
delivery, not a MemoryError the `except Exception` could swallow. A heatmap would cost the mesh.

**The thin circuit makes it worse, not better.** I assumed a wall shell would be cheap. On this
coolant 16.8% of fluid cells touch metal and `owner_cell_max` needs the one-ring, which is 32.0%.
Those one-rings carry about 154M of the 567M faces, still 3.9x the cap. High surface-to-volume is
the reason a shell is not a saving here.

**The main surface cannot take a per-face array at all.** 13,181,685 delivered faces against
DRAWN_MAX_TRIANGLES 800,000 means the payload is always reduced, and the client uses an array only
when its length equals the drawn polygon count. A 13.2M array lands as silent grey, which reads
exactly like a clean patch. Reduction is vertex clustering, not face grouping, so no face-to-
triangle map exists to fix it with, and inventing one is the decision already on record against:
"a colour on it was measured on none of the faces it stands for".

**The detail cut is the one place it works.** `detail_at` returns unmerged delivered faces, one
polygon per face, packed in the ids' own order, so `quality[idx]` gathered with the same ids lines
up exactly. One value per delivered face is one value per drawn polygon there and nowhere else.

**And the measurement should not be re-derived at all.** `checkMesh -writeAllFields` writes
nonOrthoAngle, skewness, aspectRatio and cellShapes as volFields, in the process that already runs
checkMesh per region and already holds the mesh. That route was already in this file at line 669 as
"the cheaper route than re-deriving in numpy" and I walked past it.

### Two defects in the index itself, one mine and one older

`patch_of` was `np.zeros`. The pass reads the whole boundary span because one pass is cheaper than
two, so a `skip_names` leaves a hole in the middle that is read and that no patch names - and at
zero those faces came back under the FIRST patch's name. A reader who asked for the coolant being
handed the metal, silently. It is a sentinel now, and `patch_slices` drops what no patch names.
Latent only because nothing called `write_index`; the conjugate viewer passes
`_SKIP = ("solid_to_fluid",)`, so wiring it would have made it live in the same commit.

The older one: the detail route passes no `skip_names` while the main surface drops
`solid_to_fluid`. A conjugate detail therefore draws both sides of the interface as two coincident
face sets at the same coordinates. I saw 103 faces on each side come back from the route and
reported it as the interface agreeing on both sides. It is agreement, and it is also two surfaces
drawn on top of each other. Not introduced here and not fixed here, but it is the reason the main
view skips one of them and the detail view should have a reason too.

### The colour chain, measured end to end on the 182 million cell mesh

| region | cells | checkMesh + fields | fields written | index | reduce | reduced |
|---|---|---|---|---|---|---|
| fluid | 75,162,351 | 652 s | 1.80 GB | 106.5 s / 622.9 MB | 34.9 s | 158.2 MB |
| solid | 107,107,563 | 878 s | 2.45 GB | 127.9 s / 633.6 MB | 59.9 s | 160.9 MB |

So 4.25 GB of per-cell numbers become 319 MB of per-boundary-face numbers, and only the 319 MB
is any use downstream. The whole chain is about 32 minutes on a mesh that took 4.9 hours to build.

THE REDUCED FIGURES ARE CHECKMESH'S OWN, which is the check that the reduction is right:

| | reduced max | checkMesh said |
|---|---|---|
| fluid skewness | 0.70451 | Max skewness = 0.704509 |
| solid skewness | 0.69126 | Max skewness = 0.69126 |
| solid aspect ratio | 2.09807 | Max aspect ratio = 2.09807 |
| fluid non-orthogonality | 15.70370 | Mesh non-orthogonality Max: 18.7226 |
| solid non-orthogonality | 15.54370 | Mesh non-orthogonality Max: 22.3609 |

Exact where the metric belongs to a face, and LOWER where it belongs to a cell - which it must
be, because the cells that touch the boundary are a subset of the mesh. A boundary reduction that
reproduced the whole-mesh non-orthogonality would mean the worst cell in the part happened to sit
against a wall, and it would be worth checking rather than believing.

### +15.1% was an upper bound and I nearly wrote it down as a constant

The entry above measured `-writeFields` at +15.1% on a 5,686,631-cell region and I put that figure
in `foam_exec.check_mesh` as the cost of the flag. At the size that matters it is much less:

| region | cells | fields | per cell |
|---|---|---|---|
| ws_par300 fluid | 66,764,096 | no | 9.8 us |
| ws_par247 fluid | 75,162,351 | yes | 8.7 us |
| ws_par247 solid | 107,107,563 | yes | 8.2 us |

Not a controlled A/B - different meshes - but enough to say the flag is not a 15% tax at 100 M
cells. checkMesh walks every cell and every face before it prints a summary either way; writing
down what it found is lost in that walk once the walk is big. A small region is where the write
costs most, which is the opposite of where I would have guessed, and it is why the comment now
carries both numbers instead of the convenient one.

### The version guard earned itself the same day

`INDEX_VERSION` went 1 to 2 this morning when the staleness stamp changed. The 182 M mesh's index
had been built that morning at version 1, and the reduce refused it:

    IndexUnavailable: .../boundaryIndex: index version 1, this reads 2

Which is the whole point: the quality arrays are numbered against the index's face numbering, and
reading version 1 arrays under version 2 rules would have produced a file that looked right and
coloured every face with some other face's number. It cost a 234-second rebuild. Nothing in the
product can hit it - `native._boundary_indexes` writes the index and then the quality in the same
pass, so they are always the same version - and it only bit me because I had built the index by
hand hours before.
