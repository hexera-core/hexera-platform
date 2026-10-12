# Responsibility: The delivered snap-grid mesh as the viewer draws it: every region's meshed surface under its name, every domain patch under its own, and the quality heatmap of the cells behind them.
# Owns: the viewer file the mesher writes beside the case (VIEWER_NAME) and the read of it into the viewer response.
# Boundaries: presentation only; it reads no polyMesh (the mesh is binary and split into regions) - the mesher hands over the topology it already holds, and the heatmap is measured from that by render/face_quality.
# Collaborates with: engines/snapgrid/mesher.py (writes), application/viewer_payload.py (calls the spec's viewer_surface), render/viewer_pack.py (packs), render/face_quality.py (measures).
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: The file beside the case holding the surfaces the viewer draws.
VIEWER_NAME = "snapgrid_viewer.npz"


def _gather(off: np.ndarray, flat: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The vertex ids of `faces` (rows of the offsets table `off` over `flat`), concatenated, and
    each face's vertex count - without a Python loop over the faces."""
    sizes = (off[faces + 1] - off[faces]).astype(np.int64)
    starts = np.repeat(off[faces].astype(np.int64), sizes)
    within = np.arange(int(sizes.sum()), dtype=np.int64) - np.repeat(
        np.concatenate(([0], np.cumsum(sizes)[:-1])), sizes)
    return flat[starts + within], sizes


def _polys(points: np.ndarray, off: np.ndarray, flat: np.ndarray,
           faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One surface's own points and its VTK polygon array ([n, i0, .. in-1] per face)."""
    vids, sizes = _gather(off, flat, faces)
    uniq, inv = np.unique(vids, return_inverse=True)
    polys = np.empty(len(vids) + len(sizes), dtype=np.uint32)
    starts = np.concatenate(([0], np.cumsum(sizes + 1)[:-1]))
    polys[starts] = sizes
    mask = np.ones(len(polys), dtype=bool)
    mask[starts] = False
    polys[mask] = inv
    return points[uniq].astype(np.float32), polys


def _by_group(groups: np.ndarray, n: int) -> list[np.ndarray]:
    """The positions of each group 0..n-1 in `groups`, each in ascending order (one sort, not a
    pass per group - a board has thousands of regions)."""
    order = np.argsort(groups, kind="stable")
    bounds = np.searchsorted(groups[order], np.arange(n + 1))
    return [order[bounds[g]:bounds[g + 1]] for g in range(n)]


def write_viewer(case: Path, *, points: np.ndarray, keys: np.ndarray, topo, cell_zone: np.ndarray,
                 zone_names: list[str], fluid: set[str], patches: list | None = None,
                 chunk_patches: list | None = None) -> dict:
    """What the viewer draws of a delivered snap-grid case, written beside it.

    Every REGION as one surface under its own name: its whole boundary - the interfaces it shares
    with other regions and any domain side it touches - so each part of the file is a part the
    user can pick, hide and isolate, and the interfaces are on screen wherever two regions meet.
    Every DOMAIN PATCH (`patches` / `chunk_patches`, as boxmesh.write takes them) as a surface
    under its mesh patch name. Surfaces that wrap the rest (a domain patch, a fluid region that
    reaches the domain's sides) are flagged, so the viewer opens them out of the way.

    Beside the geometry: the per-face quality heatmap (non-orthogonality, skewness, aspect ratio
    of the cell behind each drawn face, on that face's own side), measured here because this is
    the only place the whole mesh is in memory. Returns {name: faces} written."""
    from meshpipeline.engines.snapgrid.boxmesh import _boundary_patch_ids
    from meshpipeline.engines.snapgrid.curved import face_geometry

    n_int = len(topo.owner)
    ids_i = np.searchsorted(keys, topo.face_pts)
    ids_b = np.searchsorted(keys, topo.b_pts)
    # one face table over the whole mesh: internal faces, then boundary faces
    off = np.concatenate([topo.face_off, topo.face_off[-1] + topo.b_off[1:]]).astype(np.int64)
    flat = np.concatenate([ids_i, ids_b])
    owner = np.concatenate([topo.owner, topo.b_owner]).astype(np.int64)
    neighbour = np.asarray(topo.neighbour, dtype=np.int64)
    area = np.linalg.norm(face_geometry(points, off, flat)[0], axis=1)

    nz = len(zone_names)
    zo, zn = cell_zone[topo.owner], cell_zone[topo.neighbour]
    iface = np.flatnonzero(zo != zn)
    # every side of every face a region owns: both sides of an interface, the inside of a
    # domain face - with the cell on THAT side, which is the cell the region's heatmap reads
    side_face = np.concatenate([iface, iface, n_int + np.arange(len(topo.b_owner))])
    side_cell = np.concatenate([topo.owner[iface], topo.neighbour[iface], topo.b_owner])
    side_zone = cell_zone[side_cell]
    on_domain = np.concatenate([np.zeros(2 * len(iface), dtype=bool),
                                np.ones(len(topo.b_owner), dtype=bool)])

    surfaces: list[tuple[str, str, bool, np.ndarray, np.ndarray]] = []
    for z, rows in enumerate(_by_group(side_zone, nz)):
        if not len(rows):
            continue
        rows = rows[np.argsort(side_face[rows], kind="stable")]     # mesh face order
        name = zone_names[z]
        is_fluid = name in fluid
        surfaces.append((name, "patch" if is_fluid else "wall",
                         bool(is_fluid and on_domain[rows].any()),
                         side_face[rows], side_cell[rows]))
    n_regions = len(surfaces)
    if patches and chunk_patches:
        bp = _boundary_patch_ids(topo, chunk_patches)
        if len(bp) == len(topo.b_owner):
            for pid, faces in enumerate(_by_group(bp, len(patches))):
                if len(faces):
                    surfaces.append((patches[pid].name, patches[pid].type, True,
                                     n_int + faces, topo.b_owner[faces]))

    data: dict = {}
    for k, (_name, _ptype, _enc, faces, _cells) in enumerate(surfaces):
        data[f"p{k}"], data[f"f{k}"] = _polys(points, off, flat, faces)
    sizes_all = (np.sqrt(area[np.concatenate([s[3] for s in surfaces[:n_regions]])])
                 if n_regions else np.zeros(0))
    quality = _quality_json(points, off, flat, owner, neighbour, surfaces)
    if quality:
        data["quality"] = np.array(quality)
    np.savez_compressed(Path(case) / VIEWER_NAME,
                        names=np.array([s[0] for s in surfaces]),
                        types=np.array([s[1] for s in surfaces]),
                        encloses=np.array([s[2] for s in surfaces], dtype=bool),
                        counts=np.array([len(s[3]) for s in surfaces], dtype=np.int64),
                        cell_size=sizes_all.astype(np.float32), **data)
    return {s[0]: len(s[3]) for s in surfaces}


def _quality_json(points, off, flat, owner, neighbour, surfaces) -> str:
    """The heatmap fields of the drawn surfaces as render/face_quality packs them for every
    engine (the same metrics, bars and hotspots), or "" when they cannot be measured - the
    colouring is an addition to the mesh, never a reason to lose it."""
    try:
        from meshpipeline.engines.openfoam_criteria import MAX_NON_ORTHO
        from meshpipeline.render.face_quality import _Mesh, _Patch, quality_fields_of

        bar = MAX_NON_ORTHO.threshold
        mesh = _Mesh(np.ascontiguousarray(points, dtype=np.float64), flat.astype(np.int64),
                     off, owner, neighbour,
                     [_Patch(name, -1, len(faces), face_ids=faces, cells=cells)
                      for name, _t, _e, faces, cells in surfaces])
        fields = quality_fields_of(mesh, non_ortho_limit=float(bar)
                                   if isinstance(bar, (int, float)) else 65.0)
        return json.dumps(fields) if fields else ""
    except Exception:  # noqa: BLE001 - a heatmap is never worth a mesh
        logger.exception("snapgrid viewer: quality fields unavailable - drawn without a heatmap")
        return ""


def snapgrid_viewer(workspace, *, roles: dict, units: str,
                    skip_names: tuple[str, ...] = ()) -> dict | None:
    """The viewer response for a delivered snap-grid case, or None before one is written."""
    path = Path(workspace) / VIEWER_NAME
    if not path.exists():
        return None
    from meshpipeline.render.viewer_pack import polymesh_response

    with np.load(path, allow_pickle=False) as z:
        files = set(z.files)
        names = z["names"].tolist()
        # a viewer file written before the flags existed: nothing is said to enclose
        encloses = z["encloses"].tolist() if "encloses" in files else [False] * len(names)
        raw = []
        for k, (name, ptype, count) in enumerate(zip(names, z["types"].tolist(),
                                                     z["counts"].tolist())):
            if name in skip_names:
                continue
            raw.append({"name": name, "type": ptype, "face_count": int(count),
                        "encloses_domain": bool(encloses[k]),
                        "points": z[f"p{k}"].astype(np.float32).tobytes(),
                        "polys": z[f"f{k}"].astype(np.uint32).tobytes()})
        cs = z["cell_size"]
        stats = ({"p10": float(np.percentile(cs, 10)), "typ": float(np.median(cs))}
                 if len(cs) else None)
        quality = str(z["quality"]) if "quality" in files else ""
    resp = polymesh_response(raw, stats, roles, units)
    if resp and quality:
        try:
            fields = json.loads(quality)
            drawn = {p["name"] for p in raw}
            fields["patches"] = {n: v for n, v in (fields.get("patches") or {}).items()
                                 if n in drawn}
            if fields["patches"]:
                resp["quality_fields"] = fields
        except (ValueError, TypeError, AttributeError):
            logger.warning("snapgrid viewer: the stored quality fields are unreadable - "
                           "drawn without a heatmap")
    return resp
