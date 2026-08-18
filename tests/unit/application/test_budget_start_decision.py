# Responsibility: Verify the run budget is anchored on the durable claim, and an exhausted run is refused blamelessly.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.application import pipeline_budget as pb
from meshpipeline.persistence.models import JobStatus

JOB = str(uuid.uuid4())


class _Own:
    def __init__(self, deadline=None, generation=1):
        self.pipeline_deadline_at = deadline
        self.execution_generation = generation


class _Log:
    def __init__(self): self.warnings = []; self.errors = []
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): self.errors.append(a)
    def info(self, *a, **k): pass


def _at(offset_s: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset_s)


# the anchor

def test_the_deadline_comes_from_the_durable_claim_anchor():
    anchor = _at(600)
    created_long_ago = datetime.now(UTC) - timedelta(days=7)
    d = pb.decide_start(_Own(anchor), created_long_ago)
    assert d.may_start, "a run was refused on created_at despite a live durable anchor"
    assert abs(d.deadline_epoch - anchor.timestamp()) < 1e-6


def test_created_at_is_only_the_degenerate_fallback():
    d = pb.decide_start(_Own(None), datetime.now(UTC))
    assert abs(d.deadline_epoch - pb.deadline_epoch_from_created_at(datetime.now(UTC))) < 5


def test_no_ownership_still_anchors_from_created_at():
    d = pb.decide_start(None, datetime.now(UTC))
    assert d.may_start and d.deadline_epoch > 0


# the verdict, incl. boundary

def test_a_run_well_inside_its_budget_may_start():
    assert pb.decide_start(_Own(_at(3600)), None).may_start


def test_a_run_past_its_budget_is_refused():
    d = pb.decide_start(_Own(_at(-1)), None)
    assert d.exhausted and d.reason == "pipeline_timed_out" and not d.may_start


def test_the_boundary_is_the_final_permitted_instant():
    now = 1_000_000.0
    deadline = now
    assert pb.is_exhausted(deadline, now=now), "the instant the budget expires must be exhausted"
    assert not pb.is_exhausted(deadline + 0.001, now=now), (
        "a run was refused one millisecond before its budget expired")


def test_remaining_seconds_agrees_with_the_verdict():
    now = 1_000_000.0
    assert pb.remaining_seconds(now + 30, now=now) == pytest.approx(30)
    assert pb.remaining_seconds(now - 30, now=now) <= 0


def test_a_missing_created_at_does_not_crash_the_decision():
    d = pb.decide_start(_Own(None), None)
    assert isinstance(d.exhausted, bool)


# policy owns no side effect

def test_the_budget_module_persists_nothing_and_publishes_nothing():
    import ast
    import inspect
    import textwrap

    for fn in (pb.decide_start, pb.anchor_deadline):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    ast.get_docstring(node) is not None:
                node.body = node.body[1:]
        src = ast.unparse(tree)
        for effect in ("session", "commit", "publish", "record_dead_letter", "transition"):
            assert effect not in src, f"{fn.__name__} performs a side effect: {effect}"


def test_the_exhaustion_message_is_blameless_and_free_of_internals():
    msg = pb.EXHAUSTED_MESSAGE
    assert msg and "geometry" not in msg.lower()
    for internal in ("deadline_epoch", "PIPELINE_TOTAL_TIMEOUT", "created_at", "traceback"):
        assert internal not in msg


# the durable refusal

class _S:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _Repo:
    def __init__(self): self.transitions = []; self.row = type("R", (), {"failed_reason": None})()
    async def transition(self, db, jid, status):
        from meshpipeline.persistence.job_state import TransitionResult
        self.transitions.append(status)
        return TransitionResult.applied
    async def get_internal(self, db, jid): return self.row


async def test_an_exhausted_run_is_durably_failed_and_told(monkeypatch):
    from meshpipeline.application.terminal_finalize import refuse_before_execution
    from meshpipeline.errors import FailureClass

    seen: list = []
    published: list = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: seen.append(a))
    repo = _Repo()
    out = await refuse_before_execution(
        lambda: _S(), job_id=JOB, failure_class=FailureClass.INTERNAL, dependency="pipeline",
        detail="pipeline total budget exhausted before start",
        user_message=pb.EXHAUSTED_MESSAGE, reason="pipeline_timed_out",
        job_repo=repo, jlog=_Log(), publish=published.append)
    assert out == {"job_id": JOB, "status": "failed", "reason": "pipeline_timed_out"}
    assert JobStatus.failed in repo.transitions and repo.row.failed_reason is not None
    assert seen, "no dead letter recorded"
    assert published == [pb.EXHAUSTED_MESSAGE]


async def test_a_refusal_survives_a_database_that_will_not_transition(monkeypatch):
    import meshpipeline.errors as errs
    from meshpipeline.application.terminal_finalize import refuse_before_execution
    from meshpipeline.errors import FailureClass
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: None)

    class _Boom(_Repo):
        async def transition(self, *a, **k): raise RuntimeError("db down")

    log = _Log()
    out = await refuse_before_execution(
        lambda: _S(), job_id=JOB, failure_class=FailureClass.INTERNAL, dependency="pipeline",
        detail="d", user_message="m", reason="pipeline_timed_out",
        job_repo=_Boom(), jlog=log, publish=lambda m: None)
    assert out["status"] == "failed" and log.warnings


# no work after exhaustion

def test_the_run_returns_before_building_the_graph_when_exhausted():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    exhausted_at = src.index("if _start.exhausted:")
    build_at = src.index("build_graph(")
    assert exhausted_at < build_at, (
        "the exhaustion check runs after the graph is built - expensive work would already have "
        "started")
    branch = src[exhausted_at:src.index("return _refusal") + len("return _refusal")]
    assert "ainvoke" not in branch and "deliver_succeeded_run" not in branch, (
        "the exhausted branch reaches the graph or artifact delivery")


def test_the_orchestrator_no_longer_owns_the_budget_policy():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    # The run may still ASK the authority a question at terminal time (was this run over budget
    # when it ended?) - that is a call, not a re-implementation. What it must not do is anchor the
    # deadline or decide the start itself.
    start = src[:src.index("if _start.exhausted:")]
    assert "is_exhausted" not in start, "the run evaluates the start budget itself again"
    assert "deadline_epoch_from_created_at" not in src, "the run anchors the deadline itself again"
    assert "decide_start" in src and "refuse_before_execution" in src
    assert src.count("_pb.is_exhausted") == 1, (
        "the budget is queried in more than one place; the start decision has drifted back")
