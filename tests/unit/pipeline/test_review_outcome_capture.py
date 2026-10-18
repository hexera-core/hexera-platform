# Responsibility: Verify what became of an engineer's directives is logged, written back onto their items, and carried as history when the defect comes back.
# Boundaries: the node's training row, the join, and write_review against a recording repo; no database.
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.unit.pipeline.test_repair_attempt_minting import (
    _entity,
    _on,
    _op,
    _report,
    _scripted,
    _state,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.application.repair_review_park import ReviewPlan, write_review
from meshpipeline.pipeline.repair_attempt import node_repair_attempt

_AUTOUSE = _on  # the autouse fixture that switches repair on, reused as is

ITEM = str(uuid.uuid4())


def _directive(item_id=ITEM, target="fa") -> dict:
    return {"item_id": item_id, "source": "engineer", "op": "merge_faces",
            "defect_code": "small_face", "targets": [target]}


@pytest.fixture(autouse=True)
def _review_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)


@pytest.fixture
def logged(monkeypatch):
    rows = []

    class _Log:
        def __init__(self, job_id): ...
        def log(self, kind, op_id="", payload=None):
            rows.append(payload)

    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Log)
    return rows


def _run(tmp_path, monkeypatch, report, directives):
    _scripted(monkeypatch, report=report)
    state = _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                   _entity("open_shell", "error", "edge", 8, "eb"))
    state["repair_directives"] = directives
    return asyncio.run(node_repair_attempt(state))


def test_a_kept_directive_marks_its_item_and_logs_the_outcome(tmp_path, monkeypatch, logged):
    out = _run(tmp_path, monkeypatch,
               _report(operations=[_op("merge_faces", "kept", "small_face", "fa")],
                       operations_kept=1), [_directive()])
    assert logged[-1]["directives"] == [
        {"item_id": ITEM, "source": "engineer", "outcome": "kept", "reason": ""}]
    assert out["repair_directive_outcomes"][0]["outcome"] == "kept"


def test_a_rolled_back_directive_reopens_its_item_with_history(tmp_path, monkeypatch, logged):
    bad = {**_op("merge_faces", "rolled_back", "small_face", "fa"), "reason": "moved the part"}
    _run(tmp_path, monkeypatch, _report(operations=[bad, _op("x", "kept", "small_face", "fb")],
                                        operations_kept=1), [_directive()])
    outcomes = logged[-1]["directives"]
    assert outcomes[0]["outcome"] == "rolled_back" and outcomes[0]["reason"] == "moved the part"

    prior = SimpleNamespace(fingerprint="fa", state="accepted", decided_at=datetime.now(UTC),
                            decision={"by": "eng", "action": "accept"})
    repo = _Repo([prior])
    plan = ReviewPlan(items=[{"fingerprint": "fa"}, {"fingerprint": "zz"}],
                      outcomes=[{"item_id": ITEM, **outcomes[0]}])
    asyncio.run(write_review(_Db(), uuid.uuid4(), plan, repo))
    assert repo.outcomes[0][1]["outcome"] == "rolled_back"
    first, second = repo.added
    assert first["decision_history"][0]["action"] == "accept"
    assert "decision_history" not in second


def test_unplayable_directives_reopen_as_could_not_be_done_safely():
    from meshpipeline.application.repair_review_minting import directive_outcomes
    gone = _directive(target="gone")
    outcomes = directive_outcomes(
        [gone], [], [{"refusal": "directive_unplayable", "reason": "target_absent",
                      "directive": gone}])
    assert outcomes == [{"item_id": ITEM, "source": "engineer", "outcome": "unplayable",
                         "reason": "target_absent"}]
    repo = _Repo([SimpleNamespace(fingerprint="gone", state="redirected",
                                  decided_at=None, decision={"outcome": "unplayable"})],
                 fail=True)
    plan = ReviewPlan(items=[{"fingerprint": "gone"}], outcomes=[{"item_id": ITEM, **outcomes[0]}])
    # a failing lookup never blocks the park: the items are still written, without history
    asyncio.run(write_review(_Db(), uuid.uuid4(), plan, repo))
    assert repo.added == [{"fingerprint": "gone"}]


class _Sp:
    async def commit(self): ...
    async def rollback(self): ...


class _Db:
    async def begin_nested(self):
        return _Sp()


class _Repo:
    def __init__(self, decided, fail=False):
        self.decided, self.fail, self.outcomes, self.added = decided, fail, [], []

    async def record_outcome(self, db, item_id, outcome):
        self.outcomes.append((item_id, outcome))

    async def decided_items(self, db, job_id):
        if self.fail:
            raise RuntimeError("lookup failed")
        return self.decided

    async def open_park(self, db, job_id):
        return None

    async def add_items(self, db, job_id, park_id, items):
        self.added = list(items)
