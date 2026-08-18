# Responsibility: Declare the durable schema: jobs, sessions, artifacts, geometry, capture and the terminal outbox.
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
