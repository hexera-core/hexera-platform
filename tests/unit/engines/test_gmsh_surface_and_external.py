# Responsibility: Verify gmsh meshes a SURFACE upload and an EXTERNAL body, not only a CAD fluid domain.
# On main gmsh refused every external case ("submit a fluid-domain geometry") and stopped on every
# STL with "geometry.step missing" - 0 of the lab test set's STL or external rows (2026-10-04).
from __future__ import annotations

import json

import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines.gmsh import driver as D  # noqa: E402
from meshpipeline.engines.gmsh.surface_volume import bind_ports  # noqa: E402


def test_each_declared_port_binds_the_nearest_face_of_its_size():
    table = [{"tag": 1, "area": 0.0050, "centroid": [0.0, 0.0, 0.0]},      # inlet lid
             {"tag": 2, "area": 0.0050, "centroid": [1.0, 0.0, 0.0]},      # outlet lid
             {"tag": 3, "area": 0.3000, "centroid": [0.5, 0.0, 0.0]}]      # the wall
    ports = [{"name": "in", "type": "inlet", "diameter_mm": 80, "near_mm": [0, 0, 0]},
             {"name": "out", "type": "outlet", "diameter_mm": 80, "near_mm": [1000, 0, 0]},
             {"name": "wall", "type": "wall"}]
    assert bind_ports(table, ports) == {"in": [1], "out": [2]}
    # a port with a location only still takes the nearest free face
    assert bind_ports(table, [{"name": "x", "type": "outlet", "near_mm": [990, 0, 0]}]) == {"x": [2]}


def _ws(tmp_path, topology, ports, *, input_kind="", far=None):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "flow_topology").write_text(topology)
    (ws / "dimensionality").write_text("3D")
    (ws / "port_declaration.json").write_text(json.dumps(ports))
    if input_kind:
        (ws / "input_kind").write_text(input_kind)
    if far:
        (ws / "far_field_request.json").write_text(json.dumps(far))
    (ws / "gmsh_spec.json").write_text(json.dumps({"element_order": "1", "optimize": False,
                                                   "size": {"mode": "factor", "value": 0.08}}))
    return ws


def test_a_closed_stl_fluid_domain_meshes_with_its_ports_bound(tmp_path):
    import pyvista as pv
    ports = [{"name": "inlet", "type": "inlet", "diameter_mm": 100, "near_mm": [0, 0, 0]},
             {"name": "outlet", "type": "outlet", "diameter_mm": 100, "near_mm": [400, 0, 0]},
             {"name": "pipe", "type": "wall"}]
    ws = _ws(tmp_path, "internal", ports, input_kind="fluid-domain")
    pv.Cylinder(center=(0.2, 0, 0), direction=(1, 0, 0), radius=0.05, height=0.4,
                resolution=48, capping=True).triangulate().save(str(ws / "input.stl"))
    assert D.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["groups"] == {"inlet": "inlet", "outlet": "outlet", "pipe": "wall"}, q["groups"]
    assert q["cells"] > 0 and q["min_sicn"] > 0.0 and q["surface_source"].startswith("input.stl")
    assert (ws / "mesh.inp").exists()


def test_a_cad_body_is_cut_out_of_a_far_field_box_sized_as_requested(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
    ports = [{"name": "car", "type": "wall"}, {"name": "outer", "type": "farfield"}]
    far = {"requested_extents": {"upstream": 2, "downstream": 3, "lateral": 2, "vertical": 2},
           "flow_axis": "+x"}
    ws = _ws(tmp_path, "external", ports, input_kind="body-surface", far=far)
    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(0.4, 0.2, 0.1).Shape(), STEPControl_AsIs)
    w.Write(str(ws / "geometry.step"))
    assert D.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["groups"] == {"car": "wall", "outer": "farfield"}, q["groups"]
    (dmin, dmax), (bmin, bmax) = q["domain_box"], q["body_bounds"]
    assert dmin[0] == pytest.approx(-0.8, abs=1e-6) and dmax[0] == pytest.approx(0.4 + 1.2, abs=1e-6)
    # the manifest the executor judges carries THAT box around THAT body
    from meshpipeline.engines.gmsh.gmsh_runner import finalize
    assert finalize(str(ws), intake_patches=ports, engine="gmsh", domain="box body",
                    flow_topology="external")["success"]
    manifest = json.loads((ws / "mesh_manifest.json").read_text())
    v = evaluate_domain_extents(far["requested_extents"], 0.4, manifest, flow_axis="+x")
    assert v.status == "pass", (v.detail, v.misses)


def test_an_external_fluid_domain_is_meshed_as_it_is(tmp_path):
    # the user prepared the air box themselves: no second box around it
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    ws = _ws(tmp_path, "external", [], input_kind="fluid-domain")
    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape(), STEPControl_AsIs)
    w.Write(str(ws / "geometry.step"))
    assert D.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert not q.get("external") and q["bounds"][3] == pytest.approx(1.0, abs=1e-6)


def test_the_builders_groups_are_carried_through_the_far_field_cut():
    from meshpipeline.engines.gmsh.driver import _external_groups
    # body faces 11..14 came from the builder's tags 1..4; 21..26 are the box
    origin = {11: 1, 12: 2, 13: 3, 14: 4}
    spec = [{"name": "car_wall", "role": "wall", "surface_tags": [1, 2]},
            {"name": "car_wall_2", "role": "wall", "surface_tags": [3]},
            {"name": "FARFIELD", "role": "farfield", "surface_tags": [99]}]
    ports = [{"name": "car_wall", "type": "wall"}, {"name": "FARFIELD", "type": "farfield"}]
    out = {g["name"]: g["surface_tags"] for g in
           _external_groups(spec, [11, 12, 13, 14], [21, 22, 23, 24, 25, 26], origin, ports)}
    # face 4 was left out by the builder: it joins the first wall group
    assert out == {"car_wall": [11, 12, 14], "car_wall_2": [13],
                   "FARFIELD": [21, 22, 23, 24, 25, 26]}
    # no builder groups: the declared wall and far field
    auto = _external_groups(None, [11, 12], [21], origin, ports)
    assert [(g["name"], g["surface_tags"]) for g in auto] == [("car_wall", [11, 12]),
                                                              ("FARFIELD", [21])]


def test_the_case_contract_reads_the_builders_external_groups(tmp_path):
    import json

    from meshpipeline.engines import case_contract as CC
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": [
        {"name": "car_wall", "role": "wall", "surface_tags": [1]},
        {"name": "car_wall_2", "role": "wall", "surface_tags": [2]},
        {"name": "FARFIELD", "role": "farfield", "surface_tags": [3]}]}))
    (tmp_path / "flow_topology").write_text("external")
    (tmp_path / "input_kind").write_text("solid-body")
    case = CC._gmsh_boundary(tmp_path)
    assert set(case.patches) == {"car_wall", "car_wall_2", "FARFIELD"}
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": []}))
    (tmp_path / "port_declaration.json").write_text(json.dumps(
        [{"name": "body", "type": "wall"}, {"name": "far", "type": "farfield"}]))
    assert CC._gmsh_boundary(tmp_path).patches == {"body": "wall", "far": "farfield"}


def test_the_case_contract_adds_the_far_field_the_engine_adds(tmp_path):
    import json

    from meshpipeline.engines import case_contract as CC
    # the builder named the body only: the box is cut after, and the engine names it
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": [
        {"name": "body", "role": "wall", "surface_tags": [1, 2]}]}))
    (tmp_path / "flow_topology").write_text("external")
    (tmp_path / "input_kind").write_text("solid-body")
    (tmp_path / "port_declaration.json").write_text(json.dumps(
        [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]))
    assert CC._gmsh_boundary(tmp_path).patches == {"body": "wall", "farfield": "farfield"}
