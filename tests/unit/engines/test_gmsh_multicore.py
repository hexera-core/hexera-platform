# Responsibility: Prove MESH-25 - gmsh on every core with HXT filling the volume, and never a worse mesh for it: the thread setting, the defaults, the in-pass fall-back to Delaunay (FEA and CFD), and that threaded gmsh steps neither hang MESH-23's workers nor give an invalid mesh.
# Boundaries: gmsh in-process on tiny models; the FEA adapter's and the CFD driver's own attempts are stubbed where only the fall-back's choice is under test.
"""MESH-25: SPEED OVER SAMENESS, NEVER OVER A VALID MESH."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from meshpipeline.engines import determinism as det
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines.gmsh import driver as D
from meshpipeline.engines.gmsh import fea_adapter as FA


def test_gmsh_threads_are_the_jobs_cores_unless_tests_ask_for_one(monkeypatch):
    monkeypatch.setattr(det, "_GMSH_THREADS", None)
    monkeypatch.setattr(det, "job_cpus", lambda: 8)
    assert det.gmsh_threads() == 8
    monkeypatch.setattr(det, "_GMSH_THREADS", 1)
    assert det.gmsh_threads() == 1


def test_pin_gmsh_sets_the_threaded_steps_and_the_seed():
    import gmsh

    gmsh.initialize(interruptible=False)
    try:
        assert det.pin_gmsh(gmsh, 4) == 4
        for name in ("General.NumThreads", "Mesh.MaxNumThreads1D", "Mesh.MaxNumThreads3D"):
            assert gmsh.option.getNumber(name) == 4
        # gmsh's own surface stage is never threaded: it fails in PETSc on the faces it parametrizes itself
        assert gmsh.option.getNumber("Mesh.MaxNumThreads2D") == 1
        assert gmsh.option.getNumber("Mesh.RandomSeed") == det.GMSH_RANDOM_SEED
    finally:
        gmsh.finalize()


def test_the_stamp_says_whether_a_gmsh_run_is_repeatable(monkeypatch):
    monkeypatch.setattr(det, "_GMSH_THREADS", None)
    monkeypatch.setattr(det, "job_cpus", lambda: 8)
    s = det.stamp("gmsh")
    assert s["threads"]["gmsh"] == 8 and s["seeds"]["General.NumThreads"] == 8 and s["repeatable"] is False
    monkeypatch.setattr(det, "_GMSH_THREADS", 1)
    assert det.stamp("gmsh")["repeatable"] is True


def test_hxt_fills_first_and_lad_fea_2_puts_delaunay_first():
    assert FA.DEFAULT_ALGORITHM_3D == "hxt" and FA.algorithm_3d({}) == "hxt"
    assert L.mesh_settings([])["algorithm3d"] == "hxt"
    assert L.mesh_settings([{"lever": "LAD-fea-2"}])["algorithm3d"] == "delaunay"


def _plan(ws: Path, algorithm: str | None = None) -> None:
    (ws / "fea_plan.json").write_text(json.dumps({"mesh": {"algorithm3d": algorithm} if algorithm else {}}))


def _fake_attempts(monkeypatch, outcomes: dict):
    """FA._mesh_short stubbed: per 3D algorithm, raise, return an exit code, or write a quality record."""
    seen: list[str] = []

    def attempt(ws):
        algo = FA.algorithm_3d(json.loads((Path(ws) / "fea_plan.json").read_text())["mesh"])
        seen.append(algo)
        what = outcomes[algo]
        if isinstance(what, Exception):
            raise what
        if isinstance(what, dict):
            (Path(ws) / "quality.json").write_text(json.dumps(what))
            (Path(ws) / "mesh.inp").write_text(algo)
            return 0
        return int(what)

    monkeypatch.setattr(FA, "_mesh_short", attempt)
    return seen


def test_an_hxt_fill_that_fails_gives_way_to_delaunay_in_the_same_pass(tmp_path, monkeypatch):
    _plan(tmp_path)
    monkeypatch.setattr(FA, "_missed_rows", lambda q: 0)
    seen = _fake_attempts(monkeypatch, {"hxt": RuntimeError("HXT 3D mesh failed"), "delaunay": {"cells": 10}})
    assert FA.mesh(tmp_path) == 0
    assert seen == ["hxt", "delaunay"]
    q = json.loads((tmp_path / "quality.json").read_text())
    assert q["fea_algorithm3d"]["used"] == "delaunay" and "HXT 3D mesh failed" in q["fea_algorithm3d"]["why"]
    assert (tmp_path / "mesh.inp").read_text() == "delaunay"
    assert not (tmp_path / "_mesh25_first").exists()


def test_when_both_fail_delaunays_outcome_stands_as_before(tmp_path, monkeypatch):
    _plan(tmp_path)
    _fake_attempts(monkeypatch, {"hxt": 4, "delaunay": RuntimeError("PLC error")})
    with pytest.raises(RuntimeError, match="PLC error"):
        FA.mesh(tmp_path)


def test_a_valid_hxt_mesh_meeting_every_row_is_kept_at_once(tmp_path, monkeypatch):
    _plan(tmp_path)
    monkeypatch.setattr(FA, "_missed_rows", lambda q: 0)
    seen = _fake_attempts(monkeypatch, {"hxt": {"cells": 7}, "delaunay": {"cells": 9}})
    assert FA.mesh(tmp_path) == 0 and seen == ["hxt"]
    assert json.loads((tmp_path / "quality.json").read_text())["fea_algorithm3d"]["used"] == "hxt"


def test_an_hxt_mesh_short_of_a_row_gives_way_only_to_a_better_one(tmp_path, monkeypatch):
    _plan(tmp_path)
    monkeypatch.setattr(FA, "_missed_rows", lambda q: q["missed"])
    _fake_attempts(monkeypatch, {"hxt": {"missed": 1}, "delaunay": {"missed": 0}})
    assert FA.mesh(tmp_path) == 0
    assert json.loads((tmp_path / "quality.json").read_text())["fea_algorithm3d"]["used"] == "delaunay"
    _fake_attempts(monkeypatch, {"hxt": {"missed": 1}, "delaunay": {"missed": 1}})
    assert FA.mesh(tmp_path) == 0
    q = json.loads((tmp_path / "quality.json").read_text())
    assert q["fea_algorithm3d"]["used"] == "hxt" and (tmp_path / "mesh.inp").read_text() == "hxt"


def test_the_lad_fea_2_pass_falls_back_to_hxt(tmp_path, monkeypatch):
    _plan(tmp_path, "delaunay")
    monkeypatch.setattr(FA, "_missed_rows", lambda q: 0)
    seen = _fake_attempts(monkeypatch, {"delaunay": 4, "hxt": {"cells": 3}})
    assert FA.mesh(tmp_path) == 0 and seen == ["delaunay", "hxt"]


def test_the_cfd_path_falls_back_to_delaunay(tmp_path, monkeypatch):
    seen: list[str] = []

    def attempt(ws):
        seen.append(D._CFD_ALGO[0])
        if D._CFD_ALGO[0] == "hxt":
            raise RuntimeError("HXT 3D mesh failed")
        (Path(ws) / "quality.json").write_text(json.dumps({"cells": 5, "min_sicn": 0.3, "fatal": [],
                                                           "quality_bar": "cfd", "max_non_ortho": 60}))
        return 0

    monkeypatch.setattr(D, "_main", attempt)
    assert D.main(str(tmp_path)) == 0 and seen == ["hxt", "delaunay"]
    assert json.loads((tmp_path / "quality.json").read_text())["algorithm3d"]["used"] == "delaunay"


def test_the_cfd_bar_reads_faces_and_the_floor():
    ok = {"fatal": [], "min_sicn": 0.2, "quality_bar": "cfd", "max_non_ortho": 70}
    assert D._meets_bar(ok)
    assert not D._meets_bar({**ok, "max_non_ortho": 86})
    assert not D._meets_bar({**ok, "min_sicn": 0.001})
    assert not D._meets_bar({**ok, "fatal": ["no volume elements generated"]})


def _threaded_surfaces(q) -> None:
    import gmsh

    from meshpipeline.engines.gmsh import fea_parallel2d as P2D

    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        det.pin_gmsh(gmsh, 4)
        gmsh.model.add("m")
        b = gmsh.model.occ.addBox(0, 0, 0, 1, 1, 0.3)
        c = gmsh.model.occ.addCylinder(0.5, 0.5, -0.1, 0, 0, 0.5, 0.15)
        gmsh.model.occ.cut([(3, b)], [(3, c)])
        gmsh.model.occ.addBox(2, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.08)
        done = P2D.mesh_surfaces(gmsh, 3)
        gmsh.option.setNumber("Mesh.Algorithm3D", FA.ALGORITHMS_3D["hxt"])
        gmsh.model.mesh.generate(3)
        _t, tags, _n = gmsh.model.mesh.getElements(3)
        allt = [x for a in tags for x in a]
        q.put((done["workers"], len(allt), float(min(gmsh.model.mesh.getElementQualities(allt, "minSICN")))))
    finally:
        gmsh.finalize()


def test_threaded_curves_never_hang_the_surface_workers_and_hxt_on_four_threads_is_valid():
    """The curves meshed on 4 threads before MESH-23 forks its workers (lab, 13 Nov: with the step maxima left at
    the job's cores, every worker hung at 0% CPU), then an HXT fill on 4 threads: valid elements."""
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_threaded_surfaces, args=(q,))
    p.start()
    p.join(180)
    if p.is_alive():
        p.kill()
        pytest.fail("the surface workers hung after a threaded curve stage")
    workers, n, worst = q.get(timeout=5)
    assert workers == 3 and n > 1000 and worst > 0.0
