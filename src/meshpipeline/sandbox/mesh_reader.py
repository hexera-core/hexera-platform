# Responsibility: Read a mesh back into the facts a review scene is built from.
# Boundaries: all the gmsh there is: it opens the handle it is given and returns data. It resolves no path.
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import gmsh
import numpy as np
import pyvista as pv

logger = logging.getLogger(__name__)

#: gmsh element type -> (nodes per element, corner nodes to keep).
#: Second-order elements (9, 16, 10) are drawn through their corners: the review is a shape and
#: patch-identity check, and the midside nodes cost geometry without changing either.
PATCH_ELEMENT_TYPES: dict[int, tuple[int, int]] = {
    2:  (3, 3),   # 3-node triangle
    3:  (4, 4),   # 4-node quadrangle
    9:  (6, 3),   # 6-node second-order triangle
    16: (8, 4),   # 8-node second-order quadrangle
    10: (9, 4),   # 9-node second-order quadrangle
}

#: The navigation pan step is a tenth of the model's longest side. Mesh units throughout - see
#: the scene fact names, which say `_mm` for historical reasons and never convert.
PAN_STEP_FRACTION = 0.10


@dataclass(frozen=True)
class LoadedMesh:

    #: xmin/xmax/... plus `units`. Empty when the bounding box could not be queried.
    bbox: dict[str, Any] = field(default_factory=dict)
    #: A tenth of the longest side, or 1.0 when there is no bounding box to derive it from.
    pan_step: float = 1.0
    #: Surface (dim=2) physical group name -> the entity tags it covers. These are the patches.
    entity_tags_by_name: dict[str, list[int]] = field(default_factory=dict)
    #: RAW (dim, tag, name, member entity tags) across ALL dimensions - no semantics, no policy.
    #: A separate neutral extraction that an engine bundle interprets; the patch loading above is
    #: unaffected by it.
    physical_groups: list[tuple[int, int, str, tuple[int, ...]]] = field(default_factory=list)
    #: Patch name -> its reconstructed surface.
    patch_meshes: dict[str, pv.PolyData] = field(default_factory=dict)


def _map_nodes(enodes: np.ndarray, nodes_per: int, corner_cols: int,
               tag_to_idx: np.ndarray) -> np.ndarray | None:
    if len(enodes) % nodes_per != 0:
        logger.warning("Element node count %d not divisible by %d - skipping",
                       len(enodes), nodes_per)
        return None
    n = len(enodes) // nodes_per
    corners = enodes.reshape(n, nodes_per).astype(np.int64)[:, :corner_cols]
    oob = (corners >= len(tag_to_idx)).any(axis=1)
    if oob.any():
        logger.warning("Filtering %d/%d elements with out-of-range node tags", oob.sum(), n)
        corners = corners[~oob]
    idx = tag_to_idx[corners]
    missing = (idx < 0).any(axis=1)
    if missing.any():
        logger.warning("Filtering %d elements with unmapped node tags", missing.sum())
        idx = idx[~missing]
    # The single decision: an empty block, a block filtered down to nothing, and a block that
    # never had a mappable element all arrive here as an empty index array and are all "no faces".
    return idx if len(idx) > 0 else None


def extract_patch_meshes(entity_tags_by_name: dict[str, list[int]],
                         msh_path: str) -> dict[str, pv.PolyData]:
    node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
    if len(node_coords) % 3 != 0:
        raise ValueError(
            f"Node coordinate array length {len(node_coords)} is not divisible by 3 "
            f"- malformed mesh: {msh_path}"
        )
    coords = node_coords.reshape(-1, 3)

    max_tag = int(node_tags.max()) if len(node_tags) else 0
    tag_to_idx = np.full(max_tag + 1, -1, dtype=np.int64)
    tag_to_idx[node_tags.astype(int)] = np.arange(len(node_tags), dtype=np.int64)

    meshes: dict[str, pv.PolyData] = {}
    for name, surf_tags in entity_tags_by_name.items():
        face_blocks: list[np.ndarray] = []
        for stag in surf_tags:
            try:
                etypes, _, enodes_list = gmsh.model.mesh.getElements(2, stag)
            except Exception as exc:  # noqa: BLE001 - one unreadable surface, not the whole patch
                logger.warning("Could not get elements for surface %d: %s", stag, exc)
                continue
            for etype, enodes in zip(etypes, enodes_list):
                spec = PATCH_ELEMENT_TYPES.get(int(etype))
                if spec is None:
                    continue
                nodes_per, corner_cols = spec
                idx = _map_nodes(enodes, nodes_per, corner_cols, tag_to_idx)
                if idx is None:
                    continue
                pre = np.full((len(idx), 1), corner_cols, dtype=np.int64)
                face_blocks.append(np.hstack([pre, idx]))

        if face_blocks:
            faces_flat = np.vstack(face_blocks).flatten()
            meshes[name] = pv.PolyData(coords, faces_flat).clean()

    return meshes


def read_mesh(msh_path: str, mesh_units: str) -> LoadedMesh:
    bbox: dict[str, Any] = {}
    pan_step = 1.0
    entity_tags_by_name: dict[str, list[int]] = {}
    physical_groups: list[tuple[int, int, str, tuple[int, ...]]] = []

    session_is_ours = not gmsh.isInitialized()
    if session_is_ours:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        try:
            gmsh.model.remove()
        except Exception as exc:  # noqa: BLE001 - nothing to remove on a fresh session
            logger.debug("render backend: gmsh.model.remove() skipped: %s", exc)
        gmsh.model.add("sandbox_view")
        gmsh.merge(msh_path)

        try:
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
            bbox = {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax,
                    "zmin": zmin, "zmax": zmax, "units": mesh_units}
            longest = max(xmax - xmin, ymax - ymin, zmax - zmin, 1e-9)
            pan_step = round(longest * PAN_STEP_FRACTION, 1)
            logger.info("render backend: bbox L=%.0f → pan_step=%.0f", longest, pan_step)
        except Exception as exc:  # noqa: BLE001 - a scene without a bbox still renders
            logger.warning("render backend: bbox query failed: %s", exc)

        for dim, tag in gmsh.model.getPhysicalGroups(2):
            name = gmsh.model.getPhysicalName(dim, tag)
            entity_tags_by_name[name] = list(gmsh.model.getEntitiesForPhysicalGroup(dim, tag))

        for dim, tag in gmsh.model.getPhysicalGroups():
            physical_groups.append((
                int(dim), int(tag), gmsh.model.getPhysicalName(dim, tag),
                tuple(int(e) for e in gmsh.model.getEntitiesForPhysicalGroup(dim, tag)),
            ))

        patch_meshes = extract_patch_meshes(entity_tags_by_name, msh_path)
    finally:
        if session_is_ours:
            try:
                gmsh.finalize()
            except Exception as exc:  # noqa: BLE001 - a leaked session is worse than a log line
                logger.debug("render backend: gmsh.finalize() failed: %s", exc)

    return LoadedMesh(
        bbox=bbox,
        pan_step=pan_step,
        entity_tags_by_name=entity_tags_by_name,
        physical_groups=physical_groups,
        patch_meshes=patch_meshes,
    )


__all__ = ["PAN_STEP_FRACTION", "PATCH_ELEMENT_TYPES", "LoadedMesh", "extract_patch_meshes",
           "read_mesh"]
