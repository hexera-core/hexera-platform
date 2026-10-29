"""PLAN-fea-23: the general fuse runs in millimetres. The staging writes metres, and OpenCASCADE's
own tolerances are absolute: the same 406 parts fused in metres grew past 26 GB over 33 minutes, and
in millimetres took 3.2 GB and 2 minutes (o_ox_mc_laser, lab 27 Oct). The result comes back in the
parts' own unit with its faces and solids in the same order, so the sidecar still names them right."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402


def _box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _faces(shape):
    out = []
    for f in A._faces(shape):
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        out.append((g.Mass(), c.X(), c.Y(), c.Z()))
    return np.array(out)


def _parts_in_metres():
    # a 20 mm plate with a 10 mm block on it, and a bracket beside the block, all in metres
    return [_box(0, 0, 0, 0.02, 0.02, 0.002), _box(0.005, 0.005, 0.002, 0.01, 0.01, 0.01),
            _box(0.015, 0.005, 0.002, 0.003, 0.01, 0.004)]


def test_the_fuse_in_millimetres_comes_back_in_metres_in_the_same_order():
    parts = _parts_in_metres()
    plain = A.fuse(parts, fuzzy=1e-9)
    in_mm = A.fuse(parts, fuzzy=1e-9, mm_per_unit=1000.0)
    assert not plain.error and not in_mm.error
    assert in_mm.face_origins == plain.face_origins and in_mm.solid_parts == plain.solid_parts
    a, b = _faces(plain.shape), _faces(in_mm.shape)
    assert a.shape == b.shape
    # every face where it was, in metres: area to 1e-12 m^2, centre to a nanometre
    assert np.allclose(a[:, 0], b[:, 0], atol=1e-12) and np.allclose(a[:, 1:], b[:, 1:], atol=1e-9)
    lo, hi = A._box(in_mm.shape)
    assert np.allclose(hi - lo, [0.02, 0.02, 0.012], atol=1e-7)


def test_parts_in_millimetres_are_fused_as_they_are():
    parts = [_box(0, 0, 0, 20, 20, 2), _box(5, 5, 2, 10, 10, 10)]
    out = A.fuse(parts, fuzzy=1e-5, mm_per_unit=1.0)
    assert not out.error
    assert any(len({p for p, _j in o}) == 2 for o in out.face_origins)   # the shared face


def test_the_touching_pairs_are_found_alike_in_either_unit():
    parts = _parts_in_metres()
    vols = [A._props(s, True)[0] for s in parts]
    plain = A.interfaces_of(A.fuse(parts, fuzzy=1e-9), vols, scale=1000.0)
    in_mm = A.interfaces_of(A.fuse(parts, fuzzy=1e-9, mm_per_unit=1000.0), vols, scale=1000.0)
    key = [(sorted(i["faces"]), i.get("kind")) for i in plain]
    assert key == [(sorted(i["faces"]), i.get("kind")) for i in in_mm]
    assert len(in_mm) == 3                          # plate-block, plate-bracket, block-bracket


def test_the_stage_fuses_in_millimetres(tmp_path, monkeypatch):
    seen = []
    real = A.fuse

    def spy(parts, **kw):
        seen.append(kw.get("mm_per_unit"))
        return real(parts, **kw)

    monkeypatch.setattr(A, "fuse", spy)
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    comp, bld = TopoDS_Compound(), BRep_Builder()
    bld.MakeCompound(comp)
    for s in _parts_in_metres():
        bld.Add(comp, s)
    side = A.stage(comp, A.BONDED, tmp_path)
    assert seen and seen[0] == 1000.0
    assert (tmp_path / A.STAGED).exists() and side["parts"]



def test_the_fuse_in_millimetres_is_never_finer_than_the_fuse_in_metres(monkeypatch):
    # OpenCASCADE takes points closer than 1e-7 as one point, in whatever unit the shape is in. Fused in
    # metres, edges 1e-7 m (1e-4 mm) apart were one; scaled to millimetres with the run's own fuzzy value
    # (here 5e-8 m) they were not, and the fused shape kept near-coincident edges that cross in the 1D mesh
    # (fc_zcoupling: the bonded fill failed twice and the parts were tied - lab, 28 Oct). So the scaled fuse
    # gets at least that floor, scaled with the parts.
    seen = []
    real = A.fuse

    def spy(parts, **kw):
        seen.append((kw.get("fuzzy"), kw.get("mm_per_unit", 1.0)))
        return real(parts, **kw)

    monkeypatch.setattr(A, "fuse", spy)
    parts = _parts_in_metres()
    out = real(parts, fuzzy=5e-8, mm_per_unit=1000.0)
    assert not out.error
    inner = [f for f, k in seen if k == 1.0]
    assert inner and abs(inner[0] - 1e-7 * 1000.0) < 1e-15        # max(5e-8, 1e-7) m, in millimetres
    seen.clear()
    real(parts, fuzzy=3e-6, mm_per_unit=1000.0)                   # a coarser fuzzy value is kept as it is
    assert abs([f for f, k in seen if k == 1.0][0] - 3e-3) < 1e-15
