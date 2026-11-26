# Responsibility: Prove the VMTK staging's work is planned (TIME-18): the edge bound counts each round before it makes
#                 it, a staged wall past one run's share stops before its radius field with the reason, and that
#                 stop is the staging's refusal that moves the run to another engine.
from __future__ import annotations

import types

import numpy as np
import pytest

from meshpipeline.engines.vmtk import lumen_staging as LS
from meshpipeline.engines.vmtk import rims, staging_work


def _square(side, n=1):
    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float) * side
    return pts, np.array([[0, 1, 2], [0, 2, 3]])


def _sliver_pipe(radius=0.028, length=1.0, n=38):
    t = 2.0 * np.pi * np.arange(n) / n
    ring = np.stack([radius * np.cos(t), radius * np.sin(t), np.zeros(n)], axis=1)
    pts = np.concatenate([ring, ring + [0.0, 0.0, length]])
    i = np.arange(n)
    j = (i + 1) % n
    return pts, np.concatenate([np.stack([i, j, n + j], 1), np.stack([i, n + j, n + i], 1)])


@pytest.mark.parametrize("sheet, h", [(_square(4.0), 0.1), (_sliver_pipe(), 0.0112)])
def test_the_bound_counts_each_round_before_it_makes_it(sheet, h):
    pts, tri = sheet
    _, made = rims._halve_long_edges(pts, tri, h)
    # the plan is the count itself: allowed exactly that many it runs, one fewer and it stops first
    _, again = rims._halve_long_edges(pts, tri, h, max_triangles=len(made))
    assert len(again) == len(made)
    with pytest.raises(rims.TooManyTriangles) as over:
        rims._halve_long_edges(pts, tri, h, max_triangles=len(made) - 1)
    assert over.value.planned > over.value.limit == len(made) - 1


def test_a_huge_bound_stops_before_it_allocates():
    # 1 m square at a 10 micron bound would be 4 x 10**10 triangles: stopped on the plan, at once
    pts, tri = _square(1.0)
    with pytest.raises(rims.TooManyTriangles):
        rims.bound_edges(pts, tri, h_wall=1e-5, h_rim=1e-5, max_triangles=1_000_000)


def test_pieces_after_halving_is_the_halvings_own_count():
    h = 1.0
    assert rims.pieces_after_halving([0.5, 1.0, 1.0000000001, 1.5, 2.0, 2.1, 7.9, 8.0, 8.1], h).tolist() == \
        [1, 1, 1, 2, 2, 4, 8, 8, 16]


def _open_tube_stl(path, r=0.02, length=0.2, n=32):
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = []
    for k in range(n):
        a0, a1 = 2 * np.pi * k / n, 2 * np.pi * (k + 1) / n
        for j in range(4):
            x0, x1 = length * j / 4, length * (j + 1) / 4
            p = [[x, r * np.cos(a), r * np.sin(a)] for x in (x0, x1) for a in (a0, a1)]
            tris += [(p[0], p[3], p[2]), (p[0], p[1], p[3])]
        for x, s in ((0.0, -1), (length, 1)):
            q0, q1 = [x, r * np.cos(a0), r * np.sin(a0)], [x, r * np.cos(a1), r * np.sin(a1)]
            tris.append(([x, 0, 0], q1, q0) if s < 0 else ([x, 0, 0], q0, q1))
    write_stl_binary(path, tris)


PATCHES = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40},
           {"name": "outlet", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 40},
           {"name": "vessel", "type": "wall"}]


def test_the_staged_wall_records_its_planned_work(tmp_path):
    _open_tube_stl(tmp_path / "input.stl")
    rec = LS.stage_lumen(tmp_path, tmp_path / "upload.stl", prepared=None, intake_patches=PATCHES,
                         input_kind="fluid-domain")
    work = rec["staging_work"]
    assert work["rule"] == "TIME-18" and work["max_triangles"] == staging_work.MAX_STAGED_TRIANGLES
    assert work["staged_triangles"] >= rec["wall_triangles"] > 0
    assert work["planned_s"] == pytest.approx(work["staged_triangles"] * staging_work.SECONDS_PER_STAGED_TRIANGLE,
                                              abs=0.1)


@pytest.mark.parametrize("limit", [300, 2000])
def test_a_wall_past_its_share_stops_before_the_radius_field(tmp_path, monkeypatch, limit):
    # 300: the bound's own plan stops it in a round; 2000: the bound finishes, the priced wall is over
    _open_tube_stl(tmp_path / "input.stl")
    monkeypatch.setattr(staging_work, "MAX_STAGED_TRIANGLES", limit)
    read = []
    monkeypatch.setattr(LS, "local_radius", lambda *a, **k: read.append(1))
    patches = [dict(p) for p in PATCHES]
    patches[0]["diameter_mm"] = patches[1]["diameter_mm"] = 6     # fine rims: thousands of triangles
    with pytest.raises(staging_work.StagingOverWork) as stop:
        LS.stage_lumen(tmp_path, tmp_path / "upload.stl", prepared=None, intake_patches=patches,
                       input_kind="fluid-domain")
    assert not read, "the radius field ran on a wall past the plan"
    words = str(stop.value)
    assert "wall triangles" in words and "another engine" in words and "hour" in words
    assert not (tmp_path / LS.LUMEN_OPEN).exists()


def test_the_stop_is_the_stagings_refusal_and_moves_the_run_on(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import attempt
    from meshpipeline.contracts.failure_cause import retry_can_help
    from meshpipeline.pipeline import engine_fallback as lad

    def over(*_a, **_k):
        raise staging_work.StagingOverWork(staging_work.refusal_words(284, 2_868_474))
    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine",
                        lambda name: types.SimpleNamespace(stage_declared=over))
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: types.SimpleNamespace(consumed=None))
    attempt._stage_declared(tmp_path, types.SimpleNamespace(path=str(tmp_path / "part.step")),
                            {"intake_patches": PATCHES}, "vmtk")
    refusal = attempt.staging_failure(tmp_path)
    assert refusal is not None and refusal.gate == attempt.STAGING_GATE
    assert "2,868,474 wall triangles" in refusal.facts["reason"]
    assert refusal.facts["deterministic"] is True and refusal.facts["before_meshing"] is True
    # the same engine cannot change it; another engine's preparation does not share it
    assert not retry_can_help(refusal.cause, refusal.facts)
    failure = lad.classify({"executor_success": False, "executor_failed_gate": "staging",
                            "executor_failure_cause": refusal.cause, "engine": "vmtk",
                            "executor_failure_facts": dict(refusal.facts)})
    assert failure is not None and failure.kind == lad.ENGINE
