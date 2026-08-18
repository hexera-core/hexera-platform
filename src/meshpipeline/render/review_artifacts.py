# Responsibility: Build the mesh artifact a review session renders from.
# Boundaries: preparation of an input; it renders nothing.
from __future__ import annotations

from pathlib import Path


def build_review_msh(workspace, patches_tris: dict) -> tuple[dict, tuple]:
    import gmsh
    gmsh.initialize(interruptible=False)          # worker-thread safe
    patch_entities: dict = {}
    bbox = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("review")
        for idx, (name, tris) in enumerate(patches_tris.items(), start=1):
            if not tris:
                continue
            gmsh.model.addDiscreteEntity(2, idx)
            node_tags: list[int] = []; coords: list[float] = []; enodes: list[int] = []
            nt = idx * 50_000_000 + 1
            for tri in tris:
                for v in tri:
                    node_tags.append(nt); coords.extend((float(v[0]), float(v[1]), float(v[2])))
                    enodes.append(nt); nt += 1
            gmsh.model.mesh.addNodes(2, idx, node_tags, coords)
            gmsh.model.mesh.addElementsByType(idx, 2, [], enodes)
            pg = gmsh.model.addPhysicalGroup(2, [idx]); gmsh.model.setPhysicalName(2, pg, name)
            patch_entities[name] = [idx]
        bbox = gmsh.model.getBoundingBox(-1, -1)
        gmsh.write(str(Path(workspace) / "mesh.msh"))
    finally:
        gmsh.finalize()
    return patch_entities, bbox


