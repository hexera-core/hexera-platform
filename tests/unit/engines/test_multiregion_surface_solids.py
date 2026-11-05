# Responsibility: Verify a multi-region assembly that arrives as a surface or an IGES is read into its solids.
# On main the assembly reader took STEP only: an IGES was copied to geometry.step and refused, and an STL
# never became solids at all ("OpenCASCADE could not read CAD file", lab 2026-10-04).
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.engines.snappy_multiregion.surface_solids import is_closed, shell_facts, split_shells


def _box_tris(x0, y0, z0, x1, y1, z1, *, inward=False):
    import pyvista as pv
    b = pv.Box(bounds=(x0, x1, y0, y1, z0, z1)).triangulate()
    t = np.asarray(b.points)[np.asarray(b.faces).reshape(-1, 4)[:, 1:]]
    # pv.Box winds outward; flip for a cavity
    return t[:, ::-1] if inward else t


def test_two_solids_sharing_a_face_come_apart_each_with_its_copy():
    # a CAD export writes the shared face once per solid, with the same triangles
    left = _box_tris(0, 0, 0, 1, 1, 1)
    right = left.copy()
    right[..., 0] = 2.0 - right[..., 0]          # mirrored about x = 1: the shared face is identical
    right = right[:, ::-1]                       # a mirror turns the winding inside out
    for second in (right, right[:, ::-1]):       # and whichever way the file wound the copy
        shells = split_shells(np.concatenate([left, second]))
        assert len(shells) == 2
        assert all(is_closed(s) for s in shells)
        vols = sorted(round(shell_facts(s)["signed_volume"], 6) for s in shells)
        assert vols == [1.0, 1.0]


def test_a_named_ascii_stl_is_one_solid_per_name(tmp_path):
    from meshpipeline.cad.stl_io import write_stl_solids
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import read_surface_solids
    write_stl_solids(tmp_path / "a.stl", {
        "fluid": [tuple(map(tuple, t)) for t in _box_tris(0, 0, 0, 1, 1, 1)],
        "wall_solid": [tuple(map(tuple, t)) for t in _box_tris(1, 0, 0, 2, 1, 1)]})
    solids = read_surface_solids(tmp_path / "a.stl", tmp_path / "_assembly", scale=0.001)
    assert [s["name"] for s in solids] == ["fluid", "wall_solid"]
    assert solids[0]["volume"] == pytest.approx(1e-9)
    assert json.loads((tmp_path / "_assembly" / "solids.json").read_text())[1]["index"] == 1


def test_an_enclosing_solid_keeps_the_cavity_its_inner_body_leaves(tmp_path):
    from meshpipeline.cad.stl_io import write_stl_binary
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import read_surface_solids
    outer = _box_tris(0, 0, 0, 4, 4, 4)
    cavity = _box_tris(1, 1, 1, 2, 2, 2, inward=True)
    inner = _box_tris(1, 1, 1, 2, 2, 2)
    tris = np.concatenate([outer, cavity, inner])
    write_stl_binary(tmp_path / "soup.stl", [tuple(map(tuple, t)) for t in tris])
    solids = read_surface_solids(tmp_path / "soup.stl", tmp_path / "_assembly")
    assert sorted(round(s["volume"], 6) for s in solids) == [1.0, 63.0]


@pytest.mark.parametrize("fmt", ["step", "igs", "stl"])
def test_the_assembly_reads_in_every_staged_format(tmp_path, fmt):
    pytest.importorskip("OCP.STEPControl")
    from pathlib import Path

    from meshpipeline.contracts.coordinate_state import from_occ_transfer, from_source_file
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "geometry" / "cht_enclosing_2region.step"
    interp = GeometryInterpretation(interpretation_id="t", owner_id="t", geometry_source_id="t",
                                    unit=LengthUnit.millimetre, scale_to_metres=0.001,
                                    basis=ResolutionBasis.file_declared, evidence="t")
    src = tmp_path / f"asm.{fmt}"
    if fmt == "step":
        src.write_bytes(fixture.read_bytes())
    else:
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer
        from OCP.STEPControl import STEPControl_Reader
        from OCP.StlAPI import StlAPI_Writer
        r = STEPControl_Reader()
        assert r.ReadFile(str(fixture)) == IFSelect_RetDone
        r.TransferRoots()
        shape = r.OneShape()
        if fmt == "igs":
            IGESControl_Controller.Init_s()
            w = IGESControl_Writer("MM", 1)
            w.AddShape(shape)
            w.ComputeModel()
            w.Write(str(src))
        else:
            BRepMesh_IncrementalMesh(shape, 0.05, False, 0.2, True)
            sw = StlAPI_Writer()
            sw.ASCIIMode = False
            sw.Write(shape, str(src))
    ws = tmp_path / "ws"
    ws.mkdir()
    if fmt == "stl":
        from meshpipeline.cad.normalise import scale_stl_file
        scale_stl_file(src, ws / "input.stl", from_source_file(interp))
    else:
        R.tessellate_to_stl(str(src), ws / "input.stl",
                            prepared=from_occ_transfer(interp, LengthUnit.millimetre))
    solids = R.ensure_assembly_solids(ws)
    vols = sorted(round(s["volume"] * 1e9, 1) for s in solids)
    assert vols == [125.0, 7875.0], vols
