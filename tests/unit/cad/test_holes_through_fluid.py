# Responsibility: Verify INS-18 - a part read as the fluid itself (a fluid domain) whose map finds space through it
# no bigger than its own volume keeps only its own flat ends as openings: the bolt holes through its flange are
# where the fluid is not, never proposed as outlets (INS-13 proposed them, and the declaration then asked for
# ports no flat end of the fluid could meet). A hollow body's flat-face port holes are still proposed (INS-13).
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
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_flat_holes import PORTS, _cap, _write

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, x=0.0, y=0.0, z=0.0):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), r, h).Shape()


def _flanged_fluid(path: Path) -> Path:
    """The fluid of a 40 mm pipe, 200 mm long, drawn with the 100 mm flange at its foot (12 mm thick) and the
    flange's four 8 mm bolt holes through it."""
    body = BRepAlgoAPI_Fuse(_cyl(20.0, 200.0), _cyl(50.0, 12.0)).Shape()
    for a in (45.0, 135.0, 225.0, 315.0):
        t = math.radians(a)
        body = BRepAlgoAPI_Cut(body, _cyl(4.0, 12.0, 37.0 * math.cos(t), 37.0 * math.sin(t))).Shape()
    return _write(body, path)


def _ports(facts: dict) -> list[dict]:
    return [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]


def test_bolt_holes_through_a_fluid_are_not_openings(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_flanged_fluid(tmp_path / "fluid.step"), tmp_path, None, _REF)
    assert facts["input_kind"] == "fluid-domain", (facts["input_kind"], facts.get("notes"))
    bolts = [o for o in _ports(facts) if 30.0 < math.hypot(*o["centroid_mm"][:2]) < 44.0]
    assert not bolts, [(o["centroid_mm"], o["diameter_mm"]) for o in _ports(facts)]
    assert not any(o.get("from_flat_hole") for o in facts["openings"])


def _pipe_with_flange_facts(kind: str, open_mm3: float) -> dict:
    """The check's facts as it reads a pipe's fluid drawn with its flange (batch 4, 00090315): the flange face
    proposed as one 280 mm disc, the far end a 178 mm disc, and the flange's four 18 mm bolt holes - each a
    passage of the map with two mouths, measured exactly as flat-face holes on the flange's back face."""
    flats, holes = [], []
    for k, (x, z) in enumerate(((-84.8, -84.8), (-84.8, 84.8), (84.8, 84.8), (84.8, -84.8))):
        c = [x, -345.0, z]
        flats.append({"face": 10 + k, "centroid_mm": c, "centroid_m": [v / 1000.0 for v in c], "normal": [0.0, 1.0, 0.0],
                      "diameter_mm": 17.97, "area_mm2": 253.6, "width_mm": 17.97, "height_mm": 17.97})
        holes.append({"passage": True, "flat_hole": k, "centroid_mm": c, "centroid_m": [v / 1000.0 for v in c],
                      "normal": [0.0, 1.0, 0.0], "diameter_mm": 17.97})
    ends = [{"id": 1, "name": "inlet", "role": "inlet", "kind": "disc", "diameter_mm": 280.7, "area_mm2": 61884.0,
             "centroid_mm": [0.0, -372.0, 0.0], "normal": [0.0, -1.0, 0.0]},
            {"id": 2, "name": "outlet", "role": "outlet", "kind": "disc", "diameter_mm": 178.0, "area_mm2": 24885.0,
             "centroid_mm": [0.0, 0.0, 0.0], "normal": [0.0, 1.0, 0.0]}]
    return {"flow": "internal", "input_kind": kind, "openings": ends, "flat_holes": flats, "holes": holes,
            "inside": {"has_inside": True, "passages": 4, "mouths_per_passage": [2, 2, 2, 2],
                       "enclosed_mm3": 9.0e6, "open_mm3": open_mm3}, "notes": []}


def test_a_fluids_bolt_holes_are_not_added_beside_its_ends():
    from meshpipeline.application.geometry_check import flat_hole_mouths

    facts = _pipe_with_flange_facts("fluid-domain", open_mm3=7.0e4)
    flat_hole_mouths(facts)
    assert [o["name"] for o in facts["openings"]] == ["inlet", "outlet"]
    # the same holes on a part read as a body - a hollow vessel's port holes - are added, as before (INS-13)
    body = _pipe_with_flange_facts("body-surface", open_mm3=7.0e4)
    flat_hole_mouths(body)
    assert len([o for o in body["openings"] if o.get("from_flat_hole")]) == 4
    # ...and so they are on a part read as fluid whose open space is more than its own volume (a vessel)
    vessel = _pipe_with_flange_facts("fluid-domain", open_mm3=2.0e7)
    flat_hole_mouths(vessel)
    assert len([o for o in vessel["openings"] if o.get("from_flat_hole")]) == 4


def test_a_caps_port_holes_are_still_proposed(tmp_path):
    """The GL45 cap (a body): its three top holes are still added beside its stubs (INS-13, unchanged)."""
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_cap(tmp_path / "cap.step"), tmp_path, None, _REF)
    tops = [o for o in _ports(facts) if o["centroid_mm"][2] > 0.5]
    assert len(tops) == 3
    assert all(min(math.dist(o["centroid_mm"][:2], p) for p in PORTS) < 0.05 for o in tops)
