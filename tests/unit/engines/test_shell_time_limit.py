# Responsibility: Hold the shell mesh's time limit to "the best mesh so far, never nothing" (SHMESH-21b) and the
# stamped stream's optional meshes to the time left (SHMESH-21c): a run stopped at its limit delivers the best whole
# mesh it had made, told; an optional mesh that the time left would not cover is not started.
# Boundaries: the forked run and its folder in this process tree; made-up meshes and records, a fake clock; no gmsh.
from __future__ import annotations

import time

import numpy as np

from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.shell_mesh import ShellMesh


def _mesh(n_trias: int = 0) -> ShellMesh:
    xyz = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0], [2.0, 0.0, 0.0]])
    trias = np.array([[1, 4, 2]] * n_trias, dtype=np.int64).reshape(-1, 3)
    return ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]], dtype=np.int64), trias=trias,
                     wall=np.ones(1 + n_trias), mid=["Mid_1"] * (1 + n_trias), part=["Part_1"] * (1 + n_trias))


def _applied(past: int = 0, valid: bool = True, beyond: int = 0) -> dict:
    return {"quality": {"valid": valid, "nastran_past": {"taper": past} if past else {}},
            "requirements": {"rows": [{"id": "aspect", "field": "limit:aspect", "beyond": beyond, "met": not beyond,
                                       "worst": 6.0 if beyond else 4.0}]}}


class _Clock:
    """A fake clock for shell_adapter alone: monotonic and wall time move only when a pass says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return int(self.now * 1e9)

    def sleep(self, s: float) -> None:
        self.now += s


def test_a_run_stopped_at_its_limit_delivers_its_best_mesh_so_far_and_says_so(monkeypatch):
    # SHMESH-21b: the forked run makes two whole meshes (the second better: none past Nastran's limits), its third
    # pass runs past the limit and the child is stopped - pass 2's mesh is delivered, the record and a note say so
    req = {"limits": [{"id": "aspect", "op": "<=", "target": 5.0, "outer": 5.5}]}

    def run(*_a, requirements=None, **_k):
        sa._so_far(_mesh(2), _applied(past=3), 3, requirements, name="pass 1")
        sa._so_far(_mesh(2), _applied(past=0), 0, requirements, name="pass 2")
        time.sleep(30)                      # pass 3: would cross the limit
        raise AssertionError("pass 3 finished")

    monkeypatch.setattr(sa, "_mesh_shell", run)
    monkeypatch.setattr(sa, "FACE_WATCH", False)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    started = time.monotonic()
    mesh, applied = sa.mesh_shell("x.brep", {}, requirements=req)
    assert time.monotonic() - started < 15.0
    tl = applied["quality"]["time_limit"]
    assert tl["reached"] and tl["meshes_made"] == 2 and tl["delivered_mesh"] == 2 and tl["delivered"] == "pass 2"
    assert any("SHMESH-21b" in n and "after 2 whole mesh(es)" in n for n in mesh.notes)
    assert sa._SO_FAR["dir"] is None


def test_a_run_stopped_before_any_valid_mesh_ends_with_none_as_before(monkeypatch):
    # SHMESH-21b: only an invalid whole mesh was made - the stop says so in words, as SHMESH-21 always did
    import pytest

    def run(*_a, requirements=None, **_k):
        sa._so_far(_mesh(), _applied(valid=False), 0, requirements, name="pass 1")
        time.sleep(30)

    monkeypatch.setattr(sa, "_mesh_shell", run)
    monkeypatch.setattr(sa, "FACE_WATCH", False)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    with pytest.raises(ValueError, match="SHMESH-21"):
        sa.mesh_shell("x.brep", {}, requirements={})


def test_a_mesh_delivered_at_the_limit_is_not_aimed_again_at_the_users_count(tmp_path, monkeypatch):
    # SHMESH-21b: SHMESH-23's second aim is a whole shell mesh more - not after the run reached its limit
    import json

    (tmp_path / "shell.json").write_text(json.dumps({"surface_faces": []}))
    calls = []

    def shell(*_a, **_k):
        calls.append(1)
        applied = _applied()
        applied["quality"]["time_limit"] = {"reached": True}
        applied["target"] = {"asked": 1000, "planned": 1000, "correction": 1.0}
        return _mesh(), applied

    monkeypatch.setattr(sa, "mesh_shell", shell)
    _m, applied = sa.mesh_run(tmp_path, target={"elements": 1000})
    assert len(calls) == 1 and "SHMESH-21b" in applied["target"]["not_reaimed"]


def test_the_best_so_far_is_ranked_as_the_mesher_ranks_its_candidates():
    # SHMESH-21b: valid first, then inside Nastran's limits, then the user's lines, then fewer triangles; a tie keeps
    # the mesh made first
    req = {"limits": [{"id": "aspect", "op": "<=", "target": 5.0, "outer": 5.5}]}

    def v(mesh, applied, n):
        return sa._so_far_view(mesh, applied, n)

    good = v(_mesh(2), _applied(), 0)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(valid=False), 0), req)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(past=1), 1), req)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(beyond=2), 0), req)
    assert sa._so_far_beats(v(_mesh(1), _applied(), 0), good, req)
    assert not sa._so_far_beats(v(_mesh(2), _applied(), 0), good, req)


def test_an_optional_stamped_mesh_the_time_left_does_not_cover_is_not_started(monkeypatch):
    # SHMESH-21c (fake clock): each whole path takes 300 s of a 900 s run. The first path and SHMESH-76's first
    # candidate fit; the third whole mesh (without the steps and the fit-mapping) would end past the limit - it is not
    # started, the record and a note say so, and pass 2's mesh (the better) is delivered
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls = []

    def path(*_a, **_k):
        calls.append(1)
        clock.sleep(300.0)
        mesh = _mesh(4 if len(calls) == 1 else 2)
        applied = _applied()
        applied["quality"].update(strip_yield=True, fit_mapped=3)
        return mesh, applied, 0

    monkeypatch.setattr(sa, "_narrow_kept_path", path)
    monkeypatch.setattr(sa, "FACE_KEEP", False)
    monkeypatch.setattr(sa, "FIT_PER_FACE", False)
    monkeypatch.setattr(sa, "STEP_PER_STRIP", False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 900.0)
    monkeypatch.setitem(sa._RUN, "cut", False)
    mesh, applied, _n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert len(calls) == 2
    kept = applied["quality"]["strip_steps_kept"]
    assert kept["kept"] == "without the steps" and len(mesh.trias) == 2
    assert [s["mesh"] for s in kept["skipped"]] == ["without the steps and the fit-mapping"]
    assert "SHMESH-21c" in kept["skipped"][0]["why"]
    assert any("SHMESH-21c" in n for n in mesh.notes)


def test_with_time_to_spare_every_optional_mesh_starts_as_before(monkeypatch):
    # SHMESH-21c: a run far inside its limit starts each optional mesh exactly as before
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls = []

    def path(*_a, **_k):
        calls.append(1)
        clock.sleep(10.0)
        applied = _applied()
        applied["quality"].update(strip_yield=True, fit_mapped=3)
        return _mesh(2), applied, 0

    monkeypatch.setattr(sa, "_narrow_kept_path", path)
    monkeypatch.setattr(sa, "FACE_KEEP", False)
    monkeypatch.setattr(sa, "FIT_PER_FACE", False)
    monkeypatch.setattr(sa, "STEP_PER_STRIP", False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 900.0)
    monkeypatch.setitem(sa._RUN, "cut", False)
    _mesh_, applied, _n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert len(calls) == 3
    assert "skipped" not in applied["quality"]["strip_steps_kept"]
