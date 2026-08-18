# Responsibility: Carry the inputs one visual review interaction needs.
# Boundaries: a value type.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshpipeline.contracts.event_stream import (
    ExecutionEventPublisher,
    require_execution_publisher,
)

#: Every publication one visual review interaction makes, by the name it awaits. Named here
#: because this is the type that carries the port; the sites themselves are in visual.py.
REVIEW_PUBLICATIONS: tuple[str, ...] = ("astage", "anote", "ascreenshot", "averdict", "awarn")


@dataclass(frozen=True)
class VisualReviewInteractionInputs:

    # identity
    job_id: str
    step_basename: str
    retry_count: int

    # workspace + manifest facts. `manifest` is the reviewer's own read of build output - it is
    # NOT an artifact door: the surface resolves and confines every path it touches.
    workspace: Path
    manifest: dict[str, Any]
    review_save_dir: Path
    mesh_units: str

    # what the review is judged against
    review_brief: str
    request: str
    axis_names: list[str]

    # engine + user-declared purpose drive the prompt's workflow line and the composed rubric.
    # `user_id` reaches the provider router unchanged.
    engine: str
    purpose: str
    user_id: str

    # The LIVE execution publisher, not copied state: it streams to the user's session, so a
    # snapshot would be a snapshot of a socket. It is the execution-scoped port, so every
    # publication through it is awaited - the ownership check in front of each one is a database
    # round trip that a synchronous spelling could not make.
    publish: ExecutionEventPublisher

    # `user_dispute` re-opens a prior verdict - optional, so it comes last (after `publish`, which
    # has no default).
    user_dispute: dict | None = None
    # WHICH ARTIFACT THIS REVIEW IS LOOKING AT. A dispute reviews twice, and the two reviews ask
    # different questions; without this the second one was told it was inspecting "the delivered
    # mesh", which by then is the mesh the rebuild replaced.
    dispute_phase: str = ""
    # What the FIRST review found at each flag, and what the builder says it changed. Carried from
    # durable state so the post-rebuild review compares rather than re-derives.
    prior_flag_findings: tuple = ()
    builder_flag_responses: tuple = ()
    prior_reviewer_feedback: str = ""

    def __post_init__(self) -> None:
        for name in ("job_id", "mesh_units"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        # A live port, never a primitive snapshot - proven against the port itself, by the
        # contract that owns it. THESE are the publications one review interaction makes: it
        # opens with a stage and a note, may publish its opening render, and ends in exactly one
        # of a verdict or a warned non-verdict.
        require_execution_publisher(
            self.publish, awaits=REVIEW_PUBLICATIONS, where="publish")
