# Responsibility: Describe the repair catalogue in words, one line per operation, for a model prompt.
# Owns: the words. The NAMES are `operations.catalogue`'s; which optional operations are present is
#       read from the same consent flags, the same way, so the words never offer what the
#       catalogue does not hold (pinned by tests/unit/agents/test_catalogue_pinned_to_operations.py).
# Boundaries: text only. It imports no kernel and no operation, so `agents/` can re-export it
#             (agents -> cad is the allowed direction) without loading the repair core.
# Collaborates with: cad/repair/operations.py (the catalogue described), cad/repair/model_author.py
#                    and agents/repair_review/_wire.py (the prompts that carry it).
from __future__ import annotations

#: The operation that answers a periodic-surface mesh failure (CAD_REPAIR_SPLIT_CLOSED_FACES).
SPLIT_CLOSED = "split_closed_faces"
#: The operation that answers an edge-through-face mesh failure (CAD_REPAIR_FRAGMENT_BODIES).
FRAGMENT = "fragment_bodies"
#: The operation that answers an overlapping-facets mesh failure (CAD_REPAIR_REBUILD_FACE).
REBUILD = "rebuild_face"
#: The operation that closes a face's open wire, moving vertices (CAD_REPAIR_CLOSE_WIRE_GAPS).
CLOSE_GAPS = "close_wire_gaps"
#: The operation that refines the faces of a sub-mesh-size gap (CAD_REPAIR_RESOLVE_SUB_MESH_GAP).
RESOLVE_GAP = "resolve_sub_mesh_gap"

_BASE = (
    "- fix_small_edges: merge edges too short to keep",
    "- fix_small_faces: sweep away sliver faces too small to keep "
    "(it reports the removed faces' own area; rebuilding their neighbours can move the surface "
    "much further, so claim the MEASURED delta)",
    "- fix_face_boundary: repair a face whose boundary is broken or open",
    "- sew_faces: stitch faces that should share an edge",
    "- fill_planar_hole: cap a flat hole",
    "- fill_nonplanar_hole: cap a small curved hole with a constrained patch",
    "- heal_self_intersections: re-trim only the faces the kernel says cross each other",
    "- fix_shape_global: run the whole-shape healing pass",
)


def catalogue_text(*, split_closed_faces: bool | None = None,
                   fragment_bodies: bool | None = None,
                   rebuild_face: bool | None = None,
                   close_wire_gaps: bool | None = None,
                   resolve_sub_mesh_gap: bool | None = None) -> str:
    """One line per catalogued operation, for the given consent (the deployment's flags when not
    told)."""
    import meshpipeline.settings.cad_repair as rcfg

    if split_closed_faces is None:
        split_closed_faces = bool(rcfg.CAD_REPAIR_SPLIT_CLOSED_FACES)
    if fragment_bodies is None:
        fragment_bodies = bool(rcfg.CAD_REPAIR_FRAGMENT_BODIES)
    if rebuild_face is None:
        rebuild_face = bool(rcfg.CAD_REPAIR_REBUILD_FACE)
    if close_wire_gaps is None:
        close_wire_gaps = bool(rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS)
    if resolve_sub_mesh_gap is None:
        resolve_sub_mesh_gap = bool(getattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False))
    lines = list(_BASE)
    if split_closed_faces:
        lines.append(f"- {SPLIT_CLOSED}: split a closed (periodic) face - a full cylinder, cone, "
                     "sphere or torus with one seam - in two so the mesher can wrap it; only for a "
                     "2D-stage periodic-surface failure on a located face")
    if fragment_bodies:
        lines.append(f"- {FRAGMENT}: make two touching bodies share their contact faces so the "
                     "volume mesh is conformal there; declined when the bodies overlap; only for a 3D-stage "
                     "edge-through-face failure on a located face, in a part with more than one "
                     "solid")
    if rebuild_face:
        lines.append(f"- {REBUILD}: re-trim one face from its own boundary on the same surface, "
                     "when the surface mesher reports overlapping facets on it; only for a 2D-stage "
                     "overlapping-facets failure on a located face")
    if close_wire_gaps:
        lines.append(f"- {CLOSE_GAPS}: close the gaps in one face's open boundary wire by merging "
                     "its loose vertices, moving each by at most the run's tolerance; only for a "
                     "2D-stage open-loop failure or a wire gap on a located face")
    if resolve_sub_mesh_gap:
        lines.append(f"- {RESOLVE_GAP}: when two faces of one solid do NOT touch but sit closer "
                     "than the mesh size (a clash whose clash_kind is \"gap\", i.e. measured "
                     "apart beyond the touching margin; usually a thin wall), split them along "
                     "their own iso-curves so the default mesh follows the curvature; moves no "
                     "surface and never closes the gap; for an edge-through-face or "
                     "overlapping-facets failure on a located face, not for a clash whose faces "
                     "touch or cross (that is a real intersection)")
    return "\n".join(lines)
