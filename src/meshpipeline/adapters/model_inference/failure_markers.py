# Responsibility: Recognise the in-band markers a provider uses to say why it refused.
# Boundaries: classification of provider output; it decides no retry policy.
# Collaborates with: contracts/model_routing.py for the failure categories.
from __future__ import annotations

from meshpipeline.contracts.model_routing import FailureCategory

# the reason strings, per vocabulary. Both dicts MUST be exhaustive over FailureCategory.

_RICH: dict[FailureCategory, str] = {
 # conditions the old router named precisely
    FailureCategory.CIRCUIT_OPEN: "circuit_open",
    FailureCategory.EMPTY_RESPONSE: "empty_response",
    FailureCategory.TIMEOUT: "timeout",
    FailureCategory.RATE_LIMIT: "rate_limit",
    FailureCategory.CONNECTION: "connection",
    # openai.InternalServerError was `server_error`. NOT "service_unavailable": that string
    # would fall through classify_api_failure's substring checks to PROVIDER_DOWN by accident
    # rather than by intent, and would change the stored marker on every 5xx ever recorded.
    FailureCategory.SERVICE_UNAVAILABLE: "server_error",
 # conditions routing GAINED; they reuse an existing reason rather than add one
    # Admission saturation and a temporarily-withdrawn model are both "the provider cannot take
    # this right now" - exactly what `transient` has always meant here.
    FailureCategory.OVERLOAD: "transient",
    FailureCategory.MODEL_UNAVAILABLE: "transient",
 # everything the old router lumped into `non_transient` (it did not retry, it broke)
    FailureCategory.AUTH: "non_transient",
    FailureCategory.INSUFFICIENT_BALANCE: "non_transient",
    FailureCategory.INVALID_REQUEST: "non_transient",
    FailureCategory.APPLICATION_DEFECT: "non_transient",
    FailureCategory.TOOL_SCHEMA: "non_transient",
    FailureCategory.POLICY_REJECTION: "non_transient",
    FailureCategory.INVALID_OUTPUT: "non_transient",
    FailureCategory.QUALITY_FAILED: "non_transient",
}

_MINIMAL: dict[FailureCategory, str] = {
    FailureCategory.CIRCUIT_OPEN: "circuit_open",
    **{c: "unavailable" for c in FailureCategory if c is not FailureCategory.CIRCUIT_OPEN},
}

# role -> (marker prefix, vocabulary). The prefix is NOT always the role name:
#   * planner has always emitted `builder_*` - it called the builder's function;
#   * both reviewers have always emitted `reviewer_*` - the user-facing meaning is "the review
#     could not run", not which reviewer variant ran.
# Preserving those prefixes is what keeps stored markers identical while the roles become
# independently routable.
_ROLE_VOCAB: dict[str, tuple[str, dict[FailureCategory, str]]] = {
    "builder":         ("builder",  _RICH),
    "planner":         ("builder",  _RICH),
    "visual_reviewer": ("reviewer", _RICH),
    "intake":          ("intake",   _MINIMAL),
    # `summarizer` is deliberately absent: a search that cannot be distilled has never produced
    # an api_failure - it degrades the builder's context and the job continues. Asking for its
    # marker is a bug, and raises below.
}

_MISSING = sorted(c.value for c in FailureCategory if c not in _RICH or c not in _MINIMAL)
if _MISSING:  # pragma: no cover - an import-time guard, not a runtime path
    raise RuntimeError(
        "failure_markers: no api_failure marker defined for FailureCategory "
        f"{_MISSING}. Every internal category MUST translate to an established marker, or a "
        "provider failure would reach the user as an unclassified outage.")


def marker_reason(role: str, category: FailureCategory) -> str:
    try:
        _prefix, vocab = _ROLE_VOCAB[role]
    except KeyError:
        raise KeyError(
            f"no api_failure vocabulary for role {role!r}; roles that emit markers: "
            f"{sorted(_ROLE_VOCAB)}") from None
    return vocab[category]


def marker_for(role: str, category: FailureCategory) -> str:
    try:
        prefix, vocab = _ROLE_VOCAB[role]
    except KeyError:
        raise KeyError(
            f"role {role!r} does not emit api_failure markers; roles that do: "
            f"{sorted(_ROLE_VOCAB)}") from None
    return f"<<API_FAILURE:{prefix}_{vocab[category]}>>"


def emits_markers(role: str) -> bool:
    return role in _ROLE_VOCAB
