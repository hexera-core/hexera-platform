# Responsibility: Verify a non-finite metric is unavailable rather than evaluated in either direction.
from __future__ import annotations

import pytest

from meshpipeline.agents.reviewer.deterministic_evidence import (
    _is_finite_number,
    collect_deterministic_evidence,
)
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus
from meshpipeline.engines.review_types import Criterion

NON_FINITE = [float("nan"), float("inf"), float("-inf")]


def _crit(op, threshold=1.0):
    return Criterion(key="m", label="M", op=op, threshold=threshold, gating=True,
                     rationale="r", evidence_url="u")


@pytest.mark.parametrize("op", ["<=", ">", "=="])
@pytest.mark.parametrize("bad", NON_FINITE)
def test_non_finite_is_not_evaluated_in_any_direction(op, bad):
    assert _crit(op).evaluate({"m": bad}) is None


def test_finite_values_still_evaluate_normally():
    assert _crit("<=").evaluate({"m": 0.5}) is True
    assert _crit("<=").evaluate({"m": 2.0}) is False
    assert _crit(">").evaluate({"m": 2.0}) is True
    assert _crit(">").evaluate({"m": 0.5}) is False


def test_plus_infinity_does_not_satisfy_a_greater_than_bar():
    # the specific dangerous case: without the guard, inf > 1.0 is True → spurious PASS
    assert _crit(">", 1.0).evaluate({"m": float("inf")}) is None


@pytest.mark.parametrize("bad", NON_FINITE)
def test_collector_marks_a_non_finite_metric_unavailable_with_a_threshold(bad):
    class _Plan:
        required_gate_keys = frozenset()
        required_metric_keys = frozenset({"m"})
        axes = ()
    L = EvidenceLedger()
    collect_deterministic_evidence(_Plan(), L, measurements={"m": bad},
                                   criteria={"m": _crit("<=")}, gate_status={})
    rec = [x for x in L.metrics() if x.metric_key == "m"][0]
    assert rec.status is EvidenceStatus.UNAVAILABLE and not rec.usable


@pytest.mark.parametrize("bad", NON_FINITE)
def test_collector_marks_a_non_finite_metric_unavailable_without_a_threshold(bad):
    class _Plan:
        required_gate_keys = frozenset()
        required_metric_keys = frozenset({"m"})
        axes = ()
    L = EvidenceLedger()
    collect_deterministic_evidence(_Plan(), L, measurements={"m": bad},
                                   criteria={}, gate_status={})
    rec = [x for x in L.metrics() if x.metric_key == "m"][0]
    assert rec.status is EvidenceStatus.UNAVAILABLE and not rec.usable


def test_is_finite_number_helper():
    assert _is_finite_number(0) and _is_finite_number(1.5) and _is_finite_number(-3)
    assert not _is_finite_number(float("nan"))
    assert not _is_finite_number(float("inf"))
    assert not _is_finite_number(float("-inf"))
    assert not _is_finite_number(True)      # a bool is not a measurement
    assert not _is_finite_number("1.0") and not _is_finite_number(None)


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
