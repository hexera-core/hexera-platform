# Responsibility: Verify every model call's telemetry says WHO asked (investigator, reviewer,
#                 author, tournament source) and not only which route carried it - all repair
#                 calls ride the intake route, so `role` alone read `intake` for every one.
# Boundaries: hermetic - the telemetry sink is a list; no provider, no network.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.contracts import inference_telemetry as tel


class _Sink:
    def __init__(self):
        self.calls = []

    def record(self, call):
        self.calls.append(call)


@pytest.fixture
def sink():
    s = _Sink()
    tel.set_inference_telemetry(s)
    yield s
    tel.set_inference_telemetry(None)


@pytest.mark.parametrize("label,purpose", [
    ("RepairInvestigator", "investigator"),
    ("RepairPackageReview", "reviewer"),
    ("RepairReview", "author"),
    ("Intake", "intake"),
    ("SomethingNew", "somethingnew"),
])
def test_a_caller_label_names_its_purpose(label, purpose):
    with tel.calling(label):
        assert tel.current() == (label, purpose)
    assert tel.current() == ("", "")


def test_a_tournament_scope_prefixes_the_purpose():
    with tel.scope("tournament:investigator"), tel.calling("RepairInvestigator"):
        assert tel.current() == ("RepairInvestigator", "tournament:investigator")


def test_the_routers_emit_records_the_caller_and_purpose(sink):
    from types import SimpleNamespace

    from meshpipeline.adapters.model_inference import routing

    target = SimpleNamespace(provider="openai", model="m")
    route = SimpleNamespace(role="intake", standby=None)
    with tel.calling("RepairInvestigator"):
        routing._emit(route, target, "primary", "job", attempts=1, category=None,
                      circuit_state="", failover=False, cost_of=None)
    [call] = sink.calls
    assert call.role == "intake"
    assert (call.caller, call.purpose) == ("RepairInvestigator", "investigator")
    assert call.as_record()["purpose"] == "investigator"


def test_the_label_crosses_the_model_authors_worker_thread():
    from meshpipeline.cad.repair import model_author as ma

    async def start():
        return tel.current()

    with tel.scope("tournament:author"), tel.calling("RepairReview"):
        got = ma._bounded_in_thread(start, timeout_s=5.0, failure=None)
    assert got == ("RepairReview", "tournament:author")


def test_the_package_reviewer_asks_under_its_own_label(monkeypatch):
    from meshpipeline.cad.repair import package_review as pr
    from meshpipeline.contracts import model_inference as mi

    seen = {}

    async def fake(messages, job_id="", name="RepairReview", **kw):
        seen["name"] = name
        return mi.ModelReply(ok=False, assistant_text="", tool_calls=()) \
            if hasattr(mi, "ModelReply") else None

    monkeypatch.setattr(mi, "call_repair_review_model", fake)
    asyncio.run(pr._ask("s", "u", job_id="j", timeout_s=5.0))
    assert seen["name"] == "RepairPackageReview"
