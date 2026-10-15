# Responsibility: Pin that a run says why its engine stopped - refused, never started, crashed, out of time, our infrastructure - and counts only attempts that started a mesher.
"""WHY AN ENGINE STOPPED, SAID AS WHAT IT WAS.

Jobs d20ad762 and 26f5429a (2026-10-03, an aorta STL for internal flow on snappyHexMesh): the
driver refused the STL before any mesher started, and the user read "Mesh built", then "the mesher
stopped before it finished writing the mesh ... usually on our side", then the identical attempt
again, then "2 attempts". Every one of those was untrue. These tests pin the truthful account, read
from the one place every native run passes (contracts/mesh_execution.run_mesh)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meshpipeline.contracts import mesh_execution as mx
from meshpipeline.contracts.failure_cause import (
    FailureCause,
    StopClass,
    describe,
    retry_can_help,
    stop_class_of,
)
from meshpipeline.pipeline import engine_fallback as lad

# the record of native runs


class _Exec:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, 0

    def run(self, workspace, *, engine, timeout):
        self.calls += 1
        if self.raises:
            raise self.raises
        return dict(self.result)


@pytest.fixture
def executor(monkeypatch):
    def _install(result=None, raises=None, launch=None):
        ex = _Exec(result or {"rc": 0, "timed_out": False}, raises)
        monkeypatch.setattr(mx, "_executor", ex)
        monkeypatch.setattr(mx, "_launch_check", launch)
        return ex
    return _install


def test_a_workspace_this_process_never_opened_says_nothing(tmp_path, executor):
    executor()
    mx.run_mesh(tmp_path, engine="cfmesh", timeout=10)
    assert mx.native_runs(tmp_path) is None and mx.meshers_started(None) is None


def test_an_opened_attempt_records_every_native_run(tmp_path, executor):
    mx.open_native_record(tmp_path)
    assert mx.native_runs(tmp_path) == [] and mx.meshers_started([]) == 0
    executor({"rc": 0, "timed_out": False})
    mx.run_mesh(tmp_path, engine="cfmesh", timeout=10)
    executor({"rc": -1, "timed_out": True})
    mx.run_mesh(tmp_path, engine="cfmesh", timeout=10)
    runs = mx.native_runs(tmp_path)
    assert [(r["rc"], r["timed_out"]) for r in runs] == [(0, False), (-1, True)]
    assert mx.meshers_started(runs) == 2
    # reopening starts the attempt afresh
    mx.open_native_record(tmp_path)
    assert mx.native_runs(tmp_path) == []


def test_a_launch_the_last_check_refused_started_no_mesher(tmp_path, executor):
    mx.open_native_record(tmp_path)
    ex = executor(launch=lambda ws, eng: {"rc": mx.RC_CASE_CONTRACT, "timed_out": False})
    mx.run_mesh(tmp_path, engine="gmsh", timeout=10)
    assert ex.calls == 0
    runs = mx.native_runs(tmp_path)
    assert runs[0]["refused_before_launch"] is True and mx.meshers_started(runs) == 0


def test_a_run_that_raised_is_recorded_as_our_infrastructure(tmp_path, executor):
    mx.open_native_record(tmp_path)
    executor(raises=mx.SubmissionIndeterminate("ack lost"))
    with pytest.raises(mx.SubmissionIndeterminate):
        mx.run_mesh(tmp_path, engine="vmtk", timeout=10)
    (run,) = mx.native_runs(tmp_path)
    assert run["raised"] is True and run["rc"] == mx.RC_INFRASTRUCTURE


def test_the_record_never_enters_the_workspace(tmp_path, executor):
    # the workspace's bytes are the submission's identity: a fact written there would change a
    # replay's digest
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    mx.open_native_record(ws)
    executor()
    mx.run_mesh(ws, engine="cfmesh", timeout=10)
    assert list(ws.iterdir()) == []


# the executor's cause for an attempt with no mesh


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

    class _NoMesh:
        name = "x"

        def finalize(self, *a, **k):
            return {"success": False, "output": "no polyMesh", "stdout": "", "stderr": ""}
    monkeypatch.setattr(ex, "get_engine", lambda n="": _NoMesh())
    return ex


def _no_mesh(ex, ws: Path, engine: str, runs: list[dict] | None) -> dict:
    if runs is not None:
        mx.open_native_record(ws)
        for r in runs:
            mx._note_native_run(ws, r, refused=bool(r.get("refused")), raised=bool(r.get("raised")))
    return asyncio.run(ex.node_executor({"openfoam_workspace": str(ws), "job_id": "t",
                                         "engine": engine, "intake_patches": []}))


@pytest.mark.parametrize("engine,deterministic", [("snappy", True), ("cfmesh", False)])
def test_no_mesher_started_is_told_as_not_built_never_a_crash(quiet_executor, tmp_path, engine,
                                                              deterministic):
    from meshpipeline.pipeline.graph import route_after_executor
    out = _no_mesh(quiet_executor, tmp_path, engine, [])
    assert out["executor_failure_cause"] == "not_built"
    assert out["executor_failure_facts"]["meshers_started"] == 0
    assert out["executor_failure_facts"]["deterministic"] is deterministic
    what, nxt = describe(out["executor_failure_cause"], out["executor_failure_facts"],
                         engine="snappyHexMesh")
    assert "No mesh was built" in what and "before the mesher started" in what
    assert "crash" not in what and "stopped before it finished writing" not in what
    # a deterministic driver that stopped before its mesher stops the same way again: no retry
    st = {**out, "retry_count": 1, "job_id": "t"}
    assert (route_after_executor(st) == "__end__") is deterministic
    assert retry_can_help("not_built", out["executor_failure_facts"]) is not deterministic


def test_a_run_that_ran_out_of_time_is_told_so(quiet_executor, tmp_path):
    out = _no_mesh(quiet_executor, tmp_path, "cfmesh", [{"rc": -1, "timed_out": True}])
    assert out["executor_failure_cause"] == "engine_timed_out"
    what, nxt = describe("engine_timed_out", out["executor_failure_facts"])
    assert "ran out of time" in what and "crash" not in what
    assert retry_can_help("engine_timed_out")


@pytest.mark.parametrize("run", [
    {"rc": mx.RC_INFRASTRUCTURE, "timed_out": False,
     "log_tail": f"{mx.RUN_NOT_STARTED_TAG} vmtk: the job could not be dispatched"},
    {"rc": None, "raised": True}])
def test_a_run_that_never_started_on_our_side_is_told_as_ours(quiet_executor, tmp_path, run):
    out = _no_mesh(quiet_executor, tmp_path, "vmtk", [run])
    assert out["executor_failure_cause"] == "run_infrastructure"
    what, _ = describe("run_infrastructure", {})
    assert "on our side" in what and "geometry" in what


def test_snappys_own_stage_verdict_is_the_mesher_failing_not_the_service(quiet_executor,
                                                                         tmp_path):
    # RC_INFRASTRUCTURE with neither tag: every stage ran and no valid mesh came out
    out = _no_mesh(quiet_executor, tmp_path, "snappy",
                   [{"rc": mx.RC_INFRASTRUCTURE, "timed_out": False, "log_tail": ""}])
    assert out["executor_failure_cause"] == "engine_crashed"
    assert out["executor_failure_facts"]["meshers_started"] == 1


def test_a_result_that_could_not_be_brought_back_is_told_as_a_run_that_ran(quiet_executor,
                                                                           tmp_path):
    out = _no_mesh(quiet_executor, tmp_path, "snappy", [{
        "rc": mx.RC_INFRASTRUCTURE, "timed_out": False,
        "log_tail": f"{mx.RUN_UNCOLLECTED_TAG} snappy: the mesh ran (remote rc=0 cells=7000000)"}])
    assert out["executor_failure_cause"] == "run_infrastructure"
    assert out["executor_failure_facts"]["result_uncollected"] is True
    what, nxt = describe("run_infrastructure", out["executor_failure_facts"])
    assert what.startswith("The mesher ran, but its result could not be brought back")
    assert "too big to bring back" in nxt
    from meshpipeline.adapters.mesh_execution.cloud_run_client import RESULT_UNCOLLECTED_MARKER
    assert RESULT_UNCOLLECTED_MARKER == mx.RUN_UNCOLLECTED_TAG


def test_the_adapter_writes_the_never_started_tag_the_account_reads():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import _fail
    r = _fail("cfmesh", "no operation")
    assert r["rc"] == mx.RC_INFRASTRUCTURE
    assert r["log_tail"].startswith(mx.RUN_NOT_STARTED_TAG)


def test_a_mesher_that_ran_and_left_no_mesh_is_the_crash_it_is(quiet_executor, tmp_path):
    out = _no_mesh(quiet_executor, tmp_path, "gmsh", [{"rc": 1, "timed_out": False}])
    assert out["executor_failure_cause"] == "engine_crashed"
    assert out["executor_failure_facts"]["meshers_started"] == 1


def test_an_unknown_record_keeps_the_old_account(quiet_executor, tmp_path):
    out = _no_mesh(quiet_executor, tmp_path, "gmsh", None)
    assert out["executor_failure_cause"] == "engine_crashed"
    assert "meshers_started" not in out["executor_failure_facts"]


def test_the_closing_message_of_an_attempt_that_never_meshed(quiet_executor, tmp_path):
    from meshpipeline.application import final_result as fr
    out = _no_mesh(quiet_executor, tmp_path, "snappy", [])
    result = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="snappy",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate=out["executor_failed_gate"],
        api_failure="", attempts=1, attempts_max=3, required_ready=False, delivered_types=[],
        optional_warnings=[], failure_cause=out["executor_failure_cause"],
        failure_facts=out["executor_failure_facts"])
    text = fr.render_message(result)
    assert "No mesh was built: snappyHexMesh stopped while preparing the mesh" in text
    assert "We did not try again" in text
    for lie in ("stopped before it finished writing", "usually on our side", "crash"):
        assert lie not in text, text


# the six classes


def test_every_cause_belongs_to_exactly_one_stop_class():
    for cause in FailureCause:
        assert isinstance(stop_class_of(cause), StopClass), f"{cause} has no stop class"
    assert stop_class_of("geometry_rejected") is StopClass.REFUSED_BY_DESIGN
    assert stop_class_of("not_built") is StopClass.REFUSED_BY_DESIGN
    assert stop_class_of("engine_crashed") is StopClass.MESHER_CRASHED
    assert stop_class_of("engine_timed_out") is StopClass.OUT_OF_BUDGET
    assert stop_class_of("cell_budget") is StopClass.OUT_OF_BUDGET
    assert stop_class_of("mesh_quality") is StopClass.CHECK_FAILED
    assert stop_class_of("run_infrastructure") is StopClass.INFRASTRUCTURE
    assert stop_class_of("", review=True) is StopClass.REVIEW_REBUILD
    assert stop_class_of("mesh_quality", infrastructure=True) is StopClass.INFRASTRUCTURE
    assert stop_class_of("") is None


def test_every_new_cause_has_words_and_a_ladder_class():
    for cause in ("not_built", "engine_timed_out", "run_infrastructure"):
        what, nxt = describe(cause, {})
        assert what and nxt
        assert FailureCause(cause) in lad._LADDER_CLASS
        assert FailureCause(cause) in lad._REASON_BY_CAUSE


def test_the_ladder_records_why_each_attempt_stopped():
    f = lad.classify({"engine": "snappy", "executor_failed_gate": "finalize",
                      "executor_failure_cause": "not_built",
                      "executor_failure_facts": {"meshers_started": 0, "deterministic": True}})
    assert f.stop == "refused_by_design" and f.reason == "it stopped before its mesher started"
    # not retryable on this engine, and still an ENGINE failure: another engine may build it
    assert f.kind == lad.ENGINE
    review = lad.classify({"engine": "snappy", "executor_success": True,
                           "reviewer_verdict": "FAIL"})
    assert review.stop == "review_rebuild"


# the attempts a user is shown


def _att(n, **kw):
    return {"attempt": n, "engine": "snappy", "kind": "engine", "cause": "x", **kw}


def test_attempts_that_never_started_a_mesher_are_not_counted():
    # d20ad762: two builder turns, no mesher ever started
    st = {"engine": "snappy", "retry_count": 2, "executor_success": False,
          "executor_failed_gate": "finalize", "executor_failure_cause": "not_built",
          "executor_failure_facts": {"meshers_started": 0},
          "engine_ladder": {"attempts": [_att(1, built=False)]}}
    assert lad.attempts_made(st, succeeded=False) == 0
    rec = lad.final_record(st, succeeded=False, system_failure=False)
    assert rec["attempts"][-1]["built"] is False


def test_an_attempt_that_meshed_counts_and_one_that_did_not_does_not():
    st = {"engine": "cfmesh", "retry_count": 2, "executor_success": False,
          "executor_failed_gate": "quality_floor", "executor_failure_cause": "mesh_quality",
          "executor_failure_facts": {},
          "engine_ladder": {"attempts": [_att(1, built=False)]}}
    assert lad.attempts_made(st, succeeded=False) == 1
    ok = {"engine": "cfmesh", "retry_count": 3, "executor_success": True,
          "engine_ladder": {"attempts": [_att(1), _att(2, built=False)]}}
    assert lad.attempts_made(ok, succeeded=True) == 2


def test_an_attempt_the_record_knows_nothing_about_still_counts():
    # a run whose ladder record holds no history (an older state, a synthetic one): every builder
    # turn counts, exactly as the retry counter always did
    st = {"engine": "cfmesh", "retry_count": 3, "executor_success": False, "engine_ladder": {}}
    assert lad.attempts_made(st, succeeded=False) == 3


def test_a_refusal_before_building_is_never_an_attempt():
    st = {"engine": "vmtk", "retry_count": 3, "executor_success": False,
          "executor_failed_gate": "geometry", "executor_failure_cause": "geometry_rejected",
          "geometry_unsuitable_reason": "[GEOMETRY_UNSUITABLE] self-intersects",
          "executor_failure_facts": {"before_meshing": True}}
    assert lad.attempts_made(st, succeeded=False) == 0


def test_a_preflight_refusal_after_an_earlier_pass_meshed_is_still_an_attempt():
    st = {"engine": "snappy", "retry_count": 1, "executor_success": False,
          "executor_failed_gate": "domain_extent", "executor_failure_cause": "domain_extent",
          "executor_failure_facts": {"before_meshing": True, "meshers_started": 1}}
    assert lad.mesher_started(st) is True
    no_record = {**st, "executor_failure_facts": {"before_meshing": True}}
    assert lad.mesher_started(no_record) is False


def test_the_run_writes_the_attempts_that_meshed_not_the_retry_counter():
    import inspect

    import meshpipeline.application.pipeline_run as pr
    src = inspect.getsource(pr._run_async)
    assert "_attempts_shown(final_state, retry_count" in src
    assert "update_current_attempt(db, uuid.UUID(job_id), _attempts_made)" in src
    # the terminal record says the same number as the job row
    assert "attempts_made=_attempts_made" in src


def test_the_terminal_record_counts_the_attempts_made_and_judges_by_the_budget():
    from meshpipeline.application import final_result as fr
    common = {"job_id": "j", "owner_id": "o", "status": fr.TerminalStatus.failed,
              "engine": "cfmesh", "purpose": "external_cfd", "dimensionality": "3D",
              "approved_snapshot_id": "", "executor_success": False, "reviewer_verdict": "",
              "failed_gate": "", "api_failure": "", "attempts": 3, "attempts_max": 3,
              "required_ready": False, "delivered_types": [], "optional_warnings": []}
    made = fr.build_final_result(**common, attempts_made=1)
    assert made.attempts == 1
    # the category is still judged against the budget the retry counter spent
    assert made.failure_category == "attempts_exhausted"
    assert "Attempts used: 1/3." in fr.render_message(made)
    assert fr.build_final_result(**common).attempts == 3


# the builder's own notes


def test_the_closing_note_says_mesh_built_only_when_a_mesh_is_on_disk(tmp_path):
    from types import SimpleNamespace

    from meshpipeline.agents.builder.agent import _closing_note
    a = SimpleNamespace(engine="cfmesh", workspace=tmp_path)
    assert _closing_note(a, 0, 0) == ("No mesh was built: the mesher was not started in this "
                                      "attempt")
    assert _closing_note(a, 4, 1) == "No mesh came out of this attempt"
    assert _closing_note(a, 4, None) == "No mesh came out of this attempt"
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("")
    assert _closing_note(a, 4, 1) == "Mesh built - 4 steps"
    assert _closing_note(a, 0, 1) == "Mesh built"


class _Pub:
    def __init__(self):
        self.calls: list = []

    async def ameshing(self, *a, **k):
        self.calls.append(("meshing", a))

    async def ameshed(self, *a, **k):
        self.calls.append(("meshed", a))

    async def awarn(self, text, **k):
        self.calls.append(("warn", text))


def _tool_executor(monkeypatch, tmp_path, *, refusal=None, result=None):
    import meshpipeline.agents.builder.executor as bx
    from meshpipeline.agents.builder.tools import meshing as M
    ctx = type("Ctx", (), {"job_id": "job-1", "engine": "cfmesh", "workspace": tmp_path,
                           "loop_deadline": None})()
    monkeypatch.setattr(bx, "prepare_mesh_run", lambda _c: (
        M.PreparedMeshRun(refusal=refusal) if refusal is not None else
        M.PreparedMeshRun(engine="cfmesh", cap=60)))
    monkeypatch.setattr(bx, "dispatch_prepared_mesh", lambda _c, _p: json.dumps(result))
    monkeypatch.setattr(bx, "_compress_tool_output", lambda *a, **k: "")
    monkeypatch.setattr(bx, "get_spec_run_files", lambda e: ())
    pub = _Pub()
    return bx.BuilderToolExecutor(context=ctx, publish=pub), pub


def test_a_run_refused_before_any_mesher_started_publishes_nothing_about_a_mesh(monkeypatch,
                                                                                tmp_path):
    ex, pub = _tool_executor(monkeypatch, tmp_path,
                             refusal={"success": False, "identical_rerun": True})
    asyncio.run(ex.run("run_mesh", {}, call_index=0))
    assert pub.calls == [], "a refusal before the mesher published a mesh outcome"


@pytest.mark.parametrize("result,words", [
    ({"success": False, "rc": 1, "fatal_defects": ["neg"]},
     "The mesh came back with defects - reworking it"),
    ({"success": False, "timed_out": True},
     "The mesher ran out of time before it finished - reworking it smaller"),
    ({"success": False, "system_failure": True, "rc": -3},
     "The mesh run did not complete on our side - trying it again"),
    ({"success": False, "patch_contract_mismatch": True, "rc": -5},
     "The mesher was not started: the case did not build the boundaries you approved - "
     "correcting it"),
    ({"success": True, "cells": 10}, None)])
def test_an_announced_run_is_told_as_what_came_back(monkeypatch, tmp_path, result, words):
    ex, pub = _tool_executor(monkeypatch, tmp_path, result=result)
    asyncio.run(ex.run("run_mesh", {}, call_index=0))
    kinds = [c[0] for c in pub.calls]
    assert kinds[:2] == ["meshing", "meshed"]
    warns = [c[1] for c in pub.calls if c[0] == "warn"]
    assert warns == ([words] if words else [])


# no identical retries


def _case(ws: Path, spec: str = "maxCellSize 0.1;") -> Path:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "meshDict").write_text(spec)
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    (ws / "constant" / "triSurface" / "geom.stl").write_text("solid x\nendsolid x\n")
    return ws


def test_an_identical_case_from_an_earlier_attempt_is_refused_before_the_mesher(tmp_path):
    from meshpipeline.agents.builder import rerun_guard as g
    a1, a2 = _case(tmp_path / "gen" / "attempt_1"), _case(tmp_path / "gen" / "attempt_2")
    d1 = g.case_digest(a1, ("system/meshDict",))
    g.remember(a1, d1, {"success": False, "rc": 0, "fatal_defects": ["negative volume"]})
    d2 = g.case_digest(a2, ("system/meshDict",))
    assert d1 == d2 and d1
    earlier = g.earlier_identical(a2, d2)
    assert earlier == {"attempt": "attempt_1", "outcome": "its mesh did not pass its checks"}
    r = g.refusal(earlier)
    assert r["identical_rerun"] is True and r["success"] is False
    assert "already ran in attempt 1" in r["guidance"] and "Change something" in r["guidance"]
    # the same attempt's own runs are the engine's business, never matched
    assert g.earlier_identical(a1, d1) is None


def test_a_changed_case_or_another_run_is_not_refused(tmp_path):
    from meshpipeline.agents.builder import rerun_guard as g
    a1 = _case(tmp_path / "gen" / "attempt_1")
    g.remember(a1, g.case_digest(a1, ("system/meshDict",)), {"success": False, "rc": 1})
    a2 = _case(tmp_path / "gen" / "attempt_2", spec="maxCellSize 0.05;")
    assert g.earlier_identical(a2, g.case_digest(a2, ("system/meshDict",))) is None
    # a staged surface that changed is a changed case too
    a3 = _case(tmp_path / "gen" / "attempt_3")
    (a3 / "constant" / "triSurface" / "geom.stl").write_text("solid y\nendsolid y\n")
    assert g.earlier_identical(a3, g.case_digest(a3, ("system/meshDict",))) is None
    # another run's generation never matches
    other = _case(tmp_path / "other" / "attempt_2")
    assert g.earlier_identical(other, g.case_digest(other, ("system/meshDict",))) is None


@pytest.mark.parametrize("result", [{"success": False, "system_failure": True, "rc": -3},
                                    {"success": False, "patch_contract_mismatch": True}])
def test_a_run_that_reached_no_verdict_may_run_again_unchanged(tmp_path, result):
    from meshpipeline.agents.builder import rerun_guard as g
    a1, a2 = _case(tmp_path / "g" / "attempt_1"), _case(tmp_path / "g" / "attempt_2")
    g.remember(a1, g.case_digest(a1, ("system/meshDict",)), result)
    assert g.earlier_identical(a2, g.case_digest(a2, ("system/meshDict",))) is None


def test_prepare_refuses_the_identical_retry_before_the_announcement(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import rerun_guard as g
    from meshpipeline.agents.builder.tools import meshing as M
    a1, a2 = _case(tmp_path / "gen" / "attempt_1"), _case(tmp_path / "gen" / "attempt_2")
    g.remember(a1, g.case_digest(a1, ("system/meshDict",)), {"success": False, "rc": 1})
    ctx = type("Ctx", (), {"job_id": "j", "engine": "cfmesh", "workspace": a2,
                           "loop_deadline": None, "geometry": None,
                           "require_geometry": lambda self: None})()
    monkeypatch.setattr(M, "input_contract_rejection", lambda *a, **k: "")
    prepared = M.prepare_mesh_run(ctx)
    assert prepared.refusal is not None and prepared.refusal["identical_rerun"] is True
