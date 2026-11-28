# Responsibility: SHMESH-100 - the mid-surface B-rep read again once the shell mesh is made, so that the extra quality
# passes put a moved node back ON the CAD: on the surface of the sewn face it is on, or on the CAD curve of a face's
# boundary it stands on (a hole's rim).
# Boundaries: gmsh OCC in a session of its own (`opened`: initialised and finalised there, so the caller runs it in a
# forked child); plain arrays in millimetres in and out; it changes no mesh.
# Collaborates with: engines/gmsh/shell_quality_passes.py (the Projector it is), engines/gmsh/shell_adapter.py
# (_quality_passes opens it in the passes' own forked child).
from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

RULE = "SHMESH-100"
#: a face's nodes are tried on a CAD surface at up to this many of them, spread over the face (SHMESH-100: each node
#: is checked again by itself only when it is a candidate to move: a big B-spline face costs no check of every node)
SAMPLE = 32


class CadProjector:
    """sewn face k -> its CAD surface (found by its nodes standing on it), and the CAD curves, in millimetres."""

    def __init__(self, gmsh: Any, mm: float):
        self.g = gmsh
        self.mm = float(mm)
        self.surfs = [int(t) for _d, t in gmsh.model.getEntities(2)]
        self.sbox = np.array([gmsh.model.getBoundingBox(2, t) for t in self.surfs], dtype=float).reshape(-1, 6) * mm
        self.curves = [int(c) for _d, c in gmsh.model.getEntities(1)]
        self.cbox = np.array([gmsh.model.getBoundingBox(1, c) for c in self.curves], dtype=float).reshape(-1, 6) * mm
        self.tag: dict[int, int] = {}

    def _closest(self, dim: int, tag: int, pts: np.ndarray) -> np.ndarray:
        """`pts` (n, 3, mm) put on entity (dim, tag); NaN rows where gmsh gives no answer."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        try:
            got, _par = self.g.model.getClosestPoint(dim, tag, (pts / self.mm).ravel().tolist())
        except Exception:  # noqa: BLE001 - no projection: the node is not moved there
            return np.full_like(pts, np.nan)
        out = np.asarray(got, dtype=float)
        if out.size != pts.size:
            return np.full_like(pts, np.nan)
        return out.reshape(-1, 3) * self.mm

    def _near(self, box: np.ndarray, pts: np.ndarray, tol: float) -> np.ndarray:
        lo, hi = pts.min(axis=0) - tol, pts.max(axis=0) + tol
        return np.flatnonzero((box[:, :3] <= hi).all(axis=1) & (box[:, 3:] >= lo).all(axis=1))

    def _off(self, dim: int, tag: int, pts: np.ndarray) -> float:
        d = np.linalg.norm(self._closest(dim, tag, pts) - pts, axis=1)
        return float(d.max()) if np.isfinite(d).all() else math.inf

    def face(self, k: int, nodes: np.ndarray, tol: float) -> bool:
        if k in self.tag:
            return True
        nodes = np.asarray(nodes, dtype=float).reshape(-1, 3)
        if not len(nodes) or not tol > 0:
            return False
        pick = nodes[np.unique(np.linspace(0, len(nodes) - 1, min(len(nodes), SAMPLE)).astype(int))]
        best, at = math.inf, None
        for i in self._near(self.sbox, pick, tol).tolist():
            d = self._off(2, self.surfs[i], pick)
            if d < best:
                best, at = d, self.surfs[i]
        if at is None or best > tol:
            return False
        self.tag[k] = at
        return True

    def surface(self, k: int, pts: np.ndarray) -> np.ndarray:
        return self._closest(2, self.tag[k], pts)

    def curve(self, pts: np.ndarray, tol: float) -> int | None:
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        if not tol > 0:
            return None
        best, at = math.inf, None
        for i in self._near(self.cbox, pts, tol).tolist():
            d = self._off(1, self.curves[i], pts)
            if d < best:
                best, at = d, self.curves[i]
        return at if at is not None and best <= tol else None

    def on_curve(self, c: int, pts: np.ndarray) -> np.ndarray:
        return self._closest(1, c, pts)


class PatchMesher:
    """SHMESH-100's local re-mesh: a small polygon of a face meshed again in a gmsh model of its own (the open session's
    model stays current after), its outline's nodes the only nodes on it (each side one element edge)."""

    #: (gmsh 2D algorithm, quads by recombining, size factor): the ways tried
    WAYS = ((8, True, 1.0), (6, True, 1.0), (6, True, 1.3), (6, False, 1.0), (5, False, 1.0))

    def __init__(self, gmsh: Any):
        self.g = gmsh

    def patch(self, ring: np.ndarray, normal: np.ndarray, size: float) -> list[tuple[np.ndarray, list[list[int]]]]:
        g = self.g
        ring = np.asarray(ring, dtype=float).reshape(-1, 3)
        m = len(ring)
        nrm = np.asarray(normal, dtype=float)
        nrm = nrm / max(float(np.linalg.norm(nrm)), 1e-300)
        a = np.cross(nrm, [1.0, 0.0, 0.0])
        if np.linalg.norm(a) < 0.5:
            a = np.cross(nrm, [0.0, 1.0, 0.0])
        a = a / np.linalg.norm(a)
        b = np.cross(nrm, a)
        c = ring.mean(axis=0)
        uv = np.c_[(ring - c) @ a, (ring - c) @ b]
        if m < 3 or not size > 0:
            return []
        try:
            cur = g.model.getCurrent()
        except Exception:  # noqa: BLE001 - no model to go back to
            cur = None
        out: list[tuple[np.ndarray, list[list[int]]]] = []
        for way in self.WAYS:
            g.model.add("shmesh100_patch")
            try:
                got = self._way(way, uv, size, a, b, c)
            except Exception:  # noqa: BLE001 - a way gmsh does not make is not tried
                got = None
            finally:
                with contextlib.suppress(Exception):
                    g.model.remove()
                if cur is not None:
                    with contextlib.suppress(Exception):
                        g.model.setCurrent(cur)
            if got is not None:
                out.append(got)
        return out

    def _way(self, way: tuple, uv: np.ndarray, size: float, a: np.ndarray, b: np.ndarray,
             c: np.ndarray) -> tuple[np.ndarray, list[list[int]]] | None:
        """The polygon `uv` meshed one way in the current (empty) model; None when its outline did not hold."""
        g = self.g
        algo, recombine, f = way
        m = len(uv)
        geo = g.model.geo
        p = [geo.addPoint(float(u), float(v), 0.0, f * size) for u, v in uv]
        ls = [geo.addLine(p[i], p[(i + 1) % m]) for i in range(m)]
        s = geo.addPlaneSurface([geo.addCurveLoop(ls)])
        geo.synchronize()
        for ln in ls:
            g.model.mesh.setTransfiniteCurve(ln, 2)
        if recombine:
            g.model.mesh.setRecombine(2, s)
        g.option.setNumber("Mesh.Algorithm", algo)
        g.option.setNumber("Mesh.RecombinationAlgorithm", 1)
        g.model.mesh.generate(2)
        tags, xyz, _par = g.model.mesh.getNodes()
        q = np.asarray(xyz, dtype=float).reshape(-1, 3)[:, :2]
        d = np.linalg.norm(q[:, None] - uv[None], axis=2)
        near = d.argmin(axis=1)
        on = d[np.arange(len(q)), near] <= 1e-6 * size
        if int(on.sum()) != m or len(set(near[on].tolist())) != m:
            return None
        num: dict[int, int] = {}
        new: list[np.ndarray] = []
        for t, i, o, uvp in zip(np.asarray(tags).tolist(), near.tolist(), on.tolist(), q, strict=True):
            if o:
                num[int(t)] = int(i)
            else:
                num[int(t)] = m + len(new)
                new.append(uvp)
        elems: list[list[int]] = []
        types, _etags, enodes = g.model.mesh.getElements(2, s)
        for ty, en in zip(types, enodes, strict=True):
            k = {2: 3, 3: 4}.get(int(ty))
            if k is None:
                raise ValueError(f"element type {ty}")
            elems.extend([num[int(x)] for x in row] for row in np.asarray(en).reshape(-1, k).tolist())
        nw = np.asarray(new, dtype=float).reshape(-1, 2)
        return c + nw[:, :1] * a + nw[:, 1:2] * b, elems


@contextlib.contextmanager
def opened(brep: Path | str, mm: float) -> Iterator[CadProjector]:
    """The mid-surface `brep` (model units, `mm` millimetres each) open in a gmsh session of its own."""
    import gmsh

    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("on_cad")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        yield CadProjector(gmsh, mm)
    finally:
        gmsh.finalize()
