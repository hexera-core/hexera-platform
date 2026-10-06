# Responsibility: Turn a hollow wall's internal-flow staging (the metal's whole skin, caps over the whole mouths) into the fluid's own closed boundary: the bore skin and the part of each cap inside the bore.
# Owns: the bore-only surface set a mesher without a seed point (cfMesh) needs, and its closure check.
# Boundaries: reads and writes STL triangles; it decides nothing about sizing or engines. When the result would not close, it returns None and the caller keeps its staging.
# Collaborates with: cad/cad_tessellate.tessellate_internal (the staging it starts from), engines/passage.cavity_skin (which wall pieces bound the cavity), engines/cfmesh/cfmesh_runner._configure_internal.
"""THE FLUID'S OWN BOUNDARY, FROM A HOLLOW WALL'S STAGING.

tessellate_internal stages a hollow wall for a CARVE: the metal's whole skin (bore, outer skin,
end rings, flanges) as the wall and a cap over each WHOLE mouth (end ring + bore disc), so the
cavity is sealed and snappy's seed point picks it. A mesher that fills a closed surface and takes
no seed point - cfMesh - is handed a surface that closes TWO regions, the metal and the cavity, and
fills whichever it finds: on the rocket nozzle it meshed the metal (inlet patch = the end ring,
0.56 of the declared bore; HOME-TURF lab, 2026-10-06). Here the surface is cut down to the bore
skin (engines/passage.cavity_skin) and, of each cap, the triangles that have the cavity behind
them, so it closes ONE region: the fluid.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _read(paths) -> np.ndarray:
    from meshpipeline.cad.stl_io import read_stl_triangles
    tris: list = []
    for p in (paths if isinstance(paths, (list, tuple)) else [paths]):
        if Path(p).exists():
            tris.extend(read_stl_triangles(Path(p)))
    return np.asarray(tris, dtype=float).reshape(-1, 3, 3)


def _poly(tris: np.ndarray):
    import pyvista as pv
    pts = tris.reshape(-1, 3)
    faces = np.arange(len(pts)).reshape(-1, 3)
    return pv.PolyData(pts, np.hstack([np.full((len(faces), 1), 3), faces]).ravel())


def open_edges(tris: np.ndarray, tol: float) -> int:
    """How many edges of the triangle soup are used once (welded within `tol`): 0 = closed."""
    if len(tris) == 0:
        return 0
    key = np.round(tris.reshape(-1, 3) / max(tol, 1e-12)).astype(np.int64)
    _, vid = np.unique(key, axis=0, return_inverse=True)
    f = vid.reshape(-1, 3)
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    e = e[e[:, 0] != e[:, 1]]
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return int((cnt == 1).sum())


def bore_only_surfaces(srcs: dict, wall_key: str, out_dir) -> dict | None:
    """{patch: STL path} of the fluid's own closed boundary - the bore skin under `wall_key`, each
    port's cap cut to the bore - written into `out_dir`; None when the staging is not a hollow wall
    this can cut (nothing kept, or the cut surface does not close), and the caller keeps it."""
    from meshpipeline.cad.stl_io import write_stl_binary
    from meshpipeline.engines.passage import _shut_in, _triangles, cavity_skin
    wall = _read(srcs.get(wall_key) or [])
    caps = {k: _read(v) for k, v in srcs.items() if k != wall_key}
    if len(wall) == 0 or not caps or any(len(c) == 0 for c in caps.values()):
        return None
    pts, faces = _triangles(_poly(wall))
    cap_polys = [_poly(c) for c in caps.values()]
    skin_pts, skin_f = cavity_skin(pts, faces, cap_polys)
    if len(skin_f) == 0 or len(skin_f) == len(faces):
        return None                                   # nothing told apart: not a hollow wall
    skin = np.asarray(skin_pts, dtype=float)[np.asarray(skin_f)]
    all_caps = np.concatenate(list(caps.values()))
    closed = np.concatenate([skin, all_caps])
    diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    kept: dict[str, np.ndarray] = {}
    for name, ctris in caps.items():
        cr = np.cross(ctris[:, 1] - ctris[:, 0], ctris[:, 2] - ctris[:, 0])
        area = 0.5 * np.linalg.norm(cr, axis=1)
        n = cr / (2.0 * area[:, None]).clip(1e-30)
        cen = ctris.mean(axis=1)
        keep = np.zeros(len(ctris), dtype=bool)
        for i in range(len(ctris)):
            if area[i] <= 0.0:
                continue
            eps = min(1e-4 * diag, 0.25 * float(np.sqrt(area[i])))
            # the bore disc has the cavity behind it, shut in by the bore skin and the caps; the
            # end ring has the metal behind it, which the outer skin - left out here - no longer
            # closes
            keep[i] = (_shut_in(closed, cen[i] + eps * n[i]) or _shut_in(closed, cen[i] - eps * n[i]))
        if not keep.any():
            return None
        kept[name] = ctris[keep]
    surface = np.concatenate([skin, *kept.values()])
    gaps = open_edges(surface, 1e-6 * diag)
    if gaps:
        logger.warning("bore-only staging: the cut surface has %d open edge(s) - keeping the "
                       "whole-mouth staging", gaps)
        return None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {wall_key: out / f"{wall_key}.stl"}
    write_stl_binary(written[wall_key], [tuple(map(tuple, t)) for t in skin])
    for name, tris in kept.items():
        written[name] = out / f"{name}.stl"
        write_stl_binary(written[name], [tuple(map(tuple, t)) for t in tris])
    logger.info("bore-only staging: %d of %d wall faces bound the cavity; caps cut to %s",
                len(skin), len(faces), {k: len(v) for k, v in kept.items()})
    return {k: str(v) for k, v in written.items()}


__all__ = ["bore_only_surfaces", "open_edges"]
