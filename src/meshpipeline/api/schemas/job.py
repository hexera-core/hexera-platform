# Responsibility: Declare the request and response shapes of the job and dispute endpoints.
# Boundaries: the wire contract; no behaviour.
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meshpipeline.persistence.models import ArtifactType, JobStatus


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    artifact_type: ArtifactType
    download_url: str
    size_bytes: int
    created_at: datetime
    # WHAT IT IS, in the user's words. The ENGINE declares it (Deliverable.label) -
    # the UI used to hardcode "OpenFOAM case" for every engine, which mislabelled a
    # gmsh Abaqus deck. A client renders this string; it never maps a type itself.
    label: str = ""


class JobStatus_(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    current_attempt: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    artifacts: list[ArtifactOut] = []
    # A FAILED run can still have produced a mesh (gates passed, the reviewer just
    # would not sign it off - the bounded retry loop ran out). The user is entitled
    # to see it and judge it, so the status carries the review outcome and whether
    # there is anything to render. `mesh_available` NEVER implies validated.
    mesh_available: bool = False
    reviewer_verdict: str | None = None
    reviewer_reasoning: str = ""
    reviewer_findings: list[str] = []
    # The application-owned terminal verdict (deterministic message + sanitized facts), rendered
    # from durable state - never model prose. Present once the job is finalized; survives restart.
    final_message: str | None = None
    final_result: dict | None = None
    # WHAT THE REVIEW CAME TO, in one word (application/final_result.REVIEW_OUTCOMES): "passed",
    # "delivered_with_concerns" (a succeeded run whose review left points open - never a pass),
    # "review_inconclusive", or "wrong_problem" (the only way a review fails a run). None before
    # the run ends, or when no review outcome applies. `review_concerns` lists the open points.
    review_outcome: str | None = None
    review_concerns: list[str] = []
    # WHY THE OWNER CANCELLED, in their words. Set only on a `cancelled` job, and only when they
    # gave one; the closing line itself is the final_message, the same on every surface.
    cancel_reason: str | None = None
    # WHILE NO WORKER HAS THE JOB. The fleet scales to zero, so a fresh job can sit `pending` for
    # minutes while a VM boots. Set only in that state, from WORKER_WAKE_MINUTES, so the console
    # can say "waiting for a worker to start (about N minutes)" instead of a silent spinner;
    # None once a worker has started (or when the estimate is disabled).
    worker_wake_minutes: int | None = None


class DisputeFlag(BaseModel):
    x: float
    y: float
    z: float
    span: float | None = None    # view span around the spot (viewer-provided)
    patch: str = ""              # patch the click landed on, if known
    note: str = ""               # the user's concern at this spot


class DisputeIn(BaseModel):
    flags: list[DisputeFlag] = []
    comment: str = ""
    # "rebuild"  - the user wants a different mesh (re-review for targeted feedback,
    #              then always rebuild)
    # "accept"   - the user judges the EXISTING mesh good enough for their study, so
    #              the QUALITY BAR is amended (their statement joins the review brief)
    #              and the mesh is RE-REVIEWED against it. The verdict is then honoured:
    #              it can pass and deliver, or still fail. Validity is untouched - the
    #              executor gates already passed and are never user-overridable.
    mode: str = "rebuild"

    @model_validator(mode="after")
    def _accept_carries_no_rebuild_flags(self) -> DisputeIn:
        # THE INVARIANT. `accept` says "this mesh is good enough for my study": it re-reviews the
        # SAME mesh against an amended bar and never rebuilds. A spatial flag is a request to
        # change the mesh at that spot, so the two cannot be asked for in one request. Accepting
        # both would have told the reviewer "the rebuild will be judged against what you record
        # here" about a rebuild that never happens - a false promise made to the model.
        #
        # Refused rather than reinterpreted: silently dropping the flags would discard what the
        # engineer marked, and silently rebuilding would ignore what they said.
        if self.mode == "accept" and self.flags:
            raise ValueError(
                "accepting a mesh cannot carry flagged regions: accept re-reviews this mesh "
                "against your comment and never rebuilds it. Send mode=rebuild to have the "
                "flagged regions rebuilt, or accept with a comment alone.")
        return self


class DisputeOut(BaseModel):
    job_id: uuid.UUID            # the NEW dispute job (re-review -> rebuild)
    dispute_of: uuid.UUID        # the disputed (parent) job
    flags: int


class CancelIn(BaseModel):
    # WHY, in the owner's words. Optional; kept on the job and shown back on its status. Clipped
    # here so the durable column's bound is the wire contract, not a truncation surprise later.
    reason: str = Field(default="", max_length=500)


class CancelOut(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    cancel_reason: str | None = None
    # True when the job was ALREADY cancelled and this call changed nothing. A repeat is answered
    # the same way rather than refused, so a retried click or a double-submit is harmless.
    already_cancelled: bool = False
