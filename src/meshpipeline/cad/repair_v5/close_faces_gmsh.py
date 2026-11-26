# Responsibility: the facet-crossing predictor's CHILD process (v5 step 7): mesh one face pair in 2D with each gmsh
#                 member's own options and count the triangle pairs of the two sides that intersect.
# Owns: `predict` and the `python -m` entry point.
# Boundaries: gmsh is imported here only, in a process of its own (a gmsh crash ends the child, never the pool
#             worker - the `mesh_child` rule). The options are `mesh_child._profile`'s, never tuned; the kernel side
#             (`close_faces_kernel.v5_facet_cross`) writes the pair and reads the answer.
"""usage: python -m meshpipeline.cad.repair_v5.close_faces_gmsh PAYLOAD.json ANSWER.json

PAYLOAD: {"brep": the pair's faces (side A, then side B, then their first-ring NEIGHBOURS) as one compound, "boxes":
each face's box in that order, "n_a"/"n_b": faces on each side, "part_box": the WHOLE part's box (mm), "members":
{member: profile}}.
ANSWER: {member: {"crossings": n, "triangles": [na, nb]} | {"error": text}}.

The pair is meshed with its first-ring neighbours (faces sharing a vertex with it) and sized as in the part: the
profile's options are computed at the part's diagonal, and the part's box corners are added as free points so gmsh's
own characteristic length is the part's. The neighbours are there for their edges: gmsh_default carries boundary
sizes inward, and a rim vertex's size comes from every edge at it (measured on f9501251fb30: the shaft-in-bore pair
alone meshed with no crossing under gmsh_default, where the whole part crosses 87 times).
"""

from __future__ import annotations

import json
import math
import sys


def _surface_side(boxes: list[list[float]], n_a: int, n_b: int, gbox: tuple, scale: float) -> str:
    """'a', 'b' or 'n' (a neighbour, meshed for its sizes, never counted) for a gmsh surface by its box (model
    units / scale): the nearest input face box."""
    mm = [c / scale for c in gbox]
    best = min(range(len(boxes)), key=lambda i: max(abs(mm[k] - boxes[i][k]) for k in range(6)))
    return "a" if best < n_a else "b" if best < n_a + n_b else "n"


def predict(payload: dict) -> dict:
    import gmsh

    from meshpipeline.cad.repair_v4.verdict.mesh_child import REPRODUCIBLE, _profile
    from meshpipeline.cad.repair_v5.close_faces import count_crossings

    x0, y0, z0, x1, y1, z1 = payload["part_box"]
    diag_mm = math.dist((x0, y0, z0), (x1, y1, z1))
    out: dict = {}
    for member, profile in payload["members"].items():
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
            sides: dict[str, list] = {"a": [], "b": [], "n": []}
            ids: dict[str, list] = {"a": [], "b": [], "n": []}
            for _dim, tag in gmsh.model.getEntities(2):
                side = _surface_side(payload["boxes"], int(payload["n_a"]), int(payload["n_b"]),
                                     gmsh.model.getBoundingBox(2, tag), scale)
                types, _etags, nodes = gmsh.model.mesh.getElements(2, tag)
                for kind, ns in zip(types, nodes, strict=True):
                    if int(kind) != 2:
                        continue
                    for i in range(0, len(ns), 3):
                        tri = (int(ns[i]), int(ns[i + 1]), int(ns[i + 2]))
                        sides[side].append(tuple(xyz[n] for n in tri))
                        ids[side].append(tri)
            # a pair sharing a mesh node touches by construction (a mis-sided neighbour, a shared rim): not a crossing
            out[member] = {"crossings": count_crossings(sides["a"], sides["b"], nodes_a=ids["a"], nodes_b=ids["b"]),
                           "triangles": [len(sides["a"]), len(sides["b"])]}
        except Exception as exc:  # noqa: BLE001 - one member's failure is that member's "not measured"
            out[member] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            gmsh.finalize()
    return out


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    answer = predict(payload)
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(answer, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
