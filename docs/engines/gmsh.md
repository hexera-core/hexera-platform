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
