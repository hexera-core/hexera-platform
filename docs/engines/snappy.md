# snappy

**snappyHexMesh**, the production 3D flow engine. It produces a body-fitted OpenFOAM polyMesh
with prism layers on the wall, from a surface.

Choose it when boundary-layer resolution matters. For a fast draft mesh, or for 2D, use
[cfmesh](cfmesh.md).

| | |
|---|---|
| Accepted input | a **surface** (body surface) |
| Dimensionality | **3D only** |
| Purposes | `external_cfd`, `internal_cfd` |
| Capability | body-surface → fluid-volume, internal and external topologies |
| Deliverable | `openfoam_case.tar.gz`, marked by `constant/polyMesh/owner` |
| Exports | `openfoam_polymesh`, `stl_surface` |
| Symmetry plane | **yes**: the only engine that supports one |
| Multiple wall patches | no |

## What it produces

| Member | Kind | Required |
|---|---|---|
| `constant/polyMesh` | directory | yes |
| `system/blockMeshDict` | file | yes |
| `system/snappyHexMeshDict` | file | yes |
| `constant/triSurface` | directory | yes |

The background mesh dictionary and the surface it snapped to are part of the deliverable, not
scaffolding: without them the case cannot be regenerated or understood.

## Native lifecycle

`blockMesh` builds the background mesh, `surfaceFeatureExtract` finds the features to capture,
then `snappyHexMesh` castellates, snaps and adds layers. Boundary patches are created and typed
afterwards and reconciled against what was approved.

The engine can run its stages in parallel where the case supports it. `blockMesh.log` marks when
an attempt began, which is how the completeness check distinguishes this attempt's output from a
previous one's leftovers.

## Authoring controls

No free intake parameters. The builder authors the dictionaries through the engine's palette, and
a deterministic planner derives the strategy from the geometry, the model chooses strategy values
and never hand-writes engine syntax.

## Gates

| Gate | Proves |
|---|---|
| `manifest_valid` | the mesh is structurally sound: no negative-volume, open or mis-oriented cells |
| `patch_contract` | every boundary you named exists in the mesh and carries real faces |
| `boundary_types` | each boundary is typed as the solver needs it (wall / symmetry / empty) |
| `quality_floor` | skewness is localized and the body is captured on the wall |

## Quality criteria

`wall_faces`, `skew_fraction`, `layer_coverage`, `max_non_ortho`, `fatal`, plus `rc` and
`timed_out`.

`layer_coverage` is what distinguishes this engine from a Cartesian one: it measures how much of
the wall actually received the prism layers that were requested. A mesh that snapped correctly but
grew almost no layers has not done the job snappy was chosen for.

## Review

Two resolved artifacts, `mesh_paths.surface` (required) and `mesh_paths.volume` (optional), with
`patch:*` and `region:*` inspection targets. The same present-but-unreadable rule applies as for
cfmesh: an unusable optional artifact is refused, not silently skipped.

## Runtime requirements

OpenFOAM with snappyHexMesh, from the mesh image.

## Intentional limitations

- **3D only.** A 2D flow mesh is cfMesh's job, through its native 2D mesher.
- One wall patch. Geometry needing several distinct wall boundaries is cfMesh's or the
  multi-region engine's case, depending on the physics.
