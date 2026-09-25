"""The learning export: what a finished job hands the geometry agent's learning loop.

Hermetic. The rows are simple stand-ins with the attributes the exporter reads, so nothing here opens a
database or an object store.

WHAT IS HELD, and each one is a way this could hand the ingester a document that lies:

  1. every answer carries `delegated`, computed by the platform's OWN `choice_deferred` - the same predicate
     `geometry_survey.said_by_customer` used to accept the quote. Without it the ingester cannot tell "the
     customer chose this" from "the customer said you decide and this is our own pick", and it refuses to score
     a row whose flag is missing, so the flag is load-bearing and not decoration;
  2. an unfinished job is refused by name, not exported with empty halves;
  3. a half that is missing is named in `absent` rather than dropped, so a batch describes every job it read
     and not only the ones that went well;
  4. the quality numbers are copied from the delivered payload's own criteria, which is the only place several
     of them exist, and `final_result` is never mined for a metric it does not carry;
  5. the platform's `facts_sha256` (the FILE digest) is exported under its own name and never under the name
     the learning loop uses for the measurement digest.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from meshpipeline.application import learning_export as lx

SHA = "9c8b9b95e5406b7b1b55a53b37b2d29adaff8ca4d256a3ca47b3b72a1a74bb14"
JOB = "489fa1ab-90d6-49c9-9427-5be995edeb4c"
DELEGATION = "you decide everything"


def _job(status: str = "succeeded", **over):
    fr = {"schema_version": 5, "engine": "snappy", "purpose": "internal_cfd", "attempts": 1,
          "reviewer_verdict": "passed", "review_execution": "completed", "outcome_code": "success",
          "failed_gate": "", "effective_mesh_fidelity": "standard"}
    fr.update(over.pop("final_result", {}))
    return SimpleNamespace(
        id=JOB, status=SimpleNamespace(value=status), owner_id="dev-user",
        created_at=datetime(2026, 9, 25, 14, 20, 45, tzinfo=UTC),
        started_at=datetime(2026, 9, 25, 14, 21, tzinfo=UTC),
        ended_at=datetime(2026, 9, 25, 14, 35, 11, tzinfo=UTC),
        current_attempt=1, failed_reason=None, final_result=fr,
        geometry_source_id="87c83644", geometry_interpretation_id="ip-1", geometry_source=None, **over)


def _source():
    return SimpleNamespace(id="87c83644", original_filename="ahmed_variant_001.step", suffix_hint=".step",
                           sha256=SHA, size_bytes=1024, bytes_available=True)


def _measurement():
    return SimpleNamespace(
        status="ok", reason="", purpose="internal_cfd", sha256=SHA, facts_schema_version=3,
        agent_git_sha="0.1.0+gefbd71927c", measure_seconds=1.326,
        document={"facts": {"sha256": SHA}, "representation": "wall_shell",
                  "stamp": {"platform_sha": "57684388", "measurement_revision": 2,
                            "facts_schema_version": 3}})


def _survey(answers=None, step=True):
    return SimpleNamespace(
        id="70f13610", session_id="s-1", stage="settled",
        state_schema="meshpipeline.geometry_survey.v1", sha256=SHA, facts_sha256=SHA,
        agent_git_sha="0.1.0", look_queued="cached",
        composed_for={"purpose": "internal_cfd", "representation": "wall_shell"},
        asking={"put": ["q_port_roles"], "record": {"q_port_roles": {"kind": "port_role"}}},
        asked=["q_port_roles"],
        answers=answers if answers is not None else [
            {"question_id": "q_port_roles", "about": "opening.role", "subject": "o1", "value": "inlet",
             "words": DELEGATION, "answered_by": "customer"},
            {"question_id": "q_port_roles", "about": "opening.role", "subject": "o2", "value": "outlet",
             "words": "o2 is the outlet", "answered_by": "customer"}],
        late=None, planner_block=None,
        geometry_step={"status": "planned", "envelope": {"cells_high": 93200},
                       "ledger": {"events": [{"big": "x" * 5000}]}} if step else None,
        created_at=datetime(2026, 9, 25, 14, 18, 17, tzinfo=UTC),
        updated_at=datetime(2026, 9, 25, 14, 19, tzinfo=UTC))


def _interpretation():
    return SimpleNamespace(unit="mm", scale_to_metres=0.001, basis="user_confirmed", evidence="the STEP header")


VIEWER = {"engine": "snappy", "mesh_available": True,
          "quality": {"cell_count": 258540, "engine": "snappy", "mesh_units": "m", "production_grade": True,
                      "criteria": [
                          {"key": "wall_faces", "measured": 8846, "ok_when": "> 0", "passed": True,
                           "gating": True},
                          {"key": "max_non_ortho", "measured": 64.9193, "ok_when": "<= 65.0", "passed": True,
                           "gating": False},
                          {"key": "layer_coverage_pct", "measured": 94.5776, "ok_when": "> 0.0",
                           "passed": True, "gating": False}]},
          "review": {"verdict": "PASS", "attempt_reviewed": 1, "rebuild_required": False}}


def _envelope(**over):
    kwargs = {"job": _job(), "source": _source(), "interpretation": _interpretation(),
              "measurement": _measurement(), "survey": _survey(), "viewer_payload": VIEWER}
    kwargs.update(over)
    return lx.envelope(**kwargs)


# -------------------------------------------------------------------------------------------------
# 1. the delegation flag
# -------------------------------------------------------------------------------------------------

def test_every_answer_carries_whether_its_quote_was_a_delegation():
    """The ingester cannot score an answer without this, and it refuses to guess. "you decide everything" is
    the customer's authority over OUR value; "o2 is the outlet" is their own choice."""
    rows = _envelope()["survey"]["answers"]
    assert [a["delegated"] for a in rows] == [True, False]
    assert all("delegated" in a for a in rows)


def test_the_flag_comes_from_the_platform_predicate_that_accepted_the_quote():
    """Not a phrase list restated here. `geometry_survey.said_by_customer` allows a quote from an earlier
    message only when `choice_deferred` says it is a deferral, so that function is the authority on what a
    delegation is, and this export and that acceptance cannot disagree."""
    from meshpipeline.agents.intake.recommendation import choice_deferred
    for words in ("you decide everything", "your call", "whatever you think", "u decide best case"):
        assert choice_deferred(words)
        assert lx.answer_rows([{"words": words}])[0]["delegated"] is True
    for words in ("o5 inlet o6 outlet", "go", "the fluid is air", "follow measurement"):
        assert not choice_deferred(words)
        assert lx.answer_rows([{"words": words}])[0]["delegated"] is False


def test_a_retired_answer_travels_too():
    """The record still has to say what the customer said FIRST and what replaced it."""
    rows = lx.answer_rows([{"words": "o1 is the outlet", "retired": True, "retired_because": "superseded",
                            "superseded_by": "inlet"},
                           {"words": "o1 is the inlet"}])
    assert len(rows) == 2
    assert rows[0]["retired"] is True and rows[0]["superseded_by"] == "inlet"
    assert rows[0]["delegated"] is False


# -------------------------------------------------------------------------------------------------
# 2 and 3. what is refused, and what is named absent
# -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["pending", "running", "cancelled"])
def test_an_unfinished_job_is_not_exported(status):
    assert status not in lx.TERMINAL_STATUSES


def test_a_missing_half_is_named_and_the_job_is_still_exported():
    env = _envelope(survey=None, interpretation=None, viewer_payload=None,
                    quality_unavailable="the job delivered no viewer_data artifact")
    assert env["survey"] is None and env["interpretation"] is None
    assert env["job"]["job_id"] == JOB
    assert "no survey row for this job" in env["absent"]
    assert "no interpretation row for this job" in env["absent"]
    assert "the job delivered no viewer_data artifact" in env["absent"]
    assert env["quality"] == {}


def test_a_job_whose_agent_step_never_ran_says_so():
    env = _envelope(survey=_survey(step=False))
    assert env["step"] is None
    assert any("never ran" in w for w in env["absent"])


def test_a_finished_job_with_every_half_present_names_nothing_absent():
    assert _envelope()["absent"] == []


# -------------------------------------------------------------------------------------------------
# 4. the quality numbers
# -------------------------------------------------------------------------------------------------

def test_the_quality_criteria_are_flattened_and_copied():
    q = _envelope()["quality"]
    assert q["cell_count"] == 258540
    assert q["criteria"]["max_non_ortho"]["measured"] == pytest.approx(64.9193)
    assert q["criteria"]["wall_faces"]["passed"] is True
    assert q["criteria"]["layer_coverage_pct"]["gating"] is False
    assert q["review_verdict"] == "PASS" and q["attempt_reviewed"] == 1


def test_final_result_carries_the_verdict_and_no_metric():
    """Why the two come from two places. If `final_result` held the cells this module would read it there; it
    does not, on any schema version it can read, so a quality claim taken from it would have no measurement
    behind it."""
    fr = _envelope()["job"]["final_result"]
    assert fr["reviewer_verdict"] == "passed"
    assert not {"cells", "cell_count", "max_skewness", "max_non_ortho"} & set(fr)


def test_no_payload_means_no_quality_block_rather_than_zeroes():
    assert lx.quality_from_payload(None) == {}
    assert lx.quality_from_payload({"quality": {}})["cell_count"] is None


# -------------------------------------------------------------------------------------------------
# 5. the digests, the case name, and the size of the document
# -------------------------------------------------------------------------------------------------

def test_the_file_digest_is_exported_under_the_platform_s_own_name():
    env = _envelope()
    assert env["source"]["source_sha256"] == SHA
    assert env["measurement"]["facts_sha256_platform"] == SHA
    assert env["survey"]["facts_sha256_platform"] == SHA
    # and never under the name the learning loop means the measurement's canonical digest by
    for block in ("measurement", "survey"):
        assert "facts_sha256" not in env[block]


def test_the_case_name_is_prefixed_so_it_can_never_be_a_corpus_case():
    assert lx.case_name("ahmed_variant_001.step", SHA) == "platform__ahmed_variant_001__9c8b9b95"
    assert lx.case_name("Bend Elbow 003.STEP", SHA) == "platform__bend_elbow_003__9c8b9b95"
    # a file named exactly like a corpus case still cannot collide with one
    assert lx.case_name("tee_wye__tee_wye_001.step", SHA).startswith("platform__")
    assert lx.case_name("", "") == "platform__part__"


def test_the_step_travels_without_the_ledger_s_events():
    """A plan's events are a median 24 kB and every fact the loop reads off them is already in `plan`, the
    envelope and the answers."""
    env = _envelope()
    assert "ledger" not in env["step"]
    assert env["step"]["envelope"]["cells_high"] == 93200
    assert len(json.dumps(env)) < 20_000


def test_the_envelope_is_json_and_names_its_schema(tmp_path):
    env = _envelope()
    assert env["schema"] == lx.EXPORT_SCHEMA == "meshpipeline.learning_export.v1"
    path = lx.write_envelope(env, tmp_path)
    assert path.name == f"{JOB}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["case"] == env["case"]
    # re-exporting one job replaces its own file and nothing else
    lx.write_envelope(env, tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 1
