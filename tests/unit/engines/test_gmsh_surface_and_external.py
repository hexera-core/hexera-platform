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
