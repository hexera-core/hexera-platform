# Responsibility: Declare the durable schema: jobs, sessions, artifacts, geometry, capture, API keys and the terminal outbox.
# Boundaries: the ORM declaration, kept in step with the one migration baseline.

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from meshpipeline.persistence.session import Base


class JobStatus(str, PyEnum):
    pending        = "pending"
    running        = "running"
    succeeded      = "succeeded"
    failed         = "failed"
    queued         = "queued"
    pending_review = "pending_review"


class FailedReason(str, PyEnum):
    mesh_generation    = "mesh_generation"
    reviewer_rejected  = "reviewer_rejected"
    api_failure        = "api_failure"
    unhandled          = "unhandled"


class ArtifactType(str, PyEnum):
    mesh             = "mesh"
    mesh_bundle      = "mesh_bundle"
    # The viewer/quality payload the PIPELINE renders from the engine workspace it owns. The API
    # used to rebuild this from a worker's local directory, which only ever worked when the two
    # ran on one host; it is durable so the API can serve it after the workspace is gone.
    viewer_data      = "viewer_data"



class GeometrySource(Base):

    __tablename__ = "geometry_sources"

    id:            Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id:      Mapped[str]       = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    # What the user called it. Display only - never used to open a file, never trusted as a path.
    original_filename: Mapped[str]   = mapped_column(String(512), nullable=False)
    # UNTRUSTED FILENAME HINT. The lowercase suffix the upload arrived with (".step", ".vtp", …).
    # Parser dispatch still keys off this today, so it is recorded rather than re-derived from a
    # path later. It is NOT content detection and must never be called a detected format: real
    # signature-based identification is a separate, later stage.
    suffix_hint:   Mapped[str]       = mapped_column(String(16), nullable=False, server_default="")
    # Where the exact bytes live. Immutable in practice: the key embeds this row's id.
    object_key:    Mapped[str]       = mapped_column(String(1024), nullable=False, unique=True)
    # THE canonical integrity identity - lowercase hex, computed from the bytes as they streamed
    # in. Provider ETags/MD5 are metadata and are deliberately not used for this: a MinIO
    # multipart ETag is not a digest of the content at all.
    sha256:        Mapped[str]       = mapped_column(String(64), nullable=False, index=True)
    size_bytes:    Mapped[int]       = mapped_column(Integer, nullable=False)
    created_at:    Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now())
    # RETENTION. The row is lineage and is never deleted to satisfy a retention window; only the
    # stored object is. `purged_at` records that the bytes are gone, and the two claim columns
    # let exactly one maintenance worker act on a row at a time - `purge_claim_id` so a stale
    # claimant cannot finalize a newer worker's claim, `purge_claimed_at` so an abandoned claim
    # expires and the row becomes retryable instead of wedged.
    purged_at:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_claim_id:  Mapped[str | None]      = mapped_column(String(64), nullable=True)
    purge_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def bytes_available(self) -> bool:
        return self.purged_at is None

    __table_args__: tuple = (
        Index("ix_geometry_sources_owner_created", "owner_id", "created_at"),
        # The retention scan asks for "old and not yet purged". Partial, because once a row is
        # purged it never becomes eligible again - and that is where the table ends up over time.
        Index("ix_geometry_sources_unpurged_created", "created_at",
              postgresql_where=text("purged_at IS NULL")),
    )


class SimulationJob(Base):
    __tablename__ = "simulation_jobs"

    id:              Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id:        Mapped[str]       = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    status:          Mapped[JobStatus] = mapped_column(Enum(JobStatus), nullable=False, default=JobStatus.pending)
    created_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at:        Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_attempt: Mapped[int]       = mapped_column(Integer, default=0)
    workspace_purged: Mapped[bool]     = mapped_column(Boolean, default=False)
    failed_reason:   Mapped[FailedReason | None] = mapped_column(Enum(FailedReason), nullable=True)

    # WHICH BYTES this job meshes. A relation, not a path: the pipeline may run in a
    # different container from the API that received the upload, so identity has to survive
    # the process boundary and be verifiable on arrival. RESTRICT, because deleting a
    # job must never destroy geometry another record still references.
    geometry_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geometry_sources.id", ondelete="RESTRICT"),
        nullable=True, index=True)
    geometry_source: Mapped[GeometrySource | None] = relationship(
        "GeometrySource", lazy="selectin")
    # WHAT PHYSICAL SIZE those bytes are. Separate from the source because it is a different
    # question with a different lifetime: the same file can legitimately be meshed as millimetres
    # by one user and inches by another, so scale is not a property of the bytes. Bound at
    # APPROVAL, and immutable thereafter - a job that ran under millimetres must keep meaning
    # millimetres, so a correction is a new interpretation and a new job, never an edit of this.
    # Nullable because a job approved before scale was recorded has none, and inventing one would
    # be the exact assumption this table exists to remove.
    geometry_interpretation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geometry_interpretations.id", ondelete="RESTRICT"),
        nullable=True, index=True)
    # The IMMUTABLE snapshot of everything the pipeline needs to run this job - the exact
    # kwargs the API would otherwise pass in-memory to the Celery task (request_txt,
    # review_brief_txt, intake_patches, purpose, mesh_engine, engine_params, intake_events,
    # …). Persisted at dispatch so the run is reconstructable from JOB_ID ALONE: the same
    # payload drives the local Celery worker AND a hosted one-shot Cloud Run pipeline job,
    # which receives only a job id and loads everything from here. See worker/run_job.py.
    dispatch_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # DURABLE TERMINAL VERDICT - the application-rendered final result (typed facts + deterministic
    # message, schema-versioned; see application.final_result). Written in the terminal transaction so
    # a restarted API reproduces the SAME verdict from durable state. Never model prose or secrets.
    final_result:    Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # LAUNCH IDENTITY - kept DISTINCT from `status` (the pipeline's own lifecycle). This
    # records how/whether the run was HANDED OFF to its execution backend, so a lost
    # launch response can be reconciled instead of blindly re-launched (Cloud Run's
    # jobs.run creates a server-side execution even if the response is lost).
    #   pipeline_backend        which backend launched it (celery|deferred)
    #   pipeline_dispatch_state not_submitted | submitting | submitted | launch_failed
    #   pipeline_execution_id   the worker's execution identity. Load-bearing:
    #                           `persistence/lease` compares it to tell a RESUMING
    #                           execution (which keeps its generation) from a new
    #                           dispatch (which gets a new one).
    #   pipeline_submitted_at   when the submitting transition committed
    #   pipeline_launch_error   the launcher error, if it failed before acceptance
    pipeline_backend:        Mapped[str | None]      = mapped_column(String(32), nullable=True)
    pipeline_dispatch_state: Mapped[str | None]      = mapped_column(String(24), nullable=True)
    pipeline_execution_id:   Mapped[str | None]      = mapped_column(String(512), nullable=True)
    pipeline_submitted_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pipeline_launch_error:   Mapped[str | None]      = mapped_column(Text, nullable=True)

 # DURABLE WORKER OWNERSHIP + EXECUTION GENERATION
    # `execution_generation` is the monotonically-increasing epoch of ONE logical run: an initial
    # claim sets it to 1; an expired-lease TAKEOVER by a DIFFERENT backend execution increments it and
    # PERMANENTLY fences the old generation/token; a same-execution restart resumes the same generation
    # with a ROTATED token. `active_worker_token` is the opaque lease token the current owner holds -
    # every load-bearing side effect (native launch, output/gate/verdict acceptance, checkpoint write,
    # artifact ready, terminal CAS, outbox enqueue) is fenced on (id, execution_generation,
    # active_worker_token). The token is NEVER exposed via API/state/payload/logs/traces/Redis/final_result.
    execution_generation: Mapped[int]              = mapped_column(Integer, nullable=False, server_default="0")
    # Job-local, strictly increasing: bumped once per SUCCESSFUL claim, including a
    # same-generation continuation where the token rotates but the generation does not.
    # It orders CLAIMS. It is never part of a thread id, a generation, or an event identity.
    execution_claim_epoch: Mapped[int]             = mapped_column(BigInteger, nullable=False, server_default="0")
    active_worker_token:  Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_acquired_at:    Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    lease_heartbeat_at:   Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at:     Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    owning_backend:       Mapped[str | None]       = mapped_column(String(32), nullable=True)
    # the durable ABSOLUTE top-level pipeline deadline, set ONCE at the first claim (see
    # pipeline_budget). Never reset by a retry/rebuild/generation/restart/heartbeat.
    pipeline_deadline_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    # WHICH DISPUTE OPERATION this job IS, when a dispute created it. One operation has exactly one
    # child, so a repeated request resolves to this row instead of starting a second run. Null for
    # every job that a dispute did not create, which is why the uniqueness below is partial.
    dispute_operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    artifacts: Mapped[list[Artifact]] = relationship("Artifact", back_populates="job", cascade="all, delete-orphan")

    __table_args__: tuple = (
        Index("ix_jobs_cleanup", "status", "ended_at", "workspace_purged"),
        Index("ix_jobs_stalled", "status"),
        Index("ix_jobs_owner_status", "owner_id", "status"),
        # THE durable authority behind dispute idempotency. The advisory lock in
        # application/dispute_operation.py serialises identical requests; this index is what makes
        # a second child impossible even if that lock was never taken.
        Index("uq_simulation_jobs_dispute_operation", "dispute_operation_key",
              unique=True, postgresql_where=text("dispute_operation_key is not null")),
        {},
    )



class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id:                Mapped[uuid.UUID]      = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id:          Mapped[str]            = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    messages:          Mapped[list]           = mapped_column(JSONB, nullable=False, default=list)
    request_txt:       Mapped[str | None]     = mapped_column(Text, nullable=True)
    review_brief_txt:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    intake_patches:    Mapped[list | None]    = mapped_column(JSONB, nullable=True)
    dimensionality:    Mapped[str | None]     = mapped_column(String(16), nullable=True)
    # user-declared USE CASE + what the CAD represents (engines-as-tools): drive
    # the boundary vocabulary, the compatibility gate and downstream framing.
    purpose:           Mapped[str | None]     = mapped_column(String(32), nullable=True)
    input_kind:        Mapped[str | None]     = mapped_column(String(32), nullable=True)
    # The mesh-detail tier the USER chose (enums.MeshFidelity: draft|standard|max). NULL means
    # they never stated one - that is not a user selection, so the effective tier and its
    # provenance are derived at approval rather than defaulted into this column.
    requested_mesh_fidelity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # user's confirmed mesh-engine choice from intake ('cfmesh'/'snappy'); NULL
    # only for sessions that skip intake - resolved deterministically then.
    mesh_engine:       Mapped[str | None]     = mapped_column(String(32), nullable=True)
    # DESCRIPTIVE task label from intake ('elbow internal flow') - reviewer
    # context + corpus label; nothing routes on it
    domain:            Mapped[str | None]     = mapped_column(Text, nullable=True)
    # engine-NATIVE declared parameters (ONE dict, keys = the chosen engine's
    # ParamSpec keys - e.g. snappy's topology). See engine_catalog.ParamSpec.
    engine_params:     Mapped[dict | None]    = mapped_column(JSONB, nullable=True)
    # The current admission PREVIEW TOKEN record (agents.intake.admission_token): the server-issued
    # token + its canonical previewed payload + verdict + user-message revision + expiry. It is what
    # The intake AUTHORIZATION gate: {"selection": …, "admission": …}. The confirmed engine
    # SELECTION and the admission preview TOKEN it backs - together, because they are invalidated
    # together. STRUCTURALLY authorizes submit_requirements/confirm_dispatch; the model's tool
    # arguments are only a proposed interpretation and authorize nothing. NULL for a fresh session.
    intake_gate:       Mapped[dict | None]    = mapped_column(JSONB, nullable=True)
    llm_metadata:      Mapped[list]           = mapped_column(JSONB, nullable=False, default=list)
    intake_submitted:  Mapped[bool]           = mapped_column(Boolean, default=False)
    job_id:            Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="SET NULL"), nullable=True)
    # WHICH BYTES this session meshes. A relation, not a path: the pipeline may run in a
    # different container from the API that received the upload, so identity has to survive
    # the process boundary and be verifiable on arrival. RESTRICT, because deleting a
    # session must never destroy geometry another record still references.
    geometry_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geometry_sources.id", ondelete="RESTRICT"),
        nullable=True, index=True)
    geometry_source: Mapped[GeometrySource | None] = relationship(
        "GeometrySource", lazy="selectin")
    # WHAT PHYSICAL SIZE those bytes are. Separate from the source because it is a different
    # question with a different lifetime: the same file can legitimately be meshed as millimetres
    # by one user and inches by another, so scale is not a property of the bytes. Bound at
    # APPROVAL, and immutable thereafter - a job that ran under millimetres must keep meaning
    # millimetres, so a correction is a new interpretation and a new job, never an edit of this.
    # Nullable because a job approved before scale was recorded has none, and inventing one would
    # be the exact assumption this table exists to remove.
    geometry_interpretation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geometry_interpretations.id", ondelete="RESTRICT"),
        nullable=True, index=True)
    created_at:        Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:        Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__: tuple = (
        Index("ix_chat_sessions_created_at", "created_at"),
        Index("ix_chat_sessions_job_id", "job_id"),
        {},
    )




class Artifact(Base):
    __tablename__ = "artifacts"

    id:           Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id:       Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), nullable=False)
    # LOGICAL ARTIFACT IDENTITY: the stable per-job name of this deliverable (today one per
    # artifact_type, e.g. "mesh" / "mesh_bundle"; a future engine emitting two of a type gives each a
    # distinct logical_key). The DB UNIQUE(job_id, logical_key) - not a prior read - is the final
    # concurrency authority: two workers cannot create duplicate rows for the same logical artifact.
    logical_key:  Mapped[str]         = mapped_column(String(128), nullable=False, server_default="")
    # The graph's retry_count for the run that delivered this row (= SimulationJob.current_attempt,
    # which mirrors the same value). Delivery is a compare-and-set on this: a newer attempt
    # overwrites, a stale attempt is superseded (a no-op).
    delivery_attempt: Mapped[int]     = mapped_column(Integer, nullable=False, server_default="0")
    # the execution generation that delivered this row. Currency is (execution_generation,
    # then delivery_attempt): a newer generation supersedes an older one, and within a generation a
    # newer attempt supersedes. A STALE-generation upload can never overwrite a current-generation row.
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    storage_key:  Mapped[str]         = mapped_column(String(1024), nullable=False)
    size_bytes:   Mapped[int]         = mapped_column(Integer, default=0)
    checksum:     Mapped[str | None]  = mapped_column(String(128), nullable=True)  # provider checksum (integrity/dedup)
    created_at:   Mapped[datetime]    = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:   Mapped[datetime]    = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__: tuple = (
        UniqueConstraint("job_id", "logical_key", name="uq_artifacts_job_logical"),
    )

    job: Mapped[SimulationJob] = relationship("SimulationJob", back_populates="artifacts")


class TerminalOutbox(Base):
    __tablename__ = "terminal_outbox"

    id:            Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id:        Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    terminal_status: Mapped[str]      = mapped_column(String(16), nullable=False)   # succeeded | failed
    final_result_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # the deterministic event payload rendered from the COMMITTED final_result (no secrets/paths/prose)
    event_payload: Mapped[dict]       = mapped_column(JSONB, nullable=False)
    # THE authoritative terminal-event identity: deterministic + UNIQUE, so at-least-once publication is
    # idempotent and a CAS loser cannot enqueue a contradictory event. Keyed on the job (one terminal
    # event per job) - see terminal_outbox_repository.dedup_key_for.
    dedup_key:     Mapped[str]        = mapped_column(String(128), nullable=False)
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now())
    publish_attempts: Mapped[int]     = mapped_column(Integer, nullable=False, server_default="0")
    last_error:    Mapped[str | None] = mapped_column(String(256), nullable=True)   # clipped category, never raw
    published_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple = (
        UniqueConstraint("dedup_key", name="uq_terminal_outbox_dedup"),
        Index("ix_terminal_outbox_pending", "published_at"),
    )


class ReconciliationState(str, PyEnum):
    pending          = "pending"           # an orphan object whose row failed and immediate delete failed
    resolved_deleted = "resolved_deleted"  # the orphan was safely deleted
    resolved_adopted = "resolved_adopted"  # the object was complete + unclaimed → row created for it
    blocked_conflict = "blocked_conflict"  # the object/row identity changed - do NOT touch evidence
    abandoned        = "abandoned"         # gave up after the retry budget (manual attention)


class GeometryInterpretationRow(Base):

    __tablename__ = "geometry_interpretations"

    id:        Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                 default=uuid.uuid4)
    owner_id:  Mapped[str]       = mapped_column(String(256), nullable=False)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    geometry_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geometry_sources.id", ondelete="RESTRICT"),
        nullable=False)

    unit:            Mapped[str]   = mapped_column(String(8), nullable=False)
    scale_to_metres: Mapped[float] = mapped_column(Float, nullable=False)
    #: "file_declared" | "user_confirmed" - there is no inferred basis
    basis:           Mapped[str]   = mapped_column(String(16), nullable=False)
    #: short, non-private explanation; never a path, key, credential or parser stack trace
    evidence:        Mapped[str]   = mapped_column(String(256), nullable=False,
                                                   server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())

    __table_args__: tuple = (
        # One reading per (tenant, source, unit, basis): re-confirming the SAME unit is the same
        # interpretation, while a correction to a different unit is a genuinely different row.
        UniqueConstraint("owner_id", "geometry_source_id", "unit", "basis",
                         name="uq_geometry_interpretation"),
        Index("ix_geometry_interpretations_source", "owner_id", "geometry_source_id"),
    )


class CaptureOperation(Base):

    __tablename__ = "capture_operations"

    id:        Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                 default=uuid.uuid4)
    # Insertion order, and the ONLY ordering the export uses: timestamps tie under concurrency
    # and would make two exports of unchanged state differ.
    seq:       Mapped[int]       = mapped_column(BigInteger, Identity(always=False), nullable=False,
                                                 unique=True)
    owner_id:  Mapped[str]       = mapped_column(String(256), nullable=False)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    job_id:    Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="CASCADE"), nullable=False)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # WHICH operation this is, from the producer's point of view. Never derived from the payload:
    # identity answers "is this the same intended record replayed?", so content that drifts on a
    # retry stays the same operation and is caught as a conflict instead of becoming a second row.
    op_key:    Mapped[str]       = mapped_column(String(128), nullable=False)

    record_type: Mapped[str]     = mapped_column(String(32), nullable=False, server_default="span_event")
    name:      Mapped[str]       = mapped_column(String(128), nullable=False)
    kind:      Mapped[str]       = mapped_column(String(32), nullable=False, server_default="event")
    attempt:   Mapped[int | None] = mapped_column(Integer, nullable=True)

    # PRIVATE captured content. Never published; the export is the only reader.
    payload:   Mapped[dict]      = mapped_column(JSONB, nullable=False, default=dict)
    payload_sha256: Mapped[str]  = mapped_column(String(64), nullable=False, server_default="")

    # Conflict evidence, not conflict resolution. The original payload is never overwritten, and
    # a conflicted operation never becomes trusted again.
    conflicted: Mapped[bool]     = mapped_column(Boolean, nullable=False, server_default="false")
    conflict_evidence: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__: tuple = (
        # The reconciliation point. PostgreSQL enforces it, not application code: two separate
        # workers racing the same operation is exactly the case a check-then-insert loses.
        UniqueConstraint("owner_id", "job_id", "execution_generation", "op_key",
                         name="uq_capture_operations_identity"),
        # tenant + job + generation is how every read and export scopes itself
        Index("ix_capture_operations_scope", "owner_id", "job_id", "execution_generation"),
    )


class ArtifactReconciliation(Base):
    __tablename__ = "artifact_reconciliations"

    id:            Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id:      Mapped[str]        = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    job_id:        Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    delivery_attempt: Mapped[int]     = mapped_column(Integer, nullable=False, default=0)
    logical_key:   Mapped[str]        = mapped_column(String(128), nullable=False)
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), nullable=False)
    object_key:    Mapped[str]        = mapped_column(String(1024), nullable=False)
    object_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the etag/md5 at upload
    object_size:   Mapped[int]        = mapped_column(Integer, default=0)
    failure_category: Mapped[str]     = mapped_column(String(64), nullable=False, server_default="row_write_failed")
    retry_count:   Mapped[int]        = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state:         Mapped[ReconciliationState] = mapped_column(Enum(ReconciliationState), nullable=False,
                                                               default=ReconciliationState.pending)
    detail:        Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__: tuple = (
        # one live reconciliation per (job, attempt, object) - a duplicate orphan report is idempotent
        UniqueConstraint("job_id", "delivery_attempt", "object_key", name="uq_reconcile_job_attempt_object"),
        Index("ix_artifact_reconciliations_state", "state"),
    )


class SourceObjectCleanup(Base):
    # THE CLEANUP INTENT for one uploaded source object, written BEFORE the object exists.
    #
    # It is a separate table from artifact_reconciliations on purpose. That table's `job_id` is NOT
    # NULL and references simulation_jobs, and its `artifact_type` is a mesh-artifact vocabulary: a
    # source upload has no job and is not an artifact, so recording one there would mean inventing a
    # job id and calling CAD a mesh. The state vocabulary is shared because it is a state
    # vocabulary, not an artifact identity.
    #
    # WHY WRITE-AHEAD. Recording only after a failure cannot cover the two windows that produced the
    # orphan: the database being the thing that failed (so the record cannot be written either), and
    # the process dying between the object write and the source transaction. An intent committed
    # before the write is discoverable in both.

    __tablename__ = "source_object_cleanups"

    id:          Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id:    Mapped[str]       = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    # The source row this object was going to belong to. Maintenance asks whether that row exists
    # before deleting anything, so a legitimate upload can never be reclaimed by its own intent.
    source_id:   Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_key:  Mapped[str]       = mapped_column(String(1024), nullable=False, unique=True)
    state:       Mapped[ReconciliationState] = mapped_column(
        Enum(ReconciliationState), nullable=False, default=ReconciliationState.pending)
    # Bounded: the sweep abandons a record once the budget is spent, so this cannot grow forever.
    retry_count: Mapped[int]       = mapped_column(Integer, nullable=False, server_default="0")
    last_error:  Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                   onupdate=func.now())

    __table_args__: tuple = (
        # The sweep only ever reads pending rows, and a resolved history should not slow it down.
        Index("ix_source_object_cleanups_pending", "created_at",
              postgresql_where=text("state = 'pending'")),
    )


class ApiKey(Base):
    # ONE programmatic credential. The row is what a presented key is checked against, and the
    # ONLY durable trace of it: the secret half is shown once, at creation, and is not recoverable
    # from here. Losing it costs a new key, which is a per-key operation - unlike USER_TOKEN_SECRET,
    # whose rotation invalidates every identity at once.

    __tablename__ = "api_keys"

    id:        Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                 default=uuid.uuid4)
    # THE TENANT this key authenticates as, in the same String(256) shape every other table scopes
    # on. A key resolves to an owner_id and everything downstream is unchanged.
    owner_id:  Mapped[str]       = mapped_column(String(256), nullable=False, index=True)
    # WHICH ORGANISATION that owner belongs to. The foreign key 0002 promised arrives in 0004,
    # with the table it points at. Still nullable: a key issued before the backfill names none.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
    #: what the holder calls it. Display only - never used to find a key, never trusted.
    name:      Mapped[str]       = mapped_column(String(128), nullable=False, server_default="")
    # THE LOOKUP KEY: the public "hx_live_<identifier>" half. Unique because two rows sharing it
    # would make one presented key resolve to two owners, and indexed because every authenticated
    # request reads exactly one row by this value.
    key_prefix: Mapped[str]      = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: SHA-256 of the secret half, lowercase hex. The secret itself is never stored.
    key_hash:  Mapped[str]       = mapped_column(String(64), nullable=False)
    # WHICH PLAN's limits this key is held to. Empty is the normal state and means "the limits this
    # deployment configures" - see settings/plans.py.
    plan:      Mapped[str]       = mapped_column(String(32), nullable=False, server_default="")
    created_at:   Mapped[datetime]        = mapped_column(DateTime(timezone=True),
                                                          server_default=func.now(), nullable=False)
    # STALENESS, at a coarse resolution: advanced at most once a minute, because a per-request
    # UPDATE on the row every request already reads is a write amplification with no reader.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # THE TWO WAYS A KEY STOPS WORKING, both nullable because a live key has neither. Revocation is
    # an act (recorded when it happens); expiry is a term set at issuance. They are separate columns
    # so a revoked key never looks merely expired in an audit.
    revoked_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple = (
        # Listing an owner's keys, newest first - the management view's only query.
        Index("ix_api_keys_owner_created", "owner_id", "created_at"),
        # The stored value is a hex digest and nothing else. A row whose hash is a secret, a
        # truncation or an empty string cannot authenticate anything, and must not be storable.
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_api_keys_key_hash_shape"),
    )


class MembershipRole(str, PyEnum):
    owner  = "owner"
    member = "member"


class CreditEntryType(str, PyEnum):
    #: credits issued - the only kind this cycle writes
    grant  = "grant"
    #: credits consumed. Declared now so the ledger's shape is settled; nothing writes one yet.
    debit  = "debit"
    #: credits returned after a debit that should not have stood
    refund = "refund"


class Organization(Base):
    # THE TENANT. One per user today (there is no organisation UI), but the table and its
    # memberships exist from day one because widening a tenant boundary after data has
    # accumulated is the expensive migration - the same argument api_keys.organization_id
    # was already carrying an empty column for.

    __tablename__ = "organizations"

    id:   Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                            default=uuid.uuid4)
    name: Mapped[str]       = mapped_column(String(256), nullable=False)
    #: a URL-safe handle. Unique so it can address the organisation once anything needs to.
    slug: Mapped[str]       = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)


class User(Base):
    # THE PERSON. Identity Platform holds their credential; this row holds everything about them
    # that the columns in this schema scope on. There is deliberately NO password column - see
    # the design's decision 1. A password we never receive is one we cannot leak.

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    # THE IDENTITY PLATFORM SUBJECT. Nullable because the backfill creates a row per existing
    # owner_id before that person has ever signed up; it is filled in the first time they do,
    # which is what makes their existing jobs and geometry follow them in rather than being
    # stranded behind a second, empty account.
    firebase_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True,
                                                     index=True)
    # THE LINK TO owner_id, which is this address lowercased. Unique and indexed because both
    # the uid path and the backfill-linking path find a user by it.
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    name:  Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    # WHEN the address was proven, not whether. A moment survives a provider that later stops
    # reporting the flag, and it is what a future "grant only on verified email" rule would read.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                               nullable=True)
    last_login_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                               nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        # owner_id is the lowercased email everywhere else in this schema. A row that disagreed
        # would resolve to an organisation for one spelling of the address and not the other.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercased"),
    )


class Membership(Base):
    # WHICH ORGANISATION a user acts within. One row per personal organisation today; the table
    # is what makes multi-user organisations a later feature rather than a later migration.

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    # RESTRICT, like every other lineage reference in this schema: a membership is how a user's
    # rows are reachable, so neither side may be deleted out from under it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    role: Mapped[MembershipRole] = mapped_column(Enum(MembershipRole, name="membershiprole"),
                                                 nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
    )


class CreditLedgerEntry(Base):
    # ONE MOVEMENT of credits, APPEND-ONLY. The balance is SUM(amount) over an organisation and is
    # never stored: a counter decremented at submit leaks credits down every failure path, and
    # this pipeline has several (FailedReason, artifact_reconciliations). A row per movement also
    # answers "why is my balance this?", which a counter never can.
    #
    # Nothing in this cycle writes anything but a grant. debit and refund exist so that when
    # spending lands it is a new caller, not a new migration.

    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    entry_type: Mapped[CreditEntryType] = mapped_column(
        Enum(CreditEntryType, name="creditentrytype"), nullable=False)
    # SIGNED, in whole credits. A grant is positive and a debit negative, so the balance is a
    # plain SUM with no per-type arithmetic that a new entry type could get wrong. BigInteger
    # because the unit is undecided and a small unit means large numbers.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: why this entry exists, for the person reading their own ledger. Display only.
    reason: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        # THE BALANCE QUERY, and the ledger view's ordering. Both read this index.
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
        # A zero-amount entry is a row that changes nothing and explains nothing.
        CheckConstraint("amount <> 0", name="ck_credit_ledger_amount_nonzero"),
    )


class NativeSubmissionClaim(Base):
    # ONE native mesh submission per (job, execution generation). The row is the right to make at
    # most one external call, and the durable record of what became of it - a `claimed` row is
    # never evidence that the provider accepted anything.
    __tablename__ = "native_submission_claims"

    id:                   Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                            default=uuid.uuid4)
    job_id:               Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_jobs.id", ondelete="CASCADE"), nullable=False)
    execution_generation: Mapped[int]       = mapped_column(Integer, nullable=False)
    semantic_operation:   Mapped[str]       = mapped_column(String(64), nullable=False,
                                                            server_default="native_mesh_submission")
    operation_key:        Mapped[str]       = mapped_column(String(64), nullable=False)
    engine:               Mapped[str]       = mapped_column(String(64), nullable=False)
    payload_digest:       Mapped[str]       = mapped_column(String(64), nullable=False)
    disposition:          Mapped[str]       = mapped_column(String(16), nullable=False,
                                                            server_default="claimed")
    #: the claimant by fingerprint - the raw worker token is never stored
    claimant_fingerprint: Mapped[str]       = mapped_column(String(64), nullable=False)
    provider_reference:   Mapped[str | None] = mapped_column(String(512), nullable=True)
    failure_class:        Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at:           Mapped[datetime]  = mapped_column(DateTime(timezone=True),
                                                            server_default=func.now())
    updated_at:           Mapped[datetime]  = mapped_column(DateTime(timezone=True),
                                                            server_default=func.now(),
                                                            onupdate=func.now())

    __table_args__: tuple = (
        UniqueConstraint("job_id", "execution_generation", "semantic_operation",
                         name="uq_native_submission_claims_identity"),
        UniqueConstraint("operation_key", name="uq_native_submission_claims_operation_key"),
        CheckConstraint("execution_generation >= 0",
                        name="ck_native_submission_claims_generation"),
        CheckConstraint("disposition in ('claimed', 'accepted', 'indeterminate', 'failed')",
                        name="ck_native_submission_claims_disposition"),
        CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'",
                        name="ck_native_submission_claims_payload_digest"),
        CheckConstraint("operation_key ~ '^[0-9a-f]{64}$'",
                        name="ck_native_submission_claims_operation_key_shape"),
        CheckConstraint(
            "(disposition <> 'accepted') or "
            "(provider_reference is not null and length(provider_reference) > 0)",
            name="ck_native_submission_claims_accepted_has_reference"),
        Index("ix_native_submission_claims_job", "job_id", "execution_generation"),
        Index("uq_native_submission_claims_provider_reference", "provider_reference",
              unique=True, postgresql_where=text("provider_reference is not null")),
    )
