# Responsibility: Pin that a staging failure and a self-intersecting surface each reach the user as what they are, where they are, and what to do.
"""TWO REFUSALS, SAID TRUTHFULLY.

* Staging: job a76e3ca1 (VMTK) ran without its staged lumen because the staging error was logged
  and swallowed; the builder looked for the file for 15 minutes and the run ended as "the mesher
  stopped". A staging failure now carries its reason to the user and ends the attempt.
* Self-intersection: jobs 85556ed8 / 108641cf were refused as a "defect in the CAD file" with no
  place named. The refusal now says where the surface crosses itself, how big the area is, and
  what to do about it."""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import numpy as np
import pytest

import meshpipeline.agents.builder.agent as agent
import meshpipeline.agents.builder.attempt as attempt_mod
import meshpipeline.agents.builder.attempt_capture as attempt_capture
import meshpipeline.agents.builder.invoke as invoke

# staging


def test_a_staging_failure_is_recorded_with_its_reason(tmp_path):
    attempt_mod._record_staging_failure(
        tmp_path, "vmtk", RuntimeError("no planar opening matched the declared port 'outlet_3'"))
    refusal = attempt_mod.staging_failure(tmp_path)
    assert refusal is not None and refusal.gate == attempt_mod.STAGING_GATE
    assert refusal.cause == "not_built"
    assert refusal.facts["reason"] == "no planar opening matched the declared port 'outlet_3'"
    assert refusal.facts["deterministic"] is True and refusal.facts["before_meshing"] is True
    assert refusal.facts["engine"] == "VMTK"


def test_a_raising_staging_hook_no_longer_vanishes(tmp_path, monkeypatch):
    class _Engine:
        def stage_declared(self, *a, **k):
            raise ValueError("the body could not be opened at the declared ports")

    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda e: _Engine())
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: types.SimpleNamespace(consumed=None))
    geometry = types.SimpleNamespace(path=str(tmp_path / "duct.step"))
    attempt_mod._stage_declared(tmp_path, geometry, {"intake_patches": []}, "vmtk")
    refusal = attempt_mod.staging_failure(tmp_path)
    assert refusal is not None
    assert refusal.facts["reason"] == "the body could not be opened at the declared ports"


@pytest.mark.parametrize("exc", [OSError(28, "No space left on device"), MemoryError(),
                                 TimeoutError("staging took too long")])
def test_a_system_error_while_staging_is_never_the_files_refusal(tmp_path, monkeypatch, exc):
    # our disk, memory or clock - nothing about the upload, and not the same next time
    class _Engine:
        def stage_declared(self, *a, **k):
            raise exc

    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda e: _Engine())
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: types.SimpleNamespace(consumed=None))
    geometry = types.SimpleNamespace(path=str(tmp_path / "duct.step"))
    attempt_mod._stage_declared(tmp_path, geometry, {"intake_patches": []}, "vmtk")
    refusal = attempt_mod.staging_failure(tmp_path)
    # recorded - never swallowed - but as OURS: not deterministic, and the run may try again
    from meshpipeline.contracts.failure_cause import retry_can_help
    assert refusal is not None and refusal.cause == "run_infrastructure"
    assert refusal.facts["ours"] is True and refusal.facts["deterministic"] is False
    assert retry_can_help(refusal.cause, refusal.facts)


def test_a_replay_of_the_same_attempt_stages_afresh(tmp_path, monkeypatch):
    # the earlier pass's record is not this pass's verdict
    attempt_mod._record_staging_failure(tmp_path, "vmtk", OSError(28, "No space left on device"),
                                        ours=True)

    class _Engine:
        def stage_declared(self, *a, **k):
            return {"ports": [1, 2]}

    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda e: _Engine())
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: types.SimpleNamespace(consumed=None))
    geometry = types.SimpleNamespace(path=str(tmp_path / "duct.step"))
    attempt_mod._stage_declared(tmp_path, geometry, {"intake_patches": []}, "vmtk")
    assert attempt_mod.staging_failure(tmp_path) is None


def test_our_staging_failure_goes_to_the_infrastructure_replay(tmp_path, monkeypatch, published):
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.errors import classify_api_failure
    from meshpipeline.pipeline.graph import route_after_builder
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path / "workspaces")
    real_prepare = attempt_mod.prepare

    def _prepare(state, *, job_id, mode):
        att = real_prepare(state, job_id=job_id, mode=mode)
        attempt_mod._record_staging_failure(att.workspace, "vmtk",
                                            OSError(28, "No space left on device"), ours=True)
        return att

    async def _never(*a, **k):
        raise AssertionError("the builder ran on without its staged input")

    monkeypatch.setattr(attempt_mod, "prepare", _prepare)
    monkeypatch.setattr(invoke, "run_attempt", _never)
    state = {"job_id": "j", "engine": "vmtk", "retry_count": 0, "builder_mode": "initial",
             "geometry": {}, "request_txt": "r", "review_brief_txt": "b", "intake_patches": []}
    out = asyncio.run(agent.node_builder(state))
    assert out["api_failure"] == attempt_mod.STAGING_SYSTEM_FAILURE
    assert classify_api_failure(out["api_failure"]).is_retryable
    assert route_after_builder({**state, **out}) == "node_infra_retry"
    notes = [c["text"] for p in published for c in p.calls if c["method"] == "anote"]
    assert not any("from your file" in n for n in notes), "our failure was told as the file's"


def test_our_staging_failure_is_said_as_ours():
    from meshpipeline.contracts.failure_cause import describe
    what, way_on = describe("run_infrastructure", {
        "stage": "staging", "engine": "VMTK", "reason": "[Errno 28] No space left on device",
        "ours": True, "deterministic": False})
    assert what == ("No mesh was built: preparing VMTK's input failed on our side (No space left "
                    "on device), so no mesher was started. That is our failure, not your "
                    "geometry's.")
    assert way_on.startswith("You can run it again with nothing changed")


@pytest.fixture
def published(monkeypatch):
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)


def test_the_builder_ends_the_attempt_on_a_staging_failure(tmp_path, monkeypatch, published):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path / "workspaces")
    real_prepare = attempt_mod.prepare

    def _prepare(state, *, job_id, mode):
        att = real_prepare(state, job_id=job_id, mode=mode)
        attempt_mod._record_staging_failure(att.workspace, "vmtk",
                                            RuntimeError("no lumen could be opened"))
        return att

    async def _never(*a, **k):
        raise AssertionError("the builder ran on without its staged input")

    monkeypatch.setattr(attempt_mod, "prepare", _prepare)
    monkeypatch.setattr(invoke, "run_attempt", _never)
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))
    out = asyncio.run(agent.node_builder({"job_id": "j", "engine": "vmtk", "retry_count": 0,
                                          "builder_mode": "initial", "geometry": {},
                                          "request_txt": "r", "review_brief_txt": "b",
                                          "intake_patches": []}))
    assert out["retry_count"] == 1, "the attempt was not counted as the one turn it was"
    notes = [c["text"] for p in published for c in p.calls if c["method"] == "anote"]
    assert notes[-1] == ("VMTK could not prepare its input from your file: no lumen could be "
                         "opened. No mesher was started.")
    assert not any(n.startswith("Mesh built") for n in notes)


@pytest.fixture
def quiet_executor(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as ex

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    install(monkeypatch, _ep)
    monkeypatch.setattr(ex, "execution_publisher", _ep.execution_publisher)

    class _NeverFinalize:
        name = "vmtk"

        def finalize(self, *a, **k):
            raise AssertionError("an empty workspace must not be finalized")
    monkeypatch.setattr(ex, "get_engine", lambda n="": _NeverFinalize())
    return ex


def test_the_staging_reason_reaches_the_closing_message_and_is_not_retried(quiet_executor,
                                                                          tmp_path):
    from meshpipeline.application import final_result as fr
    from meshpipeline.pipeline import engine_fallback as lad
    from meshpipeline.pipeline.graph import route_after_executor
    attempt_mod._record_staging_failure(tmp_path, "vmtk",
                                        RuntimeError("no lumen could be opened"))
    out = asyncio.run(quiet_executor.node_executor({
        "openfoam_workspace": str(tmp_path), "job_id": "t", "engine": "vmtk",
        "intake_patches": []}))
    assert out["executor_failed_gate"] == "staging"
    assert out["executor_failure_cause"] == "not_built"
    st = {**out, "retry_count": 1, "job_id": "t", "engine": "vmtk"}
    assert route_after_executor(st) == "__end__", "a deterministic staging failure was retried"
    assert lad.mesher_started(st) is False and lad.attempts_made(st, succeeded=False) == 0
    result = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="vmtk",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate="staging", api_failure="",
        attempts=1, attempts_max=3, required_ready=False, delivered_types=[],
        optional_warnings=[], failure_cause="not_built",
        failure_facts=out["executor_failure_facts"])
    text = fr.render_message(result)
    assert ("No mesh was built: VMTK could not prepare its input from your file (no lumen could "
            "be opened), so no mesher was started.") in text
    assert "We did not try again" in text
    assert "crash" not in text and "stopped before it finished writing" not in text


# self-intersection


def _piercing(tmp_path: Path) -> Path:
    import pyvista as pv
    path = tmp_path / "pierced.stl"
    pts = np.array([[0, 0, 0], [0.02, 0, 0], [0, 0.02, 0],
                    [0.005, 0.005, -0.01], [0.005, 0.005, 0.01], [0.015, 0.005, 0]], dtype=float)
    pv.PolyData(pts, np.hstack([[3, 0, 1, 2], [3, 3, 4, 5]])).save(str(path))
    return path


def test_the_crossing_is_located_and_sized(tmp_path):
    pytest.importorskip("pyvista")
    from meshpipeline.cad.surface_checks import self_intersection_report, self_intersects
    p = _piercing(tmp_path)
    assert self_intersects(p) is True
    r = self_intersection_report(p)
    assert r is not None and r["pairs"] == 1 and r["more"] is False
    assert len(r["first_at_m"]) == 3 and all(abs(v) < 0.03 for v in r["first_at_m"])
    assert r["region_m"][2] == pytest.approx(0.02, abs=1e-6)


def test_a_clean_surface_has_no_crossing_report(tmp_path):
    pytest.importorskip("pyvista")
    import pyvista as pv

    from meshpipeline.cad.surface_checks import self_intersection_report
    p = tmp_path / "ball.stl"
    pv.Sphere(radius=0.01).save(str(p))
    assert self_intersection_report(p) is None


def test_the_refusal_says_where_how_big_and_what_to_do():
    from meshpipeline.engines.registry import get_spec
    reason = get_spec("vmtk").geometry_unsuitable({
        "self_intersecting": True,
        "self_intersection": {"pairs": 2, "more": False, "first_at_m": [0.191, 0.209, -0.048],
                              "region_m": [0.0032, 0.0015, 0.0008], "triangle_m": 0.0004}})
    assert reason.startswith("[GEOMETRY_UNSUITABLE] the input surface self-intersects in 2 "
                             "places - first near x=191, y=209, z=-48 mm, within about 3.2 x "
                             "1.5 x 0.8 mm")
    assert "VMTK cannot fill it" in reason and "No mesh parameter changes that" in reason
    assert "self-intersecting-face selection" in reason and "export it again" in reason
    # without a location the sentence still stands, and still gives the way on
    plain = get_spec("vmtk").geometry_unsuitable({"self_intersecting": True})
    assert plain.startswith("[GEOMETRY_UNSUITABLE] the input surface self-intersects: there")
    assert "Fix the surface where it crosses" in plain


def test_the_closing_message_keeps_the_whole_refusal_and_blames_the_file_not_cad():
    from meshpipeline.application import final_result as fr
    from meshpipeline.engines.registry import get_spec
    reason = get_spec("vmtk").geometry_unsuitable({
        "self_intersecting": True,
        "self_intersection": {"pairs": 50, "more": True, "first_at_m": [1.5, -0.25, 0.0],
                              "region_m": [0.4, 0.3, 0.2]}})
    result = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="vmtk",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate="geometry", api_failure="",
        attempts=3, attempts_max=3, required_ready=False, delivered_types=[],
        optional_warnings=[], failure_cause="geometry_rejected",
        failure_facts={"reason": reason, "phases": ["measured"], "measured_reason": reason,
                       "codes": ["geometry_unsuitable"]})
    text = fr.render_message(result)
    assert "the problem is in the geometry file" in text and "CAD file" not in text
    assert "in 50 or more places - first near x=1500, y=-250, z=0 mm" in text
    assert "or export it again from the source model" in text, "the way on was cut off"
