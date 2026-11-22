# Responsibility: Verify INS-23 on built parts: a free perforated plate is read as a plate whose openings are its
# two sides, a duct closed by a grille as a grille with one lid over it beside the duct's own end, and the stage
# asks the user in the agreed words; a flange's bolt holes, a block with deep bores and a perforated baffle inside
# a pipe are not read as perforations. The B-rep staging, given the sleeve's ends as the ports, stages the plate
# in a sleeve whose ends are the openings; given the holes themselves, the part as drawn.
# Boundaries: real OpenCASCADE solids written to STEP; cad/perforated, the geometry check's CAD reading and the
# B-rep internal staging; no mesher, no model.
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_scout import _prepared

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, p=(0.0, 0.0, 0.0), d=(0.0, 0.0, 1.0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r, h).Shape()


def _box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def _drill(shape, centres, r, z0, h):
    for x, y in centres:
        shape = BRepAlgoAPI_Cut(shape, _cyl(r, h, (x, y, z0))).Shape()
    return shape


def _grid(k: int, pitch: float):
    off = 0.5 * (k - 1) * pitch
    return [(i * pitch - off, j * pitch - off) for i in range(k) for j in range(k)]


def _plate():
    """A 60 x 60 x 5 mm plate with 36 holes of 4 mm at an 8 mm pitch."""
    return _drill(_box(-30, -30, 0, 30, 30, 5), _grid(6, 8.0), 2.0, -1.0, 7.0)


def _grille_duct():
    """A square duct 40 x 40 mm, 60 mm tall, walls 2 mm, open at the top; its bottom is a 3 mm plate with 16 holes
    of 4 mm at a 7 mm pitch."""
    duct = BRepAlgoAPI_Cut(_box(-20, -20, 0, 20, 20, 60), _box(-18, -18, 3, 18, 18, 61)).Shape()
    return _drill(duct, _grid(4, 7.0), 2.0, -1.0, 5.0)


def _flange():
    """A flange: a 100 mm disc 10 mm thick, a 50 mm bore and 8 bolt holes of 8 mm on an 80 mm circle."""
    f = BRepAlgoAPI_Cut(_cyl(50.0, 10.0), _cyl(25.0, 12.0, (0, 0, -1))).Shape()
    return _drill(f, [(40 * math.cos(math.radians(45 * k)), 40 * math.sin(math.radians(45 * k))) for k in range(8)],
                  4.0, -1.0, 12.0)


def _deep_bores():
    """A 60 x 60 x 40 mm block with 9 bores of 6 mm straight through it: a manifold, not a perforation."""
    return _drill(_box(-30, -30, 0, 30, 30, 40), _grid(3, 15.0), 3.0, -1.0, 42.0)


def _baffle_pipe():
    """A pipe (bore 40 mm, wall 3 mm, 120 mm long) with a perforated baffle in its middle: walled in both sides."""
    pipe = BRepAlgoAPI_Cut(_cyl(23.0, 120.0), _cyl(20.0, 122.0, (0, 0, -1))).Shape()
    baffle = _drill(_cyl(20.5, 4.0, (0, 0, 58)), [p for p in _grid(5, 7.0) if math.hypot(*p) < 17], 2.0, 57.0, 6.0)
    return BRepAlgoAPI_Fuse(pipe, baffle).Shape()


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def test_a_free_plate_is_read_as_a_plate():
    from meshpipeline.cad.perforated import read_perforation

    p = read_perforation(_plate())
    assert p is not None and p.kind == "plate"
    assert p.holes == 36
    assert p.hole_width == pytest.approx(4.0, rel=0.01) and p.thickness == pytest.approx(5.0, rel=0.02)
    assert p.area == pytest.approx(3600.0, rel=0.01)          # the plate's own outline
    front, back = p.ends()
    assert abs(front["centroid"][2] - back["centroid"][2]) == pytest.approx(5.0 + 2 * p.sleeve, rel=0.01)
    assert p.sleeve == pytest.approx(24.0, rel=0.01)          # three 8 mm pitches


def test_a_grille_closing_a_duct_is_read_as_a_grille_on_its_open_side():
    from meshpipeline.cad.perforated import read_perforation

    p = read_perforation(_grille_duct())
    assert p is not None and p.kind == "grille" and p.holes == 16
    assert p.normal[2] == pytest.approx(-1.0)                 # the lid faces out of the duct's bottom
    [lid] = p.ends()
    assert lid["centroid"][2] < 0.0
    assert 12.5 < p.radius < 18.0                             # round the holes, inside the duct's 40 mm face


@pytest.mark.parametrize("build", [_flange, _deep_bores, _baffle_pipe])
def test_bolt_circles_deep_bores_and_baffles_are_not_perforations(build):
    from meshpipeline.cad.perforated import read_perforation

    assert read_perforation(build()) is None


def _check(shape, tmp_path: Path):
    from meshpipeline.application import geometry_check as gc

    facts, _ = gc._scout_exact(_write(shape, tmp_path / "part.step"), tmp_path, None, _REF)
    return facts, gc.jsonable(gc._proposal(facts, None))


def test_the_check_proposes_the_plates_two_sides_and_asks(tmp_path):
    from meshpipeline.application.geometry_check import PERFORATED_ANSWERS

    facts, prop = _check(_plate(), tmp_path)
    ports = [o for o in prop["openings"] if o["role"] in ("inlet", "outlet")]
    assert prop["flow"] == "internal" and len(ports) == 2, prop["openings"]
    assert {round(o["centroid_mm"][2]) for o in ports} == {29, -24}
    assert all(o["area_mm2"] == pytest.approx(3600.0, rel=0.01) for o in ports)
    q = prop["perforated"]
    assert q["question"] == ("This looks like a perforated plate: 36 holes of 4 mm through a 5 mm plate. "
                             "Flow goes through the holes?")
    assert q["answers"] == list(PERFORATED_ANSWERS) == ["Yes", "No, each hole is its own passage",
                                                        "No, the flow goes around it"]


def test_the_check_proposes_one_lid_over_a_grille_beside_the_ducts_end(tmp_path):
    facts, prop = _check(_grille_duct(), tmp_path)
    ports = [o for o in prop["openings"] if o["role"] in ("inlet", "outlet")]
    assert prop["flow"] == "internal"
    assert len(ports) == 2, ports                               # the duct's open top, and the grille's one lid
    lid = next(o for o in ports if o.get("from_perforation"))
    assert lid["centroid_mm"][2] < 0.0 and lid["normal"][2] == pytest.approx(-1.0)
    top = next(o for o in ports if not o.get("from_perforation"))
    assert top["centroid_mm"][2] == pytest.approx(60.0, abs=0.5)
    assert "grille: 16 holes of 4 mm through a 3 mm plate" in prop["perforated"]["question"]


def _ports_of(prop) -> list[dict]:
    from meshpipeline.application.geometry_confirmation import body_of, patches_from

    return patches_from(body_of({"flow": "internal", "input_kind": prop["input_kind"], "openings": prop["openings"]}))


@pytest.mark.parametrize("fluid_solid", [False, None])
def test_staging_builds_the_sleeve_and_binds_its_ends(tmp_path, fluid_solid):
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.engines.port_binding import bind_intake, declaration_targets

    _facts, prop = _check(_plate(), tmp_path)
    patches = _ports_of(prop)
    path = tmp_path / "part.step"
    staged = tessellate_internal(path, tmp_path / "staged", prepared=_prepared(path), fluid_solid=fluid_solid,
                                 declared_ports=declaration_targets(patches))
    # the sleeve's two ends, each opening the plate's own outline, out past each side
    holes = sorted((round(v["opening"]["area"] * 1e6), round(v["opening"]["centroid"][2] * 1000))
                   for v in staged["openings"].values())
    assert holes == [(3600, -24), (3600, 29)], staged["openings"]
    bound, _wall, _note = bind_intake(staged, patches)
    assert set(bound["openings"]) == {"inlet", "outlet"}, bound["openings"]


def _stl(shape, path: Path) -> Path:
    """The part as a triangle file (millimetres), as an STL upload would be."""
    from meshpipeline.cad.occ_box import mesh_to_size
    from meshpipeline.cad.perforated import _oriented_triangles
    from meshpipeline.cad.stl_io import write_stl_binary

    mesh_to_size(shape, 1.0 / 2500.0, 0.2)
    write_stl_binary(path, _oriented_triangles(shape))
    return path


def test_a_triangle_plate_is_proposed_and_staged_in_its_sleeve(tmp_path):
    """An STL plate: its skin sewn into a solid reads as the CAD plate does; the surface staging every engine runs
    for a triangle part builds the same sleeve and binds its two ends."""
    import shutil

    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad.internal_surface import stage_internal_surface
    from meshpipeline.engines.port_binding import bind_intake

    ref = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".stl", original_filename="part.stl")
    facts, skin = gc._scout_triangles(_stl(_plate(), tmp_path / "part.stl"), tmp_path, None, ref)
    prop = gc.jsonable(gc._proposal(facts, None))
    assert prop["perforated"]["question"].startswith("This looks like a perforated plate: 36 holes of 4 mm")
    patches = _ports_of(prop)
    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copy(skin, ws / "input.stl")
    staged = stage_internal_surface(ws / "input.stl", ws / "_internal_stls", intake_patches=patches,
                                    input_kind=prop["input_kind"])
    ends = sorted((round(v["area"] * 1e6), round(v["centroid"][2] * 1000)) for v in staged["openings"].values())
    assert ends == [(3600, -24), (3600, 29)], staged["openings"]
    bound, _wall, _note = bind_intake(staged, patches)
    assert set(bound["openings"]) == {"inlet", "outlet"}


def test_staging_the_holes_themselves_keeps_the_part_as_drawn(tmp_path):
    """'No, each hole is its own passage': ports at the holes, not the sleeve's ends - no sleeve is built."""
    from meshpipeline.cad.perforated import read_perforation, sleeve_ends_declared

    p = read_perforation(_plate())                              # read in the shape's own unit (mm here)
    holes = [{"name": f"h{k}", "near_m": (x, y, 5.0)} for k, (x, y) in enumerate(_grid(6, 8.0)[:3])]
    assert not sleeve_ends_declared(p, holes)
    assert not sleeve_ends_declared(p, [])
    assert not sleeve_ends_declared(p, [{"name": "front", "near_m": p.ends()[0]["centroid"]}])   # one end only
    assert sleeve_ends_declared(p, [{"name": e["side"], "near_m": e["centroid"]} for e in p.ends()])
