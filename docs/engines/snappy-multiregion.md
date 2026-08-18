# snappy_multiregion

**snappyHexMesh multi-region (`splitMeshRegions`)**, the coupled multi-region engine. It produces
an OpenFOAM case with several conformal regions from a multi-solid assembly.

It is the only engine serving `conjugate_heat_transfer`, and the only one whose input is a solid
**assembly** rather than a single body or surface.

| | |
|---|---|
| Accepted input | a **solid assembly** |
| Dimensionality | 3D |
| Purposes | `conjugate_heat_transfer` |
| Capability | solid-assembly → multiregion-volume |
| Deliverable | `openfoam_multiregion_case.tar.gz`, marked by `constant/regionProperties` |
| Exports | `openfoam_polymesh_multiregion` |

The marker is `constant/regionProperties` rather than a polyMesh: what proves this engine
delivered is the region declaration, because a single-region mesh would satisfy a polyMesh marker
while being the wrong thing entirely.

## What it produces

| Member | Kind | Required |
|---|---|---|
| `constant` | directory | yes |
| `system/blockMeshDict` | file | yes |
| `system/snappyHexMeshDict` | file | yes |
| `constant/triSurface` | directory | yes |
| `system/topoSetDict` | file | no |

## Native lifecycle

`blockMesh`, then `surfaceFeatureExtract`, then `snappyHexMesh` over the whole assembly, then
`splitMeshRegions -cellZonesOnly`, which is the step that turns one mesh into the coupled regions.
The engine then reconciles the regions that actually exist against the regions that were declared.

## Authoring controls

| Parameter | Meaning |
|---|---|
| `fluid_topology` | whether the fluid region is internal or external to the solid assembly |

This is asked rather than derived because the same assembly geometry can legitimately mean either,
and getting it wrong produces a mesh that is valid but models the wrong problem.

## Gates

The largest gate set of any engine, because multi-region failures are structural rather than
numerical.

| Gate | Proves |
|---|---|
| `manifest_valid` | the multi-region case is sound, with no fatal defects in any region |
| `patch_contract` | the delivered mesh carries exactly the boundaries approved at intake, none merged, renamed, dropped or re-roled |
| `regions_split` | every region declared was meshed, and no undeclared region was invented |
| `interfaces` | the fluid-solid interfaces are conformal, with faces matching one-to-one |
| `region_contract` | each region carries the boundaries named for it |
| `quality_floor` | every region clears the quality bars, with skewness localized across all of them |

`regions_split` fails in both directions deliberately. A missing region is an obvious failure; an
*extra* region is equally wrong, because an undeclared background region means the mesher
partitioned the assembly differently from how the user described it.

## Quality criteria

`regions_missing`, `interface_ok`, `skew_fraction`, `layer_coverage`, `max_non_ortho`, `fatal`,
plus `rc` and `timed_out`. The first two are unique to this engine: they measure whether the
region decomposition is what was asked for, which no single-region engine can get wrong.

## Review

`mesh_paths.surface` (required) and `mesh_paths.volume` (optional), with `patch:*` and `region:*`
targets. Region-level inspection matters more here than anywhere else, since a case can look
correct at the surface while being wrong at an interface.

## Runtime requirements

OpenFOAM with snappyHexMesh and `splitMeshRegions`, from the mesh image.

## Intentional limitations

Large multi-scale assemblies (roughly, more than a handful of solid classes) and automatic
expected-interface derivation through contact detection are not supported. The interfaces you
expect must be declarable.
