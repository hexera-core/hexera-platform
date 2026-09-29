# Responsibility: Verify a refused input, a lost worker and a record-less terminal each tell the user whose problem it is and what to do next.
# Boundaries: the terminal vocabulary and its rendering; no database, no socket.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from meshpipeline.api.v1.ws import terminal_closing_text
from meshpipeline.application.final_result import (
    GEOMETRY_INPUT_GATE,
    FailureCategory,
    FinalResult,
    TerminalStatus,
    build_final_result,
    never_started_result,
    render_message,
    worker_lost_result,
)


def _build(**over):
    base = {"job_id": "j", "owner_id": "o", "status": TerminalStatus.failed,
            "engine": "cfmesh", "purpose": "external_cfd", "dimensionality": "3D",
            "approved_snapshot_id": "snap", "executor_success": False, "reviewer_verdict": "",
            "failed_gate": "", "api_failure": "", "attempts": 1, "attempts_max": 5,
            "required_ready": False, "delivered_types": [], "optional_warnings": []}
    base.update(over)
    return build_final_result(**base)


# a refused INPUT is the user's CAD, not a quality gate

def test_a_refused_input_is_input_rejected_not_a_quality_gate():
    fr = _build(failed_gate=GEOMETRY_INPUT_GATE)
    assert fr.failure_category == FailureCategory.input_rejected.value
    assert fr.outcome_code == "input_rejected"
    msg = render_message(fr)
    assert "did not complete successfully" in msg
    assert "cannot be meshed as it is" in msg
    assert "CAD file" in msg and "not our systems" in msg
    assert "Fix the geometry and upload it again." in msg
    assert "quality checks" not in msg
    assert "try running the job again" not in msg


def test_every_other_gate_is_still_gate_failed():
    for gate in ("patch_contract", "sicn_floor", "solvability", "mesh_quality"):
        fr = _build(failed_gate=gate)
        assert fr.failure_category == FailureCategory.gate_failed.value, gate
        assert "quality checks" in render_message(fr)


def test_a_refused_input_outranks_a_retained_verdict_and_a_timeout():
    fr = _build(failed_gate=GEOMETRY_INPUT_GATE, reviewer_verdict="FAIL", pipeline_timed_out=True)
    assert fr.failure_category == FailureCategory.input_rejected.value
    assert fr.reviewer_verdict is None      # never reported as this run's judgement of the mesh


def test_the_input_gate_name_is_what_the_executor_stamps():
    # The executor short-circuit and the terminal classifier must agree on the one gate key
    # that means "the input was refused"; a drift would silently demote it to gate_failed.
    src = Path(__file__).parents[3] / "src/meshpipeline/pipeline/executor.py"
    assert f'"executor_failed_gate": "{GEOMETRY_INPUT_GATE}"' in src.read_text(encoding="utf-8")


def test_input_rejected_round_trips_through_the_record():
    fr = _build(failed_gate=GEOMETRY_INPUT_GATE)
    back = FinalResult.from_dict(fr.to_dict())
    assert back.failure_category == "input_rejected"
    assert render_message(back) == render_message(fr)


# a lost worker is ours, and the run can be started again

def test_a_lost_worker_record_renders_the_honest_sentence_and_round_trips():
    fr = worker_lost_result(job_id="j", owner_id="o", attempts=2)
    back = FinalResult.from_dict(fr.to_dict())
    assert back.failure_category == FailureCategory.worker_lost.value
    assert back.status is TerminalStatus.failed
    assert back.reviewer_verdict is None and back.executor_success is False
    assert back.required_ready is False and back.delivered_types == []
    msg = render_message(back)
    assert "did not complete successfully" in msg
    assert "worker" in msg and "lost" in msg
    assert "on our side" in msg and "run it again" in msg
    assert "No downloadable mesh deliverable" in msg
    # it invents nothing it does not know
    assert back.engine == "" and back.attempts == 2 and back.attempts_max == 0


def test_a_lost_worker_never_claims_a_mesh_or_a_review():
    fr = worker_lost_result(job_id="j", owner_id="o")
    msg = render_message(fr)
    for claim in ("completed successfully", "ready to download", "Review: passed", "quality checks"):
        assert claim not in msg


# the socket's closing line for a job that is already terminal

def test_a_reaped_job_closes_with_the_lost_worker_sentence_not_job_already_failed():
    job = SimpleNamespace(status="failed",
                          final_result=worker_lost_result(job_id="j", owner_id="o").to_dict())
    text = terminal_closing_text(job)
    assert "Job already" not in text
    assert "worker" in text and "lost" in text and "run it again" in text


def test_a_failed_job_without_a_record_still_says_what_to_do():
    from meshpipeline.persistence.models import JobStatus
    text = terminal_closing_text(SimpleNamespace(status=JobStatus.failed, final_result=None))
    assert "Job already" not in text
    assert "marked failed" in text and "run it again" in text


def test_a_succeeded_job_without_a_record_names_its_status():
    from meshpipeline.persistence.models import JobStatus
    text = terminal_closing_text(SimpleNamespace(status=JobStatus.succeeded, final_result=None))
    assert "succeeded" in text


def test_an_unreadable_record_falls_back_rather_than_crashing():
    text = terminal_closing_text(SimpleNamespace(status="failed",
                                                 final_result={"schema_version": 1}))
    assert "run it again" in text


def test_a_rendered_record_wins_over_the_fallback():
    fr = _build(failed_gate=GEOMETRY_INPUT_GATE)
    text = terminal_closing_text(SimpleNamespace(status="failed", final_result=fr.to_dict()))
    assert text == render_message(fr)


# a job no worker ever picked up is told exactly that

def test_a_never_started_record_says_no_worker_picked_it_up_and_round_trips():
    back = FinalResult.from_dict(never_started_result(job_id="j", owner_id="o").to_dict())
    assert back.failure_category == FailureCategory.never_started.value
    assert back.status is TerminalStatus.failed and back.reviewer_verdict is None
    msg = render_message(back)
    assert "No worker picked this run up" in msg and "never started" in msg
    assert "lost" not in msg
    assert "run it again" in msg and "No downloadable mesh deliverable" in msg


def test_a_class_owned_outcome_states_its_cause_once():
    # The class sentence opens with what happened; the category headline must not say it again.
    for fr in (worker_lost_result(job_id="j", owner_id="o"),
               never_started_result(job_id="j", owner_id="o"),
               _build(failed_gate=GEOMETRY_INPUT_GATE)):
        msg = render_message(fr)
        for phrase in ("lost before it finished", "No worker picked this run up",
                       "cannot be meshed as it is"):
            assert msg.count(phrase) <= 1, (fr.failure_category, phrase, msg)
        assert "You can run it again from this chat" not in msg, "two next steps in one verdict"


def test_the_other_categories_keep_mains_next_step():
    msg = render_message(_build(failed_gate="sicn_floor"))
    assert 'say "run it again"' in msg and "quality checks" in msg


def test_a_cancelled_job_without_a_record_is_not_called_a_success():
    from meshpipeline.persistence.models import JobStatus
    text = terminal_closing_text(SimpleNamespace(status=JobStatus.cancelled, final_result=None))
    assert text.startswith("Cancelled by you.")
    assert "succeeded" not in text and "failed" not in text


def test_a_cancelled_record_keeps_its_own_line():
    from meshpipeline.application.final_result import build_cancelled_result
    fr = build_cancelled_result(job_id="j", owner_id="o", engine="cfmesh")
    text = terminal_closing_text(SimpleNamespace(status="cancelled", final_result=fr.to_dict()))
    assert text == render_message(fr) and text.startswith("Cancelled by you.")
