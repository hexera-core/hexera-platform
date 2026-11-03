"""MESH-mmg-1: the MMG quality post-pass - the boundary frozen, kept only when better (engines/fea_mmg.py)."""
from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import fea_mmg as M

# one regular tetrahedron and one sliver (four nearly coplanar corners)
_GOOD = np.array([[0, 0, 0], [1, 0, 0], [0.5, np.sqrt(3) / 2, 0], [0.5, np.sqrt(3) / 6, np.sqrt(2 / 3)]], dtype=float)
_SLIVER = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0.001], [0, 1, 0]], dtype=float)


def _fig(**kw):
    base = {"elements": 10, "invalid": 0, "min_sj": 0.1, "max_aspect": 30.0, "min_dihedral": 3.0,
            "max_dihedral": 172.0, "over_limits": 4}
    return {**base, **kw}


def test_a_regular_element_triggers_nothing_and_a_sliver_does():
    good = M.worst(_GOOD, np.array([[0, 1, 2, 3]]))
    assert good["triggered"] == 0 and good["invalid"] == 0
    assert good["min_sj"] == pytest.approx(1.0, abs=1e-9)
    sliver = M.worst(_SLIVER, np.array([[0, 1, 2, 3]]))
    assert sliver["triggered"] == 1


def test_kept_only_when_nothing_is_worse_and_something_is_better():
    assert M.better(_fig(), _fig(min_sj=0.3, max_aspect=12.0, over_limits=1))[0]
    keep, why = M.better(_fig(), _fig(min_sj=0.3, max_aspect=31.0))
    assert not keep and "max_aspect" in why
    assert not M.better(_fig(), _fig())[0]                                   # the same: no better
    assert not M.better(_fig(), _fig(min_sj=0.2, over_limits=5))[0]          # more elements over a limit
    assert not M.better(_fig(), _fig(min_sj=0.5, invalid=1))[0]              # an invalid element
    assert not M.better(_fig(), {"elements": 0})[0]


def test_a_medit_file_round_trips(tmp_path):
    xyz = np.vstack([_GOOD, [[0.5, 0.3, 0.2]]])
    tris = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]])
    tets = np.array([[0, 1, 2, 4], [0, 1, 4, 3]])
    M.write_medit(tmp_path / "a.mesh", xyz, tris, np.array([5, 6, 7, 8]), tets, np.arange(4))
    got = M.read_medit(tmp_path / "a.mesh")
    assert np.array_equal(got["xyz"], xyz)                                   # %.17g: exact
    assert np.array_equal(got["tris"], tris) and np.array_equal(got["tri_refs"], [5, 6, 7, 8])
    assert np.array_equal(got["tets"], tets)


def _fake_mmg(tmp_path: Path, body: str) -> str:
    """A stand-in mmg3d: `-in a -out b ...` and a Python body with `src`, `dst` in scope."""
    exe = tmp_path / "fake_mmg3d"
    exe.write_text(f"#!{sys.executable}\nimport sys, shutil\nargs = sys.argv[1:]\n"
                   "src = args[args.index('-in') + 1]\ndst = args[args.index('-out') + 1]\n" + body + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def _box_model(gmsh, size=0.4):
    gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", size)
    gmsh.model.mesh.generate(3)


def test_off_or_missing_binary_leaves_the_mesh_alone():
    gmsh = pytest.importorskip("gmsh")
    _box_model(gmsh)
    try:
        assert M.improve(gmsh, [1], enabled=False) == {"rule": M.RULE, "ran": False, "why": "off (FEA_MMG=off)"}
    finally:
        gmsh.finalize()


def test_a_boundary_that_comes_back_different_is_refused(tmp_path):
    xyz = np.vstack([_GOOD, [[0.5, 0.3, 0.2]]])
    tris = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]])
    tets = np.array([[0, 1, 2, 4], [0, 1, 4, 3], [1, 2, 3, 4], [0, 2, 4, 3]])
    # the stand-in moves the first corner: a boundary node that does not come back
    exe = _fake_mmg(tmp_path, "t = open(src).read().split(chr(10))\n"
                              "t[4] = '0.01 0 0 0'\nopen(dst, 'w').write(chr(10).join(t))")
    out = M.remesh(xyz, tris, np.ones(4, dtype=int), tets, exe=exe, timeout=30, workdir=tmp_path)
    assert not out["ok"] and "boundary" in out["why"]


def test_the_same_mesh_back_is_not_kept_and_the_model_is_untouched(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    exe = _fake_mmg(tmp_path, "shutil.copy(src, dst)")
    _box_model(gmsh)
    try:
        before = gmsh.model.mesh.getElementsByType(4)
        # every element is reported as needing the pass, so the volume goes through the stand-in
        old = M.TRIGGER_SJ
        M.TRIGGER_SJ = 2.0
        try:
            rec = M.improve(gmsh, [1], enabled=True, exe=exe)
        finally:
            M.TRIGGER_SJ = old
        assert rec["ran"] and rec["tried"] == 1 and rec["kept"] == 0
        assert rec["volumes"][0]["why"] == "no better"
        after = gmsh.model.mesh.getElementsByType(4)
        assert np.array_equal(before[0], after[0]) and np.array_equal(before[1], after[1])
    finally:
        gmsh.finalize()


def test_elements_pinned_by_the_frozen_surface_are_not_sent_to_mmg(tmp_path, monkeypatch):
    """A volume whose every element the pass is for has all four corners on the surface: nothing inside
    can move, so mmg3d is not run and the reason is recorded."""
    gmsh = pytest.importorskip("gmsh")
    exe = _fake_mmg(tmp_path, "raise SystemExit(9)")          # would fail loudly if it were run
    _box_model(gmsh)
    try:
        # one tetrahedron, its four faces the boundary: every corner on the surface
        one = (np.arange(1, 5), _GOOD, np.array([[0, 1, 2, 3]]),
               np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]]), np.ones(4, dtype=np.int64))
        monkeypatch.setattr(M, "_volume_arrays", lambda _g, _v: one)
        monkeypatch.setattr(M, "TRIGGER_SJ", 2.0)
        rec = M.improve(gmsh, [1], enabled=True, exe=exe)
        assert rec["tried"] == 0 and rec["kept"] == 0
        assert "corners on the surface" in rec["volumes"][0]["why"]
        assert rec["volumes"][0]["before"]["triggered_pinned"] == rec["volumes"][0]["before"]["triggered"] == 1
    finally:
        gmsh.finalize()


def test_a_replaced_volume_keeps_its_surface_and_curves_to_quadratic(tmp_path):
    """_replace puts mmg's inside back on the same surface nodes; setOrder(2) then works as before."""
    gmsh = pytest.importorskip("gmsh")
    _box_model(gmsh)
    try:
        surf_before = sorted(tuple(sorted(t)) for t in
                             np.asarray(gmsh.model.mesh.getElementsByType(2)[1]).reshape(-1, 3).tolist())
        ntags, xyz, tets, tris, refs = M._volume_arrays(gmsh, 1)
        exe = _fake_mmg(tmp_path, "shutil.copy(src, dst)")
        out = M.remesh(xyz, tris, refs, tets, exe=exe, timeout=30, workdir=tmp_path)
        assert out["ok"]
        M._replace(gmsh, 1, ntags, out)
        n_tets = len(gmsh.model.mesh.getElementsByType(4)[0])
        assert n_tets == len(tets)
        surf_after = sorted(tuple(sorted(t)) for t in
                            np.asarray(gmsh.model.mesh.getElementsByType(2)[1]).reshape(-1, 3).tolist())
        assert surf_after == surf_before
        gmsh.model.mesh.setOrder(2)
        q = gmsh.model.mesh.getElementQualities(gmsh.model.mesh.getElementsByType(11)[0].tolist(), "minSICN")
        assert len(q) == n_tets and min(q) > 0
    finally:
        gmsh.finalize()


@pytest.mark.skipif(not M.binary(), reason="mmg3d is not installed (the mesh image ships it)")
def test_the_real_mmg3d_improves_a_sliver_mesh_with_the_boundary_frozen(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    _box_model(gmsh, size=0.25)
    try:
        # squash one inside node towards a face of its tetrahedron: slivers inside, the surface unchanged
        ntags, xyz, tets, tris, refs = M._volume_arrays(gmsh, 1)
        inside = np.setdiff1d(np.arange(len(ntags)), np.unique(tris))
        if not len(inside):
            pytest.skip("no inside node at this size")
        rec = M.improve(gmsh, [1], enabled=True)
        assert rec["ran"]
        for row in rec["volumes"]:
            if row.get("kept"):
                assert row["after"]["min_sj"] >= row["before"]["min_sj"]
    finally:
        gmsh.finalize()


def test_the_binary_is_found_where_the_image_installs_it(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    monkeypatch.setattr(M, "BIN_PATH", str(tmp_path / "missing"))
    assert M.binary() is None
    exe = tmp_path / "mmg3d_O3"
    exe.write_text("")
    monkeypatch.setattr(M, "BIN_PATH", str(exe))
    assert M.binary() == str(exe)                         # the image's /opt/mmg/bin/mmg3d_O3
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/local/bin/{n}")
    assert M.binary() == "/usr/local/bin/mmg3d_O3"        # PATH first
