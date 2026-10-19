# Responsibility: Verify the repair node's three LLM-planner postures: off is untouched, shadow only observes, live merges.
# Boundaries: the kernel repair and the model seam are scripted doubles; no database, no real model.
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from tests.unit.pipeline.test_repair_attempt_minting import (
    _entity,
    _on,
    _report,
    _state,
)
from tests.unit.pipeline.test_repair_attempt_minting import (
    _op as _op_base,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RepairRefused
from meshpipeline.contracts import model_inference
from meshpipeline.pipeline.repair_attempt import node_repair_attempt

_AUTOUSE = _on
ITEM = str(uuid.uuid4())


class _Router:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        self.calls += 1
        return SimpleNamespace(ok=True, assistant_text=json.dumps(self.reply))


def _proposals(*ops):
    return {"proposals": [{"op": op, "defect_fingerprint": fp, "targets": [fp],
                           "confidence": 0.8, "rationale": "r"} for op, fp in ops]}


@pytest.fixture(autouse=True)
def _review_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    yield
    model_inference.set_model_router(None)


@pytest.fixture
def rows(monkeypatch):
    seen = []

    class _Log:
        def __init__(self, job_id): ...
        def log(self, kind, payload=None, op_id="", **kw):
            seen.append((kind, op_id, payload))

    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Log)
    return seen


@pytest.fixture
def kernel(monkeypatch):
    """A scripted repair_step_file recording the directives it was handed."""
    box = SimpleNamespace(directives=[], report=None, raises=None)

    def _repair(source, destination, *, directives=(), **kw):
        box.directives.append(list(directives))
        if box.raises:
            raise box.raises
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return box.report

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    return box


def _kept(name, code, target, **extra):
    return {**_op_base(name, "kept", code, target), **extra}


def _good_report(**ops_extra):
    return _report(operations=[_kept("sew_faces", "open_shell", "eb", **ops_extra)],
                   stopped_because="no_candidate", resolved=False, refusals=[],
                   planner="rules-v1", operations_attempted=1, operations_kept=1)


def _run(tmp_path, directives=None, state=None):
    if state is None:
        state = _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                       _entity("open_shell", "error", "edge", 8, "eb"))
    if directives:
        state["repair_directives"] = directives
    return asyncio.run(node_repair_attempt(state))


def _engineer():
    return {"item_id": ITEM, "source": "engineer", "op": "merge_faces",
            "defect_code": "small_face", "targets": ["fa"]}


def test_off_is_byte_for_byte_and_never_consults_the_model(tmp_path, monkeypatch, rows, kernel):
    import subprocess
    import sys
    # a clean interpreter: an off run must not even import the planner module
    code = (
        "import sys\n"
        "import meshpipeline.settings.cad_repair as rcfg\n"
        "from meshpipeline.pipeline.repair_attempt import node_repair_attempt\n"
        "assert rcfg.CAD_REPAIR_LLM_PLANNER == 'off'\n"
        "assert 'meshpipeline.agents.repair_review.planner' not in sys.modules\n")
    subprocess.run([sys.executable, "-c", code], check=True)

    router = _Router(_proposals(("sew_faces", "eb")))
    model_inference.set_model_router(router)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "off")
    monkeypatch.delitem(sys.modules, "meshpipeline.agents.repair_review.planner", raising=False)
    kernel.report = _good_report()
    out = _run(tmp_path, [_engineer()])
    assert router.calls == 0
    assert "meshpipeline.agents.repair_review.planner" not in sys.modules
    assert kernel.directives == [[_engineer()]]
    assert [r[0] for r in rows] == ["repair_attempt"]
    expected = {
        "geometry": out["geometry"], "repair_lineage": out["repair_lineage"],
        "repair_review_candidates": out["repair_review_candidates"],
        "repair_directive_outcomes": [
            {"item_id": ITEM, "source": "engineer", "outcome": "not_reached",
             "reason": "not_reached"}],
        "repair_attempt": {
            "attempted": True, "repair_status": "repaired", "resolved": False,
            "stopped_because": "no_candidate", "route": out["repair_attempt"]["route"],
            "operations": [_kept("sew_faces", "open_shell", "eb")],
            "targets": out["repair_attempt"]["targets"], "refusals": [],
            "defects_remaining": [],
            "summary": "Repaired within the conservative caps."},
    }
    assert out == expected


@pytest.mark.parametrize("exit_", ["success", "refusal", "timeout", "unavailable", "generic", "promotion", "adopt"])
def test_shadow_logs_one_row_on_success_refusal_and_early_decline(
        tmp_path, monkeypatch, rows, kernel, exit_):
    router = _Router(_proposals(("sew_faces", "eb")))
    model_inference.set_model_router(router)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "shadow")
    if exit_ == "success":
        kernel.report = _good_report()
    elif exit_ == "refusal":
        kernel.raises = RepairRefused("nothing was kept", measurements={
            "operations": [_op_base("sew_faces", "rolled_back", "open_shell", "eb")],
            "stopped_because": "stalled", "refusals": []})
    elif exit_ == "timeout":
        kernel.raises = TimeoutError()
    elif exit_ == "unavailable":
        from meshpipeline.cad.repair.conservative import RepairUnavailable
        kernel.raises = RepairUnavailable("no kernel")
    elif exit_ == "generic":
        kernel.raises = RuntimeError("boom")
    else:
        kernel.report = _good_report()
        if exit_ == "promotion":
            from meshpipeline.pipeline.repair_promote import PromotionRefused

            def _refuse(*a, **kw):
                raise PromotionRefused("nope")
            monkeypatch.setattr("meshpipeline.pipeline.repair_promote.promote", _refuse)
        else:
            def _boom(*a, **kw):
                raise OSError("unreadable")
            monkeypatch.setattr("meshpipeline.pipeline.repair_attempt._derived_source", _boom)
    _run(tmp_path)

    assert router.calls >= 1
    assert kernel.directives == [[]]                      # shadow never reaches the geometry
    shadow = [r for r in rows if r[0] == "planner_shadow"]
    assert len(shadow) == 1 and shadow[0][1] == "review:shadow"
    payload = shadow[0][2]
    assert [p["op"] for p in payload["proposals"]] == ["sew_faces"]
    assert payload["declines"] == [] and payload["call_failures"] == []
    if exit_ not in ("success", "refusal"):
        assert payload["outcome"] == "not_executed" and payload["operations"] == []
    elif exit_ == "refusal":
        assert payload["outcome"] == "refused" and payload["stopped_because"] == "stalled"
        assert payload["operations"][0]["status"] == "rolled_back"
    else:
        assert payload["outcome"] == "executed" and payload["resolved"] is False
        assert payload["stopped_because"] == "no_candidate"
        assert payload["operations"][0]["status"] == "kept"


def test_live_merges_engineer_first_then_llm_and_outcomes_join_by_directive_id(
        tmp_path, monkeypatch, rows, kernel):
    model_inference.set_model_router(_Router(_proposals(("sew_faces", "eb"))))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "live")
    kernel.report = _report(
        operations=[_kept("merge_faces", "small_face", "fa", item_id=ITEM, source="engineer"),
                    _kept("sew_faces", "open_shell", "eb", source="llm-planner",
                          directive_id="llm:0", planner_confidence=0.8)],
        stopped_because="no_candidate", resolved=False, refusals=[], planner="rules-v1",
        operations_attempted=2, operations_kept=2)
    out = _run(tmp_path, [_engineer()])

    handed = kernel.directives[0]
    assert [d.get("item_id") or d.get("directive_id") for d in handed] == [ITEM, "llm:0"]
    logged = [r for r in rows if r[0] == "repair_attempt"][-1][2]["directives"]
    assert logged[0]["outcome"] == "kept" and logged[0]["item_id"] == ITEM
    assert logged[1] == {"item_id": "", "source": "llm-planner", "directive_id": "llm:0",
                         "planner_confidence": 0.8, "outcome": "kept", "reason": ""}
    # only the engineer's directive has an item to write back onto
    assert [o["item_id"] for o in out["repair_directive_outcomes"]] == [ITEM]
    assert not [r for r in rows if r[0] == "planner_shadow"]


def test_live_with_a_declining_model_matches_off_except_the_decline_log(
        tmp_path, monkeypatch, rows, kernel):
    kernel.report = _good_report()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "off")
    state = _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                   _entity("open_shell", "error", "edge", 8, "eb"))
    off_out = _run(tmp_path, [_engineer()], state)
    off_rows = [r for r in rows if r[0] == "repair_attempt"]
    rows.clear()
    kernel.directives.clear()

    model_inference.set_model_router(_Router({"proposals": []}))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "live")
    live_out = _run(tmp_path, [_engineer()], state)
    assert live_out == off_out
    assert kernel.directives == [[_engineer()]]
    assert [r for r in rows if r[0] == "repair_attempt"] == off_rows
    assert {r[0] for r in rows} == {"repair_attempt", "repair_review"}


def test_a_rolled_back_llm_directive_never_touches_review_items():
    from tests.unit.pipeline.test_review_outcome_capture import _Db, _Repo

    from meshpipeline.application.repair_review_minting import directive_outcomes
    from meshpipeline.application.repair_review_park import ReviewPlan, write_review
    from meshpipeline.pipeline.repair_attempt import _outcome_state

    llm = {"item_id": "", "source": "llm-planner", "directive_id": "llm:0", "op": "sew_faces",
           "defect_code": "open_shell", "targets": ["eb"], "planner_confidence": 0.7}
    bad = {**_op_base("sew_faces", "rolled_back", "open_shell", "eb"), "reason": "moved the part",
           "source": "llm-planner", "directive_id": "llm:0"}
    outcomes = directive_outcomes([llm], [bad], [])
    assert outcomes[0]["outcome"] == "rolled_back" and outcomes[0]["directive_id"] == "llm:0"
    assert not _outcome_state(outcomes).get("repair_directive_outcomes")
    # even if one reached the writer, an absent / non-UUID item_id is skipped
    repo = _Repo([])
    plan = ReviewPlan(items=[], outcomes=outcomes + [{"item_id": "llm:0", "outcome": "kept"}])
    asyncio.run(write_review(_Db(), uuid.uuid4(), plan, repo))
    assert repo.outcomes == []


def test_a_replay_cannot_steal_an_llm_operations_row():
    from meshpipeline.application.repair_review_minting import directive_outcomes
    replay = {"item_id": "", "source": "replay", "op": "sew_faces", "targets": ["eb"]}
    llm = {"item_id": "", "source": "llm-planner", "directive_id": "llm:0", "op": "sew_faces",
           "targets": ["eb"], "planner_confidence": 0.8}
    ran = {**_op_base("sew_faces", "kept", "open_shell", "eb"), "source": "llm-planner",
           "directive_id": "llm:0"}
    out = directive_outcomes([replay, llm], [ran], [])
    assert out[0]["outcome"] == "not_reached"
    assert (out[1]["directive_id"], out[1]["outcome"]) == ("llm:0", "kept")
