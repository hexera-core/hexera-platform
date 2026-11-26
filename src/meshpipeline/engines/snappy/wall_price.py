# Responsibility: Price the wall faces snappyHexMesh will build, before it runs, from what the case will refine.
# Owns: the feature-edge length of a staged surface (the edges surfaceFeatureExtract will mark) and the faces the
# feature level's band adds to the wall.
# Boundaries: reads the staged STL only; snappy_runner.render_snappy_case uses the price in its cell estimate.
"""THE WALL'S FACES, PRICED FROM WHAT THE CASE WILL REFINE (RUN-12).

A plan priced its wall at area / wall cell^2. snappyHexMesh refines every feature edge two levels finer than the
surface (features ( { file "<surface>.eMesh"; level <surface + 2>; } )), and around that tube it keeps
nCellsBetweenLevels cells of each level in between. On an assembly with many sharp edges that band is most of the
wall: the RM5 surge flap's first pass held 0.50 M wall faces where area / wall cell^2 priced 0.042 M, so the plan
never knew it was over, the size brake stopped the pass after 44 minutes, and only the second pass - priced on the
faces the first had learned (RUN-8) - was delivered.

The band, per side of an edge of length L, on a wall at surface level s (cell h_s) and feature level f:
  level f:        one cell of h_f across     ->  h_f * (1/h_f^2 - 1/h_s^2) faces per metre more than the plain wall
  levels s+1..f-1: nCellsBetweenLevels of h_l ->  n * h_l * (1/h_l^2 - 1/h_s^2)
and a feature edge has two sides. The edges are the ones surfaceFeatureExtract marks with the case's own
includedAngle (normals more than 180 - includedAngle apart), plus open and non-manifold edges."""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

#: The rule (docs/RULES.md): the wall priced from what the case refines; the brake judged at castellation's end.
WALL_PRICE_RULE = "RUN-12"
#: The surfaceFeatureExtract angle when the case's dict cannot be read (snappy_runner's external default).
DEFAULT_INCLUDED_ANGLE = 150.0
#: A side of a feature edge: the plain wall on each face that meets there.
SIDES = 2
#: Cells of the feature level across its tube, per side (the cell the edge passes through).
FEATURE_TUBE_CELLS = 1.0

_FACET = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")
_INCLUDED = re.compile(r"includedAngle\s+([0-9.eE+-]+)")


def _triangles(path: Path) -> np.ndarray:
    """(n, 3, 3) float array of an ASCII or binary STL's triangles."""
    data = Path(path).read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:1024]:
        v = np.array(_FACET.findall(data), dtype=float)
        return v[: (len(v) // 3) * 3].reshape(-1, 3, 3)
    n = int(np.frombuffer(data, dtype="<u4", count=1, offset=80)[0])
    rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                        count=n, offset=84)
    return rec["v"].astype(float)


def feature_length(stl: Path, included_angle: float = DEFAULT_INCLUDED_ANGLE) -> float:
    """The total length of the edges surfaceFeatureExtract marks on this surface: two faces whose normals are more
    than 180 - includedAngle apart, and every open or non-manifold edge. 0 when the file cannot be read."""
    try:
        tri = _triangles(Path(stl))
    except (OSError, ValueError, IndexError):
        return 0.0
    if len(tri) == 0:
        return 0.0
    pts, inv = np.unique(tri.reshape(-1, 3), axis=0, return_inverse=True)
    f = inv.reshape(-1, 3)
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    ok = ln > 0
    nrm[ok] /= ln[ok, None]
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    owner = np.tile(np.arange(len(f)), 3)
    e.sort(axis=1)
    order = np.lexsort((e[:, 1], e[:, 0]))
    e, owner = e[order], owner[order]
    key = e[:, 0].astype(np.int64) * (len(pts) + 1) + e[:, 1]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    counts = np.diff(np.r_[starts, len(key)])
    cos_lim = math.cos(math.radians(180.0 - float(included_angle)))
    feat = counts != 2
    two = np.flatnonzero(counts == 2)
    a, b = owner[starts[two]], owner[starts[two] + 1]
    feat[two] = np.einsum("ij,ij->i", nrm[a], nrm[b]) < cos_lim
    ue = e[starts[feat]]
    return float(np.linalg.norm(pts[ue[:, 0]] - pts[ue[:, 1]], axis=1).sum())


def emesh_length(path: Path) -> float:
    """The total edge length of an OpenFOAM featureEdgeMesh (.eMesh) - for checking feature_length against what
    surfaceFeatureExtract wrote. 0 when it cannot be read."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return 0.0
    body = text.split("FoamFile", 1)[-1].split("}", 1)[-1]
    pts, edges = [], []
    for item in re.findall(r"\(([^()]*)\)", body):
        vals = item.split()
        if len(vals) == 3:
            pts.append([float(x) for x in vals])
        elif len(vals) == 2:
            edges.append([int(x) for x in vals])
    if not pts or not edges:
        return 0.0
    p, ed = np.array(pts), np.array(edges)
    if ed.max() >= len(p):
        return 0.0
    return float(np.linalg.norm(p[ed[:, 0]] - p[ed[:, 1]], axis=1).sum())


def band_faces(length: float, *, wall_cell: float, surface_level: int, feature_level: int,
               n_between: int = 3) -> float:
    """The wall faces the feature level's band adds over the plain wall at the surface level, for `length` metres
    of feature edge (module docstring)."""
    if not (length > 0 and wall_cell > 0) or feature_level <= surface_level:
        return 0.0
    plain = 1.0 / wall_cell ** 2
    extra = 0.0
    for lev in range(int(surface_level) + 1, int(feature_level) + 1):
        h = wall_cell / 2.0 ** (lev - int(surface_level))
        cells = FEATURE_TUBE_CELLS if lev == int(feature_level) else float(n_between)
        extra += cells * h * (1.0 / h ** 2 - plain)
    return SIDES * float(length) * extra


def included_angle_of(workspace) -> float:
    """The includedAngle the case's own surfaceFeatureExtractDict gives (DEFAULT_INCLUDED_ANGLE when unread)."""
    try:
        m = _INCLUDED.search((Path(workspace) / "system" / "surfaceFeatureExtractDict").read_text())
    except OSError:
        return DEFAULT_INCLUDED_ANGLE
    return float(m.group(1)) if m else DEFAULT_INCLUDED_ANGLE


def priced_face_factor(stl: Path, *, area: float | None, wall_cell: float, surface_level: int,
                       feature_level: int, included_angle: float = DEFAULT_INCLUDED_ANGLE,
                       n_between: int = 3) -> tuple[float, float]:
    """(how many times area / wall cell^2 the wall's faces come to with the feature band, the feature length in
    metres). (1.0, 0.0) when it cannot be priced."""
    try:
        a = float(area)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0, 0.0
    if not (a > 0 and wall_cell > 0):
        return 1.0, 0.0
    length = feature_length(stl, included_angle)
    plain = a / wall_cell ** 2
    extra = band_faces(length, wall_cell=wall_cell, surface_level=surface_level, feature_level=feature_level,
                       n_between=n_between)
    return max(1.0, (plain + extra) / plain), length


__all__ = ["WALL_PRICE_RULE", "DEFAULT_INCLUDED_ANGLE", "FEATURE_TUBE_CELLS", "SIDES", "band_faces", "emesh_length",
           "feature_length", "included_angle_of", "priced_face_factor"]
