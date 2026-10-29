# Responsibility: Verify that the CAD face-name read runs in a process of its own, so a crash, hang or memory blow-up in OCC's XDE reader costs the names only, never the staging or the run.
# Boundaries: the reader is real for the round trip; the crash is a real segmentation fault in a child, and the hang a stubbed timeout.
"""A COURTESY NEVER STOPS A RUN (FEA hunt, 27 Oct).

Staging a CAD solid for gmsh reads the face names a STEP file gives (cad/face_names) with OCC's XDE reader.
On two of the hunt's 237 real assemblies - a CATIA V4 bench and a Creo assembly - that reader died with a
segmentation fault, in the staging process itself: the run was gone, on main and on every branch.
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from meshpipeline.cad import face_names as FN

gmsh = pytest.importorskip("gmsh")
pytest.importorskip("OCP")


def _named_step(path):
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        box = occ.addBox(0, 0, 0, 60, 40, 30)
        occ.cut([(3, box)], [(3, occ.addCylinder(30, 20, -10, 0, 0, 50, 8))])
        occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
    text = path.read_text()
    for name in ("Mounting face", "Bore A"):
        text = text.replace("ADVANCED_FACE('',", f"ADVANCED_FACE('{name}',", 1)
    path.write_text(text)
    return path


def test_the_guarded_read_gives_the_same_names(tmp_path):
    src = _named_step(tmp_path / "part.step")
    direct, frame = FN.read_named_faces(src)
    guarded, gframe = FN.read_named_faces_guarded(src)
    assert sorted(n for n, _c, _a in guarded) == sorted(n for n, _c, _a in direct) == ["Bore A", "Mounting face"]
    for (n1, c1, a1), (n2, c2, a2) in zip(sorted(direct, key=lambda r: r[0]), sorted(guarded, key=lambda r: r[0])):
        assert n1 == n2 and np.allclose(c1, c2) and a1 == pytest.approx(a2)
    assert np.allclose(frame[0], gframe[0]) and frame[1] == pytest.approx(gframe[1])


def test_a_reader_that_crashes_costs_the_names_only(tmp_path, monkeypatch, caplog):
    """a REAL segmentation fault in the reader's process: no names, said, and the caller goes on"""
    src = _named_step(tmp_path / "part.step")
    from meshpipeline.sandbox import safe_exec

    real = safe_exec.run_guarded

    def crashing(args, **kw):
        return real([sys.executable, "-c", "import ctypes; ctypes.string_at(0)"], **kw)

    monkeypatch.setattr(safe_exec, "run_guarded", crashing)
    with caplog.at_level("WARNING"):
        named, frame = FN.read_named_faces_guarded(src)
    assert named == [] and frame[1] == 0.0
    assert "the reader stopped" in caplog.text
    ws = tmp_path / "ws"
    ws.mkdir()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(src))
        gmsh.model.occ.synchronize()
        assert FN.record_for_staging(gmsh, src, ws) == 0
    finally:
        gmsh.finalize()
    assert not (ws / FN.RECORD).exists()


def test_a_reader_that_hangs_costs_the_names_only(tmp_path, monkeypatch, caplog):
    src = _named_step(tmp_path / "part.step")
    from meshpipeline.sandbox import safe_exec

    def hanging(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout"))

    monkeypatch.setattr(safe_exec, "run_guarded", hanging)
    with caplog.at_level("WARNING"):
        assert FN.read_named_faces_guarded(src, timeout_s=1.0) == ([], FN._NO_FRAME)
    assert "took over" in caplog.text


def test_a_file_that_names_no_face_starts_no_process(tmp_path, monkeypatch):
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.write(str(tmp_path / "plain.step"))
    finally:
        gmsh.finalize()
    from meshpipeline.sandbox import safe_exec

    monkeypatch.setattr(safe_exec, "run_guarded", lambda *a, **k: pytest.fail("no process for a file with no names"))
    assert FN.read_named_faces_guarded(tmp_path / "plain.step") == ([], FN._NO_FRAME)
