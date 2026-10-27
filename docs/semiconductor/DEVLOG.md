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
