# Responsibility: Verify the brief carries what the user settled, judged by the real gates, and claims no authorization.
from __future__ import annotations

from meshpipeline.application.intake_brief import (
    FAIL,
    NEVER_EXPOSED,
    PASS,
    STATUSES,
    UNKNOWN,
    build_brief,
    build_checks,
)


class _Session:

    def __init__(self, **over):
        base = {
            "request_txt": "A transonic wing-body external aerodynamics mesh. " * 4,
            "review_brief_txt": "Confirm the wall and farfield patches are present. " * 3,
            "purpose": "external_cfd", "mesh_engine": "cfmesh",
            "input_kind": "body-surface", "dimensionality": "3D",
            "requested_mesh_fidelity": "standard", "domain": "wing-body cruise",
            "intake_patches": [{"name": "aircraft", "type": "wall"},
                               {"name": "farfield", "type": "farfield"}],
            "engine_params": {}, "intake_submitted": True,
            # the machinery that must never cross the boundary
            "intake_gate": {"selection": {"engine": "cfmesh"},
                            "admission": {"token": "tok_SECRET",
                                          "verdict": "supported"}},
            "llm_metadata": [{"model": "deepseek-v4-pro", "tokens": 3119}],
            "messages": [{"role": "user", "content": "hello"}],
            "owner_id": "user-42",
        }
        base.update(over)
        for k, v in base.items():
            setattr(self, k, v)


def _internal() -> _Session:
    return _Session(purpose="internal_cfd", mesh_engine="snappy",
                    intake_patches=[{"name": "pipe_wall", "type": "wall"},
                                    {"name": "inlet", "type": "inlet"},
                                    {"name": "outlet", "type": "outlet"}])


# the brief


def test_the_finalized_requirements_reach_the_boundary():
    b = build_brief(_Session())
    assert b["purpose"] == "external_cfd"
    assert b["engine"] == "cfmesh"
    assert b["input_kind"] == "body-surface"
    assert b["dimensionality"] == "3D"
    assert b["mesh_fidelity"] == "standard"
    assert b["label"] == "wing-body cruise"
    assert b["boundary_assignments"] == [{"name": "aircraft", "role": "wall"},
                                         {"name": "farfield", "role": "farfield"}]
    assert b["submitted"] is True


def test_no_brief_before_intake_settles_one():
    assert build_brief(_Session(request_txt=None)) is None
    assert build_brief(_Session(request_txt="   ")) is None
    assert build_brief(None) is None


def test_authorization_and_accountability_never_cross_the_boundary():
    b = build_brief(_Session())
    flat = repr(b)
    for secret in ("tok_SECRET", "admission", "intake_gate", "deepseek", "user-42"):
        assert secret not in flat, f"{secret!r} leaked into the brief"
    for column in NEVER_EXPOSED:
        assert column not in b


def test_a_field_the_user_never_settled_is_absent_not_blank():
    b = build_brief(_Session(requested_mesh_fidelity=None, domain="", engine_params={}))
    assert "mesh_fidelity" not in b
    assert "label" not in b
    assert "engine_params" not in b, "an empty dict rendered as a settled value"


def test_a_malformed_patch_entry_is_dropped_rather_than_crashing():
    b = build_brief(_Session(intake_patches=[{"name": "wall", "type": "wall"},
                                             "not-a-dict", {"type": "inlet"}]))
    assert b["boundary_assignments"] == [{"name": "wall", "role": "wall"}]


def test_the_brief_is_json_safe():
    import json
    json.dumps(build_brief(_Session()))


# the checks


def test_every_check_carries_the_agreed_shape():
    for c in build_brief(_Session())["checks"]:
        assert set(c) == {"id", "label", "status", "detail", "stage", "sequence"}
        assert c["status"] in STATUSES
        assert c["stage"] == "intake"
        assert "<" not in c["label"] and "<" not in c["detail"], "HTML in a backend value"
    seq = [c["sequence"] for c in build_brief(_Session())["checks"]]
    assert seq == list(range(1, len(seq) + 1))


def test_checks_are_the_applications_gates_not_a_fixed_count():
    full = build_brief(_Session())["checks"]
    bare = build_brief(_Session(intake_patches=[], dimensionality=None))["checks"]
    assert len(bare) < len(full)
    assert {c["id"] for c in bare} < {c["id"] for c in full}


def test_the_real_compatibility_gate_decides_the_engine_check():
    ok = build_brief(_Session())
    bad = build_brief(_Session(input_kind="solid-body"))   # cfmesh cannot mesh that
    assert next(c for c in ok["checks"] if c["id"] == "engine_compatible")["status"] == PASS
    fail = next(c for c in bad["checks"] if c["id"] == "engine_compatible")
    assert fail["status"] == FAIL and fail["detail"], "a failure with no reason"


def test_a_rule_that_cannot_be_evaluated_is_unknown_not_passed():
    b = build_brief(_Session(mesh_engine=None))
    c = next(c for c in b["checks"] if c["id"] == "engine_compatible")
    assert c["status"] == UNKNOWN


def test_boundary_roles_are_judged_against_the_declared_purpose():
    ext = build_brief(_Session())
    assert next(c for c in ext["checks"]
                if c["id"] == "boundary_roles_valid")["status"] == PASS
    # farfield is not a role internal_cfd admits
    wrong = build_brief(_Session(purpose="internal_cfd", mesh_engine="snappy"))
    c = next(c for c in wrong["checks"] if c["id"] == "boundary_roles_valid")
    assert c["status"] == FAIL and "farfield" in c["detail"]


def test_internal_flow_passes_its_own_boundary_vocabulary():
    b = build_brief(_internal())
    by_id = {c["id"]: c for c in b["checks"]}
    assert by_id["boundary_roles_valid"]["status"] == PASS
    assert by_id["engine_compatible"]["status"] == PASS
    assert "3 boundary assignment(s)" in by_id["boundary_roles_valid"]["label"]


def test_duplicate_boundary_names_fail_because_the_viewer_maps_by_name():
    b = build_brief(_Session(intake_patches=[{"name": "w", "type": "wall"},
                                             {"name": "w", "type": "wall"}]))
    c = next(c for c in b["checks"] if c["id"] == "boundary_names_unique")
    assert c["status"] == FAIL and "w" in c["detail"]


def test_an_unsubmitted_brief_does_not_claim_authorization():
    b = build_brief(_Session(intake_submitted=False))
    c = next(c for c in b["checks"] if c["id"] == "submission_authorized")
    assert c["status"] == UNKNOWN


def test_missing_acceptance_criteria_fails_the_capture_check():
    b = build_brief(_Session(review_brief_txt=None))
    c = next(c for c in b["checks"] if c["id"] == "requirements_captured")
    assert c["status"] == FAIL


def test_the_status_vocabulary_is_closed_and_nothing_escapes_it():
    assert STATUSES == frozenset({"pass", "fail", "unknown"})
    for sess in (_Session(), _internal(), _Session(request_txt="x" * 90,
                                                   review_brief_txt=None,
                                                   mesh_engine=None,
                                                   dimensionality="7D",
                                                   intake_patches=[],
                                                   intake_submitted=False)):
        for c in build_brief(sess)["checks"]:
            assert c["status"] in STATUSES
    # the guard itself: a hand-built brief cannot smuggle one through either
    for c in build_checks({"purpose": "external_cfd", "engine": "cfmesh",
                           "input_kind": "body-surface"}):
        assert c["status"] in STATUSES


def test_the_brief_survives_being_rebuilt_from_the_same_session():
    s = _Session()
    assert build_brief(s) == build_brief(s)
