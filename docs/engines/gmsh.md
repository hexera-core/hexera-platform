# gmsh

**Gmsh**, the FEA and solid-domain engine. It produces element decks from solid CAD: second-order
tetrahedra from a closed volume, plane-stress triangles from a planar face.

It is the only engine that serves `structural`, and the only one whose input is a **solid** rather
than a surface.

| | |
|---|---|
| Accepted input | a **solid** |
| Dimensionality | 2D and 3D |
| Purposes | `structural`, `external_cfd`, `internal_cfd` |
| Capabilities | solid-body → solid-volume; fluid-domain → fluid-volume (internal and external); planar-domain → surface-mesh |
| Deliverable | `gmsh_case.tar.gz`, marked by `mesh.inp` |
| Exports | `abaqus_inp`, `nastran_bdf`, `gmsh_msh`, `unv` |

Three declared capabilities, which is why one engine covers both FEA and CFD: it tetrahedralizes a
solid body for structural analysis and a supplied fluid domain for flow.

## What it produces

| Member | Kind | Required |
|---|---|---|
| `mesh.inp` | file | yes |
| `gmsh_spec.json` | file | yes |
| `geometry.step` | file | no |
| `mesh.bdf` | file | no |
| `mesh.unv` | file | no |

`gmsh_spec.json` is required alongside the deck: it is the declarative, re-runnable description
the deck was generated from, so a delivered mesh can be explained and regenerated.

## Native lifecycle

The driver runs as a module of the installed distribution rather than as a loose script, so the
mesh image and the wheel cannot drift apart. It reads the STEP solid, applies the authored spec,
meshes, and writes the deck plus the optional alternative formats.

## The deck (`mesh.inp`)

The deck is written by `engines/gmsh/abaqus_deck.py`, not by gmsh's own Abaqus writer. gmsh's
writer put every named face in the file as flat 2D elements (CPS6) with no section, and
CalculiX refused the file (`gen3delem`). The deck now holds:

- the 3D elements only (`C3D10`, or `C3D4` for first order), all in `ELSET=solid`;
- every CAD face as `Face_1 ... Face_N`: an element-face `*SURFACE` and an `*NSET` of the same
  name, so supports and loads can be put on any face in the user's own solver;
- each group the plan named (`fixed`, `load`, the default `free`, ...) as an extra surface and
  node set over the same faces;
- a face's own name from the CAD file, where the STEP gives one (`cad/face_names.py`), as one more
  surface and node set beside its `Face_k`.

The header lists which `Face_k` is which CAD face and states the length unit (metres).

Tested for real in CalculiX 2.21 (static and modal solves on the named faces, textbook answers in
`tests/unit/engines/test_gmsh_fea_accuracy.py`). Abaqus reads the same keyword subset, but the
deck has **not yet been tested in the real Abaqus solver**: it is checked by file readers (meshio,
and an input-rules check in `tests/unit/engines/test_gmsh_fea_deck.py`).

## The solver files of an FEA mesh (FEA build step 6)

A mesh built from an FEA plan is written once and exported for every version-1 solver
(`engines/fea_writers.py`), with the same node and element numbers and the same `Face_k` in
every file, lengths in metres:

| File | Solver | Element | Every CAD face as | Tested |
|---|---|---|---|---|
| `mesh.inp` | CalculiX, Abaqus | C3D10 | `*SURFACE` (element faces) and `*NSET` named `Face_k` | solved in CalculiX; **Abaqus not yet tested in the real solver** |
| `mesh.bdf` | Nastran (MSC / NX), bulk data to INCLUDE | 10-node CTETRA, PSOLID 1 -> MAT1 1 | `SET3` of its grids and `BSURFS` of its element faces, both numbered k (names in comments) | **not yet tested in the real solver** |
| `mesh.cdb` | Ansys MAPDL | SOLID187 | node component `FACE_k` (and one per readable CAD name) | **not yet tested in the real solver** |

The 10-node node order is the same in all three (corners, then the midpoints 1-2, 2-3, 3-1, 1-4,
2-4, 3-4). Without Abaqus, Nastran or Ansys licences, these files are checked by reading them back:
our own readers in the unit tests, and in the lab pyNastran, meshio and ansys-mapdl-reader, which
find the same nodes, elements, face sets and positive element volumes as the deck.

Each file is its own download (an optional `mesh` artifact under `export_*`), labelled with
whether its solver has really run it; the bundle carries them all. The result screen gives each
solver a pass / warnings / fail line by that solver's own published element checks
(`engines/fea_solvers.py`).

## Textbook accuracy (FEA build step 7)

`tests/unit/engines/test_fea_textbook.py` meshes six classic parts with the real FEA driver and
solves them in CalculiX on the deck's own named faces: plate with a hole (Kt), cantilever (tip
stiffness), Lame cylinder (bore hoop stress), U-grooved bar (Kt), cantilever modes and an Euler
column. Each is held to its closed-form answer (3% on stress, 1% on stiffness, frequency and
buckling load). CI's "FEA accuracy (CalculiX)" lane installs Ubuntu's `calculix-ccx` and runs it at
Quick on every change and at Standard on the weekly and manual runs.

## Authoring controls

| Parameter | Meaning |
|---|---|
| `element_order` | 1 for linear elements, 2 for second-order |

Second-order elements matter for structural accuracy in bending; the choice is the user's because
it trades solver cost against fidelity.

## Gates

| Gate | Proves |
|---|---|
| `manifest_valid` | the element deck was written and parses back, with no fatal defects |
| `patch_contract` | every named group you asked for exists in the deck |
| `sicn_floor` | no degenerate elements: every element clears the quality floor for FE assembly |

## Quality criteria

`min_sicn`, `sicn_low_fraction`, `fatal`, `rc`, `timed_out`.

SICN (signed inverse condition number) is the FE-relevant measure here rather than
non-orthogonality: what breaks an assembly is a degenerate or inverted element, not a skewed
finite-volume cell. The floor is gating; the low fraction bounds how much of the mesh may sit near
it.

## Review

One resolved artifact, `mesh_paths.surface`, with `group:*` inspection targets, gmsh's named
groups are the boundaries a user asked for.

Review is metric-led: the measured evidence carries most of the verdict, because element quality
in an FE deck is a number rather than something judged by eye.

## Runtime requirements

Gmsh and the OpenCASCADE stack, from the mesh image.

## Intentional limitations

Advanced Gmsh modes are deliberately not exposed: periodic boundaries, embedded curves and
surfaces, transfinite meshing, shell export and quad-dominant meshing. Requesting one is rejected
at admission rather than half-supported.
