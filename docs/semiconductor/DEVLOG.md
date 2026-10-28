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
