# Responsibility: Turn one builder turn's result into the state patch the graph applies.
# Boundaries: only fields the data contract declares for the builder may appear.
from __future__ import annotations

from dataclasses import dataclass

#: The EXACT set of state keys a Builder turn may return.
BUILDER_RETURN_KEYS = frozenset({
    "retry_count", "builder_noop_count", "openfoam_workspace",
    "request_txt", "review_brief_txt", "api_failure",
    "builder_deadline_epoch",   # Aggregate-budget bookkeeping (set once, carried, never reset)
    # What the builder DECLARES it changed per engineer-flagged region. A claim the
    # post-rebuild review measures for itself - never a substitute for that review.
    "builder_flag_responses",
})


@dataclass(frozen=True)
class TurnPatch:

    workspace: str
    request_txt: str
    review_brief_txt: str
    deadline_epoch: float
    retry_count: int
    noop_count: int = 0
    api_failure: str = ""
    flag_responses: list | None = None

    def state(self) -> dict:
        fields = {
            "retry_count": self.retry_count,
            "openfoam_workspace": self.workspace,
            "request_txt": self.request_txt,
            "review_brief_txt": self.review_brief_txt,
            "builder_deadline_epoch": self.deadline_epoch,
        }
        # Both are omitted rather than defaulted when they do not apply: an `api_failure` of ""
        # on a healthy turn would look like a cleared failure, and the provider-failure exit
        # deliberately carries no no-op count.
        if self.api_failure:
            fields["api_failure"] = self.api_failure
        if not self.api_failure:
            fields["builder_noop_count"] = self.noop_count
        # Omitted when empty: an ordinary build must not write an empty list over a
        # declaration a previous attempt of the same dispute already made.
        if self.flag_responses:
            fields["builder_flag_responses"] = list(self.flag_responses)
        return builder_state(**fields)


def builder_state(**fields) -> dict:
    unauthorised = set(fields) - BUILDER_RETURN_KEYS
    if unauthorised:
        raise AssertionError(
            f"node_builder attempted to write state key(s) it does not own: "
            f"{sorted(unauthorised)}. The Builder write surface is exactly "
            f"{sorted(BUILDER_RETURN_KEYS)} - execution truth, gate/reviewer verdicts, approved "
            "intent and final_result belong to other owners.")
    return dict(fields)


__all__ = ["BUILDER_RETURN_KEYS", "TurnPatch", "builder_state"]
