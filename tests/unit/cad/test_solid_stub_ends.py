# Responsibility: Verify INS-15 and INS-16 on an open cup with solid rod stubs on its side (the siphoned box,
# as drawn): the inside map reads the cup's space with its one mouth, the rim, never the stubs' roots
# (INS-15); the check never proposes a stub's flat end as an opening (INS-15); and the B-rep staging, given
# the stubs' ends as the ports, refuses with the cup's mouth offered instead of seeding in the metal
# (INS-16). The same cup with hollow stubs (bores through the wall) keeps every bore's mouth.
# Boundaries: real OpenCASCADE solids written to STEP; the map, the geometry check's CAD reading and the
# B-rep internal staging; no mesher, no model.
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad import inside_space as ins
from meshpipeline.cad.open_ends import surface

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")
STUBS = (0.0, 90.0, 180.0, 270.0)
STUBS8 = tuple(45.0 * k for k in range(8))


def _cyl(r, h, p=(0.0, 0.0, 0.0), d=(0.0, 0.0, 1.0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r, h).Shape()


def _cup(hollow_stubs: bool = False, stubs=STUBS):
    """A cup open at the top: wall radius 70-75 mm, floor 5 mm, 150 mm tall, its rim at z = 0; 46 mm stubs
    on its side at z = -45, out to a radius of 100 mm - solid rods, or tubes bored into the cup."""
    cup = BRepAlgoAPI_Cut(_cyl(75.0, 150.0, (0, 0, -150)), _cyl(70.0, 146.0, (0, 0, -145))).Shape()
    for a in stubs:
        u = (math.cos(math.radians(a)), math.sin(math.radians(a)), 0.0)
        cup = BRepAlgoAPI_Fuse(cup, _cyl(23.0, 29.0, (71.0 * u[0], 71.0 * u[1], -45.0), u)).Shape()
    if hollow_stubs:
        for a in stubs:
            u = (math.cos(math.radians(a)), math.sin(math.radians(a)), 0.0)
            cup = BRepAlgoAPI_Cut(cup, _cyl(18.0, 40.0, (65.0 * u[0], 65.0 * u[1], -45.0), u)).Shape()
    return cup


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _map(shape, tmp: Path):
    """The inside map of the skin the geometry check draws of the part (metres)."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad.stl_io import read_stl_triangles

    _facts, skin = gc._scout_exact(_write(shape, tmp / "part.step"), tmp, None, _REF)
    tris = np.asarray(read_stl_triangles(skin), dtype=float).reshape(-1, 3, 3)
    return ins.read_inside(surface(tris), cells=2_000_000)


def test_the_cups_only_mouth_is_its_rim_never_a_solid_stubs_root(tmp_path):
    m = _map(_cup(), tmp_path)
    [p] = m.passages()
    assert len(p.mouths) == 1, [(np.round(m.mouth(i).centroid * 1000, 1), round(m.mouth(i).diameter * 1000, 1))
                                for i in p.mouths]
    rim = m.mouth(p.mouths[0])
    assert rim.centroid[2] == pytest.approx(0.0, abs=1e-3) and rim.normal[2] > 0.99
    assert rim.diameter == pytest.approx(0.140, rel=0.03)
    assert not m.has_inside()                     # one mouth: no way through


def test_hollow_stubs_keep_their_bores_mouths(tmp_path):
    m = _map(_cup(hollow_stubs=True), tmp_path)
    [p] = m.passages()
    assert len(p.mouths) == 1 + len(STUBS)
    assert m.has_inside()


@pytest.mark.parametrize("stubs", [STUBS, STUBS8])
def test_the_check_proposes_no_stub_end_and_the_stage_asks(tmp_path, stubs):
    """Four stubs read as a fluid body's ends, eight as a machined body's (the siphoned box): either way
    none of them is proposed, and the stage asks."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.geometry_confirmation import ONE_MOUTH_QUESTION, flow_question

    facts, _ = gc._scout_exact(_write(_cup(stubs=stubs), tmp_path / "cup.step"), tmp_path, None, _REF)
    ports = [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]
    ends = [o for o in facts["openings"] if math.hypot(*o["centroid_mm"][:2]) > 99.0]
    assert ends, facts["openings"]                                  # the stubs' ends were read ...
    assert not any(o in ports for o in ends), ports                 # ... and none is proposed
    assert facts["inside"]["mouths_per_passage"] == [1] and not facts["inside"]["has_inside"]
    # "the fluid flows through it" is asked about: one opening, no way through
    assert flow_question(facts["inside"], ports, facts.get("surface")) == ONE_MOUTH_QUESTION


def test_a_cup_with_bored_stubs_keeps_its_openings(tmp_path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_write(_cup(hollow_stubs=True), tmp_path / "cup.step"), tmp_path, None, _REF)
    ports = [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]
    assert len([o for o in ports if math.hypot(*o["centroid_mm"][:2]) > 99.0]) == len(STUBS), ports


def _stub_ports():
    out = []
    for k, a in enumerate(STUBS):
        u = (math.cos(math.radians(a)), math.sin(math.radians(a)))
        name = "inlet" if k == 0 else f"outlet_{k}"
        out.append({"name": name, "type": "inlet" if k == 0 else "outlet", "diameter_mm": 46.0,
                    "near_mm": [100.0 * u[0], 100.0 * u[1], -45.0]})
    return out + [{"name": "wall", "type": "wall"}]


@pytest.mark.parametrize("fluid_solid", [False, None])
def test_staging_refuses_stub_ends_and_offers_the_cups_mouth(tmp_path, fluid_solid):
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.engines.port_binding import BindError, declaration_targets

    path = _write(_cup(), tmp_path / "cup.step")
    with pytest.raises(BindError) as got:
        tessellate_internal(path, tmp_path / "staged", prepared=_prepared(path), fluid_solid=fluid_solid,
                            declared_ports=declaration_targets(_stub_ports()))
    said = str(got.value)
    assert "no space inside the part reaches them" in said
    assert "opens at 1 place" in said and "facing +z" in said, said


def test_staging_of_the_bored_stubs_is_not_refused(tmp_path):
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.engines.port_binding import declaration_targets

    path = _write(_cup(hollow_stubs=True), tmp_path / "cup.step")
    ports = [{**p, "diameter_mm": 36.0} if p.get("diameter_mm") else p for p in _stub_ports()]
    try:
        tessellate_internal(path, tmp_path / "staged", prepared=_prepared(path), fluid_solid=False,
                            declared_ports=declaration_targets(ports))
    except Exception as exc:  # noqa: BLE001 - any other staging answer is not this rule's
        assert "no space inside the part reaches them" not in str(exc)
