# Responsibility: Prove MESH-25 - an FEA solid's volume filled by HXT on every core, filled once, and never a failed pass for it: the thread setting, the defaults, the in-pass fall-back to Delaunay when the fill fails, that every other gmsh path stays on one thread, and that a threaded HXT fill after MESH-23's workers neither hangs them nor gives an invalid mesh.
# Boundaries: gmsh in-process on tiny models; the FEA adapter's own attempts are stubbed where only the fall-back's choice is under test.
"""MESH-25: SPEED OVER SAMENESS, NEVER OVER A DELIVERED MESH."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from meshpipeline.engines import determinism as det
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines.gmsh import fea_adapter as FA


def test_fea_gmsh_threads_are_the_jobs_cores_unless_tests_ask_for_one(monkeypatch):
    monkeypatch.setattr(det, "_GMSH_THREADS", None)
    monkeypatch.setattr(det, "job_cpus", lambda: 8)
    assert det.gmsh_threads() == 8 and FA.mesh_threads({}) == 8
    # the plan's own count still wins
    assert FA.mesh_threads({"threads": 2}) == 2
    monkeypatch.setattr(det, "_GMSH_THREADS", 1)
    assert det.gmsh_threads() == 1 and FA.mesh_threads({}) == 1


def test_every_other_gmsh_path_stays_on_one_thread(monkeypatch):
    """Only the FEA solid's fill is threaded: staging and the surface files keep one thread and the fixed seed,
    whatever MESH_GMSH_THREADS says (the shell route and the CFD path set one thread themselves)."""
    import gmsh

    from meshpipeline.engines import surface_deliverable as SD
    from meshpipeline.engines.gmsh import gmsh_runner as GR

    monkeypatch.setattr(det, "_GMSH_THREADS", 8)
    assert det.GMSH_THREADS == 1
    stamp = det.stamp("gmsh")
    assert stamp["threads"]["gmsh"] == 1 and stamp["seeds"]["General.NumThreads"] == 1
    gmsh.initialize(interruptible=False)
    try:
        for pin in (GR._pin_gmsh, SD._pin_gmsh):
            gmsh.option.setNumber("General.NumThreads", 6)
            pin(gmsh)
            assert gmsh.option.getNumber("General.NumThreads") == 1
            assert gmsh.option.getNumber("Mesh.RandomSeed") == det.GMSH_RANDOM_SEED
    finally:
        gmsh.finalize()


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
        # the mesher's log, as the runner writes it while the mesher runs
        with open(Path(ws) / "gmshfea.log", "a") as log:
            log.write(f"{algo} attempt\n")
        what = outcomes[algo]
        if isinstance(what, Exception):
            raise what
        (Path(ws) / "mesh.inp").write_text(algo)
        if isinstance(what, dict):
            (Path(ws) / "quality.json").write_text(json.dumps(what))
            return 0
        return int(what)

    monkeypatch.setattr(FA, "_mesh_short", attempt)
    return seen


def test_an_hxt_fill_that_fails_gives_way_to_delaunay_in_the_same_pass(tmp_path, monkeypatch):
    _plan(tmp_path)
    seen = _fake_attempts(monkeypatch, {"hxt": RuntimeError("[GMSH-FEA] EMPTY VOLUMES: volume(s) [1]"),
                                        "delaunay": {"cells": 10}})
    assert FA.mesh(tmp_path) == 0
    assert seen == ["hxt", "delaunay"]
    q = json.loads((tmp_path / "quality.json").read_text())
    assert q["fea_algorithm3d"]["used"] == "delaunay" and "EMPTY VOLUMES" in q["fea_algorithm3d"]["why"]
    assert (tmp_path / "mesh.inp").read_text() == "delaunay"
    # the mesher's own log is never thrown away with the failed attempt's files
    assert (tmp_path / "gmshfea.log").read_text() == "hxt attempt\ndelaunay attempt\n"
    assert not (tmp_path / "_mesh25_inputs").exists()


def test_an_invalid_hxt_mesh_gives_way_to_delaunay(tmp_path, monkeypatch):
    _plan(tmp_path)
    seen = _fake_attempts(monkeypatch, {"hxt": 4, "delaunay": {"cells": 10}})
    assert FA.mesh(tmp_path) == 0 and seen == ["hxt", "delaunay"]
    assert json.loads((tmp_path / "quality.json").read_text())["fea_algorithm3d"]["why"] == "exit 4"


def test_when_both_fail_delaunays_outcome_stands_as_before(tmp_path, monkeypatch):
    _plan(tmp_path)
    _fake_attempts(monkeypatch, {"hxt": 4, "delaunay": RuntimeError("PLC error")})
    with pytest.raises(RuntimeError, match="PLC error"):
        FA.mesh(tmp_path)
    _fake_attempts(monkeypatch, {"hxt": RuntimeError("HXT 3D mesh failed"), "delaunay": 4})
    assert FA.mesh(tmp_path) == 4


def test_an_hxt_mesh_that_came_back_is_kept_once_even_short_of_a_row(tmp_path, monkeypatch):
    """The owner's decision (14 Nov): HXT fills once. A mesh that came back is the pass's - held to every limit as
    any pass is, never meshed again with Delaunay to compare."""
    _plan(tmp_path)
    seen = _fake_attempts(monkeypatch, {"hxt": {"cells": 7, "fea_max_aspect_ratio": 900.0},
                                        "delaunay": {"cells": 9}})
    assert FA.mesh(tmp_path) == 0 and seen == ["hxt"]
    rec = json.loads((tmp_path / "quality.json").read_text())["fea_algorithm3d"]
    assert rec["used"] == "hxt" and "second" not in rec


def test_no_geometry_is_never_the_algorithms(tmp_path, monkeypatch):
    _plan(tmp_path)
    seen = _fake_attempts(monkeypatch, {"hxt": 3, "delaunay": {"cells": 9}})
    assert FA.mesh(tmp_path) == 3 and seen == ["hxt"]


def test_the_lad_fea_2_pass_falls_back_to_hxt(tmp_path, monkeypatch):
    _plan(tmp_path, "delaunay")
    seen = _fake_attempts(monkeypatch, {"delaunay": 4, "hxt": {"cells": 3}})
    assert FA.mesh(tmp_path) == 0 and seen == ["delaunay", "hxt"]


def _threaded_fill(q) -> None:
    import gmsh

    from meshpipeline.engines.gmsh import fea_parallel2d as P2D

    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        # the FEA adapter's settings (fea_adapter._mesh): curves and surfaces on one thread, the fill on four
        gmsh.option.setNumber("General.NumThreads", 4)
        gmsh.option.setNumber("Mesh.MaxNumThreads1D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads2D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads3D", 4)
        gmsh.option.setNumber("Mesh.RandomSeed", 1)
        gmsh.model.add("m")
        b = gmsh.model.occ.addBox(0, 0, 0, 1, 1, 0.3)
        c = gmsh.model.occ.addCylinder(0.5, 0.5, -0.1, 0, 0, 0.5, 0.15)
        gmsh.model.occ.cut([(3, b)], [(3, c)])
        gmsh.model.occ.addBox(2, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.08)
        gmsh.model.mesh.generate(1)
        done = P2D.mesh_surfaces(gmsh, 3)
        gmsh.option.setNumber("Mesh.Algorithm3D", FA.ALGORITHMS_3D["hxt"])
        gmsh.model.mesh.generate(3)
        _t, tags, _n = gmsh.model.mesh.getElements(3)
        allt = [x for a in tags for x in a]
        q.put((done["workers"], len(allt), float(min(gmsh.model.mesh.getElementQualities(allt, "minSICN")))))
    finally:
        gmsh.finalize()


def test_an_hxt_fill_on_four_threads_after_the_surface_workers_is_valid():
    """MESH-23's workers mesh the surfaces, then HXT fills on 4 threads: nothing hangs, every element valid."""
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_threaded_fill, args=(q,))
    p.start()
    p.join(180)
    if p.is_alive():
        p.kill()
        pytest.fail("the surface workers or the threaded fill hung")
    workers, n, worst = q.get(timeout=5)
    assert workers == 3 and n > 1000 and worst > 0.0
