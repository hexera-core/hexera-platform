# Responsibility: the facet_crossing_local predictor's CHILD process: mesh a set of faces (or the whole part) in 2D
#                 with ONE gmsh member's own options and count, per (face, face), the triangle pairs that intersect
#                 without sharing a mesh node - within a face, across a shared edge, between touching faces.
# Owns: `predict` and the `python -m` entry point.
# Boundaries: gmsh is imported here only, in a process of its own (the `mesh_child` rule: a gmsh crash ends the
#             child). The options are `mesh_child._profile`'s, never tuned (as close_faces_gmsh). The sweep is
#             `facet_local.self_crossings` (pure); the kernel side is facet_local_kernel.v5_local_cross.
"""usage: python -m meshpipeline.cad.repair_v5.facet_local_gmsh PAYLOAD.json ANSWER.json

PAYLOAD: {"brep": the faces (FOCUS first, then their first-ring neighbours) as one compound, or the whole part,
"boxes": each input face's box in that order, "n_focus": how many lead the list, "part_box": the WHOLE part's box (mm),
"member": name, "profile": the mesh_child profile, "cap": crossings at most}.
ANSWER: {"pairs": [[i, j, n, [x, y, z]]] (0-based input face indices, i <= j), "triangles": n} | {"error": text}.

The faces are sized as in the part (the profile's options at the part's diagonal; the part box's corners added as
free points), exactly as close_faces_gmsh does, so a face's facets are the ones the shipped run makes.
"""

from __future__ import annotations

import json
import math
import sys


def _nearest(boxes: list[list[float]], gbox: tuple, scale: float) -> int:
    mm = [c / scale for c in gbox]
    return min(range(len(boxes)), key=lambda i: max(abs(mm[k] - boxes[i][k]) for k in range(6)))


def predict(payload: dict) -> dict:
    import gmsh

    from meshpipeline.cad.repair_v4.verdict.mesh_child import REPRODUCIBLE, _profile
    from meshpipeline.cad.repair_v5.facet_local import self_crossings

    x0, y0, z0, x1, y1, z1 = payload["part_box"]
    diag_mm = math.dist((x0, y0, z0), (x1, y1, z1))
    profile = payload["profile"]
    scale, _ = _profile(profile, 1.0)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        for key, value in REPRODUCIBLE.items():
            gmsh.option.setNumber(key, value)
        gmsh.option.setNumber("Geometry.OCCScaling", scale)
        gmsh.model.occ.importShapes(payload["brep"])
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        for cx in (x0, x1):
            for cy in (y0, y1):
                for cz in (z0, z1):
                    gmsh.model.occ.addPoint(cx * scale, cy * scale, cz * scale)
        gmsh.model.occ.synchronize()
        _s, options = _profile(profile, diag_mm * scale)
        for key, value in options.items():
            gmsh.option.setNumber(key, value)
        gmsh.model.mesh.generate(2)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        xyz = {int(t): (coords[3 * i] / scale, coords[3 * i + 1] / scale, coords[3 * i + 2] / scale)
               for i, t in enumerate(tags)}
        boxes = payload["boxes"]
        tris, nodes, owner = [], [], []
        for _dim, tag in gmsh.model.getEntities(2):
            face = _nearest(boxes, gmsh.model.getBoundingBox(2, tag), scale)
            types, _etags, ns_all = gmsh.model.mesh.getElements(2, tag)
            for kind, ns in zip(types, ns_all, strict=True):
                if int(kind) != 2:
                    continue
                for i in range(0, len(ns), 3):
                    tri = (int(ns[i]), int(ns[i + 1]), int(ns[i + 2]))
                    tris.append(tuple(xyz[n] for n in tri))
                    nodes.append(tri)
                    owner.append(face)
        n_focus = int(payload.get("n_focus") or len(boxes))
        counts, where = self_crossings(tris, nodes, owner, focus=set(range(n_focus)) if n_focus < len(boxes)  # type: ignore[arg-type]
                                       else None, cap=int(payload.get("cap") or 20_000))
        return {"pairs": [[i, j, n, [round(c, 6) for c in where[(i, j)]]] for (i, j), n in sorted(counts.items())],
                "triangles": len(tris)}
    except Exception as exc:  # noqa: BLE001 - this member's failure is its "not measured"
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        gmsh.finalize()


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    answer = predict(payload)
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(answer, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
