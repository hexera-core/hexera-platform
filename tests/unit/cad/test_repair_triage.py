# Responsibility: Verify shadow triage advises only where it has evidence, abstains honestly, and structurally cannot act on a job.
from __future__ import annotations

import inspect

from meshpipeline.cad.repair import triage
from meshpipeline.cad.repair.triage import (
    ROUTE_ABSTAIN,
    ROUTE_ASK_CUSTOMER,
    ROUTE_CONSERVATIVE_REPAIR,
    ROUTE_MANUAL_CLEANUP,
    ROUTE_MESH_AS_IS,
    recommend,
)


def _report(*defects, **extra) -> dict:
    return {"report": {"summary": "inspected",
                       "defects": [{"code": c, "severity": s} for c, s in defects]}, **extra}


# IT CANNOT ACT


def test_the_recommender_takes_no_session_and_no_repository():
    # the guardrail is STRUCTURAL, not a promise in a comment: with no session and no repository
    # in the signature there is no path by which advice can move a job, touch geometry, or mark
    # anything delivered
    params = set(inspect.signature(recommend).parameters)
    assert params == {"repair_status", "report", "target_engine"}
    for forbidden in ("db", "session", "repo", "repository", "state"):
        assert forbidden not in params


def test_the_module_imports_nothing_that_could_write():
    # Over the IMPORTS, not the file's text: the module is entitled to explain in a comment that
    # it holds no session, and a substring search cannot tell that apart from holding one.
    import ast

    tree = ast.parse(inspect.getsource(triage))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    reachable = " ".join(sorted(imported))
    for forbidden in ("repositories", "session", "object_storage", "artifact"):
        assert forbidden not in reachable, (
            f"triage imports {forbidden}, which could give advice a way to act")


def test_every_recommendation_declares_itself_as_shadow_and_names_its_advisor():
    advice = recommend(repair_status="clean", report=_report()).to_dict()
    # a reader acting on this is contradicting the payload; and a stored recommendation has to say
    # which advisor produced it or the corpus cannot tell a baseline from a model
    assert advice["shadow"] is True
    assert advice["advisor"] == "rules-v1"


# WHEN IT ADVISES


def test_a_clean_file_is_advised_to_be_meshed_as_it_arrived():
    advice = recommend(repair_status="clean", report=_report())
    assert advice.route == ROUTE_MESH_AS_IS
    assert advice.confidence > 0.5 and not advice.abstained
    assert advice.profile == ""


def test_defects_in_how_the_file_was_written_are_advised_to_conservative_repair():
    advice = recommend(repair_status="repairable",
                       report=_report(("wire_gap", "error"), ("small_edge", "warning")))
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert advice.profile == "conservative"
    assert "not in what the part is" in advice.reasons[0]
    assert advice.evidence["defect_codes"] == ["small_edge", "wire_gap"]


def test_defects_that_are_questions_about_intent_go_to_a_person():
    for code in ("self_intersection", "non_manifold_surface", "small_face"):
        advice = recommend(repair_status="repairable", report=_report((code, "error")))
        # whether an overlap is a modelling error or deliberate is not a question a cap answers
        assert advice.route == ROUTE_MANUAL_CLEANUP, code
        assert advice.profile == "manual_review"


def test_a_judgement_defect_outranks_a_repairable_one():
    # a file with both still needs the person: repairing the easy half would not settle the hard
    advice = recommend(repair_status="repairable",
                       report=_report(("wire_gap", "error"), ("self_intersection", "error")))
    assert advice.route == ROUTE_MANUAL_CLEANUP


def test_an_unusable_file_sends_us_back_to_the_customer():
    advice = recommend(repair_status="unrepairable", report=_report(("invalid_brep", "fatal")))
    assert advice.route == ROUTE_ASK_CUSTOMER
    # and it is more certain when the inspection called it fatal than when it did not
    softer = recommend(repair_status="repairable", report=_report(("invalid_brep", "error")))
    assert advice.confidence > softer.confidence


def test_asking_the_customer_outranks_everything_else():
    advice = recommend(repair_status="unrepairable",
                       report=_report(("invalid_brep", "fatal"), ("wire_gap", "error"),
                                      ("self_intersection", "error")))
    assert advice.route == ROUTE_ASK_CUSTOMER


# WHEN IT ABSTAINS


def test_an_inspection_that_fell_over_produces_no_advice():
    advice = recommend(repair_status="inconclusive",
                       report={"service_failure": "RuntimeError: occt unavailable"})
    assert advice.abstained
    # OUR failure says nothing about the file; advising on it would invent a verdict
    assert "did not complete" in advice.abstain_reason
    assert advice.confidence == 0.0


def test_an_inconclusive_inspection_produces_no_advice():
    advice = recommend(repair_status="inconclusive", report=_report())
    assert advice.abstained and "inconclusive" in advice.abstain_reason


def test_a_job_with_no_report_produces_no_advice():
    for empty in (None, {}):
        advice = recommend(repair_status="", report=empty)
        assert advice.abstained and "no inspection report" in advice.abstain_reason


def test_a_defect_the_baseline_has_no_rule_for_is_admitted_rather_than_guessed():
    advice = recommend(repair_status="repairable",
                       report=_report(("engine_staging_failure", "fatal")))
    assert advice.abstained
    assert "no rule covers engine_staging_failure" in advice.abstain_reason


def test_abstention_is_not_dressed_up_as_low_confidence():
    advice = recommend(repair_status="inconclusive", report=_report())
    # "I do not know" and "probably repair" are different answers, and a confidence of 0.1 on a
    # real route would read as the second
    assert advice.route == ROUTE_ABSTAIN
    assert advice.abstain_reason and advice.reasons == ()


def test_a_report_that_claims_defects_but_lists_none_is_not_advised_on():
    advice = recommend(repair_status="repairable", report={"report": {"summary": "x"}})
    assert advice.abstained and "not readable as advice" in advice.abstain_reason


def test_a_malformed_report_never_raises():
    # an older or hand-edited payload must not break the operator's screen
    for junk in ({"report": "not-a-mapping"}, {"report": {"defects": "nope"}},
                 {"report": {"defects": [None, 7, {}]}}):
        assert recommend(repair_status="repairable", report=junk).abstained
