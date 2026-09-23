# Responsibility: Read a triangle file - STL, OBJ, VTP - and propose what the CAD scout proposes
# for a STEP file: the openings, what kind of body it is, which way the fluid goes, a point inside
# the flow. Triangles carry no faces to measure, so the openings are read from the mesh's open
# rims and from flat rings and discs in it: close, not exact, and said to be.
# Boundaries: numpy over triangles. No OpenCASCADE, no model, no storage; the decisions mirror
# cad/scout so the two roads meet the same stage.
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from meshpipeline.cad.scout import (
    BOX_LIKE_FLAT_SHARE,
    MAX_OPENINGS,
    MIN_OPENING_FRACTION,
    MIN_RING_BORE_FRACTION,
    Opening,
    ScoutResult,
    _name_openings,
    drop_flange_twins,
    drop_stacked_rings,
    measured_faces,
)

#: Two triangles lie in one plane when their normals agree within this and their offsets within
#: a thousandth of the part's size.
PLANE_COS = math.cos(math.radians(3.0))
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
    if len(tris) < 4:
        raise ValueError("the file holds too few triangles to read")
    verts, faces = _weld(tris)
    bbox_min, bbox_max = verts.min(axis=0), verts.max(axis=0)
    diag = float(np.linalg.norm(bbox_max - bbox_min)) or 1.0
    centre = (bbox_min + bbox_max) / 2.0
    fn, fa, fc = _face_normals(verts, faces)

    edges, edge_faces, counts = _edges(faces)
    interior = counts == 2
    components = _components(len(faces), edge_faces[interior])
    n_components = int(components.max() + 1) if len(components) else 1

    notes: list[str] = []
    candidates: list[Opening] = []

    # OPEN RIMS: the boundary of an open mesh - the uncapped ends of a pipe wall, the open bottom
    # of a scene - each closed loop of edges that belong to one triangle only
    boundary_edges = edges[counts == 1]
    rims = 0
    for loop in _loops(boundary_edges):
        poly = _polygon(verts[loop])
        if poly is None:
            continue
        c, n, area, wh = poly
        if np.dot(n, c - centre) < 0:
            n = -n                                            # a rim faces away from the part
        candidates.append(_opening(-1, "rim", c, n, area, wh, bbox_min, bbox_max, diag))
        rims += 1

    # FLAT REGIONS: coplanar, edge-connected triangles. One boundary loop is a disc (a fluid
    # body's mouth, or a box's side); two or more make a ring whose hole is the opening.
    regions = _plane_regions(faces, fn, fc, edge_faces[interior], diag)
    sx, sy, sz = (float(v) for v in (bbox_max - bbox_min))
    box_skin = 2.0 * (sx * sy + sy * sz + sz * sx) or 1.0
    ground_area = 0.0
    for region in regions:
        area = float(fa[region].sum())
        if area < MIN_REGION_SHARE * box_skin:
            continue
        normal = fn[region].mean(axis=0)
        normal /= (np.linalg.norm(normal) or 1.0)
        region_edges, region_counts = _region_boundary(faces[region])
        loops = [verts[lp] for lp in _loops(region_edges[region_counts == 1])]
        polys = [p for p in (_polygon(lp) for lp in loops) if p is not None]
        if not polys:
            continue
        polys.sort(key=lambda p: p[2], reverse=True)
        outer = polys[0]
        w, h = outer[3]
        if min(w, h) < 0.05 * max(w, h):
            continue          # a facet strip of a curved skin, not a flat face of the part
        if np.dot(normal, outer[0] - centre) < 0 and n_components == 1:
            normal = -normal                                  # a lid faces out of its body
        if normal[2] < -0.9 and abs(outer[0][2] - bbox_min[2]) < 0.01 * diag:
            ground_area += area
        if len(polys) >= 2 and polys[1][2] >= MIN_RING_BORE_FRACTION * outer[2]:
            hole = polys[1]
            candidates.append(_opening(int(region[0]), "ring", hole[0], normal, hole[2], hole[3],
                                       bbox_min, bbox_max, diag))
        else:
            candidates.append(_opening(int(region[0]), "disc", outer[0], normal, area, outer[3],
                                       bbox_min, bbox_max, diag))

    candidates = drop_flange_twins(candidates, (float(centre[0]), float(centre[1]), float(centre[2])))
    candidates = drop_stacked_rings(candidates)
    measured = measured_faces(candidates)
    rings = [c for c in candidates if c.kind == "ring"]
    discs = [c for c in candidates if c.kind == "disc" and c.on_extremity]
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
        flat_share = sum(c.area for c in candidates if c.kind == "disc") / box_skin
        if len(discs) >= 2 and flat_share < BOX_LIKE_FLAT_SHARE:
            body_kind, input_kind, flow, pool, conf = "single_solid", "fluid-domain", "internal", discs, 0.6
        else:
            body_kind, input_kind, flow, pool, conf = "single_solid", "solid-body", "external", [], 0.6
            if len(discs) >= 2:
                notes.append(f"flat faces cover {100 * flat_share:.0f}% of the part's box, so it reads as a "
                             "solid body in a flow, not a fluid passage")
    notes.append("measured from triangles: positions and sizes are close, not exact")

    pool = sorted(pool, key=lambda o: o.area, reverse=True)
    if pool:
        largest = pool[0].area
        pool = [o for o in pool if o.area >= MIN_OPENING_FRACTION * largest]
    if len(pool) > MAX_OPENINGS:
        notes.append(f"{len(pool)} candidate openings found; only the {MAX_OPENINGS} largest are proposed")
        pool = pool[:MAX_OPENINGS]
    openings = _name_openings(pool)
    for k, o in enumerate(openings, start=1):
        o.sticker = k
        o.confidence = 0.65 if o.on_extremity else 0.45
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
        planar_faces=len(regions),
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
    }
    return result


# ---------------------------------------------------------------------------- the geometry ----
def _weld(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pts = tris.reshape(-1, 3)
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    key = np.round(pts / (1e-6 * span)).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = pts[first]
    faces = inverse.reshape(-1, 3)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return verts, faces[keep]


def _face_normals(verts, faces):
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    cross = np.cross(b - a, c - a)
    area2 = np.linalg.norm(cross, axis=1)
    normals = cross / np.where(area2 > 0, area2, 1.0)[:, None]
    return normals, area2 / 2.0, (a + b + c) / 3.0


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


def _plane_regions(faces, fn, fc, pairs, diag: float) -> list[np.ndarray]:
    """Edge-connected groups of coplanar triangles, each as an array of face indices."""
    d = np.einsum("ij,ij->i", fn, fc)
    parent = np.arange(len(faces))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tol = 1e-3 * diag
    for a, b in pairs:
        if a < 0 or b < 0:
            continue
        if np.dot(fn[a], fn[b]) >= PLANE_COS and abs(d[a] - d[b]) <= tol:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    roots = np.array([find(i) for i in range(len(faces))])
    groups: dict[int, list[int]] = {}
    for i, r in enumerate(roots):
        groups.setdefault(int(r), []).append(i)
    return [np.asarray(g) for g in groups.values() if len(g) >= 1]


def _region_boundary(region_faces: np.ndarray):
    e = np.concatenate([region_faces[:, [0, 1]], region_faces[:, [1, 2]], region_faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return uniq, counts


def _loops(edge_list: np.ndarray) -> list[list[int]]:
    """Closed vertex loops walked along the given edges; a vertex met by more than two edges is
    taken through whichever unvisited edge comes first."""
    if len(edge_list) == 0:
        return []
    adj: dict[int, list[int]] = {}
    for a, b in edge_list.tolist():
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    used: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    for start in adj:
        for nxt in adj[start]:
            if (start, nxt) in used or (nxt, start) in used:
                continue
            loop = [start]
            prev, cur = start, nxt
            used.add((prev, cur))
            while cur != start and len(loop) < 200_000:
                loop.append(cur)
                choices = [v for v in adj.get(cur, []) if (cur, v) not in used and (v, cur) not in used]
                if not choices:
                    break
                prev, cur = cur, choices[0]
                used.add((prev, cur))
            if cur == start and len(loop) >= 3:
                loops.append(loop)
    return loops


def _polygon(points: np.ndarray):
    """Centroid, unit normal (Newell), area and in-plane extents of a closed polygon, or None
    when it has no area."""
    if len(points) < 3:
        return None
    p = np.asarray(points, dtype=float)
    q = np.roll(p, -1, axis=0)
    n = np.array([np.sum((p[:, 1] - q[:, 1]) * (p[:, 2] + q[:, 2])),
                  np.sum((p[:, 2] - q[:, 2]) * (p[:, 0] + q[:, 0])),
                  np.sum((p[:, 0] - q[:, 0]) * (p[:, 1] + q[:, 1]))])
    area = float(np.linalg.norm(n)) / 2.0
    if area <= 0:
        return None
    n = n / (2.0 * area)
    c = p.mean(axis=0)
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= (np.linalg.norm(u) or 1.0)
    v = np.cross(n, u)
    rel = p - c
    su, sv = rel @ u, rel @ v
    wh = (float(su.max() - su.min()), float(sv.max() - sv.min()))
    return c, n, area, wh


def _opening(face_index: int, kind: str, c, n, area: float, wh, bbox_min, bbox_max, diag: float) -> Opening:
    n = np.asarray(n, dtype=float)
    ts = [((bbox_max[k] if n[k] > 0 else bbox_min[k]) - c[k]) / n[k] for k in range(3) if abs(n[k]) > 1e-6]
    on_extremity = bool(ts) and min(ts) <= 0.03 * diag
    return Opening(face_index=face_index, kind=kind, centroid=(float(c[0]), float(c[1]), float(c[2])),
                   normal=(float(n[0]), float(n[1]), float(n[2])), area=float(area),
                   wh=(float(wh[0]), float(wh[1])), clear_ahead=on_extremity, on_extremity=on_extremity)


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
