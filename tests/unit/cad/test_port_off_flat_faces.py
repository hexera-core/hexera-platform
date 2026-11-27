# Responsibility: Verify INS-19 - a declared port whose mouth is on no flat face (a hole through a domed end) is
# never staged as some other flat face: the B-rep staging raises FlatFacesMissPort (a staging fault, so the
# drivers stage the part's surface instead, which binds the declaration) where it used to hand the binder a flat
# face 30 mm off, which refused the user's declaration. Ports on flat faces stage as before, and the check never
# refuses a staging the binder would bind.
# Boundaries: real OpenCASCADE solids written to STEP; the B-rep internal staging (cad_tessellate), the surface
# staging the drivers fall back to (internal_surface.stage_internal) and the shared port binding; no mesher.
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_flat_holes import _write
from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad.cad_tessellate import FlatFacesMissPort, held_to_declaration, tessellate_internal
from meshpipeline.engines.port_binding import BindError, DeclaredPatch, bind_intake, bind_ports, declaration_targets


def _cyl(r: float, z0: float, h: float):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1)), r, h).Shape()


def _tube(domed_top: bool):
    """A 20 mm tube (bore 16 mm), 60 mm long on z, with a small flat milled on its side. Its top either a plain
    flat ring, or a 2 mm spherical dome with a 12 mm hole drilled through its crown (the mouth: a loop on a
    curved face, no flat face anywhere near it)."""
    tube = BRepAlgoAPI_Cut(_cyl(10.0, 0.0, 60.0), _cyl(8.0, -1.0, 62.0)).Shape()
    if domed_top:
        shell = BRepAlgoAPI_Cut(BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 60), 10.0).Shape(),
                                BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 60), 8.0).Shape()).Shape()
        dome = BRepAlgoAPI_Cut(shell, BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, 40), 40.0, 40.0, 20.0).Shape()).Shape()
        tube = BRepAlgoAPI_Cut(BRepAlgoAPI_Fuse(tube, dome).Shape(), _cyl(6.0, 55.0, 20.0)).Shape()
    return BRepAlgoAPI_Cut(tube, BRepPrimAPI_MakeBox(gp_Pnt(9.6, -3, 20), 2.0, 6.0, 20.0).Shape()).Shape()


FLAT = [{"name": "inlet", "type": "inlet", "diameter_mm": 16.0, "near_mm": [0.0, 0.0, 0.0]},
        {"name": "outlet", "type": "outlet", "diameter_mm": 16.0, "near_mm": [0.0, 0.0, 60.0]},
        {"name": "wall", "type": "wall"}]
DOMED = [FLAT[0], {"name": "outlet", "type": "outlet", "diameter_mm": 12.0, "near_mm": [0.0, 0.0, 68.0]}, FLAT[2]]


def _stage(path: Path, tmp: Path, ports):
    return tessellate_internal(path, tmp / "staged", prepared=_prepared(path), fluid_solid=False,
                               declared_ports=declaration_targets(ports))


def test_a_port_on_a_domed_end_is_not_staged_as_another_flat_face(tmp_path):
    with pytest.raises(FlatFacesMissPort) as got:
        _stage(_write(_tube(domed_top=True), tmp_path / "dome.step"), tmp_path, DOMED)
    assert "outlet" in str(got.value)


def test_the_surface_way_then_binds_the_declaration_where_the_user_put_it(tmp_path):
    # the drivers' way on: the part's own surface, staged with the confirmed openings, and bound
    from meshpipeline.cad.cad_tessellate import tessellate_to_stl
    from meshpipeline.cad.internal_surface import stage_internal
    step = _write(_tube(domed_top=True), tmp_path / "dome.step")
    tessellate_to_stl(step, tmp_path / "input.stl", prepared=_prepared(step))
    t = stage_internal(step, tmp_path, prepared=_prepared(step), intake_patches=DOMED, input_kind="body")
    assert "FlatFacesMissPort" in t["facts"]["cad_path_failed"]
    out, _wall, _note = bind_intake(t, DOMED, bore=True)
    got = {p["name"]: p for p in out["binding"]["ports"]}
    assert abs(got["outlet"]["centroid"][2] * 1000.0 - 68.0) < 3.0, got
    assert 0.5 < got["outlet"]["area_m2"] * 1e6 / 113.1 < 2.0, got


def test_ports_on_flat_ends_stage_as_before(tmp_path):
    t = _stage(_write(_tube(domed_top=False), tmp_path / "flat.step"), tmp_path, FLAT)
    zs = sorted(round(rec["centroid"][2] * 1000.0) for rec in t["openings"].values())
    assert zs == [0, 60], t["openings"]
    bind_intake(t, FLAT, bore=True)


def _rec(area_mm2, c_mm):
    return {"area": area_mm2 * 1e-6, "centroid": [v / 1000.0 for v in c_mm]}


@pytest.mark.parametrize("ports", [
    # bound by location and size
    [("inlet", 16.0, (0, 0, 0)), ("outlet", 16.0, (0, 0, 60))],
    # a hint a little off, a size 1.8x off: still the binder's
    [("inlet", 12.0, (0, 1, 2)), ("outlet", 20.0, (1, 0, 57))],
    # both ports nearest the same opening: the second takes the next one, as the binder does
    [("a_in", 16.0, (0, 0, 20)), ("b_out", 16.0, (0, 0, 25))],
    # two openings equally near: not decisive, left to the binder
    [("inlet", 16.0, (0, 0, 30)), ("outlet", 16.0, (0, 0, 60))],
    # no location: the binder's size rule, never this check's
    [("inlet", 40.0, None), ("outlet", 16.0, (0, 0, 60))],
])
def test_the_check_never_refuses_what_the_binder_would_bind(ports):
    openings = {"inlet": _rec(201.1, (0, 0, 0)), "outlet": _rec(201.1, (0, 0, 60))}
    intake = [{"name": n, "type": "outlet" if "out" in n else "inlet", "diameter_mm": d,
               **({"near_mm": list(c)} if c else {})} for n, d, c in ports] + [{"name": "wall", "type": "wall"}]
    t = {"openings": openings, "bbox_min": [-0.01, -0.01, 0.0], "bbox_max": [0.01, 0.01, 0.06]}
    try:
        bind_ports([DeclaredPatch.from_intake(p) for p in intake], t)
        binds = True
    except BindError:
        binds = False
    try:
        held_to_declaration(openings, declaration_targets(intake), t["bbox_min"], t["bbox_max"])
        held = True
    except FlatFacesMissPort:
        held = False
    assert held or not binds, "a staging the binder binds must never be refused"


def test_a_port_far_from_every_flat_face_or_far_off_its_size_is_refused():
    openings = {"inlet": _rec(201.1, (0, 0, 0)), "outlet": _rec(26.0, (9.6, 0, 30))}
    box = ([-0.01, -0.01, 0.0], [0.01, 0.01, 0.07])
    far = [{"name": "inlet", "area_m2": 201e-6, "near_m": (0, 0, 0)},
           {"name": "outlet", "area_m2": 113e-6, "near_m": (0, 0, 0.068)}]
    with pytest.raises(FlatFacesMissPort, match="outlet"):
        held_to_declaration(openings, far, *box)
    wrong_size = [far[0], {"name": "outlet", "area_m2": 2978e-6, "near_m": (0.0096, 0, 0.03)}]
    with pytest.raises(FlatFacesMissPort, match="2978"):
        held_to_declaration(openings, wrong_size, *box)
