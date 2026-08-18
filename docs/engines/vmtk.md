# vmtk

**VMTK**, the vascular engine. It produces a centreline-based, radius-adaptive tetrahedral volume
mesh that fills a closed lumen, from a surface.

It serves internal flow only, and its vocabulary is anatomical rather than mechanical: openings,
branches and layer regions instead of patches and zones.

| | |
|---|---|
| Accepted input | a **surface** (body surface) |
| Dimensionality | 3D |
| Purposes | `internal_cfd` |
| Capability | body-surface → fluid-volume, **internal only** |
| Deliverable | `vmtk_case.tar.gz`, marked by `mesh.vtu` |
| Exports | `vtu`, `vtp`, `gmsh_msh`, `stl_surface` |

## What it produces

| Member | Kind | Required |
|---|---|---|
| `mesh.vtu` | file | yes |
| `vmtk_spec.json` | file | yes |
| `lumen.vtp` | file | yes |
| `centerlines.vtp` | file | no |
| `surface_remeshed.vtp` | file | no |
| `mesh.msh` | file | no |

`lumen.vtp` is required, not incidental: the extracted lumen is what the volume mesh was built
inside, and it is the artifact a reviewer needs to judge whether the right cavity was filled.

## Native lifecycle

The surface is remeshed, the lumen is closed and capped, centrelines are computed, and the volume
is filled with tetrahedra whose size adapts to the local radius, a narrow vessel gets finer cells
than a wide one without a global refinement. Boundary layers are grown inward from the wall.

## Authoring controls

| Parameter | Meaning |
|---|---|
| `wall_layers` | how many boundary-layer cells to grow inward from the vessel wall |

## Gates

| Gate | Proves |
|---|---|
| `manifest_valid` | the volume mesh was written and parses back, with no fatal defects |
| `patch_contract` | the wall and every inlet/outlet cap are present and match what you declared |
| `quality_floor` | no inverted or degenerate tetrahedra |

The patch contract is anatomical here: a vascular mesh with the wall but a missing outlet cap is
not a usable domain, and the number of caps is a fact about the geometry the user described.

## Quality criteria

`min_quality`, `cells`, `layer_coverage`, `fatal`, `rc`, `timed_out`.

`cells` is a criterion rather than a statistic because a radius-adaptive mesh that collapsed to
very few cells has silently failed to resolve the vessel.

## Review

Three resolved artifacts, the most of any engine: `mesh_paths.surface`, `mesh_paths.lumen` and
`mesh_paths.centerlines`. Inspection targets are `opening:*`, `branch:*` and `layer_region:*`.

Review is metric-led, like gmsh: what makes a vascular mesh usable is measurable.

## Runtime requirements

VMTK and VTK, from the mesh image.

## Intentional limitations

Internal flow only. An external-flow capability is not declared, so requesting one is rejected at
admission rather than attempted.
