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
     the learning loop uses for the measurement digest;
  6. the WITHOUT-THE-LOOK composition: both halves are recomputed under one build, the look block the
     composition carries as an input is never one of the compared fields, a field the look left alone is
     reported rather than dropped, and a replay that does not reproduce the stored row refuses to publish a
     diff at all.
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
        asking={"put": ["q_port_roles"],
                "record": {"q_port_roles": {"kind": "port_role",
                                            "default": "the default sentence for q_port_roles",
                                            "targets": [
                                                {"field": "openings[o1].role", "place": "o1",
                                                 "proposed": "inlet"},
                                                {"field": "openings[o2].role", "place": "o2",
                                                 "proposed": "outlet"}]}}},
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


# -------------------------------------------------------------------------------------------------
# 6. what the platform would have proposed WITHOUT the look
# -------------------------------------------------------------------------------------------------
# Hermetic: `geometry_survey.composition` is stubbed, because what is being held here is the
# BOOKKEEPING - which fields are compared, that the look block can never be one of them, that both halves are
# recomputed under one build, and that a replay which does not reproduce the stored row refuses to publish a
# diff. The real composition is exercised end to end against the local database, whose numbers are in
# geometry_agent/docs/learning_loop.md section 11.


def _made(*, representation="wall_shell", put=("q_port_roles",), roles=(("o1", "inlet"), ("o2", "outlet")),
          cells_high=93200, inlet="o1", places=(("junction", "j1"),), warnings=(), look_echo=True):
    """A stand-in for `geometry_survey.composition`'s return value, carrying the look block the real one does."""
    record = {}
    for qid in put:
        record[qid] = {"kind": "port_role", "default": f"the default sentence for {qid}",
                       "targets": [{"field": f"openings[{p}].role", "place": p, "proposed": v}
                                   for p, v in roles]}
    composed = {
        "representation": representation, "fluid_side": "inside",
        "forecast": {"cells_low": cells_high, "cells_high": cells_high, "over_cap": False, "tier": "under 1M"},
        "planner_block": {"representation": representation, "inlet_opening_id": inlet,
                          "inlet_bore_m": 0.1, "smallest_port_min_dim_m": 0.03,
                          "agent_forecast_cells": cells_high, "customer_cell_cap": None,
                          "places": [{"kind": k, "id": i, "where_m": [0.1, 0.2, 0.3]} for k, i in places]},
        "warnings": [{"kind": k} for k in warnings],
    }
    if look_echo:
        # the real composition carries the look through in both of these, which is exactly what must not be
        # counted as a difference
        composed["look"] = {"status": "ok", "impression": {"looks_like": "a tee"}}
        composed["planner_block"]["look"] = {"status": "ok"}
    return {"composed": composed, "asking": {"put": list(put), "record": record},
            "survey": SimpleNamespace(uncertainties=[SimpleNamespace(id=f"u_role_{p}") for p, _ in roles])}


def _stub_composition(monkeypatch, with_look, without_look):
    """`composition` answering one thing for the document that has a look and another for the one that has not,
    so a test can say what the look changed without running the package."""
    calls = []
    from meshpipeline.application import geometry_survey as gs

    def fake(document, **kwargs):
        calls.append("with" if isinstance(document.get("look"), dict) else "without")
        return with_look if isinstance(document.get("look"), dict) else without_look

    monkeypatch.setattr(gs, "composition", fake)
    return calls


def _doc(look_status: str = "ok", **over):
    doc = {"status": "ok", "facts": {"sha256": SHA}, "representation": "wall_shell",
           "stamp": {"platform_sha": "57684388", "agent_git_sha": "0.1.0+gefbd71927c",
                     "facts_schema_version": 3}}
    if look_status:
        doc["look"] = {"status": look_status, "impression": {"looks_like": "a tee"}}
    doc.update(over)
    return doc


def test_every_declared_proposal_field_is_produced_and_nothing_else_is_invented():
    """`PROPOSAL_FIELDS` is the closed list, and a field added to `proposal_of` without being declared fails
    here. The per-place and per-question keys carry an id, so they are held to the declared prefixes."""
    fields = lx.proposal_of(_made())
    assert set(lx.PROPOSAL_FIELDS) <= set(fields)
    extra = sorted(f for f in fields if f not in lx.PROPOSAL_FIELDS)
    assert extra == ["openings[o1].role", "openings[o2].role", "questions[q_port_roles].default"]
    assert all(f.startswith(lx.PROPOSAL_FIELD_PREFIXES) for f in extra)


def test_the_look_itself_is_never_one_of_the_compared_fields():
    """THE TRAP THIS LIST EXISTS FOR. `composed["look"]` and `planner_block["look"]` are the look copied
    through, so a diff over the whole document would report "the look changed the look" on every job that had
    one - 100 percent by construction, and a number that means nothing."""
    fields = lx.proposal_of(_made(look_echo=True))
    assert "look" not in fields and not any(f.endswith(".look") for f in fields)
    assert not any("impression" in (v or "") for v in fields.values())
    # and the comparison is unchanged whether the stand-in echoes the look or not
    assert lx.proposal_of(_made(look_echo=True)) == lx.proposal_of(_made(look_echo=False))


def test_a_field_the_look_left_alone_is_reported_and_not_dropped():
    """A job where the look changed nothing is as informative as one where it changed everything."""
    rows = lx.deltas(lx.proposal_of(_made()), lx.proposal_of(_made()))
    assert rows and all(r["changed"] is False for r in rows)
    assert {r["field"] for r in rows} >= set(lx.PROPOSAL_FIELDS)


def test_the_per_field_row_says_which_field_moved_and_to_what():
    a = lx.proposal_of(_made(representation="wall_shell", roles=(("o1", "inlet"), ("o5", "outlet"))))
    b = lx.proposal_of(_made(representation="fluid_domain", roles=(("o1", "inlet"), ("o5", "inlet"))))
    rows = {r["field"]: r for r in lx.deltas(a, b)}
    assert rows["representation"]["with_look"] == "wall_shell"
    assert rows["representation"]["without_look"] == "fluid_domain"
    assert rows["representation"]["changed"] is True
    assert rows["openings[o5].role"] == {"field": "openings[o5].role", "with_look": "outlet",
                                        "without_look": "inlet", "changed": True}
    assert rows["openings[o1].role"]["changed"] is False


def test_the_question_set_is_compared_because_a_look_can_raise_a_question(monkeypatch):
    """MEASURED on job c17d3523 in the local database: with the look the finder put `q_port_roles` and
    `q_look_dispute`, and without it `q_port_roles` alone. A per-job boolean would have said "something
    changed" and thrown away what."""
    calls = _stub_composition(monkeypatch, _made(put=("q_port_roles", "q_look_dispute")),
                              _made(put=("q_port_roles",)))
    out = lx.without_the_look(_doc(), _survey())
    assert calls == ["with", "without"]
    rows = {r["field"]: r for r in out["fields"]}
    assert rows["questions.put"]["with_look"] == '["q_port_roles", "q_look_dispute"]'
    assert rows["questions.put"]["without_look"] == '["q_port_roles"]'
    assert rows["questions.put"]["changed"] is True


def test_both_halves_are_recomposed_so_a_code_change_is_not_counted_as_the_look(monkeypatch):
    """The whole reason this is not "the stored row against a fresh composition": the row was written by the
    build that was deployed when the job ran."""
    calls = _stub_composition(monkeypatch, _made(), _made())
    out = lx.without_the_look(_doc(), _survey())
    assert calls == ["with", "without"]
    assert out["computed"] is True
    assert out["recomposed_under"]["platform_sha"] == "57684388"
    assert out["job_measured_under"]["agent_git_sha"] == "0.1.0+gefbd71927c"
    # the inputs the replay used travel, so the composition can be made again by hand
    assert out["inputs"]["purpose"] == "internal_cfd"


def test_a_replay_that_does_not_reproduce_the_stored_row_says_so(monkeypatch):
    """THE CHECK THAT FAILS DIFFERENTLY FROM WHAT IT CHECKS. The diff compares two compositions to each other
    and can never notice that neither is the one the customer saw; this compares the with-the-look half against
    the row the platform actually stored."""
    _stub_composition(monkeypatch, _made(representation="fluid_domain"), _made(representation="fluid_domain"))
    survey = _survey()                              # the row says wall_shell
    out = lx.without_the_look(_doc(), survey)
    assert out["computed"] is True
    assert out["replay_faithful"] is False
    assert {m["field"] for m in out["replay_mismatch"]} == {"representation"}
    assert out["replay_mismatch"][0] == {"field": "representation", "stored": "wall_shell",
                                        "replayed": "fluid_domain"}
    # and the envelope names it, because a delta list from an unfaithful replay is not about the look
    env = lx.envelope(job=_job(), source=_source(), interpretation=_interpretation(),
                      measurement=_measurement(), survey=survey, viewer_payload=VIEWER, counterfactual=out)
    assert any("does not reproduce the stored survey row" in w for w in env["absent"])


def test_a_row_that_cannot_check_the_replay_is_not_reported_as_faithful(monkeypatch):
    """A CHECK THAT CANNOT FAIL IS NOT A CHECK. A survey row with no comparable field vouches for nothing, and
    `replay_faithful: true` there would be the same blind spot as the thing it checks."""
    _stub_composition(monkeypatch, _made(), _made())
    out = lx.without_the_look(_doc(), _survey())
    assert out["replay_faithful"] is True and out["replay_checked"] >= 1

    bare = _survey()
    bare.composed_for, bare.asking, bare.planner_block = {}, {}, None
    out = lx.without_the_look(_doc(), bare)
    assert out["computed"] is True
    assert out["replay_faithful"] is None and out["replay_checked"] == 0
    assert out["replay_mismatch"] == []


def test_the_stored_row_is_read_for_every_field_it_carries():
    stored = lx.stored_proposal(_survey())
    assert stored["representation"] == "wall_shell"
    assert stored["questions.put"] == '["q_port_roles"]'
    # a field the row does not carry is absent rather than a guessed None
    assert "forecast.cells_high" not in stored
    assert "planner_block.inlet_bore_m" not in stored


@pytest.mark.parametrize("status,expect", [("failed", "failed"), ("not_attempted", "not_attempted"),
                                          ("pending", "pending")])
def test_a_job_whose_look_did_not_land_has_no_comparison_rather_than_a_clean_one(status, expect):
    """A look that failed composes the same document either way. Calling that "the look changed nothing" would
    count a deployment with no reader as evidence that the reader is not needed."""
    out = lx.without_the_look(_doc(look_status=status), _survey())
    assert out["computed"] is False and out["fields"] == []
    assert expect in out["why_not"]
    assert out["look_status"] == status


def test_a_job_with_no_look_block_at_all_is_the_same_answer():
    out = lx.without_the_look(_doc(look_status=""), _survey())
    assert out["computed"] is False and out["fields"] == []
    assert "the look on this job is absent" in out["why_not"]
    assert out["look_status"] == "not_attempted"


def test_a_composition_that_raises_is_a_reason_and_never_a_failed_export(monkeypatch):
    from meshpipeline.application import geometry_survey as gs
    monkeypatch.setattr(gs, "composition", lambda *a, **k: (_ for _ in ()).throw(ValueError("no facts")))
    out = lx.without_the_look(_doc(), _survey())
    assert out["computed"] is False and out["fields"] == []
    assert "could not be run again (ValueError: no facts)" in out["why_not"]


def test_a_counterfactual_that_could_not_be_computed_on_a_looked_job_is_named_absent():
    env = lx.envelope(job=_job(), source=_source(), interpretation=_interpretation(),
                      measurement=_measurement(), survey=_survey(), viewer_payload=VIEWER,
                      counterfactual={"computed": False, "why_not": "the composition broke",
                                      "look_status": lx.LOOK_OK})
    assert any("could not be computed: the composition broke" in w for w in env["absent"])
    # and a job whose look never landed is not counted as a failure of this measurement
    env = lx.envelope(job=_job(), source=_source(), interpretation=_interpretation(),
                      measurement=_measurement(), survey=_survey(), viewer_payload=VIEWER,
                      counterfactual={"computed": False, "why_not": "the look failed",
                                      "look_status": "failed"})
    assert env["absent"] == []


def test_the_look_ok_word_is_the_survey_module_s_own():
    """This module may not import the package at module scope, so the constant is a copy; it is pinned here
    rather than trusted."""
    from meshpipeline.application.geometry_survey import LOOK_OK
    assert lx.LOOK_OK == LOOK_OK == "ok"


def test_whether_the_two_builds_are_the_same_has_three_answers():
    """R2 turns on it, so "cannot tell" is not folded into "different". A wheel built outside the vendor script
    has no `+g<sha>` and a measurement stamped with a bare sha has no version."""
    assert lx.same_build("0.1.0+ga3a9ce94d2", "0.1.0+ga3a9ce94d2") is True
    # the short sha is a prefix of the long one, which is the same commit
    assert lx.same_build("0.1.0+ga3a9ce94d2", "0.1.0+ga3a9ce94d23cdb2bccb3") is True
    assert lx.same_build("0.1.0+ga3a9ce94d2", "0.1.0+g1fb530cf5e") is False
    # neither side names a commit, or one side is empty: not a claim either way
    assert lx.same_build("0.1.0", "0.1.0") is None
    assert lx.same_build("", "0.1.0+ga3a9ce94d2") is None
    assert lx.same_build("0.1.0+ga3a9ce94d2", "") is None
    # a bare git sha on the row against a version that carries it
    assert lx.same_build("0.1.0+ga3a9ce94d2", "a3a9ce94d2") is True


def test_a_value_compared_is_a_string_and_a_missing_one_stays_missing():
    assert lx._as_text(None) is None
    assert lx._as_text(True) == "true" and lx._as_text(False) == "false"
    assert lx._as_text(93200) == "93200"
    assert lx._as_text(["b", "a"]) == '["b", "a"]'
    # the places are kind:id and carry no coordinate: a float in a compared string is noise
    assert lx._places({"places": [{"kind": "junction", "id": "j2", "where_m": [1.0, 2.0, 3.0]},
                                  {"kind": "stub", "id": "s1"}]}) == "junction:j2,stub:s1"
    assert lx._places(None) is None


def test_the_counterfactual_rides_on_the_envelope_and_the_envelope_is_still_json(monkeypatch):
    _stub_composition(monkeypatch, _made(), _made(representation="fluid_domain"))
    out = lx.without_the_look(_doc(), _survey())
    env = lx.envelope(job=_job(), source=_source(), interpretation=_interpretation(),
                      measurement=_measurement(), survey=_survey(), viewer_payload=VIEWER, counterfactual=out)
    assert env["look_counterfactual"]["schema"] == lx.COUNTERFACTUAL_SCHEMA
    assert json.loads(json.dumps(env, default=str))["look_counterfactual"]["computed"] is True
