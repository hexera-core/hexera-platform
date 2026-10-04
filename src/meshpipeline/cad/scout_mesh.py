# Responsibility: Read a triangle file - STL, OBJ, VTP - and propose what the CAD scout proposes
# for a STEP file: the openings, what kind of body it is, which way the fluid goes, a point inside
# the flow. Triangles carry no faces to measure, so the openings are the holes cad/open_ends finds
# (the one definition the stage's "Add an opening" shares) and the flat discs of a fluid body:
# close, not exact, and said to be.
# Boundaries: numpy over triangles. No OpenCASCADE, no model, no storage; the decisions mirror
# cad/scout so the two roads meet the same stage.
from __future__ import annotations

from pathlib import Path

import numpy as np

from meshpipeline.cad.scout import (
    BOX_LIKE_FILL,
    BOX_LIKE_FLAT_SHARE,
    FINNED_FLAT_SHARE,
    MAX_OPENINGS,
    MIN_MOUTH_OF_THICKNESS,
    MIN_OPENING_FRACTION,
    MIN_RING_BORE_FRACTION,
    MOUTH_MAX_ASPECT,
    THIN_FLAT,
    Opening,
    ScoutResult,
    _bladed,
    _name_openings,
    _wall_pairs,
    measured_faces,
)

#: This many separate pieces read as a scene - buildings, a city block - not a part with mouths.
MANY_BODIES = 6
#: A flat region smaller than this share of the part's box is trim, not a mouth.
MIN_REGION_SHARE = 1e-4


def read_triangles(path: Path) -> np.ndarray:
    """The file's triangles as an (n, 3, 3) array in the file's own unit."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".stl":
        from meshpipeline.cad.stl_io import read_stl_triangles
        tris = read_stl_triangles(path)
        return np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if suffix == ".obj":
        return _read_obj(path)
    if suffix == ".vtp":
        import pyvista as pv
        mesh = pv.read(str(path)).triangulate()
        faces = np.asarray(mesh.faces).reshape(-1, 4)[:, 1:]
        return np.asarray(mesh.points, dtype=float)[faces]
    raise ValueError(f"not a triangle file: {suffix}")


def _read_obj(path: Path) -> np.ndarray:
    verts: list[list[float]] = []
    tris: list[tuple[int, int, int]] = []
    with path.open() as fh:
        for line in fh:
            if line.startswith("v "):
                parts = line.split()
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    i = int(tok.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):        # a polygon fans into triangles
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not tris:
        raise ValueError("the OBJ file holds no triangles")
    v = np.asarray(verts, dtype=float)
    return v[np.asarray(tris, dtype=int)]


def write_mesh_skin(path: Path, dest: Path, *, scale_to_m: float) -> Path:
    """The same triangles, in metres, as the binary STL the pictures and the stage draw."""
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = read_triangles(path) * float(scale_to_m)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(dest, [(t[0].tolist(), t[1].tolist(), t[2].tolist()) for t in tris])
    return dest


# ------------------------------------------------------------------------------ the scout ----
def scout_mesh(path: Path, *, scale_to_m: float) -> ScoutResult:
    tris = read_triangles(path) * float(scale_to_m)
    from meshpipeline.cad.open_ends import find_caps, find_holes, surface

    # the skin as one surface, read once for the holes and the lids: an assembly's faces where its
    # solids touch are inside the part
    mesh = surface(tris)
    if mesh is None:
        raise ValueError("the file holds too few triangles to read")
    verts, faces = mesh.verts, mesh.faces
    bbox_min, bbox_max = verts.min(axis=0), verts.max(axis=0)
    diag = float(np.linalg.norm(bbox_max - bbox_min)) or 1.0

    edges, edge_faces, counts = _edges(faces)
    interior = counts == 2
    components = _components(len(faces), edge_faces[interior])
    n_components = int(components.max() + 1) if len(components) else 1

    notes: list[str] = []
    candidates: list[Opening] = []

    # THE HOLES, by the one definition the stage's "Add an opening" uses too (cad/open_ends): a
    # thin wall's open rim (a "rim"), or the mouth of a thick wall's bore at its cut end (a
    # "ring", the scout's word for an end face whose hole is the opening) - flat or not, square
    # to the axis or not, wherever the part sits
    holes = find_holes(mesh=mesh)
    for h in holes:
        candidates.append(_opening(-1, "rim" if h.kind == "rim" else "ring", h.centroid, h.normal, h.area, h.wh,
                                   bbox_min, bbox_max, diag))
        candidates[-1].confidence = h.confidence

    # FLAT FACES, each measured as a lid might be (cad/open_ends, WHAT A CAP IS). A fluid body's
    # mouths are lids: flat, with a sharp rim all the way round and open air outside - wherever
    # they sit, a side branch's end as much as the part's far end. A coarse tessellation's flat
    # facets are not lids, and a face with a real hole in it is the holes' business above. Every
    # flat face big enough to click on stays for "add an opening"; only the lids are proposed.
    sx, sy, sz = (float(v) for v in (bbox_max - bbox_min))
    box_skin = 2.0 * (sx * sy + sy * sz + sz * sx) or 1.0
    caps = find_caps(mesh=mesh, min_area=MIN_REGION_SHARE * box_skin)
    lids: set[int] = set()
    ground_area = 0.0
    # how much of the part is flat faces as its CAD model had them (Cap.face_of_part). A coarse
    # tessellation's facets of a curved wall - an elbow's torus drawn in flat quads - are flat
    # too, but no bigger than the facets round them and meeting them as gently as the wall
    # curves; counted as a box's sides they turned every coarse elbow into a "solid body".
    drawn_flat = 0.0
    for cap in caps:
        if cap.area < MIN_REGION_SHARE * box_skin and not cap.likely:
            continue
        if min(cap.wh) < 0.05 * max(cap.wh):
            continue          # a facet strip of a curved skin, not a flat face of the part
        if cap.normal[2] < -0.9 and abs(cap.centroid[2] - bbox_min[2]) < 0.01 * diag:
            ground_area += cap.area
        if cap.inner >= MIN_RING_BORE_FRACTION * (cap.area + cap.inner):
            continue          # an end face around a hole: the hole, if it is one, is found above
        candidates.append(_opening(cap.face, "disc", cap.centroid, cap.normal, cap.area, cap.wh,
                                   bbox_min, bbox_max, diag))
        candidates[-1].confidence = cap.confidence
        if cap.face_of_part:
            drawn_flat += cap.area
        if cap.likely:
            lids.add(id(candidates[-1]))

    measured = measured_faces(candidates)
    rings = [c for c in candidates if c.kind == "ring"]
    # a passage's mouth is no sliver: a lid far longer than it is wide is a wing's tip or a plate's
    # edge, unless its narrow side is a real size against the part (a wide flat duct's mouth) -
    # cad/scout's rule for a mouth's shape
    discs = [c for c in candidates if c.kind == "disc" and id(c) in lids
             and (max(c.wh) <= MOUTH_MAX_ASPECT * max(min(c.wh), 1e-12) or min(c.wh) >= THIN_FLAT * diag)]
    # A DUCT'S SIDE WALLS are lids by every local measure - flat, cornered, open air outside - but
    # each has a partner of its size facing it across the duct (cad/scout reads them the same way);
    # a short fat passage whose two ends face each other is the exception, its ends all it has
    fill = _fill(verts, faces, bbox_min, bbox_max)
    walls = _wall_pairs(discs)
    if discs and all(id(o) in walls for o in discs) and fill >= BOX_LIKE_FILL:
        walls = set()
    discs = [o for o in discs if id(o) not in walls]
    rim_openings = [c for c in candidates if c.kind == "rim"]
    size = bbox_max - bbox_min
    footprint = float(size[0] * size[1]) or 1.0
    grounded = ground_area >= 0.3 * footprint or _most_touch_ground(verts, faces, components, bbox_min, diag)

    if n_components >= MANY_BODIES:
        body_kind, input_kind, flow, pool, conf = "scene", "solid-body", "external", [], 0.7
        notes.append(f"{n_components} separate pieces: this reads as a scene the fluid flows around, not a part with openings")
    elif len(rings) >= 2:
        body_kind, input_kind, flow, pool, conf = "pipe_wall", "body-surface", "internal", rings, 0.75
    elif rim_openings and (len(rim_openings) >= 2 or rings):
        body_kind, input_kind, flow, pool, conf = "surface", "body-surface", "internal", rim_openings + rings, 0.55
        notes.append("the mesh is open; its rims are read as the openings")
    elif rim_openings:
        body_kind, input_kind, flow, pool, conf = "surface", "solid-body", "external", [], 0.45
        notes.append("the mesh is open on one side only, so it reads as a body in a flow; say if it is a passage")
    else:
        # as cad/scout reads a closed solid: a body whose flat faces cover much of its box AND which
        # fills it is a body (the Ahmed body), a bent or branched passage fills far less of its box
        # however flat its walls (a square-section elbow); same-size flats fanning round an axis are
        # blade tips
        flat_share = drawn_flat / box_skin
        box_like = (flat_share >= BOX_LIKE_FLAT_SHARE and fill >= BOX_LIKE_FILL) or flat_share >= FINNED_FLAT_SHARE
        bladed = _bladed(discs)
        # a passage's widest mouth is a real share of the part's thickness; a nacelle's 8 mm tail
        # flat on a 200 mm body is no mouth (its small branches may be: only the widest is held to it)
        thinnest = max(min(sx, sy, sz), 1e-12)
        mouthed = bool(discs) and max(o.equivalent_diameter for o in discs) >= MIN_MOUTH_OF_THICKNESS * thinnest
        if len(discs) >= 2 and mouthed and not box_like and not bladed:
            body_kind, input_kind, flow, pool, conf = "single_solid", "fluid-domain", "internal", discs, 0.6
        else:
            body_kind, input_kind, flow, pool, conf = "single_solid", "solid-body", "external", [], 0.6
            if len(discs) >= 2 and box_like:
                notes.append(f"flat faces cover {100 * flat_share:.0f}% of the part's box and the part fills "
                             f"{100 * fill:.0f}% of it, so it reads as a solid body in a flow, not a fluid passage")
            elif len(discs) >= 2 and bladed:
                notes.append("its flat ends are alike and fan out round an axis like blade tips, so it reads as a "
                             "body in a flow, not a fluid passage")
            elif len(discs) >= 2:
                notes.append("its flat ends are all small against the part's thickness, so it reads as a body in "
                             "a flow, not a fluid passage")
    notes.append("measured from triangles: positions and sizes are close, not exact")

    pool = sorted(pool, key=lambda o: o.area, reverse=True)
    if pool:
        largest = pool[0].area
        # a hole is an opening however small (an aorta's 3.7 mm branch beside its 27.7 mm root),
        # and so is a lid (its 1.6 mm side branch's); any other flat face has to be a real share
        # of the largest to be one
        pool = [o for o in pool if o.kind in ("ring", "rim") or id(o) in lids or o.area >= MIN_OPENING_FRACTION * largest]
    if len(pool) > MAX_OPENINGS:
        notes.append(f"{len(pool)} candidate openings found; only the {MAX_OPENINGS} largest are proposed")
        pool = pool[:MAX_OPENINGS]
    openings = _name_openings(pool)
    for k, o in enumerate(openings, start=1):
        o.sticker = k                 # its confidence is the finder's: how surely its shape reads as one
    if flow == "external":
        openings = []

    seed = None
    if openings:
        big = max(openings, key=lambda o: o.area)
        inward = -np.asarray(big.normal)
        p = np.asarray(big.centroid) + inward * 0.5 * big.equivalent_diameter
        seed = (float(p[0]), float(p[1]), float(p[2]))
        notes.append("the point inside the flow is placed just inside the largest opening; the mesher checks it")

    result = ScoutResult(
        body_kind=body_kind, input_kind=input_kind, flow=flow, solids=n_components,
        planar_faces=len(caps),
        bbox_min=(float(bbox_min[0]), float(bbox_min[1]), float(bbox_min[2])),
        bbox_max=(float(bbox_max[0]), float(bbox_max[1]), float(bbox_max[2])),
        openings=openings, seed_point=seed,
        confidence={"input_kind": conf,
                    "openings": (sum(o.confidence for o in openings) / len(openings)) if openings else 0.0},
        notes=notes, faces=measured)
    # facts the CAD scout does not know: read by the check when it builds the proposal
    result.extra = {  # type: ignore[attr-defined]
        "components": n_components, "grounded": bool(grounded),
        "flow_axis_guess": "+x" if size[0] >= size[1] else "+y",
        # every hole, for the stage: "Add an opening" snaps to these, so a sticker added by hand
        # lands where the measuring step would have put it
        "holes": [h.as_dict() for h in holes],
    }
    return result


# ---------------------------------------------------------------------------- the geometry ----
def _edges(faces):
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    uniq, inverse, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    # the two faces on each interior edge, in the order they were met
    edge_faces = np.full((len(uniq), 2), -1, dtype=np.int64)
    order = np.argsort(inverse, kind="stable")
    seen = np.zeros(len(uniq), dtype=np.int64)
    for k in order:
        u = inverse[k]
        if seen[u] < 2:
            edge_faces[u, seen[u]] = owner[k]
            seen[u] += 1
    return uniq, edge_faces, counts


def _components(n_faces: int, pairs: np.ndarray) -> np.ndarray:
    parent = np.arange(n_faces)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a < 0 or b < 0:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    roots = np.array([find(i) for i in range(n_faces)])
    _, labels = np.unique(roots, return_inverse=True)
    return labels


def _opening(face_index: int, kind: str, c, n, area: float, wh, bbox_min, bbox_max, diag: float,
             outer_wh=None) -> Opening:
    n = np.asarray(n, dtype=float)
    ts = [((bbox_max[k] if n[k] > 0 else bbox_min[k]) - c[k]) / n[k] for k in range(3) if abs(n[k]) > 1e-6]
    on_extremity = bool(ts) and bool(min(ts) <= 0.03 * diag)     # a plain bool: numpy's is not JSON
    return Opening(face_index=face_index, kind=kind, centroid=(float(c[0]), float(c[1]), float(c[2])),
                   normal=(float(n[0]), float(n[1]), float(n[2])), area=float(area),
                   wh=(float(wh[0]), float(wh[1])), clear_ahead=on_extremity, on_extremity=on_extremity,
                   outer_wh=(float(outer_wh[0]), float(outer_wh[1])) if outer_wh is not None else (float(wh[0]), float(wh[1])))


def _fill(verts, faces, bbox_min, bbox_max) -> float:
    """How much of its box the part fills: the volume its faces enclose (they face out, see
    cad/open_ends), over the box's."""
    a, b, c = (verts[faces[:, k]] - bbox_min for k in range(3))
    vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())) / 6.0
    box = float(np.prod(np.maximum(bbox_max - bbox_min, 1e-12)))
    return vol / box


def _most_touch_ground(verts, faces, components, bbox_min, diag: float) -> bool:
    """A scene stands on the ground when most of its pieces reach the lowest plane."""
    n = int(components.max() + 1) if len(components) else 0
    if n < 2:
        return False
    lows = np.full(n, np.inf)
    zmin = verts[faces].min(axis=1)[:, 2]
    np.minimum.at(lows, components, zmin)
    return bool(np.mean(lows <= bbox_min[2] + 0.01 * diag) >= 0.5)


__all__ = ["read_triangles", "scout_mesh", "write_mesh_skin"]
