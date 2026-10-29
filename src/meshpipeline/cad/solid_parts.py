# Responsibility: Draw a CAD file's solids as the separate, named parts they are, for the geometry stage.
# Boundaries: it draws. It measures nothing the scout measures, names no openings and decides no engine.
# Collaborates with: cad/fea_assembly.py (the solid order and the file's own names), cad/scout.py (the welded skin it stands beside), render/skin_mesh.py (the patch shape the console reads), cad/ingest/ecxml_place.placed_skin (the same payload, for a thermal model).
"""A FILE OF SEVERAL PARTS, DRAWN AS SEVERAL PARTS.

WHAT THE STAGE SHOWED BEFORE. One welded skin, whatever the file held. A thermal model (ECXML)
was the exception - `ecxml_place.placed_skin` has always drawn one block per part under its own
name and colour, and the console has always known how to draw that: `{"parts": True, "patches":
[...]}`. A STEP assembly got none of it. The Corintis package is six solids (a top lid, a board
cooling circuit, an I/O splitter, a microfluidic core and two fittings) and arrived on the stage
as one grey lump, so a customer checking that we read their file correctly could not see whether
we had found their parts at all.

THE SAME PAYLOAD, from a STEP instead of a placed model. Nothing in the console changes.

NAMED FROM THE FILE where the file names them (`fea_assembly.read_body_names`, which reads the
STEP's own product names), and `Part_k` where it does not - the same identity the FEA path has
used all along, so a part the stage calls Part_3 is the part every other record calls Part_3.

ONE SOLID IS NOT AN ASSEMBLY and gets nothing from this: the existing single-patch skin stands,
which is both what it should look like and the path every corpus part takes.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Above this many solids the parts are not drawn separately. A chip package can state two
#: thousand (Corintis, 2026-10-29: ECXML "up to 2000 solids"), and two thousand patches is a
#: payload the console has to unpack one at a time before it can draw anything. The welded skin is
#: what a model that big is drawn as, and the thermal path has its own placed drawing for it.
MAX_DRAWN_PARTS = 64

#: Colours in the order parts are found, reused round the ring. The same ring the placed drawing
#: uses, so one file drawn both ways does not change colour.
from meshpipeline.cad.ingest.ecxml_place import PART_COLOURS  # noqa: E402  (the shared ring)


def _solid_triangles(solid) -> list:
    """Every triangle of one solid, in world coordinates, wound the way its faces face.

    The shape must already be TESSELLATED - this reads the triangulation that is there and meshes
    nothing. The caller has just drawn the whole shape (scout.write_view_stl meshes it to size),
    so the triangles exist and are the ones the stage is already showing; meshing again here would
    draw the parts at a different fineness from the skin beside them.
    """
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.fluid_volume import _face_triangles

    out: list = []
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        out.extend(_face_triangles(TopoDS.Face_s(exp.Current())))
        exp.Next()
    return out


def part_skins(shape, *, path: Path | str | None = None) -> list[dict]:
    """[{name, triangles}] for every solid of an already-tessellated shape, in OpenCASCADE's order.

    `path` is the file the shape was read from, used only to read the names it states. Empty for a
    shape of one solid or none, and for one with more solids than MAX_DRAWN_PARTS.
    """
    import numpy as np

    from meshpipeline.cad.fea_assembly import part_id, readable_body_name, solids

    bodies = solids(shape)
    if len(bodies) < 2 or len(bodies) > MAX_DRAWN_PARTS:
        return []
    stated: list[str] = []
    if path is not None:
        try:
            from meshpipeline.cad.fea_assembly import read_body_names

            stated = [str(n) for n, _c, _v in read_body_names(Path(path))]
        except Exception:                 # noqa: BLE001 - a drawing is never worth failing a check
            logger.warning("solid parts: the file's own body names could not be read",
                           exc_info=True)
    out: list[dict] = []
    for k, body in enumerate(bodies, start=1):
        tris = _solid_triangles(body)
        if not tris:
            # A SOLID WITH NO TRIANGLES IS STILL A SOLID, and leaving it out silently would show a
            # five-part assembly as four. It is named in the reply with nothing to draw, so the
            # count a customer reads matches the count the file has.
            out.append({"name": _name(stated, k), "id": part_id(k), "triangles": None})
            continue
        out.append({"name": _name(stated, k), "id": part_id(k),
                    "triangles": np.asarray(tris, dtype=float).reshape(-1, 3, 3)})
    _ = readable_body_name            # named above; kept imported for the name rule it owns
    return out


def _name(stated: list[str], k: int) -> str:
    from meshpipeline.cad.fea_assembly import part_id, readable_body_name

    said = readable_body_name(stated[k - 1]) if 0 < k <= len(stated) else ""
    return said or part_id(k)


def parts_payload(shape, *, path: Path | str | None = None) -> dict | None:
    """The stage's skin as named parts, in the payload the console already draws
    (`ecxml_place.placed_skin`'s), or None when this file is not drawn that way.

    EVERY PART CARRIES ITS OWN VOLUME AND EXTENT, because "is this my part?" is the question the
    stage exists to answer and a name alone does not answer it: two solids called Part_3 and
    Part_4 look the same in a list, and 19.4 mm3 against 276,324 mm3 does not.
    """
    import base64

    import numpy as np

    from meshpipeline.render.skin_mesh import prepare_skin, skin_patch

    parts = part_skins(shape, path=path)
    if not parts:
        return None
    patches, edge_pts, edge_lines, offset = [], [], [], 0
    drawn = 0
    for k, part in enumerate(parts):
        tris = part.get("triangles")
        if tris is None or not len(tris):
            continue
        prepared = prepare_skin(tris)
        patch = skin_patch(prepared, name=part["name"])
        lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
        patch.update(part_id=part["id"], color=list(PART_COLOURS[k % len(PART_COLOURS)]),
                     triangles=int(len(tris)),
                     size_mm=[round(float(v) * 1000.0, 4) for v in (hi - lo)],
                     volume_mm3=round(_volume(tris) * 1e9, 4))
        patches.append(patch)
        lines = prepared["edge_lines"].reshape(-1, 3).astype(np.int64)
        lines[:, 1:] += offset
        edge_pts.append(prepared["edge_points"])
        edge_lines.append(lines)
        offset += len(prepared["edge_points"])
        drawn += 1
    if not patches:
        return None
    out: dict = {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
                 "parts": True, "patches": patches, "part_count": len(parts)}
    if drawn < len(parts):
        # SAID, NOT HIDDEN. See part_skins: a part with no triangulation is a part the stage
        # cannot draw, and a customer counting parts on the picture needs to know one is missing.
        out["parts_not_drawn"] = [p["name"] for p in parts
                                  if p.get("triangles") is None or not len(p["triangles"])]
    b64 = base64.b64encode(
        np.ascontiguousarray(np.concatenate(edge_pts).astype(np.float32)).tobytes()).decode("ascii")
    lines_all = np.concatenate(edge_lines).astype(np.uint32).ravel()
    out["edges"] = {"points_b64": b64,
                    "lines_b64": base64.b64encode(
                        np.ascontiguousarray(lines_all).tobytes()).decode("ascii"),
                    "count": int(len(lines_all) // 3)}
    return out


def _volume(tris) -> float:
    """The signed volume a closed triangle surface encloses, m3. The divergence sum, which is the
    same one cad/fluid_volume.surface_volume uses - a part drawn inside out reads negative and that
    is worth seeing rather than hiding behind an absolute value."""
    import numpy as np

    a, b, c = tris[:, 0, :], tris[:, 1, :], tris[:, 2, :]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


__all__ = ["MAX_DRAWN_PARTS", "part_skins", "parts_payload"]
