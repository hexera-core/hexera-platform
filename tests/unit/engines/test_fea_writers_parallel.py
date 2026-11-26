# Responsibility: Pin that the Nastran and Ansys files written side by side in their own processes
# (engines/fea_writers.start_all, the FEA pass's writing stage) are the very bytes written one after the other
# (write_all), and that a writer that fails there is told as before (its file left out, the other delivered).
# Boundaries: a small second-order box meshed by gmsh here; nothing else is run.
from __future__ import annotations

from pathlib import Path

import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines import fea_writers as FW  # noqa: E402
from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh  # noqa: E402


@pytest.fixture(scope="module")
def solid():
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.model.occ.addBox(0, 0, 0, 0.02, 0.01, 0.006)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.003)
        gmsh.model.mesh.generate(3)
        gmsh.model.mesh.setOrder(2)
        return from_gmsh(gmsh, [t for _d, t in gmsh.model.getEntities(3)], by_tag=True)
    finally:
        gmsh.finalize()


def test_side_by_side_is_the_same_bytes_as_one_after_the_other(tmp_path, solid):
    names = {"Face_1": "base"}
    a, b = tmp_path / "serial", tmp_path / "parallel"
    a.mkdir()
    b.mkdir()
    one = FW.write_all(a, solid, cad_names=names, units="m", source="test")
    started = FW.start_all(b, solid, cad_names=names, units="m", source="test")
    two = started.result()
    assert one == two and set(one) == {"nastran", "ansys"}
    for name in ("mesh.bdf", "mesh.cdb"):
        assert (a / name).read_bytes() == (b / name).read_bytes() and (a / name).stat().st_size > 1000


def test_a_writer_that_fails_in_its_process_leaves_its_file_out_and_says_why(tmp_path, solid, monkeypatch):
    def broken(path, prep, **_kw):
        Path(path).write_text("half a file")
        raise ValueError("no room")
    monkeypatch.setattr(FW, "_WRITERS", (("nastran", "mesh.bdf", broken), ("ansys", "mesh.cdb", FW.write_cdb)))
    got = FW.start_all(tmp_path, solid, units="m").result()
    assert got["nastran"] == {"error": "ValueError: no room"} and not (tmp_path / "mesh.bdf").exists()
    assert got["ansys"]["file"] == "mesh.cdb" and (tmp_path / "mesh.cdb").exists()


def test_where_no_process_can_be_forked_they_are_written_here(tmp_path, solid, monkeypatch):
    import multiprocessing as mp

    def no_fork(method=None):
        raise ValueError("cannot find context for 'fork'")
    monkeypatch.setattr(mp, "get_context", no_fork)
    got = FW.start_all(tmp_path, solid, units="m").result()
    assert got["nastran"]["file"] == "mesh.bdf" and (tmp_path / "mesh.cdb").exists()
