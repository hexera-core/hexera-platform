# THE PORT-AREA GATE, shared by every engine: a delivered inlet/outlet far from the size the user
# declared is not that opening. On the HOME-TURF audit (2026-10-06) six gmsh fluid-domain meshes
# passed every gate with ports 2.4-5.3x their declared openings.
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.region_check import (
    PORT_AREA_BAND,
    declared_port_area_m2,
    delivered_areas,
    gate_port_areas,
    port_area_misses,
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


def _ctx(tmp_path, areas, patches):
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({"quality": {"patch_areas_m2": areas}}))
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
