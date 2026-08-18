# cfmesh

**cfMesh (`cartesianMesh`)**, the fast Cartesian flow engine. It produces an OpenFOAM polyMesh
from a surface, in 2D or 3D.

Choose it when you want a flow mesh quickly and the geometry does not demand body-fitted prism
layers. For production external aerodynamics with boundary layers, use [snappy](snappy.md).

| | |
|---|---|
| Accepted input | a **surface** (body surface) |
| Dimensionality | 2D and 3D |
| Purposes | `external_cfd`, `internal_cfd` |
| Capability | body-surface → fluid-volume, internal and external topologies |
| Deliverable | `openfoam_case.tar.gz`, marked by `constant/polyMesh/owner` |
| Exports | `openfoam_polymesh`, `stl_surface` |
| Multiple wall patches | yes |
| Symmetry plane | no |

## What it produces

An OpenFOAM case whose required members are:

| Member | Kind | Required |
|---|---|---|
| `constant/polyMesh` | directory | yes |
| `system/meshDict` | file | yes |
| `geom.stl` | file | yes |
| `geom.fms` | file | no |

Delivery means all required members are present. A case with a `meshDict` but no `polyMesh` has
not delivered, and is not reported as success.

## Native lifecycle

The engine runs `cartesianMesh` for 3D and the native `cartesian2DMesh` for 2D, genuinely
different binaries, not a 3D mesh with one cell in the third direction. Boundary patches are
created and typed after meshing, and the resulting boundary is reconciled against what the user
approved.

Each OpenFOAM command is bounded by `OPENFOAM_COMMAND_TIMEOUT`. The builder loop that drives them
must be at least twice that, which is enforced at startup.

## Authoring controls

cfMesh declares no free intake parameters: its strategy is derived from the geometry rather than
asked of the user. The builder authors `meshDict` through the engine's configuration palette, and
the palette rejects unknown, cross-engine or out-of-range fields loudly rather than passing them
to the mesher.

## Gates

All blocking, a failure here means the mesh never reaches review.

| Gate | Proves |
|---|---|
| `manifest_valid` | the mesh is structurally sound: no negative-volume, open or mis-oriented cells |
| `patch_contract` | every boundary you named exists in the mesh and carries real faces |
| `boundary_types` | each boundary is typed as the solver needs it (wall / symmetry / empty) |
| `quality_floor` | the mesh clears every quality bar cfMesh requires, with no fatal topology defects |

## Quality criteria

Measured and compared against declared bars: `max_non_ortho` (maximum non-orthogonality),
`fatal` (fatal topology defects), plus the run-level `rc` and `timed_out`.

Non-orthogonality is advisory in the shared OpenFOAM table and gating here through
`quality_floor`; fatal topology is always gating, for either flow engine.

## Review

The reviewer receives two resolved artifacts: `mesh_paths.surface` (required) and
`mesh_paths.volume` (optional). It inspects named patches and regions (`patch:*`, `region:*`).

An optional volume that is absent costs the reviewer its sectional views, not its session. An
optional volume that is *present but unreadable* is refused rather than treated as absent, a
corrupt file is a different thing from a missing one.

## Runtime requirements

OpenFOAM and cfMesh, which live in the mesh image. Because meshing is dispatched, you do not install
them locally; the mesh runs off-box.

## Intentional limitations

- No symmetry-plane support: use [snappy](snappy.md) where a symmetry plane matters.
- No prism-layer generation. Cartesian cells meet the wall directly, which is the trade for speed.
- 2D internal profiles are not supported.
