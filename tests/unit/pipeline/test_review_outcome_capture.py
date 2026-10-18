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
    _report,
    _scripted,
    _state,
)
from tests.unit.pipeline.test_repair_attempt_minting import (
    _op as _op_base,
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


def _op(name, status, code, target, **extra):
    """An attempt entry as the loop writes it; a directive's carries its item_id and source."""
    return {**_op_base(name, status, code, target), **extra}


def _directed(*args, item_id=ITEM, **kw):
    return _op(*args, item_id=item_id, source="engineer", **kw)


def _run(tmp_path, monkeypatch, report, directives):
    _scripted(monkeypatch, report=report)
    state = _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                   _entity("open_shell", "error", "edge", 8, "eb"))
    state["repair_directives"] = directives
    return asyncio.run(node_repair_attempt(state))


def test_a_kept_directive_marks_its_item_and_logs_the_outcome(tmp_path, monkeypatch, logged):
    out = _run(tmp_path, monkeypatch,
               _report(operations=[_directed("merge_faces", "kept", "small_face", "fa")],
                       operations_kept=1), [_directive()])
    assert logged[-1]["directives"] == [
        {"item_id": ITEM, "source": "engineer", "outcome": "kept", "reason": ""}]
    assert out["repair_directive_outcomes"][0]["outcome"] == "kept"


def test_a_rolled_back_directive_reopens_its_item_with_history(tmp_path, monkeypatch, logged):
    bad = _directed("merge_faces", "rolled_back", "small_face", "fa", reason="moved the part")
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
    # an unreached directive is NOT "could not be done safely"
    unreached = directive_outcomes([gone], [], [{"refusal": "directive_unplayable",
                                                 "reason": "not_reached", "directive": gone}])
    assert unreached[0]["outcome"] == "not_reached"
    repo = _Repo([SimpleNamespace(fingerprint="gone", state="redirected",
                                  decided_at=None, decision={"outcome": "unplayable"})],
                 fail=True)
    plan = ReviewPlan(items=[{"fingerprint": "gone"}], park=True, outcomes=[{"item_id": ITEM, **outcomes[0]}])
    # a failing lookup never blocks the park: the items are still written, without history
    db = _Db()
    park_id = asyncio.run(write_review(db, uuid.uuid4(), plan, repo))
    assert repo.added == [{"fingerprint": "gone"}]
    assert repo.parks == ["created"] and park_id is not None   # the park itself survived
    assert db.log == ["sp_rollback"]                           # only the history savepoint rolled back


class _Sp:
    def __init__(self, db): self.db = db
    async def commit(self): self.db.log.append("sp_commit")
    async def rollback(self): self.db.log.append("sp_rollback")


class _Db:
    def __init__(self): self.log = []
    async def begin_nested(self):
        return _Sp(self)


class _Repo:
    def __init__(self, decided, fail=False):
        self.decided, self.fail, self.outcomes, self.added = decided, fail, [], []
        self.parks = []

    async def record_outcome(self, db, item_id, outcome):
        self.outcomes.append((item_id, outcome))

    async def decided_items(self, db, job_id):
        if self.fail:
            raise RuntimeError("lookup failed")
        return self.decided

    async def open_park(self, db, job_id):
        return None

    async def create_park(self, db, job_id, kept, sha, session):
        self.parks.append("created")
        return SimpleNamespace(id=uuid.uuid4())

    async def add_items(self, db, job_id, park_id, items):
        self.added = list(items)


# the join is exact

def test_two_overlapping_same_op_directives_are_attributed_by_item_id_when_one_is_not_reached():
    from meshpipeline.application.repair_review_minting import directive_outcomes
    a, b = _directive("item-a"), _directive("item-b")
    ran = _directed("merge_faces", "kept", "small_face", "fa", item_id="item-a")
    out = directive_outcomes([a, b], [ran], [{"refusal": "directive_unplayable",
                                              "reason": "not_reached", "directive": b}])
    assert [(o["item_id"], o["outcome"]) for o in out] == [("item-a", "kept"),
                                                          ("item-b", "not_reached")]
    # even with the refusal lost, b cannot claim a's entry
    out = directive_outcomes([a, b], [ran], [])
    assert [(o["item_id"], o["outcome"]) for o in out] == [("item-a", "kept"),
                                                          ("item-b", "not_reached")]


def test_an_autonomous_operation_is_never_claimed_by_a_directive():
    from meshpipeline.application.repair_review_minting import directive_outcomes
    auto = _op("merge_faces", "kept", "small_face", "fa")          # no item_id, no source
    replay = {"source": "replayed", "op": "merge_faces", "targets": ["fa"]}
    assert directive_outcomes([_directive()], [auto], [])[0]["outcome"] == "not_reached"
    assert directive_outcomes([replay], [auto], [])[0]["outcome"] == "not_reached"
    stamped = _op("merge_faces", "kept", "small_face", "fa", source="replayed")
    assert directive_outcomes([replay], [stamped], [])[0]["outcome"] == "kept"


def test_the_planner_stamps_the_directive_onto_what_it_proposes(tmp_path):
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.directives import DirectedPlanner
    from meshpipeline.cad.repair.operations import OPERATIONS
    from meshpipeline.cad.repair.plan import RunHistory
    d = SimpleNamespace(fingerprint="fa", entity_type="face", entity_index=3)
    planner = DirectedPlanner([{**_directive(), "op": OPERATIONS[0].name}])
    cand = planner.propose([d], RunHistory(), shape=None)
    assert (cand.item_id, cand.source) == (ITEM, "engineer")
    assert DefectCode("small_face")


# early declines still answer every directive

@pytest.mark.parametrize("raises", [RuntimeError("boom")])
def test_a_run_that_declines_early_logs_every_directive_as_not_reached(
        tmp_path, monkeypatch, logged, raises):
    _scripted(monkeypatch, raises=raises)
    state = _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                   _entity("open_shell", "error", "edge", 8, "eb"))
    state["repair_directives"] = [_directive()]
    out = asyncio.run(node_repair_attempt(state))
    assert logged[-1]["directives"][0]["outcome"] == "not_reached"
    assert out["repair_directive_outcomes"][0]["item_id"] == ITEM


# the key survives the real state merge and reaches the plan

def test_outcomes_reach_plan_review_through_the_state_merge(tmp_path, monkeypatch, logged):
    """LangGraph keeps only the keys PipelineState declares, so the node's output is filtered by the
    real declared fields (langgraph itself is faked in unit tests) before it reaches the plan."""
    import typing

    from meshpipeline.application.repair_review_park import plan_review
    from meshpipeline.contracts.pipeline_state import PipelineState
    from meshpipeline.persistence.models import JobStatus

    out = _run(tmp_path, monkeypatch, _report(
        operations=[_directed("merge_faces", "kept", "small_face", "fa")], operations_kept=1),
        [_directive()])
    declared = typing.get_type_hints(PipelineState)
    merged = {k: v for k, v in out.items() if k in declared}
    plan = plan_review({**merged, "repair_review_candidates": []}, status=JobStatus.failed)
    assert plan is not None and plan.park is False and plan.items == []
    assert plan.outcomes[0]["outcome"] == "kept"


def test_the_outcome_key_is_declared_in_the_contract():
    from meshpipeline.contracts.pipeline_state import PipelineState
    from meshpipeline.pipeline.data_contract import STATE_FIELDS
    assert "repair_directive_outcomes" in STATE_FIELDS
    assert "repair_directive_outcomes" in PipelineState.__annotations__


def test_a_kept_directives_prior_item_is_only_decorated_never_re_stated():
    # the write merges outcome fields into `decision` and nothing else (see the repository test);
    # write_review passes the outcome through untouched and re-mints nothing for a kept defect
    plan = ReviewPlan(items=[], outcomes=[{"item_id": ITEM, "outcome": "kept", "reason": ""}])
    repo = _Repo([])
    asyncio.run(write_review(_Db(), uuid.uuid4(), plan, repo))
    assert repo.outcomes[0][1]["outcome"] == "kept" and repo.added == [] and repo.parks == []


def test_outcomes_without_items_on_a_failed_run_leave_the_status_alone():
    from meshpipeline.application.repair_review_park import plan_review
    from meshpipeline.persistence.models import JobStatus
    plan = plan_review({"repair_directive_outcomes": [{"item_id": ITEM, "outcome": "kept"}]},
                       status=JobStatus.failed)
    assert plan.park is False and plan.items == []
    assert plan_review({"repair_directive_outcomes": [{"item_id": ITEM}]},
                       status=JobStatus.failed, system_failure=True) is None
