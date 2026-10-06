# THE PORT-AREA GATE, shared by every engine: a delivered inlet/outlet far from its opening's size is
# not that opening. On the HOME-TURF audit (2026-10-06) six gmsh fluid-domain meshes passed every
# gate with ports 2.4-5.3x their declared openings. The opening is the one MEASURED on the geometry
# when the staging measured one, the typed size only otherwise: pvc_mixing_tee's spec typed "about
# 20 mm" for a 32 mm bore, and judged by the typed size every engine's correct mesh was refused.
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.region_check import (
    PORT_AREA_BAND,
    declared_port_area_m2,
    delivered_areas,
    expected_port_areas,
    flow_area_m2,
    gate_port_areas,
    measured_openings,
    measured_port_areas,
    port_area_misses,
    record_port_openings,
    recorded_port_openings,
    size_notes,
)
from meshpipeline.engines.registry import get_spec


@pytest.mark.parametrize("patch,want_mm2", [
    ({"diameter_mm": 100.0}, math.pi * 2500),
    ({"diameter_mm": 151.19, "inner_diameter_mm": 124.75}, math.pi / 4 * (151.19 ** 2 - 124.75 ** 2)),
    ({"width_mm": 40.0, "height_mm": 25.0}, 1000.0),
    ({"area_mm2": 240.0}, 240.0),
    ({"near_mm": [0, 0, 0]}, None),
])
def test_the_declared_area(patch, want_mm2):
    got = declared_port_area_m2(patch)
    assert (got is None) if want_mm2 is None else got == pytest.approx(want_mm2 * 1e-6)


def test_misses_are_outside_the_band_only():
    exp = {"inlet": 1.0, "outlet": 1.0, "free": 1.0}
    got = {"inlet": 5.3, "outlet": 1.2, "other": 9.0}
    m = port_area_misses(got, exp)
    assert [x["name"] for x in m] == ["inlet"] and m[0]["ratio"] == 5.3
    assert port_area_misses({"inlet": PORT_AREA_BAND[0]}, {"inlet": 1.0}) == []
    assert port_area_misses({"inlet": PORT_AREA_BAND[1] * 1.01}, {"inlet": 1.0})


def _ctx(tmp_path, areas, patches, openings=None):
    quality = {"patch_areas_m2": areas, **({"port_openings_m2": openings} if openings else {})}
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({"quality": quality}))
    return GateCtx(workspace=tmp_path, engine="gmsh", intake_patches=patches)


PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 68.59},
           {"name": "outlet", "type": "outlet", "diameter_mm": 50.0},
           {"name": "wall", "type": "wall"}]


def test_the_gate_refuses_a_port_that_swept_in_the_wall(tmp_path):
    a_in = math.pi / 4 * 0.06859 ** 2
    ok, why = gate_port_areas(_ctx(tmp_path, {"inlet": 5.325 * a_in, "outlet": math.pi / 4 * 0.05 ** 2,
                                              "wall": 1.0}, PATCHES))
    assert not ok and "inlet" in why and "5.325 times" in why


def test_the_gate_passes_sound_ports_and_judges_nothing_unmeasured(tmp_path):
    good = {"inlet": math.pi / 4 * 0.06859 ** 2 * 0.998, "outlet": math.pi / 4 * 0.05 ** 2 * 1.01}
    assert gate_port_areas(_ctx(tmp_path, good, PATCHES)) == (True, "")
    assert gate_port_areas(_ctx(tmp_path, {}, PATCHES)) == (True, "")
    located_only = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0]}]
    assert gate_port_areas(_ctx(tmp_path, {"inlet": 99.0}, located_only)) == (True, "")


def test_areas_are_read_from_the_engines_quality_record():
    assert delivered_areas({"quality": {"patch_areas_m2": {"inlet": 0.1}}}) == {"inlet": 0.1}
    assert delivered_areas({"patch_areas_m2": {"inlet": 0.2}}) == {}
    assert delivered_areas({"quality": {"patch_areas_m2": {"inlet": "x"}}}) == {}


@pytest.mark.parametrize("engine", ["snappy", "cfmesh", "gmsh"])
def test_every_flow_engine_declares_the_shared_gate(engine):
    gates = [g for g in get_spec(engine).gates if g.key == "port_areas"]
    assert len(gates) == 1 and gates[0].check is gate_port_areas


def test_gmsh_holds_a_builders_port_group_to_its_opening():
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("t")
        gmsh.model.occ.addCylinder(0, 0, 0, 0.2, 0, 0, 0.02)
        gmsh.model.occ.synchronize()
        faces = [t for _, t in gmsh.model.getEntities(2)]
        area = {t: gmsh.model.occ.getMass(2, t) for t in faces}
        side = max(faces, key=lambda t: area[t])
        discs = sorted((t for t in faces if t != side),
                       key=lambda t: gmsh.model.occ.getCenterOfMass(2, t)[0])
        ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40.0},
                 {"name": "outlet", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 40.0}]
        swept = [{"name": "inlet", "role": "inlet", "surface_tags": [discs[0], side]},
                 {"name": "outlet", "role": "outlet", "surface_tags": [discs[1]]},
                 {"name": "wall", "role": "wall", "surface_tags": []}]
        out = {g["name"]: g["surface_tags"] for g in
               driver._checked_port_groups(gmsh, swept, faces, ports, cad=True)}
        assert out == {"inlet": [discs[0]], "outlet": [discs[1]], "wall": [side]}
        sound = [{"name": "inlet", "role": "inlet", "surface_tags": [discs[0]]},
                 {"name": "outlet", "role": "outlet", "surface_tags": [discs[1]]},
                 {"name": "wall", "role": "wall", "surface_tags": [side]}]
        assert driver._checked_port_groups(gmsh, sound, faces, ports, cad=True) == sound
    finally:
        gmsh.finalize()


def test_gmsh_walls_the_faces_a_swept_port_gave_back_when_the_builder_named_no_wall():
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("t")
        gmsh.model.occ.addCylinder(0, 0, 0, 0.2, 0, 0, 0.02)
        gmsh.model.occ.synchronize()
        faces = [t for _, t in gmsh.model.getEntities(2)]
        area = {t: gmsh.model.occ.getMass(2, t) for t in faces}
        side = max(faces, key=lambda t: area[t])
        discs = sorted((t for t in faces if t != side),
                       key=lambda t: gmsh.model.occ.getCenterOfMass(2, t)[0])
        ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40.0},
                 {"name": "outlet", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 40.0},
                 {"name": "wall", "type": "wall"}]
        swept = [{"name": "inlet", "role": "inlet", "surface_tags": [discs[0], side]},
                 {"name": "outlet", "role": "outlet", "surface_tags": [discs[1]]}]
        out = {g["name"]: g["surface_tags"] for g in
               driver._checked_port_groups(gmsh, swept, faces, ports, cad=True)}
        assert out == {"inlet": [discs[0]], "outlet": [discs[1]], "wall": [side]}
    finally:
        gmsh.finalize()


def test_the_prelaunch_check_lets_the_engine_build_the_declared_wall(tmp_path):
    from meshpipeline.engines.case_contract import _gmsh_boundary
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": [
        {"name": "inlet", "role": "inlet", "surface_tags": [1, 2]},
        {"name": "outlet", "role": "outlet", "surface_tags": [3]}]}))
    (tmp_path / "port_declaration.json").write_text(json.dumps([
        {"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
        {"name": "wall", "type": "wall"}]))
    (tmp_path / "flow_topology").write_text("internal")
    assert _gmsh_boundary(tmp_path).patches == {"inlet": "inlet", "outlet": "outlet", "wall": "wall"}


# pvc_mixing_tee as staged (dry configure, 2026-10-06): each port face is the pipe end's metal ring
# (330 mm2) round the 32 mm bore its inner wire encloses (803 mm2); the spec typed "about 20 mm"
PVC_RING = {"area": 0.00032987, "centroid": [-0.038, 0.0, 0.0],
            "opening": {"area": 0.00080292, "centroid": [-0.038, 0.0, 0.0], "wh": [0.03198, 0.03199]}}
PVC = [{"name": "inlet_1", "type": "inlet", "near_mm": [-38, 0, 0], "diameter_mm": 20.0},
       {"name": "inlet_2", "type": "inlet", "near_mm": [0, 0, 48], "diameter_mm": 20.0},
       {"name": "outlet", "type": "outlet", "near_mm": [44, 0, 0], "diameter_mm": 20.0},
       {"name": "wall", "type": "wall"}]
PVC_BORES = {"inlet_1": 0.00080292, "inlet_2": 0.00080292, "outlet": 0.00080292}


def test_the_flow_crosses_a_bodys_bore_and_a_fluid_solids_own_face():
    # a part declared a body: the fluid is the bore its ring encloses
    assert flow_area_m2(PVC_RING, bore=True) == pytest.approx(0.00080292)
    # the solid is the fluid: the ring face itself is the opening (an annular passage)
    assert flow_area_m2(PVC_RING, bore=False) == pytest.approx(0.00032987)
    # annular_001: the tube's ring encloses 17,923 mm2, the rod's end disc fills 12,223 of it
    annulus = {"area": 0.00266667, "opening": {"area": 0.01792321, "filled": 0.01222281}}
    assert flow_area_m2(annulus, bore=True) == pytest.approx(0.0057004)
    assert flow_area_m2({"area": 0.0036}, bore=True) == pytest.approx(0.0036)   # a plain disc
    # a surface staging measured its lid: that is the opening, whatever the face reads
    lid = {"area": 0.0041, "flow_area": 0.004, "opening": {"area": 0.009}}
    assert flow_area_m2(lid, bore=True) == pytest.approx(0.004)
    # a typed size the bore agrees with, and a clean ring round its own bore the typed size does not
    assert flow_area_m2(annulus, bore=True, typed=0.00573) == pytest.approx(0.0057004)
    assert flow_area_m2(PVC_RING, bore=True, typed=0.000314) == pytest.approx(0.00080292)


def test_a_ring_that_does_not_say_its_bore_leaves_the_typed_size_standing():
    # hvac_transition_duct as staged (2026-10-06): the inlet bound to a flange plate (53,097 mm2 of
    # metal, the typed 260 mm bore by coincidence) whose cut-out (146,816 mm2, 50 mm off centre)
    # holds the duct's own end (43,708 mm2): neither the cut-out nor the cut-out less the duct's
    # end is the opening, so nothing is measured and the gate holds the typed size
    flange = {"area": 0.05309743, "centroid": [0.0, 0.0, 0.0],
              "opening": {"area": 0.146816, "centroid": [0.0, 0.0496, 0.0], "filled": 0.04370834}}
    assert flow_area_m2(flange, bore=True, typed=math.pi / 4 * 0.26 ** 2) is None
    assert measured_port_areas({"inlet": flange}, typed={"inlet": math.pi / 4 * 0.26 ** 2}) == {}
    off_centre = dict(PVC_RING, opening=dict(PVC_RING["opening"], centroid=[-0.038, 0.004, 0.0]))
    assert flow_area_m2(off_centre, bore=True, typed=0.000314) is None
    assert measured_port_areas({"inlet_1": PVC_RING, "x": {"centroid": [0, 0, 0]}}) == {
        "inlet_1": pytest.approx(0.00080292)}


def test_the_measured_opening_wins_over_the_typed_size():
    exp = expected_port_areas(PVC, {"inlet_1": 0.000803})
    assert exp["inlet_1"] == (pytest.approx(0.000803), "measured")
    assert exp["outlet"] == (pytest.approx(math.pi / 4 * 0.02 ** 2), "declared")
    assert "wall" not in exp


def test_a_typed_size_the_geometry_disagrees_with_is_said_plainly():
    notes = size_notes(PVC, {**PVC_BORES, "outlet": 0.000330})
    assert notes == [
        "inlet_1: you said about 20 mm across; the opening measures 32 mm across (803 mm2); "
        "using the measured opening.",
        "inlet_2: you said about 20 mm across; the opening measures 32 mm across (803 mm2); "
        "using the measured opening."]
    ring = [{"name": "inlet", "type": "inlet", "diameter_mm": 151.19, "inner_diameter_mm": 124.75}]
    assert size_notes(ring, {"inlet": 0.0179}) == [
        "inlet: you said an annulus about 151.19 mm across round a 124.75 mm centre; the opening "
        "measures 17,900 mm2 (about 151 mm across); using the measured opening."]
    assert size_notes(ring, {"inlet": 0.0057}) == []           # agrees: nothing to say
    assert size_notes(PVC, {}) == []                            # nothing measured: nothing to say


def test_pvc_mixing_tee_passes_on_its_measured_bores_and_its_metal_still_fails(tmp_path):
    bore_mesh = {"inlet_1": 0.000801, "inlet_2": 0.000801, "outlet": 0.000801, "wall": 0.02}
    assert gate_port_areas(_ctx(tmp_path, bore_mesh, PVC, openings=PVC_BORES)) == (True, "")
    # the WRONG REGION - the pipe's metal meshed, its end rings delivered as the ports
    metal_mesh = {"inlet_1": 0.00033, "inlet_2": 0.00033, "outlet": 0.00033, "wall": 0.02}
    ok, why = gate_port_areas(_ctx(tmp_path, metal_mesh, PVC, openings=PVC_BORES))
    assert not ok and "0.411 times the opening measured on the geometry" in why
    # nothing measured: the typed size is all there is, and the bore is 2.55x it
    ok, why = gate_port_areas(_ctx(tmp_path, bore_mesh, PVC))
    assert not ok and "2.55 times the opening declared" in why


def test_the_measured_openings_travel_from_staging_to_the_manifest(tmp_path):
    assert recorded_port_openings(tmp_path) == {}
    got = record_port_openings(tmp_path, {"inlet_1": PVC_RING}, bore=True, intake_patches=PVC)
    assert got == recorded_port_openings(tmp_path) == {"inlet_1": pytest.approx(0.00080292)}
    assert measured_openings({"quality": {"port_openings_m2": got}}) == got
    assert record_port_openings(tmp_path / "x", {}, bore=True) == {}      # nothing measured


def test_bind_intake_says_a_wrong_typed_bore_before_meshing(tmp_path):
    # a 32 mm bore in a 3 mm wall, typed as "about 20 mm": the binder matches the 20 mm to the
    # end ring's metal (330 mm2), the flow crosses the bore - and the user is told
    pytest.importorskip("OCP.STEPControl")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    from meshpipeline.engines.port_binding import bind_intake, declaration_targets
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0))
    pipe = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(ax, 19.0, 120.0).Shape(),
                           BRepPrimAPI_MakeCylinder(ax, 16.0, 120.0).Shape()).Shape()
    w = STEPControl_Writer()
    w.Transfer(pipe, STEPControl_AsIs)
    w.Write(str(tmp_path / "pipe.step"))
    prepared = from_occ_transfer(GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=0.001, basis=ResolutionBasis.file_declared, evidence="t"),
        LengthUnit.millimetre)
    patches = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 20.0},
               {"name": "outlet", "type": "outlet", "near_mm": [120, 0, 0], "diameter_mm": 20.0},
               {"name": "wall", "type": "wall"}]
    t = tessellate_internal(tmp_path / "pipe.step", tmp_path / "out", prepared=prepared,
                            declared_ports=declaration_targets(patches), fluid_solid=False)
    out, _wall, note = bind_intake(t, patches, bore=True)
    bore = math.pi * 0.016 ** 2
    assert measured_port_areas(out["openings"]) == {"inlet": pytest.approx(bore, rel=0.01),
                                                    "outlet": pytest.approx(bore, rel=0.01)}
    assert "inlet: you said about 20 mm across; the opening measures 32 mm across" in note
    assert out["binding"]["size_notes"] and len(out["binding"]["size_notes"]) == 2


def test_gmsh_measures_each_port_on_the_face_its_binder_finds():
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("t")
        gmsh.model.occ.addCylinder(0, 0, 0, 0.2, 0, 0, 0.02)
        gmsh.model.occ.synchronize()
        faces = [t for _, t in gmsh.model.getEntities(2)]
        area = {t: gmsh.model.occ.getMass(2, t) for t in faces}
        side = max(faces, key=lambda t: area[t])
        discs = sorted((t for t in faces if t != side),
                       key=lambda t: gmsh.model.occ.getCenterOfMass(2, t)[0])
        # typed as 20 mm on a 40 mm bore: the faces at the declared places are still the openings
        ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 20.0},
                 {"name": "outlet", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 20.0}]
        got = driver._port_openings(gmsh, faces, ports, cad=True)
        assert got == {"inlet": pytest.approx(area[discs[0]]), "outlet": pytest.approx(area[discs[1]])}
        assert size_notes(ports, got)[0].startswith("inlet: you said about 20 mm across; the "
                                                    "opening measures 40 mm across")
        sound = [{"name": "inlet", "role": "inlet", "surface_tags": [discs[0]]},
                 {"name": "outlet", "role": "outlet", "surface_tags": [discs[1]]},
                 {"name": "wall", "role": "wall", "surface_tags": [side]}]
        assert driver._checked_port_groups(gmsh, sound, faces, ports, cad=True) == sound
        swept = [{"name": "inlet", "role": "inlet", "surface_tags": [discs[0], side]},
                 {"name": "outlet", "role": "outlet", "surface_tags": [discs[1]]},
                 {"name": "wall", "role": "wall", "surface_tags": []}]
        out = {g["name"]: g["surface_tags"] for g in
               driver._checked_port_groups(gmsh, swept, faces, ports, cad=True)}
        assert out == {"inlet": [discs[0]], "outlet": [discs[1]], "wall": [side]}
    finally:
        gmsh.finalize()
