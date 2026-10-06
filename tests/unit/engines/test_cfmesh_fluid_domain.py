# Responsibility: Verify cfMesh's internal path stages a prepared FLUID DOMAIN as the closed surface it meshes from inside.
# The catalog refused every fluid-domain internal case before staging ("cfmesh cannot produce a
# internal_cfd mesh from a 'fluid-domain' geometry"), although cartesianMesh fills a closed surface
# from the inside and a fluid solid's skin is exactly that surface.
from __future__ import annotations

import json

import pytest

pytest.importorskip("OCP.STEPControl")


def _fluid_rod_step(path, radius_mm=40.0, length_mm=400.0):
    """A cylinder solid: the fluid of a straight pipe, inlet at x=0, outlet at x=length."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    solid = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)),
                                     radius_mm, length_mm).Shape()
    w = STEPControl_Writer()
    # OCC's write unit is process-global: an earlier test's INCH fixture would leave it set, and
    # this rod would be written in inches and read back as millimetres (random-order flake)
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(solid, STEPControl_AsIs)
    w.Write(str(path))
    return path


def test_a_fluid_rod_is_staged_as_wall_plus_declared_ports(tmp_path, monkeypatch):
    from tests._geometry_support import materialized

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.engines.cfmesh import cfmesh_runner as R

    monkeypatch.setattr(R, "_to_fms", lambda ws, angle, bashrc: "geom.stl")
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="rod.step")
    _fluid_rod_step(geom.local_path)
    ws = tmp_path / "ws"
    surface = prepare_surface(geom, ws / "input.stl", engine="cfmesh")
    patches = [{"name": "inlet", "type": "inlet", "diameter_mm": 80.0, "near_mm": [0, 0, 0]},
               {"name": "outlet", "type": "outlet", "diameter_mm": 80.0, "near_mm": [400, 0, 0]},
               {"name": "wall", "type": "wall"}]
    (ws / "flow_topology").write_text("internal\n")
    (ws / "port_declaration.json").write_text(json.dumps(patches))
    out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                           wall_patch="wall", contract_patches=patches, args={},
                           cell_budget=2_000_000)
    assert out.get("success"), out
    assert set(out["openings"]) == {"inlet", "outlet"}
    from meshpipeline.cad.stl_io import read_stl_solids
    solids = read_stl_solids(ws / "geom.stl")
    assert set(solids) == {"wall", "inlet", "outlet"}, sorted(solids)
    # the passage is the 40 mm bore: 13 cells across it at the wall, nothing coarser
    assert out["passage_radius"]["p05"] == pytest.approx(0.04, rel=0.1)
    assert out["wall_cell_size"] <= 2 * 0.04 / 13 * 1.01
