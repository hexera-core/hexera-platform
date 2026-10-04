# Responsibility: Verify the internal staging of a file with SEVERAL solids - a pipe and a centre rod
# modelled as bodies of their own, a bundle of inserts - treats the flow as the space all of them
# bound: the seed lands outside every solid, and each port's lid is the bore less what the other
# solids fill in its plane. annular_001 (a 151 mm bore round a 125 mm rod, two solids) was seeded
# in the pipe's own metal when the file did not say what it was, and both declarations got lids
# spanning the whole bore - over the rod's end faces, a port patch lying on the wall.
# Boundaries: OpenCASCADE shapes written as STEP in the test's own folder; no engine, no storage.
import math
from pathlib import Path

import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopoDS import TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

import numpy as np

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.cad.stl_io import read_stl_triangles
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)
from meshpipeline.engines.port_binding import declaration_targets

R_OUT, R_BORE, L = 50.0, 40.0, 100.0           # mm: the pipe


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _cyl(x0, r, length, y=0.0, z=0.0):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x0, y, z), gp_Dir(1, 0, 0)), r, length).Shape()


def _pipe():
    return BRepAlgoAPI_Cut(_cyl(0.0, R_OUT, L), _cyl(-1.0, R_BORE, L + 2.0)).Shape()


def _write(path: Path, *solids):
    both = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(both)
    for s in solids:
        b.Add(both, s)
    w = STEPControl_Writer()
    w.Transfer(both, STEPControl_AsIs)
    assert w.Write(str(path)) is not None
    return path


def _declared():
    return declaration_targets([
        {"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0], "diameter_mm": 2 * R_BORE},
        {"name": "outlet", "type": "outlet", "near_mm": [L, 0.0, 0.0], "diameter_mm": 2 * R_BORE},
        {"name": "wall", "type": "wall"}])


def _in(shape, p_mm) -> bool:
    cls = BRepClass3d_SolidClassifier(shape)
    cls.Perform(gp_Pnt(*p_mm), 1e-7)
    return cls.State() == TopAbs_IN


def _area_mm2(path) -> float:
    t = np.asarray(read_stl_triangles(Path(path)), float).reshape(-1, 3, 3)
    return float(np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).sum() / 2) * 1e6


def _open_edges(paths) -> int:
    tris = [tri for p in paths for tri in read_stl_triangles(Path(p))]
    v = np.round(np.asarray(tris, float).reshape(-1, 3) / 1e-9).astype(np.int64)
    _, inv = np.unique(v, axis=0, return_inverse=True)
    f = np.asarray(inv).reshape(-1, 3)
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return int((cnt == 1).sum())


RING = math.pi * (R_OUT ** 2 - R_BORE ** 2)


@pytest.mark.parametrize("fluid_solid", [False, None])
def test_a_rod_flush_with_the_pipe_ends_is_wall_and_the_lid_is_the_annulus(tmp_path, fluid_solid):
    # the rod ends in each port's plane: its end discs are wall, and the flow crosses only the
    # annulus between rod and bore - whether the file says it is a body or says nothing
    r_rod = 24.0
    pipe, rod = _pipe(), _cyl(0.0, r_rod, L)
    step = _write(tmp_path / "pipe_rod.step", pipe, rod)
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=fluid_solid,
                            declared_ports=_declared())
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert not _in(pipe, (x, y, z)) and not _in(rod, (x, y, z)), f"seed ({x}, {y}, {z}) mm is in the metal"
    assert r_rod < math.hypot(y, z) < R_BORE and 0.0 < x < L, f"seed ({x}, {y}, {z}) mm is not in the annulus"
    annulus = math.pi * (R_BORE ** 2 - r_rod ** 2)
    for port in ("inlet", "outlet"):
        lid = _area_mm2(t["stls"][port]) - RING            # the port patch carries the pipe's end ring
        assert lid == pytest.approx(annulus, rel=0.02), f"{port} lid {lid:.0f} mm2, the annulus is {annulus:.0f}"
    assert _open_edges(t["stls"].values()) == 0            # the lid meets the rod's side wall edge to edge
    # the rod's end discs are no boundary of the flow: left in, they closed the rod into a
    # region of its own, and cfMesh meshed the rod instead of the annulus
    for path in t["stls"].values():
        tri = np.asarray(read_stl_triangles(Path(path)), float).reshape(-1, 3, 3) * 1000.0
        c = tri.mean(axis=1)
        on_end = (np.minimum(np.abs(c[:, 0]), np.abs(c[:, 0] - L)) < 1e-6) & (np.hypot(c[:, 1], c[:, 2]) < r_rod)
        assert not on_end.any(), f"{path}: {int(on_end.sum())} triangles of the rod's end discs staged"


def test_a_rod_running_through_the_port_plane_takes_its_section_off_the_lid(tmp_path):
    # the rod sticks out past both pipe ends: at each port plane it is a section, not an end face
    r_rod = 20.0
    pipe, rod = _pipe(), _cyl(-15.0, r_rod, L + 30.0)
    step = _write(tmp_path / "pipe_long_rod.step", pipe, rod)
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declared())
    lid = _area_mm2(t["stls"]["inlet"]) - RING
    assert lid == pytest.approx(math.pi * (R_BORE ** 2 - r_rod ** 2), rel=0.02)
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert r_rod < math.hypot(y, z) < R_BORE and 0.0 < x < L


def test_a_bundle_of_inserts_is_taken_off_the_lid_one_by_one(tmp_path):
    # three tubes standing in the bore, flush with its ends: the lid is the bore less three discs
    r_tube, at = 8.0, 22.0
    pipe = _pipe()
    tubes = [_cyl(0.0, r_tube, L, y=at * math.cos(a), z=at * math.sin(a))
             for a in (0.0, 2 * math.pi / 3, 4 * math.pi / 3)]
    step = _write(tmp_path / "bundle.step", pipe, *tubes)
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declared())
    lid = _area_mm2(t["stls"]["outlet"]) - RING
    assert lid == pytest.approx(math.pi * (R_BORE ** 2 - 3 * r_tube ** 2), rel=0.02)
    p = [v * 1000.0 for v in t["interior_point"]]
    assert not _in(pipe, p) and not any(_in(s, p) for s in tubes), f"seed {p} mm is in the metal"


def test_a_single_pipe_keeps_its_whole_bore_as_the_lid(tmp_path):
    # nothing stands in the bore: the lid spans it, exactly as before
    step = _write(tmp_path / "pipe.step", _pipe())
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declared())
    lid = _area_mm2(t["stls"]["inlet"]) - RING
    assert lid == pytest.approx(math.pi * R_BORE ** 2, rel=0.02)
