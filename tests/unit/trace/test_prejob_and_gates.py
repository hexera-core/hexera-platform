# Responsibility: Verify the pre-job sink speaks the job publisher's contracts and can never fail the turn.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._scan import scanned
from tests.product_modes import set_modes

from meshpipeline.trace.sink import PublicTraceSink, project_all

ROOT = Path(__file__).parent.parent.parent.parent
APP = ROOT / "src" / "meshpipeline"

SECRET = "sk-live-PREJOBSENTINEL01"
COT = "As GLM 5.2 I will read /srv/workspaces/j/brief.txt. Bearer " + SECRET


@pytest.fixture
def raw(monkeypatch):
    set_modes(monkeypatch, disclosure="raw")


@pytest.fixture
def safe(monkeypatch):
    set_modes(monkeypatch, disclosure="safe")


# pre-job transport


def test_the_sink_speaks_the_same_contracts_as_the_job_publisher():
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    for name in ("reasoning", "tool_call", "tool_result", "rationale"):
        assert hasattr(PublicTraceSink, name)
        assert hasattr(JobPublisher, name)


def test_the_sink_needs_no_job_identity_to_exist(safe):
    sink = PublicTraceSink()
    sink.reasoning("r:session:intake:1:0", "started")
    assert sink.events[0]["type"] == "reasoning"
    assert PublicTraceSink().events == []


def test_safe_mode_never_writes_reasoning_text_into_session_storage(safe):
    sink = PublicTraceSink()
    sink.reasoning("r:1", "completed", duration_ms=8300, content=COT)
    stored = sink.session_events()
    assert SECRET not in json.dumps(stored)
    assert stored[0]["payload"]["content"] is None
    assert stored[0]["type"] == "public_trace"


def test_raw_mode_stores_sanitized_reasoning(raw):
    sink = PublicTraceSink()
    sink.reasoning("r:1", "completed", duration_ms=8300, content=COT)
    body = json.dumps(sink.session_events())
    assert "I will read" in body, "raw mode stored none of the returned prose"
    # the path is normalized whole - filename included - so nothing of it survives
    assert SECRET not in body and "GLM" not in body
    assert "/srv/workspaces" not in body and "brief.txt" not in body


def test_a_session_written_raw_is_reprojected_when_the_server_restarts_safe(raw, monkeypatch):
    sink = PublicTraceSink()
    sink.reasoning("r:1", "completed", duration_ms=10, content=COT)
    sink.tool_call("c1", "submit_requirements", {"purpose": "external_cfd"})
    stored = sink.session_events()

    set_modes(monkeypatch, disclosure="safe")
    out = project_all(stored)
    assert all(e.get("content") is None for e in out if e["type"] == "reasoning")
    assert all(e.get("tool_name") is None for e in out if e["type"] == "tool_call")
    assert "submit_requirements" not in json.dumps(out)


def test_project_all_ignores_anything_that_is_not_a_public_event():
    assert project_all(None) == []
    assert project_all([{"type": "agent_run", "payload": {"secret": 1}}]) == []
    assert project_all(["not a dict", {}]) == []


def test_a_trace_failure_cannot_fail_the_turn():
    sink = PublicTraceSink()
    sink.reasoning("r", "not-a-phase")          # invalid: the constructor raises
    assert sink.events == [], "a bad event was stored"
    # and the caller still gets a usable list
    assert sink.session_events() == []


def test_public_trace_is_not_coupled_to_data_collection():
    src = (APP / "trace" / "sink.py").read_text() + (APP / "trace" / "policy.py").read_text()
    assert "DATA_COLLECTION_ENABLED" not in src


# rationale


class _Sink(PublicTraceSink):
    def said(self):
        return [e["conclusion"] for e in self.events if e["type"] == "rationale"]


def test_a_refusal_never_names_the_authorization_machinery(safe):
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    R.intake_submission(s, authorized=False,
                        reason="the submission did not match the configuration you confirmed")
    body = json.dumps(s.events)
    for leak in ("token", "preview", "admission", "canonical"):
        assert leak not in body.lower(), f"a refusal exposed {leak!r}"


def test_a_rationale_never_claims_the_system_made_the_users_choice(safe):
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    R.intake_compatibility(s, engine="cfmesh", purpose="internal_cfd",
                           input_kind="body-surface", supported=True)
    said = s.said()[0].lower()
    for wrong in ("we chose", "we selected", "system selected", "automatically selected"):
        assert wrong not in said, f"the rationale claimed authorship of the user's choice: {said}"


def test_the_same_decision_observed_repeatedly_states_itself_once(safe):
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    for _ in range(4):
        R.intake_compatibility(s, engine="cfmesh", purpose="external_cfd",
                               input_kind="body-surface", supported=True)
    assert len(s.said()) == 1


def test_a_terminal_failure_rationale_carries_no_stack_trace():
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    R.terminal_failure(s, classification="executor_failed")
    body = json.dumps(s.events)
    assert "Traceback" not in body and 'File "' not in body


def test_a_failed_review_names_its_typed_axes_in_readable_words():
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    R.reviewer_verdict(s, passed=False, failed_axes=("cell_quality", "boundary_integrity"))
    said = s.events[-1]
    assert "does not meet" in said["conclusion"]
    assert "cell quality" in said["because"] and "boundary integrity" in said["because"]


def test_a_review_that_could_not_conclude_implies_no_verdict():
    from meshpipeline.contracts import rationale as R
    s = _Sink()
    R.reviewer_evidence_incomplete(s)
    said = s.events[-1]
    assert "no verdict" in said["conclusion"].lower()
    for forbidden in ("PASS", "FAIL", "passed", "failed the"):
        assert forbidden not in said["conclusion"], \
            "a review that could not conclude implied a verdict"


def test_every_rationale_template_has_a_real_producer():
    import inspect
    import re

    from meshpipeline.contracts import rationale as R
    templates = {n for n, o in vars(R).items()
                 if inspect.isfunction(o) and not n.startswith("_")}
    called: set[str] = set()
    for py in scanned(APP.rglob("*.py"), "the shipped application package"):
        if py.name == "rationale.py":
            continue
        src = py.read_text()
        for t in templates:
            if re.search(rf"\b(?:_R|_rationale|rationale)\.{t}\(", src):
                called.add(t)
    # An execution-owned counterpart (`a<name>`) renders the SAME sentence as its synchronous
    # twin through the ownership-checked contract. It is a second authority route to one
    # decision, not a second decision, so EITHER twin having a live producer proves the decision
    # is really made - including after its caller migrates from one route to the other.
    def _twin(t: str) -> str:
        return t[1:] if t.startswith("a") and t[1:] in templates else f"a{t}"

    orphans = sorted(t for t in templates - called if _twin(t) not in called)
    assert not orphans, (
        f"rationale templates with no live producer: {orphans}. Connect each to its "
        f"real gate or delete it - the public catalog must describe decisions the "
        f"application actually makes.")
