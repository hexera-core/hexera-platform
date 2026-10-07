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
    # FATAL means there is no geometry to work with - unreadable, or no faces at all. Only a
    # different file helps, so this is the one route that asks the customer for one.
    advice = recommend(repair_status="unrepairable", report=_report(("invalid_brep", "fatal")))
    assert advice.route == ROUTE_ASK_CUSTOMER


def test_an_invalid_part_we_cannot_localise_goes_to_a_person_not_the_customer():
    # THE DECISION THIS PINS: before defects were localised, any invalid B-rep went straight back
    # to the customer - which meant asking them for a new file having diagnosed nothing. An
    # invalid part we cannot point at is now a person's problem first.
    advice = recommend(repair_status="repairable", report=_report(("invalid_brep", "error")))
    assert advice.route == ROUTE_MANUAL_CLEANUP
    assert advice.profile == "manual_review"


def test_the_unattributed_case_says_so_in_its_reasoning():
    report = {"report": {"defects": [{"code": "invalid_brep", "severity": "error",
                                      "details": {"reason": "unattributed"}}]}}
    advice = recommend(repair_status="repairable", report=report)
    assert advice.route == ROUTE_MANUAL_CLEANUP
    assert "attributes it to no entity" in advice.reasons[0]


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


# LOCATED FAILURE POINTS - what a repair aims at


def _located(*entities, defects=None) -> dict:
    """A report in the shape the localiser produces: summary defects plus located entities."""
    codes = defects or [(e["code"], e.get("severity", "error")) for e in entities]
    return {"report": {"summary": "inspected",
                       "defects": [{"code": c, "severity": s} for c, s in codes],
                       "entities": list(entities)}}


def _entity(code, *, entity="edge:8", severity="error", region="unknown", **measurements) -> dict:
    return {"code": code, "severity": severity, "entity": entity, "region": region,
            "message": f"{code} here", "measurements": measurements,
            "location": {"centroid": [5.0, 5.0, 10.0]}}


def test_a_hole_we_can_point_at_is_advised_to_repair_even_though_the_part_is_invalid():
    # THE WHOLE POINT OF LOCALISATION. A part with a hole is invalid, and before this every such
    # STEP routed to the customer - so the conservative repair could never be recommended for the
    # one format it can actually repair.
    advice = recommend(repair_status="repairable",
                       report=_located(_entity("open_shell", boundary_edges=[8, 12, 2, 6])))

    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert advice.profile == "conservative"


def test_the_advice_carries_what_a_repair_would_aim_at():
    advice = recommend(repair_status="repairable",
                       report=_located(_entity("open_shell", boundary_edges=[8, 12, 2, 6],
                                               boundary_length=40.0)))

    # a route without targets is just a verdict on the whole part
    assert len(advice.targets) == 1
    target = advice.targets[0]
    assert target["entity"] == "edge:8"
    assert target["measurements"]["boundary_edges"] == [8, 12, 2, 6]
    assert target["location"]["centroid"] == [5.0, 5.0, 10.0]
    assert advice.to_dict()["targets"][0]["entity"] == "edge:8"


def test_a_defect_inside_the_part_is_carried_as_such():
    advice = recommend(repair_status="repairable",
                       report=_located(_entity("open_shell", entity="edge:3",
                                               region="interior")))
    # the one nobody spots by looking at the part in a viewer
    assert advice.targets[0]["region"] == "interior"


def test_targets_are_worst_first():
    advice = recommend(
        repair_status="repairable",
        report=_located(_entity("small_edge", entity="edge:1", severity="warning"),
                        _entity("open_shell", entity="edge:9", severity="error")))
    assert [t["entity"] for t in advice.targets] == ["edge:9", "edge:1"]


def test_a_judgement_defect_still_wins_and_still_carries_its_targets():
    advice = recommend(
        repair_status="repairable",
        report=_located(_entity("open_shell", entity="edge:9"),
                        _entity("self_intersection", entity="face:4")))
    assert advice.route == ROUTE_MANUAL_CLEANUP
    # and the person is pointed at the defect that needs the judgement, not at the easy one
    assert [t["entity"] for t in advice.targets] == ["face:4"]


def test_a_thousand_slivers_do_not_become_a_thousand_targets():
    many = [_entity("small_edge", entity=f"edge:{i}", severity="warning") for i in range(200)]
    advice = recommend(repair_status="repairable", report=_located(*many))
    assert len(advice.targets) == 20
    # the real count is still reported, so nothing is hidden by the trim
    assert advice.evidence["located_entities"] == 200


def test_advice_with_no_located_entities_still_routes_but_aims_at_nothing():
    # the surface path and older reports carry no entities; the route is unchanged and the
    # absence of targets is the honest signal that there is nothing to aim at
    advice = recommend(repair_status="repairable", report=_report(("wire_gap", "error")))
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert advice.targets == ()
