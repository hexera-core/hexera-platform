# A hollow wall staged for a carve closes TWO regions (metal and cavity); a mesher with no seed
# point fills either - cfMesh meshed the rocket nozzle's metal (HOME-TURF lab, 2026-10-06). The
# bore-only staging keeps the bore skin and the bore's part of each cap: ONE closed region, the
# fluid, with each port the size of its bore.
from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("OCP")
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402

from meshpipeline.cad.bore_staging import _read, bore_only_surfaces, open_edges  # noqa: E402
from meshpipeline.cad.cad_tessellate import tessellate_internal  # noqa: E402
from meshpipeline.contracts.coordinate_state import from_occ_transfer  # noqa: E402
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)


def _prepared():
    return from_occ_transfer(GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=0.001, basis=ResolutionBasis.file_declared, evidence="mm"),
        LengthUnit.millimetre)


def _step(shape, path):
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _area(tris):
    return 0.5 * float(np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]),
                                      axis=1).sum())


def _volume(tris):
    from meshpipeline.cad.bore_staging import _poly
    surf = _poly(tris).clean(tolerance=1e-9).compute_normals(
        consistent_normals=True, auto_orient_normals=True)
    return float(surf.volume)


def test_a_flanged_tube_stages_its_bore_alone(tmp_path):
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    tube = BRepPrimAPI_MakeCylinder(ax, 30.0, 200.0).Shape()
    flange = BRepPrimAPI_MakeCylinder(ax, 50.0, 10.0).Shape()
    body = BRepAlgoAPI_Fuse(tube, flange).Shape()
    bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), 20.0, 202.0).Shape()
    part = BRepAlgoAPI_Cut(body, bore).Shape()
    from meshpipeline.engines.port_binding import declaration_targets
    ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40.0},
             {"name": "outlet", "type": "outlet", "near_mm": [0, 0, 200], "diameter_mm": 40.0}]
    t = tessellate_internal(_step(part, tmp_path / "p.step"), tmp_path / "stls", prepared=_prepared(),
                            declared_ports=declaration_targets(ports))
    srcs = dict(t["stls"])
    whole_inlet = _area(_read(srcs["inlet"]))
    out = bore_only_surfaces(srcs, "wall", tmp_path / "bore")
    assert out is not None
    tris = {k: _read(v) for k, v in out.items()}
    disc = math.pi * 0.020 ** 2
    assert whole_inlet > 1.5 * disc                      # the carve's cap spans the mouth
    for port in ("inlet", "outlet"):
        assert _area(tris[port]) == pytest.approx(disc, rel=0.02)
    surface = np.concatenate(list(tris.values()))
    assert open_edges(surface, 1e-9) == 0
    assert _volume(surface) == pytest.approx(math.pi * 0.020 ** 2 * 0.200, rel=0.02)


def test_a_solid_that_is_the_flow_is_not_cut(tmp_path):
    rod = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 25.0, 200.0).Shape()
    t = tessellate_internal(_step(rod, tmp_path / "r.step"), tmp_path / "stls", prepared=_prepared())
    assert t["wall_bounds_fluid"] is True
    assert bore_only_surfaces(dict(t["stls"]), "wall", tmp_path / "bore") is None
