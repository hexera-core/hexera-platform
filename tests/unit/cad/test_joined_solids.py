# Responsibility: Verify INS-11 - closed solids that touch face to face are read as the one body they
# make (a fluid domain modelled in regions: inlet duct, rotor passage, outlet duct), so its own flat
# ends are its openings and the check never calls the regions "loose pieces" for CAD repair; and that
# solids which overlap, or do not touch, are never joined.
# Boundaries: real OpenCASCADE solids written to STEP; the geometry check's own CAD reading; no model.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.scout import joined_solids, read_cad

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(radius: float, length: float, x0: float):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x0, 0, 0), gp_Dir(1, 0, 0)), radius, length).Shape()


def _write(parts, path: Path) -> Path:
    comp, b = TopoDS_Compound(), BRep_Builder()
    b.MakeCompound(comp)
    for p in parts:
        b.Add(comp, p)
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(comp, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _regions(path: Path) -> Path:
    """A turbomachine's fluid domain modelled as three regions end to end, as CFD downloads have it:
    an inlet duct, the rotor passage round its hub (an annulus), an outlet duct - each a closed solid,
    meeting the next at a flat interface."""
    rotor = BRepAlgoAPI_Cut(_cyl(100.0, 75.0, -30.0), _cyl(39.0, 75.0, -30.0)).Shape()
    return _write([_cyl(100.0, 40.0, -70.0), rotor, _cyl(100.0, 40.0, 45.0)], path)


def test_touching_solids_join_into_one_body(tmp_path):
    joined, how = joined_solids(read_cad(_regions(tmp_path / "regions.step")))
    assert joined is not None and how["joined"] is True
    assert how["bodies"] == 1 and how["volume_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert how["shared_skin"] > 0.0


def test_overlapping_solids_are_never_joined(tmp_path):
    path = _write([_cyl(40.0, 150.0, 0.0), _cyl(40.0, 150.0, 100.0)], tmp_path / "overlap.step")
    joined, how = joined_solids(read_cad(path))
    assert joined is None and "overlap" in how["why"]


def test_solids_apart_are_never_joined(tmp_path):
    path = _write([_cyl(40.0, 100.0, 0.0), _cyl(40.0, 100.0, 150.0)], tmp_path / "apart.step")
    joined, how = joined_solids(read_cad(path))
    assert joined is None and "2 separate bodies" in how["why"]


def test_a_fluid_domain_in_regions_reads_as_the_fluid_with_its_two_ends(tmp_path):
    """Before INS-11 each region's interface face hid the far end, the check proposed no opening, and
    the three closed regions were called '3 loose pieces' needing CAD repair."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.geometry_confirmation import flow_question

    facts, _skin = gc._scout_exact(_regions(tmp_path / "regions.step"), tmp_path, None, _REF)
    ports = [o for o in facts["openings"] if o["role"] in ("inlet", "outlet")]
    assert (facts["flow"], facts["input_kind"]) == ("internal", "fluid-domain")
    assert len(ports) == 2
    assert sorted(round(o["centroid_mm"][0]) for o in ports) == [-70, 85]
    assert facts["solids"] == 3 and facts["joined_solids"]["kept"] is True
    assert facts["surface"]["pieces"] == 1
    # the point inside the flow is in the fluid, never in the hub the regions close off
    x, y, z = facts["seed_point_mm"]
    assert not (-30.0 < x < 45.0 and (y * y + z * z) ** 0.5 < 39.0)
    assert -70.0 < x < 85.0 and (y * y + z * z) ** 0.5 < 100.0
    assert facts["flow_kind"]["flow"] == "internal"
    assert flow_question(facts["inside"], ports, facts["surface"]) == ""


def test_the_regions_read_one_by_one_hide_their_ends(tmp_path):
    """The parts read as they are (before INS-11): no flat end reads as a mouth."""
    from tests.unit.cad.test_scout import _prepared

    from meshpipeline.cad.scout import scout_cad

    path = _regions(tmp_path / "regions.step")
    r = scout_cad(path, prepared=_prepared(path))
    assert len([o for o in r.openings if o.role in ("inlet", "outlet")]) < 2


def test_a_single_solid_reads_as_before(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _skin = gc._scout_exact(_write([_cyl(40.0, 300.0, 0.0)], tmp_path / "one.step"), tmp_path, None, _REF)
    assert "joined_solids" not in facts         # one solid: nothing to join, read as before
