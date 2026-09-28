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


def test_only_the_conversation_a_run_came_from_is_offered_to_run_it_again():
    # Two ways a finished run is not this page's conversation's: it was opened from a link and
    # the page holds no session (a composer opened then would swallow whatever is typed), or a
    # later upload opened a session for OTHER geometry (a "run again" would go to that part).
    main = (UI / "js" / "main.js").read_text()
    on_terminal = main.split("onTerminal(job) {", 1)[1].split("\n  },", 1)[0]
    guard, _, rest = on_terminal.partition(
        "if (!session || session !== getState.runSessionId()) {")
    assert rest, "the end of a run offers another run without asking whose run it was"
    assert "const session = getState.sessionId();" in guard
    assert "enableInput()" not in guard and "offerNewRun()" not in guard, (
        "the composer opens before the run's conversation is checked")
    not_ours = rest.split("\n    }\n", 1)[0]
    assert "return;" in not_ours
    assert "enableInput()" not in not_ours and "offerNewRun()" not in not_ours
    # no session at all: closed, and pointing at the upload that opens one
    no_session = not_ours.split("if (!session) {", 1)[1].split("\n      }", 1)[0]
    assert "disableInput()" in no_session and "Upload a geometry file" in no_session
    # the run records the conversation it came from; a run opened by link records none
    attach = main.split("function attachJob(", 1)[1].split("\n}", 1)[0]
    assert 'origin = getState.sessionId() } = {})' in attach, (
        "a run the chat started no longer defaults to the page's own conversation")
    assert "beginRun(id, { sessionId: origin })" in attach
    assert "beginRun(deepLinkJob);" in main
    state = (UI / "js" / "core" / "state.js").read_text()
    begin = state.split("export function beginRun(", 1)[1].split("\n}", 1)[0]
    assert "state.runSessionId = sessionId || null;" in begin
    assert "state.runOrigins[jobId] = state.runSessionId;" in begin


def test_a_re_review_belongs_to_the_disputed_runs_conversation():
    # A run opened by link, then an upload of other geometry, then a dispute of the linked run:
    # the re-review must not adopt the upload's session, or its "run again" proposes a run on the
    # wrong part. The dispute names the disputed run, and the re-review takes that run's origin.
    dispute = (UI / "js" / "viewer" / "dispute.js").read_text()
    assert dispute.count("onRerun(d.job_id,") == 2
    for call in dispute.split("onRerun(d.job_id,")[1:]:
        assert call.split(");", 1)[0].rstrip().endswith("job"), (
            "a re-review no longer says which run it disputes")
    main = (UI / "js" / "main.js").read_text()
    wiring = main.split("configureDispute({", 1)[1].split("\n});", 1)[0]
    assert "onRerun: (id, message, disputedJobId) => attachJob(id," in wiring
    assert "origin: getState.runOrigin(disputedJobId)" in wiring


def test_an_answer_about_a_run_the_page_has_left_is_dropped():
    # A status poll for run A still in flight when run B starts must not become B's status, stop
    # B's polling, or announce A's end over B - which re-offered "run again" while B was running.
    stream = (UI / "js" / "realtime" / "stream.js").read_text()
    poll = stream.split("async function poll() {", 1)[1].split("\n}", 1)[0]
    asked, _, answered = poll.partition("await getJob(jobId")
    assert "const jobId = getState.jobId();" in asked
    guard = "if (getState.jobId() !== jobId) return;"
    assert guard in answered, "a stale poll answer is applied to the current run"
    assert answered.index(guard) < answered.index("setState.jobStatus(job.status)")
    assert answered.index(guard) < answered.index("sinks.onTerminal(job)")


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


# a run the conversation has moved on from keeps its label in the run lists

def _compiled(statement) -> str:
    from sqlalchemy.dialects import postgresql
    return " ".join(str(statement.compile(dialect=postgresql.dialect(),
                                          compile_kwargs={"literal_binds": True})).split())


def test_a_released_run_is_listed_under_the_label_it_was_dispatched_with():
    # Releasing the session's link breaks the join that supplied the label. The label then comes
    # from the run's own dispatch payload - the same `domain`, frozen at approval - while a
    # still-linked session keeps supplying it exactly as before.
    from meshpipeline.persistence.repositories import job_repository as jr

    label = _compiled(jr._task_label())
    assert label.startswith("coalesce(chat_sessions.domain, "), label
    assert "jsonb_extract_path_text(simulation_jobs.dispatch_payload, 'domain')" in label
    assert "nullif(" in label, "an empty dispatched label must read as no label, not ''"


async def test_both_run_lists_select_that_label():
    from unittest.mock import AsyncMock, MagicMock

    from meshpipeline.persistence.repositories import job_repository as jr

    seen: list = []
    db = MagicMock()

    async def _execute(statement):
        seen.append(_compiled(statement))
        return MagicMock(all=lambda: [])
    db.execute = AsyncMock(side_effect=_execute)
    await jr.JobRepository().list_for_owner(db, "alice")
    await jr.JobRepository().list_recent_admin(db)
    label = _compiled(jr._task_label())
    assert len(seen) == 2 and all(label in sql for sql in seen), seen
    assert all("LEFT OUTER JOIN chat_sessions" in sql for sql in seen), (
        "a run with no session must still be listed")
