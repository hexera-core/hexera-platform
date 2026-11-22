# Responsibility: Verify SHMESH-93: in the one-count tests a side shorter than the user's shortest edge fits one
# segment (any mesh of the face has it as one edge): _most_segments, and on a real stamped dart (a fillet round a
# bend's corner, its inner arc 0.79 mm under the 2 mm shortest edge) the corner fillets are mapped, with fewer
# triangles than paved and the mesh valid and inside Nastran's limits.
# Boundaries: a pure function, then the real surface run on a small bend with one dart built here (seconds). No
# solver.
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.gmsh import shell_adapter as SA


def test_a_curve_shorter_than_the_shortest_edge_takes_one_segment(monkeypatch):
    monkeypatch.setattr(SA, "SHORT_SIDE_ONE", True)
    assert SA._most_segments(0.79, 1.9) == 1
    assert SA._most_segments(5.0, 1.9) == 2
    assert SA._most_segments(1.9, 1.9) == 1


def test_without_the_rule_such_a_curve_fits_no_count(monkeypatch):
    monkeypatch.setattr(SA, "SHORT_SIDE_ONE", False)
    assert SA._most_segments(0.79, 1.9) == 0
    assert SA._most_segments(5.0, 1.9) == 2


# ------------------------------------------------------------------------------------------- a real dart
gmsh = pytest.importorskip("gmsh")


def _on_planes(dim: int, tag: int, planes, tol: float = 1e-4) -> bool:
    b = gmsh.model.getBoundingBox(dim, tag)
    return any(abs(b[ax] - v) < tol and abs(b[ax + 3] - v) < tol for ax, v in planes)


def _sharp(curve: int) -> float | None:
    up = list(gmsh.model.getAdjacencies(1, curve)[0])
    if len(up) != 2:
        return None
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    x = gmsh.model.getValue(1, curve, [0.5 * (lo[0] + hi[0])])
    ns = []
    for f in up:
        _c, uv = gmsh.model.getClosestPoint(2, f, x)
        n = np.array(gmsh.model.getNormal(f, uv))
        ns.append(n / max(np.linalg.norm(n), 1e-30))
    return math.degrees(math.acos(max(-1.0, min(1.0, abs(float(np.dot(ns[0], ns[1])))))))


def _dart_bend(path: Path) -> Path:
    """An L bend along x (flange 30 mm, inner radius 1.5 mm, flange down 25 mm, 60 mm long) with one dart 7 mm deep and
    6 mm wide stamped across the inside of the bend, every sharp edge of the skin rounded 1 mm; the skin (the faces
    off the block's outer planes) written as a surface STEP."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        L, a, b, r, E = 60.0, 30.0, 25.0, 1.5, 15.0
        planes = [(0, -L / 2), (0, L / 2), (1, 0.0), (1, a + r + E), (2, E), (2, -b)]
        pts = [occ.addPoint(-L / 2, y, z) for y, z in ((0, 0), (a, 0), (a + r, -r), (a + r, -b), (a + r + E, -b),
                                                         (a + r + E, E), (0, E))]
        pc = occ.addPoint(-L / 2, a, -r)
        cs = [occ.addLine(pts[0], pts[1]), occ.addCircleArc(pts[1], pc, pts[2])]
        cs += [occ.addLine(pts[i], pts[i + 1]) for i in range(2, 6)] + [occ.addLine(pts[6], pts[0])]
        v = [t for t in occ.extrude([(2, occ.addPlaneSurface([occ.addCurveLoop(cs)]))], L, 0, 0) if t[0] == 3]
        size, width = 7.0, 6.0
        q = [occ.addPoint(-width / 2, a - size, 0.0), occ.addPoint(-width / 2, a + r, -r - size),
             occ.addPoint(-width / 2, a + r, 0.0)]
        qs = occ.addPlaneSurface([occ.addCurveLoop([occ.addLine(q[0], q[1]), occ.addLine(q[1], q[2]),
                                                    occ.addLine(q[2], q[0])])])
        w = [t for t in occ.extrude([(2, qs)], width, 0, 0) if t[0] == 3]
        v = [t for t in occ.fuse(v, w)[0] if t[0] == 3]
        occ.synchronize()
        curves = {abs(c) for vv in v for _d, f in gmsh.model.getBoundary([vv], oriented=False)
                  for _d1, c in gmsh.model.getBoundary([(2, abs(f))], oriented=False)}
        take = [c for c in sorted(curves) if not _on_planes(1, c, planes) and (_sharp(c) or 0.0) >= 8.0]
        v = [t for t in occ.fillet([vv[1] for vv in v], take, [1.0], removeVolume=True) if t[0] == 3]
        occ.synchronize()
        faces = [abs(f) for vv in v for _d, f in gmsh.model.getBoundary([vv], oriented=False)
                 if not _on_planes(2, abs(f), planes)]
        drop = [(2, abs(f)) for vv in v for _d, f in gmsh.model.getBoundary([vv], oriented=False)
                if abs(f) not in faces]
        occ.remove(v)
        occ.remove(drop, recursive=True)
        occ.removeAllDuplicates()
        occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
    return path


@pytest.fixture(scope="module")
def dart(tmp_path_factory) -> Path:
    return _dart_bend(tmp_path_factory.mktemp("dart") / "dart.step")


def _run(ws: Path, step: Path) -> dict:
    import json

    from tests.unit.engines.test_surface_requirements import asked

    from meshpipeline.engines import surface_run

    ws.mkdir(parents=True, exist_ok=True)
    surface_run.run(ws, step, mm_per_unit=1.0, thickness_mm=1.0, said_shell=True, source="dart.step",
                    requirements=asked(4.0))
    return json.loads((ws / "shell_quality.json").read_text())["quality"]


def test_a_darts_corner_fillets_are_mapped_with_fewer_triangles(tmp_path, dart, monkeypatch):
    monkeypatch.setattr(SA, "SHORT_SIDE_ONE", True)
    on = _run(tmp_path / "on", dart)
    monkeypatch.setattr(SA, "SHORT_SIDE_ONE", False)
    off = _run(tmp_path / "off", dart)
    assert on["valid"] is True and on["within_nastran"] is True
    assert int(on.get("fit_mapped") or 0) > int(off.get("fit_mapped") or 0)
    assert int(on["trias"]) < int(off["trias"]), (on["trias"], off["trias"])
