# Responsibility: Verify every surface a user meets after a finished run points at something that
# exists: the chat takes the next run, the console offers it, and no failure text sends them to a
# control that is not there.
# Boundaries: text and shipped source only; the behaviour itself is proven in
# tests/unit/agents/intake/test_message_new_run.py.
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UI = REPO / "ui"
SRC = REPO / "src" / "meshpipeline"


# the console offers the next run

def test_the_composer_offers_to_run_again_or_change_something():
    composer = (UI / "js" / "shell" / "composer.js").read_text()
    assert "export function offerNewRun" in composer
    assert "export function clearNewRunOffer" in composer
    assert "Run again" in composer and "Change something" in composer
    # "Run again" sends a real chat message: the server takes it from there, through the
    # ordinary proposal -> summary -> approval cycle, and nothing dispatches from the browser.
    assert "sendText(RUN_AGAIN_TEXT)" in composer
    assert "same requirements" in composer.split("RUN_AGAIN_TEXT =", 1)[1].split("\n", 1)[0]


def test_the_offer_is_made_when_the_run_ends_and_withdrawn_when_one_starts():
    main = (UI / "js" / "main.js").read_text()
    on_terminal = main.split("onTerminal(job) {", 1)[1].split("},", 1)[0]
    assert "offerNewRun()" in on_terminal and "enableInput()" in on_terminal
    assert "Start a new simulation" not in main, (
        "the composer still tells the user to start over instead of continuing")
    attach = main.split("function attachJob(", 1)[1].split("\n}", 1)[0]
    assert "clearNewRunOffer()" in attach


def test_the_offer_never_appears_without_a_session_to_continue():
    composer = (UI / "js" / "shell" / "composer.js").read_text()
    body = composer.split("export function offerNewRun()", 1)[1].split("\n}", 1)[0]
    assert "getState.sessionId()" in body, (
        "a deep-linked run has no conversation here; offering one would send nothing")


def test_the_offer_is_styled_in_both_shipped_copies():
    for tree in (UI, REPO / "apps" / "console" / "public" / "static"):
        css = (tree / "css" / "result.css").read_text()
        assert ".rerun-chip" in css and ".rerun{" in css, f"{tree}: the offer has no style"


# every failure text points at the chat that exists

@pytest.mark.parametrize("category", ["native_execution_failed", "review_rejected",
                                      "attempts_exhausted", "internal_pipeline_failure"])
def test_a_retryable_verdict_names_the_chat_not_a_button_that_is_not_there(category):
    from meshpipeline.application import final_result as fr

    result = fr.FinalResult(
        schema_version=fr.FINAL_RESULT_SCHEMA_VERSION, job_id="j", owner_id="o",
        status=fr.TerminalStatus.failed, failure_category=category, attempts=3, attempts_max=3)
    text = fr.render_message(result)
    assert "run it again" in text and "this chat" in text
    assert "try running the job again" not in text


def test_a_verdict_that_needs_a_change_says_where_to_make_it():
    from meshpipeline.application import final_result as fr

    result = fr.FinalResult(
        schema_version=fr.FINAL_RESULT_SCHEMA_VERSION, job_id="j", owner_id="o",
        status=fr.TerminalStatus.failed, failure_category="incompatible_requirements")
    text = fr.render_message(result)
    assert "A change to the request is needed" in text
    assert "in this chat" in text


def test_a_launch_failure_says_how_to_go_again():
    approval = (SRC / "agents" / "intake" / "approval.py").read_text()
    assert "please try again." not in approval, "the launch failure still names nothing"
    assert 'say \\"run it again\\" in this chat' in approval


def test_no_surface_still_says_a_finished_session_is_already_running():
    message = (SRC / "agents" / "intake" / "message.py").read_text()
    assert "is already running for this session" not in message
    assert "STILL_RUNNING_REPLY" in message


# a dispute of a run whose conversation has moved on keeps that run's intake context

_PAYLOAD = {"session_id": "sess-1", "review_brief_txt": "the brief", "purpose": "external_cfd",
            "input_kind": "body-surface", "dimensionality": "3D",
            "intake_patches": [{"name": "wing", "type": "wall"}]}


def test_a_disputed_run_whose_session_moved_on_inherits_its_own_dispatch_payload():
    from types import SimpleNamespace

    from meshpipeline.api.v1 import simulation

    got = simulation._parent_intake(None, _PAYLOAD)
    assert got.session_id == "sess-1" and got.review_brief_txt == "the brief"
    assert got.purpose == "external_cfd" and got.input_kind == "body-surface"
    assert got.dimensionality == "3D" and got.intake_patches == [{"name": "wing", "type": "wall"}]
    # the amended brief reads the same context object
    assert simulation._amended_brief(got, "accept", "fine for me").startswith("the brief")

    # a linked session still wins, exactly as before
    session = SimpleNamespace(id="sess-9", review_brief_txt="live brief", intake_patches=[],
                              dimensionality="2D", purpose="internal_cfd", input_kind="solid-body")
    linked = simulation._parent_intake(session, _PAYLOAD)
    assert linked.session_id == "sess-9" and linked.purpose == "internal_cfd"
    assert linked.intake_patches == [] and linked.review_brief_txt == "live brief"

    # a run with neither (older than dispatch payloads, session gone) degrades to nothing
    none = simulation._parent_intake(None, None)
    assert none.session_id == "" and none.intake_patches == [] and none.purpose == ""
