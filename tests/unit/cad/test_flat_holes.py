# Responsibility: Verify INS-13 - every hole of a flat face is measured, and a passage opens where its bore
# meets the part's flat face: a cap's three port holes in one top face are its openings (not the
# step down each bore the map reads tightest), and a flange's bolt holes, which open no passage, are not.
# Boundaries: real OpenCASCADE solids written to STEP; the geometry check's own CAD reading; no model.
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")
PORTS = [(9.0 * math.cos(math.radians(a)), 9.0 * math.sin(math.radians(a))) for a in (90.0, 210.0, 330.0)]


def _cyl(r: float, h: float, x: float = 0.0, y: float = 0.0, z: float = 0.0):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), r, h).Shape()


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _cap(path: Path) -> Path:
    """A bottle cap with three ports, as the GL45 three-port cap is drawn: a 9 mm plate, each port a 6 mm
    counterbore from the top face down to a step, then a 3 mm bore on through a stub 4 mm below."""
    body = _cyl(20.0, 9.0)
    for x, y in PORTS:
        body = BRepAlgoAPI_Fuse(body, _cyl(2.5, 4.0, x, y, -4.0)).Shape()
    for x, y in PORTS:
        body = BRepAlgoAPI_Cut(body, _cyl(3.0, 6.0, x, y, 3.0)).Shape()
        body = BRepAlgoAPI_Cut(body, _cyl(1.5, 7.0, x, y, -4.0)).Shape()
    return _write(body, path)


def _flange(path: Path) -> Path:
    """A flanged pipe: a 30 mm bore through a 100 mm tube with a flange at one end, six 6 mm bolt holes."""
    body = BRepAlgoAPI_Fuse(_cyl(20.0, 100.0), _cyl(40.0, 10.0)).Shape()
    body = BRepAlgoAPI_Cut(body, _cyl(15.0, 100.0)).Shape()
    for a in range(6):
        t = math.radians(60.0 * a)
        body = BRepAlgoAPI_Cut(body, _cyl(3.0, 10.0, 30.0 * math.cos(t), 30.0 * math.sin(t))).Shape()
    return _write(body, path)


def _ports(facts: dict) -> list[dict]:
    return [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]


def test_every_hole_of_a_flat_face_is_measured(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_cap(tmp_path / "cap.step"), tmp_path, None, _REF)
    top = [h for h in facts["flat_holes"] if abs(h["centroid_mm"][2] - 9.0) < 0.01]
    assert len(top) == 3 and all(h["diameter_mm"] == pytest.approx(6.0, rel=0.02) for h in top)


def test_a_cap_opens_at_its_top_holes_and_its_stubs(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_cap(tmp_path / "cap.step"), tmp_path, None, _REF)
    ports = _ports(facts)
    # each port's upper end is a flat face's hole of its own bore, measured exactly: the 6 mm hole in the
    # top face, or the 3 mm hole in the counterbore's floor where the map reads the bore tightest
    tops = [o for o in ports if o["centroid_mm"][2] > 0.5]
    stubs = [o for o in ports if abs(o["centroid_mm"][2] + 4.0) < 0.05]
    assert len(tops) == 3 and len(stubs) == 3, [(o["centroid_mm"], o["diameter_mm"]) for o in ports]
    for o in tops:
        z, dia = o["centroid_mm"][2], o["diameter_mm"]
        assert (abs(z - 9.0) < 0.05 and dia == pytest.approx(6.0, rel=0.02)) or \
               (abs(z - 3.0) < 0.05 and dia == pytest.approx(3.0, rel=0.02)), (z, dia)
        assert min(math.dist(o["centroid_mm"][:2], p) for p in PORTS) < 0.05


def test_a_flanges_bolt_holes_are_not_openings(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_flange(tmp_path / "flange.step"), tmp_path, None, _REF)
    ports = _ports(facts)
    assert len(ports) == 2
    assert all(o["diameter_mm"] == pytest.approx(30.0, rel=0.03) for o in ports)
    assert all(math.hypot(*o["centroid_mm"][:2]) < 1.0 for o in ports)


def test_a_screw_hole_through_a_tubes_wall_is_not_an_opening(tmp_path):
    """A hole into the side of a passage (a screw through a tube) opens the passage but is no end of it:
    the tube keeps its two ends, and the staging seals the screw hole into the wall, as before."""
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    from meshpipeline.application import geometry_check as gc

    tube = BRepAlgoAPI_Cut(_cyl(10.0, 100.0), _cyl(7.0, 100.0)).Shape()
    screw = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-12.0, 0.0, 50.0), gp_Dir(1, 0, 0)), 1.5, 24.0).Shape()
    facts, _ = gc._scout_exact(_write(BRepAlgoAPI_Cut(tube, screw).Shape(), tmp_path / "tube.step"),
                               tmp_path, None, _REF)
    ports = _ports(facts)
    assert sorted(round(o["centroid_mm"][2]) for o in ports) == [0, 100]
