# Responsibility: Verify every execution-publishable event has one explicit, gated async wrapper.
# Boundaries: the wrapper contract only - Redis transport and call-site migration are elsewhere.
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.event_stream import EventPublisher

#: `emit` is the transport primitive, not an event. `publish_terminal` belongs to the terminal
#: authority - a terminal event is published exactly when execution ownership is gone - so gating
#: it on ownership would make it unpublishable. Everything else a worker can say is execution
#: vocabulary. Derived here from the protocol so a new event method cannot be added without one.
NOT_EXECUTION_VOCABULARY = frozenset({"emit", "publish_terminal"})


def _protocol_methods() -> dict:
    return {name: fn for name, fn in vars(EventPublisher).items()
            if callable(fn) and not name.startswith("_")
            and name not in NOT_EXECUTION_VOCABULARY}


VOCABULARY = sorted(_protocol_methods())


def _wrappers() -> dict:
    return {name: fn for name, fn in vars(OwnershipCheckedPublisher).items()
            if inspect.iscoroutinefunction(fn) and not name.startswith("_")}


# a recording stand-in for the configured adapter: it records the call and touches nothing
class _Inner:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))
        return record


class _Own:
    def __init__(self, job_id="job-1", generation=3):
        self.job_id = job_id
        self.execution_generation = generation
        self.worker_token = uuid.UUID(int=7)


def _bind(monkeypatch, *, own, current=True):
    import meshpipeline.application.execution_publisher as mod

    async def is_current_owner():
        return current

    monkeypatch.setattr(mod._fence, "current_ownership", lambda: own)
    monkeypatch.setattr(mod._fence, "is_current_owner", is_current_owner)


# Build a call for a wrapper from its own signature, so a parameter that is added, renamed or
# reordered changes what this test sends and is caught rather than skipped.
def _args_for(sig: inspect.Signature):
    args, kwargs = [], {}
    for i, (name, p) in enumerate(sig.parameters.items()):
        if name == "self":
            continue
        value = f"v{i}" if p.annotation in (str, "str") else i
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = value if p.default is inspect.Parameter.empty else p.default
        else:
            args.append(value)
    return args, kwargs


# completeness


def test_every_protocol_event_method_has_one_gated_execution_wrapper():
    missing = [m for m in VOCABULARY if f"a{m}" not in _wrappers()]
    assert missing == [], (
        f"these publishable events have no execution wrapper, so a worker can only publish them "
        f"through the ungated contract: {missing}")


def test_the_execution_protocol_declares_every_gated_wrapper():
    # The implementation and the CONTRACT must agree: a caller typed as ExecutionEventPublisher
    # can only reach what the protocol declares, so a wrapper missing from it is unusable.
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

    declared = {n for n, f in vars(ExecutionEventPublisher).items()
                if callable(f) and not n.startswith("_")}
    missing = sorted(f"a{m}" for m in VOCABULARY if f"a{m}" not in declared)
    assert missing == [], (
        f"these gated methods exist on the implementation but not on the contract, so nothing "
        f"typed as ExecutionEventPublisher can call them: {missing}")


def test_no_execution_wrapper_exists_without_a_protocol_counterpart():
    orphans = [w for w in _wrappers() if w[1:] not in VOCABULARY]
    assert orphans == [], f"execution wrappers with no protocol event: {orphans}"


@pytest.mark.parametrize("method", VOCABULARY)
def test_the_wrapper_signature_matches_its_protocol_method(method):
    proto = inspect.signature(_protocol_methods()[method])
    wrapper = inspect.signature(_wrappers()[f"a{method}"])
    p = [(n, v.kind, v.default) for n, v in proto.parameters.items() if n != "self"]
    w = [(n, v.kind, v.default) for n, v in wrapper.parameters.items() if n != "self"]
    assert w == p, (
        f"a{method} does not accept what {method} accepts; a caller migrating to the execution "
        f"publisher would lose or reorder arguments\n  protocol: {p}\n  wrapper:  {w}")


# authorization


@pytest.mark.parametrize("method", VOCABULARY)
def test_an_authorized_wrapper_delegates_once_with_its_arguments_unchanged(monkeypatch, method):
    inner = _Inner()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    pub = OwnershipCheckedPublisher(inner)
    args, kwargs = _args_for(inspect.signature(_wrappers()[f"a{method}"]))

    asyncio.run(getattr(pub, f"a{method}")(*args, **kwargs))

    assert len(inner.calls) == 1, f"a{method} delegated {len(inner.calls)} times, not once"
    name, got_a, got_k = inner.calls[0]
    assert name == method, f"a{method} delegated to {name}"

    # Compare what the inner method BINDS, not how it was spelled: forwarding a value
    # positionally or by keyword is equivalent, dropping or renaming one is not.
    proto = inspect.signature(_protocol_methods()[method])
    sig = proto.replace(parameters=[p for n, p in proto.parameters.items() if n != "self"])
    want = sig.bind(*args, **kwargs)
    want.apply_defaults()
    got = sig.bind(*got_a, **got_k)
    got.apply_defaults()
    assert got.arguments == want.arguments, (
        f"a{method} altered the arguments on the way through\n"
        f"  sent:      {want.arguments}\n  delegated: {got.arguments}")


@pytest.mark.parametrize("method", VOCABULARY)
@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_an_unauthorized_wrapper_never_reaches_the_publisher(monkeypatch, method, case):
    inner = _Inner()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)

    pub = OwnershipCheckedPublisher(inner)
    args, kwargs = _args_for(inspect.signature(_wrappers()[f"a{method}"]))

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(getattr(pub, f"a{method}")(*args, **kwargs))

    assert inner.calls == [], (
        f"a{method} reached the publisher with {case} ownership; the event would have been "
        "written to Redis")


def test_the_refusal_names_the_contract_it_is_enforcing(monkeypatch):
    _bind(monkeypatch, own=None)
    pub = OwnershipCheckedPublisher(_Inner())
    with pytest.raises(StaleExecutionPublish, match="no execution ownership is bound"):
        asyncio.run(pub.anote("anything"))
