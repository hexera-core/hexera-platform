# Responsibility: Verify each role and category keeps its marker and message, over a closed, provider-free vocabulary.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference.failure_markers import (
    emits_markers,
    marker_for,
    marker_reason,
)
from meshpipeline.contracts.model_routing import FailureCategory
from meshpipeline.errors import FailureClass, classify_api_failure, user_message_for

# THE MATRIX: (role, category) -> exact marker, measured from the pre-routing router
# builder/reviewer used a precise reason; intake collapsed everything to "unavailable".
MATRIX: list[tuple[str, FailureCategory, str, FailureClass]] = [
    # role,            category,                             exact marker,                    taxonomy
    ("builder", FailureCategory.CIRCUIT_OPEN,        "<<API_FAILURE:builder_circuit_open>>",   FailureClass.PROVIDER_DOWN),
    ("builder", FailureCategory.EMPTY_RESPONSE,      "<<API_FAILURE:builder_empty_response>>", FailureClass.PROVIDER_TRANSIENT),
    ("builder", FailureCategory.TIMEOUT,             "<<API_FAILURE:builder_timeout>>",        FailureClass.PROVIDER_TRANSIENT),
    ("builder", FailureCategory.RATE_LIMIT,          "<<API_FAILURE:builder_rate_limit>>",     FailureClass.PROVIDER_TRANSIENT),
    ("builder", FailureCategory.CONNECTION,          "<<API_FAILURE:builder_connection>>",     FailureClass.DEPENDENCY_DOWN),
    ("builder", FailureCategory.SERVICE_UNAVAILABLE, "<<API_FAILURE:builder_server_error>>",   FailureClass.PROVIDER_DOWN),
    ("builder", FailureCategory.AUTH,                "<<API_FAILURE:builder_non_transient>>",  FailureClass.PROVIDER_DOWN),
    ("builder", FailureCategory.INVALID_REQUEST,     "<<API_FAILURE:builder_non_transient>>",  FailureClass.PROVIDER_DOWN),
    ("builder", FailureCategory.APPLICATION_DEFECT,  "<<API_FAILURE:builder_non_transient>>",  FailureClass.PROVIDER_DOWN),
    # planner has ALWAYS emitted builder_* markers - it called the builder's function.
    ("planner", FailureCategory.TIMEOUT,             "<<API_FAILURE:builder_timeout>>",        FailureClass.PROVIDER_TRANSIENT),
    ("planner", FailureCategory.SERVICE_UNAVAILABLE, "<<API_FAILURE:builder_server_error>>",   FailureClass.PROVIDER_DOWN),
    # the reviewer has ALWAYS emitted reviewer_* markers.
    ("visual_reviewer", FailureCategory.CIRCUIT_OPEN,        "<<API_FAILURE:reviewer_circuit_open>>", FailureClass.PROVIDER_DOWN),
    ("visual_reviewer", FailureCategory.RATE_LIMIT,          "<<API_FAILURE:reviewer_rate_limit>>",   FailureClass.PROVIDER_TRANSIENT),
    ("visual_reviewer", FailureCategory.SERVICE_UNAVAILABLE, "<<API_FAILURE:reviewer_server_error>>", FailureClass.PROVIDER_DOWN),
    ("visual_reviewer", FailureCategory.TIMEOUT,             "<<API_FAILURE:reviewer_timeout>>",      FailureClass.PROVIDER_TRANSIENT),
    ("visual_reviewer", FailureCategory.APPLICATION_DEFECT,  "<<API_FAILURE:reviewer_non_transient>>", FailureClass.PROVIDER_DOWN),
    # intake: only ever circuit_open or unavailable.
    ("intake", FailureCategory.CIRCUIT_OPEN,        "<<API_FAILURE:intake_circuit_open>>", FailureClass.PROVIDER_DOWN),
    ("intake", FailureCategory.TIMEOUT,             "<<API_FAILURE:intake_unavailable>>",  FailureClass.PROVIDER_TRANSIENT),
    ("intake", FailureCategory.RATE_LIMIT,          "<<API_FAILURE:intake_unavailable>>",  FailureClass.PROVIDER_TRANSIENT),
    ("intake", FailureCategory.SERVICE_UNAVAILABLE, "<<API_FAILURE:intake_unavailable>>",  FailureClass.PROVIDER_TRANSIENT),
    ("intake", FailureCategory.AUTH,                "<<API_FAILURE:intake_unavailable>>",  FailureClass.PROVIDER_TRANSIENT),
]


@pytest.mark.parametrize("role,category,marker,fc", MATRIX,
                         ids=[f"{r}-{c.value}" for r, c, _m, _fc in MATRIX])
def test_each_role_and_category_keeps_its_marker_taxonomy_and_user_message(
        role, category, marker, fc):
    assert marker_for(role, category) == marker
    assert classify_api_failure(marker) is fc
    assert user_message_for(classify_api_failure(marker)) == user_message_for(fc)


def test_the_mapping_is_exhaustive_over_every_internal_category():
    for role in ("builder", "planner", "visual_reviewer", "intake"):
        for category in FailureCategory:
            reason = marker_reason(role, category)
            assert reason, f"{role}/{category.value} has no marker reason"


def test_the_marker_vocabulary_stays_closed():
    established = {"circuit_open", "empty_response", "timeout", "rate_limit", "connection",
                   "server_error", "transient", "non_transient", "unavailable"}
    emitted = {marker_reason(role, c)
               for role in ("builder", "visual_reviewer", "intake")
               for c in FailureCategory}
    assert emitted <= established, f"new marker reasons introduced: {emitted - established}"


def test_the_new_routing_conditions_reuse_an_established_transient_reason():
    for category in (FailureCategory.OVERLOAD, FailureCategory.MODEL_UNAVAILABLE):
        assert marker_for("builder", category) == "<<API_FAILURE:builder_transient>>"
        assert classify_api_failure(marker_for("builder", category)) is FailureClass.PROVIDER_TRANSIENT


def test_a_non_transient_marker_classifies_as_a_non_retryable_outage_not_transient():
    for marker in ("<<API_FAILURE:builder_non_transient>>",
                   "<<API_FAILURE:reviewer_non_transient>>",
                   "non_transient"):
        fc = classify_api_failure(marker)
        assert fc is FailureClass.PROVIDER_DOWN, marker
        assert not fc.is_retryable, f"{marker} must never be retried"
    # and a genuinely transient marker still classifies transient - the guard is narrow
    assert classify_api_failure("<<API_FAILURE:builder_transient>>") is FailureClass.PROVIDER_TRANSIENT


def test_no_provider_specific_terminology_reaches_the_marker_vocabulary():
    for role in ("builder", "planner", "visual_reviewer", "intake"):
        for c in FailureCategory:
            m = marker_for(role, c).lower()
            for vendor in ("deepinfra", "deepseek", "openai", "fireworks", "together", "qwen",
                           "glm", "zai"):
                assert vendor not in m, f"{m} names a provider"


def test_the_summarizer_emits_no_marker_and_says_so():
    assert not emits_markers("summarizer")
    with pytest.raises(KeyError, match="does not emit api_failure markers"):
        marker_for("summarizer", FailureCategory.TIMEOUT)


def test_an_unknown_role_fails_clearly_rather_than_guessing():
    with pytest.raises(KeyError):
        marker_for("not_a_role", FailureCategory.TIMEOUT)
