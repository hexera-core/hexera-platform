# Responsibility: Verify gmsh fills the closed fluid boundary a triangle-surface upload is staged as
# for internal flow: stage_declared writes it under the declared names, geometry_report lists its
# surfaces by those names, and the driver meshes the volume with every declared group on it.
# Boundaries: a small tube built here; the real gmsh driver, in-process; no files beyond tmp_path.
from __future__ import annotations

import json
import math

import pytest

gmsh = pytest.importorskip("gmsh")

PATCHES = [{"name": "feed", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40},
           {"name": "exit", "type": "outlet", "near_mm": [150, 0, 0], "diameter_mm": 40},
           {"name": "pipe", "type": "wall"}]


def _open_tube(path, r=0.02, length=0.15, n=24, rows=6):
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = []
    for k in range(n):
        a0, a1 = 2 * math.pi * k / n, 2 * math.pi * ((k + 1) % n) / n
        for j in range(rows):
            x0, x1 = length * j / rows, length * (j + 1) / rows
            p = [[x, r * math.cos(a), r * math.sin(a)] for x in (x0, x1) for a in (a0, a1)]
            tris += [(p[0], p[3], p[2]), (p[0], p[1], p[3])]
    write_stl_binary(path, tris)


def test_a_surface_upload_is_filled_with_every_declared_group_on_it(tmp_path):
    from meshpipeline.engines.gmsh import driver
    from meshpipeline.engines.gmsh import gmsh_runner as G

    _open_tube(tmp_path / "input.stl")
    (tmp_path / "port_declaration.json").write_text(json.dumps(PATCHES))
    (tmp_path / "flow_topology").write_text("internal")
    rec = G.stage_declared(tmp_path, geometry_path=str(tmp_path / "scan.stl"), prepared=None,
                           intake_patches=PATCHES, input_kind="body-surface")
    assert rec is not None and set(rec["ports"]) == {"feed", "exit"}
    rep = G.inspect_stl(tmp_path)
    names = {s["name"] for s in rep["surfaces"]}
    assert names == {"pipe", "feed", "exit"}, rep
    roles = {p["name"]: p["type"] for p in PATCHES}
    groups = [{"name": s["name"], "role": roles[s["name"]], "surface_tags": [s["tag"]]}
              for s in rep["surfaces"]]
    (tmp_path / "gmsh_spec.json").write_text(json.dumps(
        {"element_order": 1, "size": {"mode": "factor", "value": 0.08}, "groups": groups,
         "default_group": "pipe"}))
    assert driver.main(str(tmp_path)) == 0
    q = json.loads((tmp_path / "quality.json").read_text())
    assert q["cells"] > 0 and not q["fatal"]
    assert set(q["groups"]) == {"pipe", "feed", "exit"}


def test_a_cad_fluid_solid_is_left_to_its_brep(tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner as G
    _open_tube(tmp_path / "input.stl")
    assert G.stage_declared(tmp_path, geometry_path=str(tmp_path / "duct.step"), prepared=None,
                            intake_patches=PATCHES, input_kind="fluid-domain") is None
    assert not (tmp_path / G.FLUID_BOUNDARY).exists()
