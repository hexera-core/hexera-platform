# Responsibility: Verify every reviewer execution event is published under an ownership check.
# Boundaries: the reviewer publisher-object closure - intake keeps the synchronous route, terminal stays plain.
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid

import pytest

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)

SRC = pathlib.Path(visual.__file__).resolve().parents[2]

#: The reviewer's own publications, and where they live.
REVIEWER = {
    "agents/reviewer/visual.py": {"astage": 1, "anote": 1, "averdict": 1, "awarn": 1,
                                  "ascreenshot": 1},
}

#: Published after execution ownership has ended, so they must stay plain and ungated.
POST_OWNERSHIP = {"application/pipeline_run.py": 5, "application/maintenance/cleanup.py": 1}

SYNC_EVENTS = {"stage", "note", "check", "warn", "error", "verdict", "screenshot", "rationale",
               "reasoning", "attempt", "meshed", "meshing", "file", "search", "action",
               "tool_call", "tool_result"}


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text())


def _publisher_calls(rel: str, tree=None):
    for node in ast.walk(tree if tree is not None else _tree(rel)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        recv = node.func.value
        name = (recv.id if isinstance(recv, ast.Name)
                else recv.attr if isinstance(recv, ast.Attribute)
                else ast.unparse(recv) if isinstance(recv, ast.Call) else "")
        if "pub" not in name.lower() and "publish" not in name.lower():
            continue
        yield node


# the reviewer execution closure


@pytest.mark.parametrize("rel", sorted(REVIEWER))
def test_the_reviewer_publishes_exactly_its_gated_events(rel):
    counted: dict[str, int] = {}
    for call in _publisher_calls(rel):
        counted[call.func.attr] = counted.get(call.func.attr, 0) + 1
    assert counted == REVIEWER[rel], (
        f"{rel} no longer publishes what the reviewer closure says it does\n"
        f"  expected: {REVIEWER[rel]}\n  found:    {counted}")


def test_the_reviewer_root_builds_the_ownership_checked_publisher():
    src = (SRC / "agents/reviewer/visual.py").read_text()
    assert "execution_publisher(" in src, "the reviewer no longer builds an execution publisher"
    built = [n.lineno for n in ast.walk(_tree("agents/reviewer/visual.py"))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "publisher"]
    assert built == [], f"the reviewer still constructs a plain publisher at {built}"


@pytest.mark.parametrize("rel", sorted(REVIEWER))
def test_no_synchronous_reviewer_publication_remains(rel):
    stragglers = [f"{rel}:{c.lineno} {c.func.attr}" for c in _publisher_calls(rel)
                  if c.func.attr in SYNC_EVENTS]
    assert stragglers == [], f"these reach Redis without an ownership check: {stragglers}"


@pytest.mark.parametrize("rel", sorted(REVIEWER))
def test_every_reviewer_publication_is_awaited(rel):
    tree = _tree(rel)
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    unawaited = [f"{rel}:{c.lineno} {c.func.attr}" for c in _publisher_calls(rel, tree)
                 if c.func.attr.startswith("a") and id(c) not in awaited]
    assert unawaited == [], f"a gated reviewer publication is not awaited: {unawaited}"


def test_the_reviewer_traces_through_the_execution_context():
    src = (SRC / "agents/reviewer/unified.py").read_text()
    assert "ExecutionTraceContext(" in src, "the reviewer no longer builds an execution trace"
    assert "execution_trace=" in src
    assert "TraceContext(publisher" not in src.replace("ExecutionTraceContext(publisher", ""), \
        "the reviewer still builds a plain trace context"


def test_the_reviewer_never_reaches_around_the_gate():
    for rel in (*REVIEWER, "agents/reviewer/unified.py"):
        src = (SRC / rel).read_text()
        assert "_inner" not in src, f"{rel} reaches around the ownership check"
        assert 'getattr(self.executor, "_publish"' not in src


# intake is untouched


def test_intake_keeps_its_synchronous_route():
    src = (SRC / "agents/intake/agent.py").read_text()
    assert "ExecutionTraceContext" not in src, "intake was migrated with the reviewer"
    assert "execution_trace=" not in src
    assert "TraceContext(publisher" in src.replace("ExecutionTraceContext(publisher", ""), \
        "intake stopped building the synchronous trace context"


# the post-ownership authorities stay plain


@pytest.mark.parametrize("rel,count", sorted(POST_OWNERSHIP.items()))
def test_the_closing_events_are_still_ungated(rel, count):
    calls = [c for c in _publisher_calls(rel) if c.func.attr in ("closing", "aclosing")]
    assert len(calls) == count, f"{rel} has {len(calls)} closing events, expected {count}"
    gated = [f"{rel}:{c.lineno}" for c in calls if c.func.attr == "aclosing"]
    assert gated == [], (
        "these publish precisely when the claim is gone; gating them would make them "
        f"unpublishable: {gated}")


# behaviour


class _Recorder:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))
        return record


class _Own:
    def __init__(self, job_id="job-1", generation=2):
        self.job_id = job_id
        self.execution_generation = generation
        self.worker_token = uuid.UUID(int=7)


def _bind(monkeypatch, *, own, current=True):
    import meshpipeline.application.execution_publisher as mod

    async def is_current_owner():
        return current

    monkeypatch.setattr(mod._fence, "current_ownership", lambda: own)
    monkeypatch.setattr(mod._fence, "is_current_owner", is_current_owner)


def test_the_inspection_image_publishes_once_under_ownership(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    from meshpipeline.trace import policy as _policy
    monkeypatch.setattr(_policy, "current_mode", lambda: _policy.RAW)

    asyncio.run(visual._publish_inspection_image(
        OwnershipCheckedPublisher(inner), "aGVsbG8=", op_id="inspect:1"))

    assert [c[0] for c in inner.calls] == ["screenshot"]
    assert inner.calls[0][1][0] == "aGVsbG8="
    assert inner.calls[0][2]["op_id"] == "inspect:1"


@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_a_lost_claim_stops_the_inspection_image_before_the_publisher(monkeypatch, case):
    inner = _Recorder()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)
    from meshpipeline.trace import policy as _policy
    monkeypatch.setattr(_policy, "current_mode", lambda: _policy.RAW)

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(visual._publish_inspection_image(
            OwnershipCheckedPublisher(inner), "aGVsbG8=", op_id="inspect:1"))
    assert inner.calls == [], f"{case}: the screenshot reached the publisher"


def test_a_transport_failure_still_never_costs_a_review(monkeypatch):
    class _Broken(_Recorder):
        def __getattr__(self, name):
            def boom(*_a, **_k):
                raise RuntimeError("redis is down")
            return boom

    _bind(monkeypatch, own=_Own())
    from meshpipeline.trace import policy as _policy
    monkeypatch.setattr(_policy, "current_mode", lambda: _policy.RAW)
    asyncio.run(visual._publish_inspection_image(
        OwnershipCheckedPublisher(_Broken()), "aGVsbG8="))      # must not raise


def test_safe_mode_still_publishes_no_image(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    from meshpipeline.trace import policy as _policy
    monkeypatch.setattr(_policy, "current_mode", lambda: "SAFE")
    asyncio.run(visual._publish_inspection_image(
        OwnershipCheckedPublisher(inner), "aGVsbG8="))
    assert inner.calls == [], "a non-raw trace mode published the inspection image"


class _Rationale:
    # No catch-all: the once-per-run bookkeeping probes the publisher with getattr, and a
    # stand-in that answers every name defeats it.
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    def rationale(self, conclusion, because=""):
        self.calls.append(("rationale", conclusion, because))


def test_the_reviewer_rationale_counterparts_say_the_same_thing(monkeypatch):
    from meshpipeline.contracts import rationale as R

    for passed in (True, False):
        sync_inner, async_inner = _Rationale(), _Rationale()
        R.reviewer_verdict(sync_inner, passed=passed, failed_axes=("skew",))
        _bind(monkeypatch, own=_Own(job_id=async_inner.job_id))
        asyncio.run(R.areviewer_verdict(OwnershipCheckedPublisher(async_inner),
                                        passed=passed, failed_axes=("skew",)))
        assert sync_inner.calls == async_inner.calls, (sync_inner.calls, async_inner.calls)

    sync_inner, async_inner = _Rationale(), _Rationale()
    R.reviewer_evidence_incomplete(sync_inner)
    _bind(monkeypatch, own=_Own(job_id=async_inner.job_id))
    asyncio.run(R.areviewer_evidence_incomplete(OwnershipCheckedPublisher(async_inner)))
    assert sync_inner.calls == async_inner.calls
