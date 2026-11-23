# ECXML conformance lab

Two questions about an uploaded `.ecxml`, asked of real files rather than of the code:

1. **Does it give the same answer twice?** The same file read, placed, planned and meshed again has
   to produce the same mesh, or a customer cannot be told anything about the one they were sent.
2. **Does it work with any file, and refuse the rest in words?** A file the mesher cannot take has
   to say why. A traceback is not a refusal.

## What it does

    python3 make_lab.py                 # write 29 .ecxml files into ecxml_lab/models
    python3 determinism.py              # read / place / plan, each twice, compared canonically
    python3 determinism.py --hash       # one hash per file and stage, for comparing interpreters
    bash    mesh_all.sh                 # mesh every model TWICE through the deployed image
    python3 compare.py                  # compare the two passes, file by file

`mesh_all.sh` and `mesh_one.sh` run `meshpipeline.engines.snapgrid.cli` inside the deployed mesh
image, the same way `devtools/semiconductor/run.sh` runs `semi.py`: the repository at `/work`, the
lab at `/lab`, the caller's uid, no network. Set `HEXERA_MESH_IMAGE` to pick the image.

## The three groups, and why each exists

| group | files | what they are |
|---|---|---|
| `shipped` | 5 | the sample models the reader's own tests use: a vented set-top box, a ducted board, a tiny board, a wirebond package with and without a cap gap |
| `awkward` | 15 | valid files that are hard |
| `hostile` | 9 | files that must be refused |

`awkward` is the interesting one. All sixteen geometry elements at once. The same overlapping boxes
under Flotherm's later-wins rule and Icepak's embedded-solid-wins rule, which make the same file two
different models. Four levels of assembly, against the same blocks written flat, because an assembly
only groups and the two must give the same grid. Inactive objects, one on its own and one inside an
inactive assembly. Float32 values padded to fifteen decimal places, which is what a real export
looks like. A ten-nanometre film beside a ten-millimetre block: six decades of length in one file.
Faces that coincide exactly. A 2D object on each of the six planes. Non-ASCII names and the three
characters XML escapes. A file with no geometry at all. A material referenced but never declared.

`hostile` is a DTD, an internal entity, a billion-laughs entity, an external entity pointing at
`/etc/passwd`, three hundred levels of nesting, a zero-size block, a negative size, a wrong root
element, a truncated file, and bytes that are not XML. An upload is untrusted, and every one of
these has to come back as a sentence.

## What it measured

Run 2026-11-23, image `5f88e7de3afd`, `--level draft --ascii`.

| | result |
|---|---|
| read / place / plan, twice each, 29 files | **60 SAME, 27 REFUSED, 0 DIFFER, 0 CRASH** |
| the same, across three interpreters (`PYTHONHASHSEED` 1, 98765, random) | **87 of 87 hashes identical** |
| meshed twice, 20 valid files, every file in the case compared | **20/20 byte-identical** |
| the 9 hostile files, meshed twice | **exit 2 both times, every one** - the CLI's "cannot be meshed", with the reason printed |

Across the twenty valid models that is 780 files: `points`, `faces`, `owner`, `neighbour`,
`boundary`, `cellZones` for every region, `regionProperties`, `thermal_model.json`,
`snapgrid_viewer.npz`, and every `fvSchemes` and `fvSolution`. Not compared: OpenFOAM's own logs and
the VTK dumps, 249 files, which carry wall-clock times and dates.

**One honest caveat.** `snapgrid_report.json` differed between the two passes in exactly ten key
paths, and every one of them describes the MACHINE: eight `timings_s.*`, plus
`memory.available_bytes` and the `memory.cells_that_fit` that follows from it. On a model large
enough for the cell budget to bind, free memory lowers the budget, so the mesh is deterministic
**given the same machine state** and not absolutely. On every model in this lab the budget never
bound and the meshes matched to the byte.

## What it found

Two defects, both fixed, both with a regression test that fails against the version before the fix.

**A flat vent was refused.** A grille in Flotherm is a face on a wall and has no thickness, so an
export writes zero for the size on the plane's axis. The reader kept two lists - the objects whose
plane IS the object, and the objects allowed a zero size - and the grille was in the first and not
the second, although `ecxml_place` puts a grille through the same branch as the 2D fan listed beside
it. The two lists are now one name (`ecxml.ZERO_SIZE_ALLOWED = TWO_D`), so a future element cannot
be added to one and not the other. Test: `tests/unit/cad/test_a_flat_vent_is_a_vent.py`.

**A file with no solid crashed the mesher.** Three real files land there: a set of 2D plates with no
thickness, a file whose `<geometry>` is empty, and a file whose every object is inactive. In all
three the zone array is AIR throughout, so nothing was ever going to select a part - but `np.where`
evaluates both of its branches, and indexing an empty part table raises before the `where` is
reached:

    cell_zone = np.where(zone >= 0, part_zone[np.maximum(zone, 0)], airs)
    IndexError: index 0 is out of bounds for axis 0 with size 0

The fix skips the indexing rather than merely not selecting it, and the build report now says what
kind of mesh the file asked for. Test:
`tests/unit/engines/test_a_file_with_no_solid_is_still_a_mesh.py`.

## What it did NOT find, having looked

An undeclared material is **not** silent. The reader notices it and writes a note, and the note
reaches `placement_sidecar`'s own `notes`. The region it describes does carry a `material` key that
does not resolve in the sidecar's own `materials` map, which a consumer reading the JSON and not the
prose would find dangling - but the product says so, in words, and refusing a whole model over one
typo'd material name would be the worse answer.

## And through the product, not just the engine

Run 2026-11-23, job `41fb5cb3-5ba0-40ba-81ed-ce4e89dfc351`, local stack, Cloud Run mesh. The lab
above exercises the ENGINE. This is the same file going through the thing a customer touches.

    POST /api/v1/upload/step-file   set_top_box.ecxml, 8,155 B      ->  session
    the intake read it              "a Flotherm thermal model with 8 parts, 8.7 W in total
                                     and 35 °C room air"
    the geometry check              route thermal, unit_needed False, status ready, 9 regions
                                     named as in the file: air, Case, PCB, SoC, DDR_1, DDR_2,
                                     PMIC, Bulk_cap, SoC_heat_sink
    engine_select                   snapgrid
    the mesher                      Cloud Run, 108 s, executor success=True
    the reviewer                    ran; the page says PASS
    delivered                       498,657 cells in 9 regions, max non-ortho 62.6°,
                                     max skewness 2.66

`/api/v1/client-config` offers ECXML to the browser as a first-class format - `.ecxml, .xml`,
`declares_units: true`, `kind: cad` - and the upload page names it in its own accepted-formats line.

**The deliverable was downloaded and opened, because "ready to download" is a claim about a
button.** All three artifacts came back at the byte counts the product stated:

| artifact | bytes | what it actually is |
|---|---|---|
| OpenFOAM case (multi-region) | 18,953,794 | 71 files, 59.3 MiB unpacked, **a polyMesh for every one of the 9 regions**; `constant/regionProperties` reads `fluid (air)` / `solid (Case PCB SoC DDR_1 DDR_2 PMIC Bulk_cap SoC_heat_sink)` |
| Surface preview (.msh) | 50,666 | `$PhysicalNames 8`, every ECXML part under its own name |
| CAD inspection report (.json) | 1,555 | the inspection, declined because repair is off in this deployment |

and the physics came through unchanged:

| region | type | material | power |
|---|---|---|---|
| air | fluid | - | - |
| Case | solid | Steel sheet | 0.0 W |
| PCB | solid | FR4 board | 0.0 W |
| SoC | solid | - (a twoResistorModel has none) | 6.0 W |
| DDR_1, DDR_2 | solid | Mold compound | 0.6 W each |
| PMIC | solid | Silicon | 0.9 W |
| Bulk_cap | solid | Aluminium 6063 | 0.1 W |
| SoC_heat_sink | solid | Aluminium 6063 | 0.0 W |

`total_power_W` 8.7, five materials, and the region cell counts sum to the 498,657 the page shows.

**And the viewer draws the interior.** The page offers named slices - "Across PCB, mid-plane",
"Through SoC (6.5 W)", "Through PMIC (0.9 W)", "Through DDR 1 (0.6 W)" - and a slice renders the
real cells through the part with one colour per region and a legend, beside a per-part table of
cells and thinnest dimension. What the page says about the mesh it says in figures, not adjectives:

    470,026 of 498,657 cells (94.3%) are plain boxes aligned with the model; the other 28,631 sit
    where the grid steps finer or wraps a round part. Worst angle 63°; no bad cells.
    Thinnest layer: PMIC, 1 mm with 3 cells through it (asked for 3).
    Air gaps: 10 measured; the fewest cells across one is 4 (gap inside SoC_heat_sink, 2 mm; asked 4).
    Heated surfaces: 7 parts; the warm-air layer on each has at least 6 cells (asked 6).
    Round parts follow the file within 0.4% of their volume (Bulk cap, Case); box parts are exact.

### Two things worth knowing about that run

**The worker's credentials are a FILE BIND MOUNT, so refreshing them on the host is not enough.**
The first attempt failed in 15 seconds with `RefreshError: Reauthentication is needed`, after
`gcloud auth login --update-adc` had already succeeded. `docker-compose.yml` mounts
`${GOOGLE_ADC_FILE}:/gcp/adc.json:ro`, which points at the repository's own
`secrets/gcp/application_default_credentials.json` and not at `~/.config/gcloud`. A bind mount
follows the INODE, so `gcloud` writing a new file leaves the container reading the old one, and
`docker compose up --force-recreate` does not help because the path it re-resolves still holds the
stale copy. Copy the refreshed file over the repository one IN PLACE - `cp` keeps the inode, so the
running container sees it at once, with no restart.

**The pipeline was honest about it, which is the part worth keeping.** It retried, noticed the
authored mesh spec had not changed (`consecutive_noops=1`), stopped rather than burn three more
attempts, recorded `cause=run_infrastructure`, and `engine_fallback` declined to switch engines
because "another engine cannot change this failure". None of that pointed at the customer's file.

**A CAD surface inspection runs on an ECXML model and finds defects that are the format.** The
inspection reads the derived `source.stl` and reports 2 duplicate faces and 18 non-manifold edges
with "Repair recommended before meshing". ECXML boxes touch and overlap by design (JEP181A 4.5.1
resolves the overlap by precedence) and `snapgrid` never reads that STL - it reads the boxes and
paints them onto a grid. It blocks nothing, and the job is delivered, but a customer is handed a
"CAD inspection report" saying their thermal model has non-manifold edges when it has none. Open.
