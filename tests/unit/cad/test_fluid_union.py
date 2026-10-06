# A declared fluid domain modelled as several touching solids is ONE fluid: the faces where the
# solids touch are no wall. Left in, the staged surface closed two regions and a carve's seed or
# cfMesh's fill took one (volute_pump_domains: 'outlet' with no faces on both engines).
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("OCP")
from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.cad_tessellate import tessellate_internal  # noqa: E402
from meshpipeline.cad.stl_io import read_stl_triangles  # noqa: E402
from meshpipeline.contracts.coordinate_state import from_occ_transfer  # noqa: E402
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)
from meshpipeline.engines.port_binding import declaration_targets  # noqa: E402

PORTS = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40.0},
         {"name": "outlet", "type": "outlet", "near_mm": [0, 0, 300], "diameter_mm": 40.0}]


def _prepared():
    return from_occ_transfer(GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=0.001, basis=ResolutionBasis.file_declared, evidence="mm"),
        LengthUnit.millimetre)


def _stack(tmp_path, cuts):
    """A 40 mm bore, 300 mm long, as len(cuts)+1 separate touching solids along z."""
    zs = [0.0, *cuts, 300.0]
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for z0, z1 in zip(zs, zs[1:]):
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1)), 20.0, z1 - z0)
        bld.Add(comp, BRepBuilderAPI_Copy(cyl.Shape()).Shape())
    w = STEPControl_Writer()
    w.Transfer(comp, STEPControl_AsIs)
    path = tmp_path / "stack.step"
    w.Write(str(path))
    return path


def _edge_use(tris):
    key = np.round(tris.reshape(-1, 3) * 1e7).astype(np.int64)
    _, vid = np.unique(key, axis=0, return_inverse=True)
    f = vid.reshape(-1, 3)
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, c = np.unique(e, axis=0, return_counts=True)
    return set(c.tolist())


@pytest.mark.parametrize("cuts", [[150.0], [80.0, 210.0]])
def test_touching_fluid_solids_stage_as_one_closed_region(tmp_path, cuts):
    t = tessellate_internal(_stack(tmp_path, cuts), tmp_path / "stls", prepared=_prepared(),
                            fluid_solid=True, declared_ports=declaration_targets(PORTS))
    tris = np.concatenate([np.asarray(read_stl_triangles(Path(p)), float).reshape(-1, 3, 3)
                           for p in t["stls"].values()])
    assert _edge_use(tris) == {2}, "a face between the solids is still in the staged surface"
    import pyvista as pv
    surf = pv.PolyData(tris.reshape(-1, 3), np.hstack(
        [np.full((len(tris), 1), 3), np.arange(len(tris) * 3).reshape(-1, 3)]).ravel())
    vol = surf.clean(tolerance=1e-9).compute_normals(consistent_normals=True,
                                                     auto_orient_normals=True).volume
    assert vol == pytest.approx(math.pi * 0.02 ** 2 * 0.3, rel=0.02)


def test_a_solid_touching_no_port_carrying_solid_is_left_alone(tmp_path):
    # a body declared a fluid domain but undeclared ports: nothing is fused
    t = tessellate_internal(_stack(tmp_path, [150.0]), tmp_path / "stls", prepared=_prepared(),
                            fluid_solid=True)
    assert t["stls"]
