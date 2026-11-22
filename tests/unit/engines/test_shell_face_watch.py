# Responsibility: Verify that one face never costs a whole shell mesh - SHMESH-71's watchdog on gmsh's 2D mesh (a face
# gmsh holds too long goes down a ladder: Delaunay, MeshAdapt, coarser, finer, re-made as a B-spline, then left out),
# SHMESH-72 (a face re-made as a B-spline that stops gmsh again is left out, not re-made in a loop) and SHMESH-74 (a
# face left out is said first and never passes quietly) - and that with nothing tripped the mesh is the same as with
# no watchdog at all.
# Boundaries: the real surface run and shell mesher on a small beaded sheet built here (a few seconds a mesh); gmsh's
# slowness is stood for by a zero time limit (every face "too long"), a stop on a face by the adapter's own _Periodic.
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests.unit.engines.test_surface_requirements import asked  # noqa: E402

from meshpipeline.engines import shell_delivery as SD  # noqa: E402
from meshpipeline.engines import surface_run  # noqa: E402
from meshpipeline.engines.gmsh import shell_adapter as SA  # noqa: E402


def _beaded_sheet(path: Path, r: float = 1.0) -> Path:
    """A 120 x 80 sheet with one drafted bead 80 x 12 x 4 mm, its sharp edges rounded r, written as a surface STEP."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        box = occ.addBox(-60, -40, -10, 120, 80, 10)
        h, w, ln, below = 4.0, 12.0, 80.0, 2.0
        d = (h + below) / math.tan(math.radians(60))
        r1, r0, a = w / 2, w / 2 + d, ln / 2 - w / 2

        def loop(pts):
            tg = [occ.addPoint(*p) for p in pts]
            return occ.addPlaneSurface([occ.addCurveLoop([occ.addLine(tg[i], tg[(i + 1) % 4]) for i in range(4)])])

        s = loop([(-a, -r0, -below), (-a, r0, -below), (-a, r1, h), (-a, -r1, h)])
        bead = [t for t in occ.extrude([(2, s)], 2 * a, 0, 0) if t[0] == 3]
        for sx in (-1, 1):
            hs = loop([(sx * a, 0, -below), (sx * a, r0, -below), (sx * a, r1, h), (sx * a, 0, h)])
            rv = [t for t in occ.revolve([(2, hs)], sx * a, 0, 0, 0, 0, 1, math.pi if sx < 0 else -math.pi)
                  if t[0] == 3]
            bead = [t for t in occ.fuse(bead, rv)[0] if t[0] == 3]
        vol = [t for t in occ.fuse([(3, box)], bead)[0] if t[0] == 3]
        occ.synchronize()
        sharp = []
        for _d, f in gmsh.model.getBoundary(vol, oriented=False):
            for _d1, c in gmsh.model.getBoundary([(2, abs(f))], oriented=False):
                b = gmsh.model.getBoundingBox(1, abs(c))
                if not (abs(b[0]) < 59 and abs(b[3]) < 59 and abs(b[1]) < 39 and abs(b[4]) < 39 and b[2] > -1):
                    continue
                up = list(gmsh.model.getAdjacencies(1, abs(c))[0])
                lo, hi = gmsh.model.getParametrizationBounds(1, abs(c))
                x = gmsh.model.getValue(1, abs(c), [0.5 * (lo[0] + hi[0])])
                ns = []
                for f2 in up:
                    _p, uv = gmsh.model.getClosestPoint(2, f2, x)
                    n = np.array(gmsh.model.getNormal(f2, uv))
                    ns.append(n / np.linalg.norm(n))
                if len(ns) == 2 and abs(float(ns[0] @ ns[1])) < math.cos(math.radians(8)):
                    sharp.append(abs(c))
        vol = [t for t in occ.fillet([v[1] for v in vol], sorted(set(sharp)), [r]) if t[0] == 3]
        occ.synchronize()
        drop = []
        for _d, f in gmsh.model.getBoundary(vol, oriented=False):
            b = gmsh.model.getBoundingBox(2, abs(f))
            if ((abs(b[0] - b[3]) < 1e-6 and abs(abs(b[0]) - 60) < 1e-6)
                    or (abs(b[1] - b[4]) < 1e-6 and abs(abs(b[1]) - 40) < 1e-6) or abs(b[5] + 10) < 1e-6):
                drop.append(abs(f))
        occ.remove(vol)
        occ.remove([(2, f) for f in drop], recursive=True)
        occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
    return path


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> Path:
    return _beaded_sheet(tmp_path_factory.mktemp("watch") / "bead.step")


def _run(ws: Path, step: Path) -> tuple[dict, dict]:
    ws.mkdir(parents=True, exist_ok=True)
    rec = surface_run.run(ws, step, mm_per_unit=1.0, thickness_mm=1.0, said_shell=True, source="bead.step",
                          requirements=asked(4.0))
    return rec, json.loads((ws / "shell_quality.json").read_text())


def test_a_face_gmsh_holds_goes_down_its_ladder_then_is_left_out_and_said(tmp_path, sheet, monkeypatch):
    monkeypatch.setattr(SA, "FACE_WATCH_S", 0.0)       # every face "held too long": the first one meshed trips
    monkeypatch.setattr(SA, "FACE_WATCH_POLL", 0.02)
    monkeypatch.setattr(SA, "FACE_WATCH_TRIPS", len(SA.FACE_LADDER) + 1)
    real_read = SA._watch_read
    chosen: list = []

    def one_face(d, seen):
        # (only one face is "held" - the largest under a fifth of the part: the same face every pass)
        got = real_read(d, seen)
        if got is None:
            return None
        rows = json.loads((Path(d) / "tags.json").read_text())
        total = sum(v[1] for v in rows.values())
        if not chosen:
            chosen.append(rows[max((t for t in rows if rows[t][1] < 0.2 * total), key=lambda t: rows[t][1])][1])
        return got if str(got[0]) in rows and abs(rows[str(got[0])][1] - chosen[0]) < 1e-6 * chosen[0] else None

    monkeypatch.setattr(SA, "_watch_read", one_face)
    rec, applied = _run(tmp_path / "w", sheet)
    watch = applied["quality"].get("face_watch") or {}
    assert rec["made"] and rec["quality"]["valid"] is True
    # (every rung tried - the curve rung only when on and gmsh named a curve -, then left out)
    assert watch.get("trips") == len(SA.FACE_LADDER) + int(SA.CURVE_RUNG) and watch.get("left_out") == 1
    assert watch["left_out_mm2"][0] > 0 and 0 < watch["left_out_share"] < 1
    # said first, plainly, and never a quiet pass
    assert "left out" in watch["words"] and "opening" in watch["words"] and "mm2 (" in watch["words"]
    assert surface_run.words(rec).startswith(watch["words"])
    q = SD.quality_of(json.loads((tmp_path / "w" / "shell_run.json").read_text()))
    check = SD.results(tmp_path / "w", q, {})["check"]
    assert check["outcome"] != "pass" and "left out" in check["words"]
    assert any(r["key"] == "faces_left_out" and r["ok"] is False for r in check["rows"])
    monkeypatch.setattr(SA, "FACE_WATCH_S", 240.0)
    whole, _a = _run(tmp_path / "whole", sheet)
    assert 0 < rec["elements"] < whole["elements"]                       # the rest of the part is meshed


def test_with_nothing_tripped_the_watched_mesh_is_the_mesh_without_a_watchdog(tmp_path, sheet, monkeypatch):
    _run(tmp_path / "on", sheet)
    monkeypatch.setattr(SA, "FACE_WATCH", False)
    _run(tmp_path / "off", sheet)
    assert (tmp_path / "on" / "shell.bdf").read_bytes() == (tmp_path / "off" / "shell.bdf").read_bytes()


def test_a_face_remade_once_that_stops_gmsh_again_is_left_out_not_remade_again(tmp_path, sheet, monkeypatch):
    monkeypatch.setattr(SA, "MESH_DEADLINE_S", 0)       # in this process: the stops are raised here
    monkeypatch.setattr(SA, "REMADE_EARLY", True)       # (the switch: off by default, re-made again as before)
    calls = {"once": 0, "remade": 0}
    once, remade = SA._mesh_once, SA.respline

    def stopping_once(*a, **k):
        calls["once"] += 1
        if calls["once"] <= 2:
            raise SA._Periodic(0, loop=True)               # gmsh "stops" on the same face, before and after re-made
        return once(*a, **k)

    def counting_respline(*a, **k):
        calls["remade"] += 1
        return remade(*a, **k)

    monkeypatch.setattr(SA, "_mesh_once", stopping_once)
    monkeypatch.setattr(SA, "respline", counting_respline)
    rec, applied = _run(tmp_path / "r", sheet)
    assert rec["made"] and calls["remade"] == 1                       # re-made once, never again
    # left out of the structured path; a path with fewer steps meshed it, and was kept
    guard = (applied["quality"].get("structure") or {}).get("guard") or {}
    assert ((applied["quality"].get("face_watch") or {}).get("left_out") == 1
            or guard.get("rule") in ("SHMESH-72", "SHMESH-45g", "SHMESH-45"))


def test_past_a_quarter_of_the_part_left_out_the_mesh_is_given_up(tmp_path, sheet, monkeypatch):
    monkeypatch.setattr(SA, "FACE_WATCH_S", 0.0)
    monkeypatch.setattr(SA, "FACE_WATCH_POLL", 0.02)
    real_read = SA._watch_read

    def big_face(d, seen):
        got = real_read(d, seen)
        if got is None:
            return None
        rows = json.loads((Path(d) / "tags.json").read_text())
        return got if str(got[0]) == max(rows, key=lambda t: rows[t][1]) else None

    monkeypatch.setattr(SA, "_watch_read", big_face)
    ws = tmp_path / "g"
    ws.mkdir()
    rec = surface_run.run(ws, sheet, mm_per_unit=1.0, thickness_mm=1.0, said_shell=True, source="bead.step",
                          requirements=asked(4.0))
    assert not rec["made"] and "given up" in rec["why"] and "SHMESH-72" in rec["why"]
