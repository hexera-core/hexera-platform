# Responsibility: Run one pipeline execution end to end, from dispatch to a durable terminal result.
# Owns: run entry, generation and ownership acquisition, checkpoint threading, graph execution, and terminalization.
# Boundaries: it orchestrates.
# Collaborates with: pipeline/graph.py, application/execution_fence.py and application/terminal_finalize.py.
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import MutableMapping
from typing import Any

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg

logger = logging.getLogger(__name__)

# a stable per-PROCESS id. A redelivery that lands in the SAME process (an in-process
# restart) shares it; a fresh process is a fresh execution. It is the last-resort backend execution
# identity when neither a Cloud Run execution nor a Celery task id is available (direct/test runs).
_PROCESS_RUN_ID = uuid.uuid4().hex


def _backend_execution_id(job_id: str) -> tuple[str, str]:
    backend = (provcfg.PIPELINE_BACKEND or "direct").strip()[:32] or "direct"
    exec_id = (provcfg.PIPELINE_EXECUTION_ID or "").strip() or f"proc-{_PROCESS_RUN_ID}"
    return backend, exec_id


async def _persist_orphans(owner_id: str, job_id: str, orphans) -> None:
    if not orphans:
        return
    try:
        from meshpipeline.persistence.repositories.reconciliation_repository import (
            ReconciliationRepository,
        )
        from meshpipeline.persistence.session import get_db as _get_db
        repo = ReconciliationRepository()
        async with _get_db() as _db:
            for _o in orphans:
                await repo.record_orphan(
                    _db, owner_id=owner_id, job_id=uuid.UUID(job_id),
                    delivery_attempt=_o.delivery_attempt, logical_key=_o.logical_key,
                    artifact_type=_o.artifact_type, object_key=_o.object_key,
                    object_checksum=_o.checksum, object_size=_o.size_bytes)
            await _db.commit()
    except Exception as _exc:  # noqa: BLE001
        logger.error("could not persist orphan reconciliation records for job %s: %s", job_id, _exc)


async def dispatch(db, job_id: str, payload: dict) -> None:
    from meshpipeline.application.dispatch_contract import validate
    from meshpipeline.contracts.pipeline_execution import get_pipeline_launcher
    from meshpipeline.persistence.repositories.job_repository import JobRepository

    validate(payload, where=f"dispatch job {job_id}")
    await JobRepository().set_dispatch_payload(db, uuid.UUID(job_id), payload)
    await db.commit()
    await get_pipeline_launcher().launch(db, job_id, payload)
    # AFTER the backend accepted, never before: a launch that raises must leave no successful
    # submission timestamp behind, and the caller's `mark_launch_failed` records that case.
    #
    # BEST EFFORT, DELIBERATELY. The job is running by this point. If recording the acceptance
    # failed loudly, the caller's handler would report "nothing is running" and mark a launch
    # failure for a job the broker had already taken - a false failure is far worse than missing
    # bookkeeping, and this is observability, not the product action.
    try:
        import meshpipeline.settings.providers as _provcfg
        await JobRepository().mark_launched(db, uuid.UUID(job_id), _provcfg.PIPELINE_BACKEND)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - the launch already succeeded; never undo that
        logger.error("dispatch: job %s launched but its submission record failed: %s", job_id, exc)


def run_from_job(job_id: str) -> dict:
    import asyncio

    # ONE event loop for load-and-validate: the async engine is process-cached, so a second
    # asyncio.run() would find it bound to the loop the first one already closed - and the
    # failure we most need to record (an unusable payload) is exactly when that would bite.
    # That reasoning covers this function's own two calls, but `run_pipeline` below opens ANOTHER
    # loop, and asyncpg binds a pooled connection's futures to the loop that opened it. Leaving a
    # live connection in the pool here hands the next loop something it cannot await
    # ("got Future attached to a different loop"). Disposing inside this loop - the only place
    # that still can - closes them properly instead of abandoning them.
    async def _load_then_release():
        try:
            return await _load_and_validate(job_id)
        finally:
            from meshpipeline.persistence.session import dispose_engine
            await dispose_engine()

    kwargs = asyncio.run(_load_then_release())
    logger.info("one-shot pipeline run - job_id=%s (%d run kwargs)", job_id, len(kwargs))
    return run_pipeline(**kwargs)


async def _load_and_validate(job_id: str) -> dict:
    from meshpipeline.application.dispatch_contract import DispatchContractError, to_run_kwargs

    payload = await _load_payload(job_id)
    if payload is None:
        raise SystemExit(f"job {job_id} has no dispatch_payload - nothing to run "
                         "(was it dispatched through application.pipeline_run.dispatch?)")
    try:
        return to_run_kwargs(payload, where=f"job {job_id} dispatch_payload")
    except DispatchContractError as exc:
        logger.error("one-shot pipeline run - unusable dispatch_payload for job %s: %s", job_id, exc)
        await _mark_payload_unusable(job_id, str(exc))
        raise SystemExit(f"job {job_id}: {exc}") from exc


async def _mark_payload_unusable(job_id: str, reason: str) -> None:
    try:
        from meshpipeline.persistence.repositories.job_repository import JobRepository
        from meshpipeline.persistence.session import get_db
        async with get_db() as db:
            await JobRepository().mark_launch_failed(
                db, uuid.UUID(job_id), f"unusable dispatch_payload: {reason}"[:2000])
    except Exception as exc:  # noqa: BLE001 - best effort; the SystemExit above is the real signal
        logger.error("could not record unusable dispatch_payload for job %s: %s", job_id, exc)


async def _load_payload(job_id: str) -> dict | None:
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        return await JobRepository().get_dispatch_payload(db, uuid.UUID(job_id))


def _geometry_provenance(state) -> dict | None:
    from meshpipeline.pipeline.geometry_state import geometry_ref
    ref = geometry_ref(state)
    return ref.to_payload() if ref else None


async def _classify_checkpoint(thread_id: str) -> str:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    import meshpipeline.errors as _errors
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    from meshpipeline.errors import FailureClass
    from meshpipeline.pipeline.graph import build_graph

    dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    try:
        async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
            # The saver owns its own tables and creates them on first use. Without this a brand
            # new deployment has no `checkpoints` relation, the read raises UndefinedTable, and
            # the classifier reports an unreadable checkpoint - so the very first job on a fresh
            # install would fail instead of starting from START. setup() is idempotent.
            await cp.setup()
            # Reads are deliberately unfenced - a legitimate restart must be able to look at its
            # own thread before it owns anything else.
            snapshot = await build_graph(checkpointer=cp).aget_state(
                {"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001 - narrowed immediately
        _fc = _errors.classify_exception(exc, "postgres").failure_class
        _unreachable = _fc in (FailureClass.DEPENDENCY_DOWN, FailureClass.PROVIDER_TRANSIENT)
        if _unreachable and not polcfg.REQUIRE_DURABLE_CHECKPOINTER:
            # The SAME policy the execution path already applies: where durable checkpointing is
            # not required, an unreachable store degrades to in-process checkpoints, and an
            # in-process saver has no prior thread. Saying "absent" here is that reality, not a
            # new fallback - and where durable checkpointing IS required the run still fails.
            logger.warning("checkpoint classify: store unreachable and durable checkpointing is "
                           "not required - treating this thread as absent: %s", exc)
            return "absent"
        cls = FailureClass.DEPENDENCY_DOWN if _unreachable else FailureClass.INTERNAL
        logger.error("checkpoint classify: thread unreadable (%s): %s", cls.value, exc)
        raise GeometrySourceError("the saved execution state could not be read",
                                  failure_class=cls, dependency="checkpoint_store") from exc

    if snapshot is None or snapshot.created_at is None:
        return "absent"
    return "pending" if snapshot.next else "complete"


def _coerce_source_ref(value):
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, GeometrySourceRef):
        return value
    return GeometrySourceRef.from_payload(value)


def _coerce_interpretation_ref(value):
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, GeometryInterpretationRef):
        return value
    return GeometryInterpretationRef.from_payload(value)


class JobRequest:

    __slots__ = ("job_id", "owner_id", "geometry_source", "geometry_interpretation",
                 "session_id", "request_txt",
                 "review_brief_txt", "intake_patches", "dimensionality", "purpose",
                 "input_kind", "requested_mesh_fidelity", "effective_mesh_fidelity",
                 "mesh_fidelity_source", "fidelity_policy_version", "sample_id",
                 "user_dispute", "mesh_engine", "domain", "engine_params",
                 "approved_snapshot_id", "approved_patch_contract",
                 "approved_intent_fingerprint", "intake_events",
                 "requested_extents", "reference_length_m", "requirements_strict",
                 "flow_axis")

    def __init__(self, job_id, owner_id="dev-user", geometry_source=None,
                 geometry_interpretation=None, session_id="",
                 request_txt="", review_brief_txt="",
                 intake_patches=None, dimensionality="", purpose="", input_kind="",
                 requested_mesh_fidelity=None, effective_mesh_fidelity="",
                 mesh_fidelity_source="", fidelity_policy_version="", sample_id=None,
                 user_dispute=None, mesh_engine="", domain="", engine_params=None,
                 approved_snapshot_id="", approved_patch_contract=None,
                 approved_intent_fingerprint="", intake_events=None,
                 requested_extents=None, reference_length_m=None,
                 requirements_strict=True, flow_axis=None):
        self.job_id           = job_id
        self.owner_id         = owner_id
        # Replayed only once the claim has bound the tenant these records file under. They are
        # carried here rather than written at dispatch because the writer needs an owner taken
        # from the job ROW, and nothing has read that row yet at dispatch time.
        self.intake_events    = list(intake_events or [])
        # The COMPLETE approved reference, parsed on the way in. A partially populated snapshot is
        # rejected here rather than at the point of use: this object is what every entry - Celery,
        # Cloud Run, retry, resume - hands to execution, and a run that cannot say which bytes it
        # was approved for must not reach the graph at all.
        self.geometry_source  = _coerce_source_ref(geometry_source)
        # The physical meaning the run was approved under. Parsed here for the same reason the
        # source is: a run that cannot say what its coordinates MEAN must not reach the graph.
        self.geometry_interpretation = _coerce_interpretation_ref(geometry_interpretation)
        self.session_id       = session_id
        self.request_txt      = request_txt
        self.requested_extents  = dict(requested_extents) if requested_extents else None
        self.reference_length_m = (None if reference_length_m is None
                                   else float(reference_length_m))
        # default TRUE: a dispatch that never says otherwise keeps the blocking contract -
        # near-miss delivery is opt-out only through an approval that carries the bit
        self.requirements_strict = bool(requirements_strict)
        self.flow_axis = (str(flow_axis).strip().lower() if flow_axis else None)
        self.review_brief_txt = review_brief_txt
        self.intake_patches   = intake_patches
        self.dimensionality   = dimensionality
        # user-declared use case + submitted geometry kind (engines-as-tools)
        self.purpose          = (purpose or "").strip()
        self.input_kind       = (input_kind or "").strip()
        # mesh-detail preference. When the payload carries the resolved trio we keep it verbatim
        # (it is what was approved and fingerprinted); when it carries only a requested value - or
        # nothing - we resolve it with the SAME shared policy every other layer uses. Either way the
        # result is validated, so an inconsistent trio fails here rather than reaching the engines.
        from meshpipeline.pipeline.enums import (
            FIDELITY_POLICY_VERSION as _FPV,
        )
        from meshpipeline.pipeline.enums import (
            assert_fidelity_consistent as _assert_fid,
        )
        from meshpipeline.pipeline.enums import (
            canonical_mesh_fidelity as _canon_fid,
        )
        from meshpipeline.pipeline.enums import (
            resolve_mesh_fidelity as _resolve_fid,
        )
        if effective_mesh_fidelity and mesh_fidelity_source:
            _rq = _canon_fid(requested_mesh_fidelity)
            self.requested_mesh_fidelity = None if _rq is None else _rq.value
            self.effective_mesh_fidelity = str(effective_mesh_fidelity).strip().lower()
            self.mesh_fidelity_source    = str(mesh_fidelity_source).strip().lower()
        else:
            _rq2, _ef, _src = _resolve_fid(requested_mesh_fidelity)
            self.requested_mesh_fidelity = None if _rq2 is None else _rq2.value
            self.effective_mesh_fidelity = _ef.value
            self.mesh_fidelity_source    = _src.value
        self.fidelity_policy_version = str(fidelity_policy_version or _FPV)
        _assert_fid(requested=self.requested_mesh_fidelity,
                    effective=self.effective_mesh_fidelity,
                    source=self.mesh_fidelity_source,
                    policy_version=self.fidelity_policy_version)
        self.sample_id        = sample_id
        # {"flags": [...], "comment": str, "of_job_id": str} - a user dispute of a
        # delivered mesh; empty/None = normal run. See graph.PipelineState.user_dispute.
        self.user_dispute     = user_dispute or {}
        # user's explicit engine choice from intake ('cfmesh'/'snappy'; '' = auto).
        # Pins state['engine'] so resolution keeps it - the user has the final word.
        self.mesh_engine      = (mesh_engine or "").strip().lower()
        # DESCRIPTIVE task label - reviewer context + corpus; nothing routes on it
        self.domain           = (domain or "").strip()
        # engine-NATIVE declared parameters (ParamSpec answers, one dict);
        # resolved against the engine's spec at seeding (defaults fill gaps)
        self.engine_params    = dict(engine_params or {})
        # DIAGNOSTIC PROVENANCE ONLY: ties this execution to the canonical intake snapshot the
        # user approved. It authorizes nothing (owner/session binding was already enforced at
        # approval), carries no secret, and never influences engine behaviour.
        self.approved_snapshot_id = str(approved_snapshot_id or "")
        # THE typed, fingerprinted approved patch contract (a dict, or None for a direct/internal
        # dispatch). This is the AUTHORITY the graph-admission gate checks the execution patch set
        # against - exactly, before the builder. Built once from the approval snapshot at confirm
        # time and carried verbatim through reconstruction; never re-derived from prose or manifests.
        self.approved_patch_contract = approved_patch_contract
        # the COMPLETE approved-intent fingerprint (engine/purpose/input_kind/dimensionality/
        # patches/engine_params). Re-verified at graph admission against the run's own fields.
        self.approved_intent_fingerprint = str(approved_intent_fingerprint or "")


class _JobAdapter(logging.LoggerAdapter):

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        job_id = (self.extra or {}).get("job_id", "?")
        return f"{msg} [job_id={job_id}]", kwargs


def _terminal_event_id(job_id: str) -> str:
    from meshpipeline.persistence.repositories.terminal_outbox_repository import dedup_key_for
    return dedup_key_for(job_id)


def execution_publisher(job_id: str, stage: str):
    from meshpipeline.application.execution_publisher import (
        execution_publisher as _ep,
    )
    return _ep(job_id, agent=stage)


def _pub(job_id: str, stage: str = "outcome"):
    from meshpipeline.contracts.event_stream import publisher
    return publisher(job_id, agent=stage)


def _incr_delivery_count(job_id: str) -> int:
    try:
        from meshpipeline.contracts.delivery_guard import record_attempt
        return record_attempt(job_id)
    except Exception as exc:
        logger.warning("_incr_delivery_count: delivery guard unavailable (%s) - assuming first delivery", exc)
        return 1


def _emit_intake_events(job_id: str, intake_events: list) -> None:
    from meshpipeline.capture.logger import TrainingLogger
    tlogger = TrainingLogger(job_id)
    for _i, ev in enumerate(intake_events):
        ev_type    = ev.get("type", "")
        ev_payload = ev.get("payload", {})
        if ev_type:
            # Position in the intake stream. A re-delivery replays the same list in the same
            # order, so the index re-derives the same identity for the same event.
            tlogger.log(ev_type, ev_payload, op_id=f"intake:{_i}")
    logger.info(
        "_emit_intake_events: emitted %d events for job_id=%s",
        len(intake_events), job_id,
    )


def run_pipeline(
    job_id:                  str,
    owner_id:                str        = "dev-user",
    geometry_source:         dict | None = None,
    #: The immutable physical interpretation the job was approved under. Travels with the source
    #: because bytes alone cannot be meshed - their scale is a separate durable fact.
    geometry_interpretation: dict | None = None,
    session_id:              str        = "",
    request_txt:             str        = "",
    review_brief_txt:        str        = "",
    intake_patches:          list | None = None,
    dimensionality:          str        = "",
    purpose:                 str        = "",
    input_kind:              str        = "",
    # the mesh-detail preference is FOUR explicit fields - the user's own choice (nullable),
    # the deterministic operational tier, which of the two produced it, and the policy version.
    requested_mesh_fidelity: str | None = None,
    effective_mesh_fidelity: str        = "",
    mesh_fidelity_source:    str        = "",
    fidelity_policy_version: str        = "",
    intake_events:           list | None = None,
    user_dispute:            dict | None = None,
    mesh_engine:             str        = "",
    domain:                  str        = "",
    engine_params:           dict | None = None,
    approved_snapshot_id:    str        = "",
    approved_patch_contract: dict | None = None,
    approved_intent_fingerprint: str    = "",
    requested_extents:       dict | None = None,
    reference_length_m:      float | None = None,
    requirements_strict:     bool       = True,
    flow_axis:               str | None = None,
) -> dict:


    # This job's existing capture files are deliberately KEPT on re-delivery. A takeover worker
    # recovers a crashed worker's claims BY READING THE FILE - the only channel it has, since the
    # worker that wrote them is gone. Clearing it first would rewrite every replayed record as a
    # second training example and make a disagreeing takeover undetectable. Keyed records dedupe
    # against the recovered claims; conflicting ones quarantine.
    # Records with no operation identity stay at-least-once across a re-delivery, which is the
    # documented contract for them: nothing can tell a re-delivery apart from a node legitimately
    # emitting the same event twice, and collapsing those would lose real work.

    return asyncio.run(_run_async(JobRequest(
        intake_events=intake_events or [],
        job_id=job_id,
        owner_id=owner_id,
        geometry_source=geometry_source,
        geometry_interpretation=geometry_interpretation,
        session_id=session_id,
        request_txt=request_txt,
        review_brief_txt=review_brief_txt or "",
        intake_patches=intake_patches or [],
        dimensionality=dimensionality or "",
        purpose=purpose or "",
        input_kind=input_kind or "",
        requested_extents=requested_extents,
        reference_length_m=reference_length_m,
        requirements_strict=requirements_strict,
        flow_axis=flow_axis,
        requested_mesh_fidelity=requested_mesh_fidelity,
        effective_mesh_fidelity=effective_mesh_fidelity or "",
        mesh_fidelity_source=mesh_fidelity_source or "",
        fidelity_policy_version=fidelity_policy_version or "",
        mesh_engine=mesh_engine or "",
        domain=domain or "",
        engine_params=engine_params or {},
        user_dispute=user_dispute or {},
        approved_snapshot_id=approved_snapshot_id or "",
        approved_patch_contract=approved_patch_contract,
        approved_intent_fingerprint=approved_intent_fingerprint or "",
    )))


# THE run entry, captured as a stable object and PUBLISHED to the neutral dispatch_types seam. The
# dispatch contract derives its accepted/required field set from THIS (read via dispatch_types.
# run_entry), not from a module attribute lookup - a test (or anything else) rebinding `run_pipeline`
# must not silently redefine what a dispatch payload may contain. Registering here (rather than the
# contract importing this module) is what keeps dispatch_contract from importing the orchestrator.
from meshpipeline.application.dispatch_types import register_run_entry  # noqa: E402

RUN_ENTRY = register_run_entry(run_pipeline)


async def _run_async(req: JobRequest) -> dict:
    # Initialized before ANY raise-able statement: the crash handler reads it, and a job that
    # dies before the checkpointer section (e.g. a dispute whose parent was purged) must
    # crash-finalize with the intent fallback, not UnboundLocalError inside the handler.
    _checkpointer_ok = False
    # Unpack once; the body keeps working with the original local names.
    job_id           = req.job_id
    owner_id         = req.owner_id
    session_id       = req.session_id
    request_txt      = req.request_txt
    review_brief_txt = req.review_brief_txt
    intake_patches   = req.intake_patches
    dimensionality   = req.dimensionality
    purpose          = req.purpose
    input_kind       = req.input_kind
    sample_id        = req.sample_id
    # The APPROVED INTENT, captured before anything can fail. Its fingerprint was verified at
    # dispatch, so it stays authoritative even if the run later crashes and unwinds - which is
    # exactly when the terminal verdict needs it (see the crash handler at the end of this
    # function, and final_result.merge_durable_facts).
    _approved_payload_for_crash = {
        "mesh_engine": req.mesh_engine or "", "purpose": req.purpose or "",
        "input_kind": req.input_kind or "", "dimensionality": req.dimensionality or "",
        "approved_snapshot_id": req.approved_snapshot_id or "",
    }

    jlog = _JobAdapter(logger, {"job_id": job_id})
    from langgraph.checkpoint.memory import MemorySaver
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION, PipelineState
    from meshpipeline.persistence.models import JobStatus
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.pipeline.graph import build_graph

    _worker_engine = create_async_engine(
        provcfg.POSTGRES_DSN,
        echo=False,
        pool_size=2,
        max_overflow=3,
        pool_pre_ping=True,
    )
    AsyncSessionLocal = async_sessionmaker(
        bind=_worker_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    job_repo = JobRepository()

    try:
        # DELIVERY ADMISSION - the retry-storm guard and the atomic claim that decides whether
        # this delivery is the active worker. Both live in application/execution_fence, beside the
        # ownership they produce; the refusal comes back as data so this orchestrator keeps the
        # terminal side effects (publishing, disposing its own engine) it owns.
        from meshpipeline.application import execution_fence as _fence
        _refusal = await _fence.guard_redelivery(
            AsyncSessionLocal, job_repo, job_id, jlog=jlog,
            max_redeliveries=rtcfg.CELERY_MAX_REDELIVERIES,
            deliveries=_incr_delivery_count(job_id))
        if _refusal is not None:
            from meshpipeline.errors import FailureClass, user_message_for
            _pub(job_id).closing(user_message_for(FailureClass.RESOURCE), _terminal_event_id(job_id))
            await _worker_engine.dispose()
            return _refusal.detail

        _backend_name, _backend_exec_id = _backend_execution_id(job_id)
        _claimed = await _fence.claim_delivery(
            AsyncSessionLocal, job_repo, job_id, jlog=jlog,
            backend=_backend_name, backend_execution_id=_backend_exec_id)
        if isinstance(_claimed, _fence.DeliveryRefused):
            return _claimed.detail
        ownership, worker_token = _claimed.ownership, _claimed.worker_token
        # The intake transcript is written HERE, after the claim, because claiming is what binds
        # the tenant these records file under - taken from the job ROW, never from the dispatch
        # payload. Written any earlier there is no owner to file them under, so every one is
        # dropped; capture is fail-open, so the run still succeeds having retained nothing.
        if req.intake_events:
            _emit_intake_events(job_id, req.intake_events)
        # Stateless; used below for the heartbeat, the pre-finalize ownership check and the
        # fenced terminal transition.
        from meshpipeline.persistence.lease import LeaseRepository
        lease_repo = LeaseRepository()

        # RUN-BOUNDARY ADMISSION: does this run still match what the user approved? Both gates -
        # the exact-equality patch contract and the COMPLETE approved-intent fingerprint - and the
        # durable refusal that follows either belong to approved_patch_contract. They run before
        # any expensive work: no state, no graph, no builder, no executor, no native process.
        from meshpipeline.application import approved_patch_contract as _apc
        _admission_error = _apc.check_admission(req)
        if _admission_error is not None:
            _refusal_detail = await _apc.refuse_admission(
                AsyncSessionLocal, _admission_error, job_id=job_id,
                snapshot_id=req.approved_snapshot_id, job_repo=job_repo, jlog=jlog,
                publish=lambda msg: _pub(job_id).closing(msg, _terminal_event_id(job_id)))
            await _worker_engine.dispose()
            return _refusal_detail

        jlog.info("STEP mode: owner=%s", owner_id)

        # THE execution entry. Whatever local path a previous process wrote into a checkpoint is
        # irrelevant here - this process resolves the tenant's row, reconciles it against the
        # approved snapshot, downloads into its OWN workspace and verifies the bytes before the
        # graph exists. A failure is terminal on purpose: meshing the wrong geometry is worse than
        # not meshing at all, and past this point the two are indistinguishable.
        from meshpipeline.application import geometry_materializer as _gm
        from meshpipeline.application import job_service as _js

        # CLASSIFY BEFORE FETCHING. The disposition of this generation's thread decides whether
        # the source bytes are needed at all, so it is read first. A run whose graph already
        # finished needs no geometry to be terminalized - and making it download the source would
        # mean a completed computation becomes unrecoverable the moment its input is deleted.
        # A checkpoint we cannot read is likewise no reason to pull bytes we will never use.
        _ckpt_generation = ownership.execution_generation if ownership is not None else 0
        _ckpt_thread = f"{job_id}:s{STATE_SCHEMA_VERSION}:g{_ckpt_generation}"
        # EXECUTION PREPARATION: resolve the checkpoint disposition and materialize the geometry.
        # Both failures - an unreadable checkpoint and unmaterializable geometry - are terminal
        # BEFORE execution and share one classified shape, so geometry_materializer owns them.
        _prep = await _gm.prepare_for_execution(
            AsyncSessionLocal,
            job_id=job_id, geometry_source=req.geometry_source,
            geometry_interpretation=req.geometry_interpretation,
            classify_checkpoint=_classify_checkpoint, checkpoint_thread=_ckpt_thread,
            job_repo=job_repo, jlog=jlog,
            publish=lambda msg: _pub(job_id).closing(msg, _terminal_event_id(job_id)))
        if _prep.refusal is not None:
            await _worker_engine.dispose()
            return _prep.refusal
        _materialized = _prep.materialized
        _disposition = _prep.disposition

        from meshpipeline.pipeline.state_factory import (
            make_pipeline_state,
            pin_selected_engine,
        )
        initial_state: PipelineState = make_pipeline_state(
            job_id=job_id,
            domain=req.domain,
            user_id=owner_id,
            session_id=session_id,
            geometry=_materialized.to_state() if _materialized else None,
            request_txt=request_txt,
            review_brief_txt=review_brief_txt or "",
            intake_patches=intake_patches or [],
            dimensionality=dimensionality or "",
            purpose=purpose or "",
            input_kind=input_kind or "",
            requested_mesh_fidelity=req.requested_mesh_fidelity,
            effective_mesh_fidelity=req.effective_mesh_fidelity,
            mesh_fidelity_source=req.mesh_fidelity_source,
            requested_extents=req.requested_extents,
            reference_length_m=req.reference_length_m,
            requirements_strict=req.requirements_strict,
            flow_axis=req.flow_axis,
            agent_model_configs={
                "builder":    {"model": bcfg.BUILDER_MODEL,  "temperature": bcfg.BUILDER_TEMPERATURE,    "max_tokens": bcfg.BUILDER_MAX_TOKENS},
                "reviewer":   {"model": rcfg.REVIEWER_MODEL, "temperature": rcfg.REVIEWER_TEMPERATURE,   "max_tokens": rcfg.REVIEWER_MAX_TOKENS},
                # No terminal-response entry: the run's final verdict is rendered by application
                # code from durable facts, so no model shapes it and there is no provenance to record.
                # capture provenance for the OTHER LLM surfaces too - the corpus
                # must record every model that shaped the sample
                "intake":     {"model": provcfg.DEEPSEEK_MODEL, "temperature": icfg.INTAKE_TEMPERATURE, "max_tokens": icfg.INTAKE_MAX_TOKENS},
                "planner":           {"model": bcfg.BUILDER_MODEL},
                "engine_selector":   {"model": None},
                "search_summarizer": {"model": provcfg.SEARCH_SUMMARIZER_MODEL,
                                      "temperature": provcfg.SEARCH_SUMMARIZER_TEMPERATURE,
                                      "max_tokens": provcfg.SEARCH_SUMMARIZER_MAX_TOKENS},
            },
        )

        # THE USER'S ENGINE IS THE RUN'S ENGINE. Pinned into the state before the graph exists, so
        # node_engine_select honours it rather than deriving a candidate from the purpose. Placed
        # BEFORE dispute seeding on purpose: a dispute rebuild must use the engine the delivered
        # mesh came from, so the parent's pin wins below.
        async def _announce_engine(name: str) -> None:
            # Execution-scoped: ownership is verified per event, and a refusal propagates - a
            # superseded worker must not keep orchestrating. The graph binding at the bottom of
            # this function covers the run itself; orchestration before it must bind its own.
            from meshpipeline.application.execution_fence import execution_ownership as _own
            with _own(ownership, session_factory=AsyncSessionLocal):
                _e = execution_publisher(job_id, "engine_select")
                await _e.astage(op_id="pinned")
                await _e.anote(f"Mesh engine: {name} - the one you asked for", op_id="pinned")

        _pinned_engine = await pin_selected_engine(initial_state, req.mesh_engine, jlog=jlog,
                                                   publish=_announce_engine)

        # Engine-native params ONLY: resolved deterministically against the spec of the engine
        # that will ACTUALLY run (an unknown name above stays unpinned, so params resolve
        # against the default). The flow `topology` is NOT a param - it is a DERIVED FACT of
        # the purpose (external_cfd IS external flow; asking it twice is how `internal_cfd` +
        # `topology=external` became an accepted submission). It travels on the neutral
        # `flow_topology` state field, keeping engine_params to engine-native knobs only.
        from meshpipeline.engines.purposes import topology_of
        from meshpipeline.engines.registry import resolve_engine_params
        initial_state["engine_params"] = resolve_engine_params(_pinned_engine, req.engine_params)
        initial_state["flow_topology"] = topology_of(getattr(req, "purpose", "") or "")

        if req.user_dispute:
            # DISPUTE RUN - the user flagged issues on a delivered mesh. The parent resolution, the
            # durable record of what was disputed and the eight state keys it seeds all belong to
            # job_service, beside the final-attempt lookup they depend on. The parent's engine
            # deliberately overwrites the user pin above: a rebuild must use the mesher the
            # delivered mesh came from.
            async def _announce_dispute(flags: int) -> None:
                from meshpipeline.application.execution_fence import execution_ownership as _own
                with _own(ownership, session_factory=AsyncSessionLocal):
                    _i = execution_publisher(job_id, "intake")
                    await _i.astage(op_id="dispute-opened")
                    await _i.anote(f"Re-reviewing the mesh at {flags} region(s) you flagged",
                                   op_id="dispute-opened")

            await _js.seed_dispute_run(initial_state, req.user_dispute, job_id=job_id, jlog=jlog,
                                       publish=_announce_dispute)

        initial_state["schema_version"] = STATE_SCHEMA_VERSION
        # MAY THIS RUN BEGIN? pipeline_budget anchors the absolute top-level deadline and decides;
        # terminal_finalize persists the refusal. This routes between them and owns neither.
        from meshpipeline.application import fenced_checkpointer as _fc
        from meshpipeline.application import pipeline_budget as _pb
        _start = _pb.decide_start(ownership, _claimed.job_created_at)
        _pipeline_deadline = _start.deadline_epoch
        initial_state["pipeline_deadline_epoch"] = _pipeline_deadline
        # the generation every side effect namespaces on (workspace dir, artifact binding).
        initial_state["execution_generation"] = (
            ownership.execution_generation if ownership is not None else 0)
        if _start.exhausted:
            from meshpipeline.application.terminal_finalize import refuse_before_execution
            from meshpipeline.errors import FailureClass
            jlog.error("Pipeline budget exhausted before start - failing job_id=%s", job_id)
            _refusal = await refuse_before_execution(
                AsyncSessionLocal, job_id=job_id, failure_class=FailureClass.INTERNAL,
                dependency="pipeline",
                detail="pipeline total budget exhausted before start",
                user_message=_pb.EXHAUSTED_MESSAGE, reason=_start.reason,
                job_repo=job_repo, jlog=jlog,
                publish=lambda msg: _pub(job_id).closing(msg, _terminal_event_id(job_id)))
            await _worker_engine.dispose()
            return _refusal
        # namespace the checkpoint thread by schema version AND execution GENERATION. Schema
        # version keeps a redelivery under changed PipelineState shape from resuming an incompatible
        # old-schema checkpoint. Generation makes the checkpoint state generation-fenced: a DIFFERENT-
        # execution takeover (a new generation) gets a FRESH thread and re-runs idempotently, so a
        # stale generation's worker can never write into - or resume from - the current generation's
        # checkpoint. A SAME-execution restart keeps its generation, so it resumes its own thread
        # exactly (no duplicated native/model work).
        _generation = ownership.execution_generation if ownership is not None else 0
        _thread_id = f"{job_id}:s{STATE_SCHEMA_VERSION}:g{_generation}"
        graph_config = {"configurable": {"thread_id": _thread_id}}
        _using_memory_saver = False
        _checkpointer_ok = False

        def _fenced_checkpointer(inner):
            from meshpipeline.application.fenced_checkpointer import FencedCheckpointer
            return FencedCheckpointer(
                inner, job_id=job_id, execution_generation=_generation,
                approved_intent_fingerprint=req.approved_intent_fingerprint or "",
                state_schema_version=STATE_SCHEMA_VERSION)


        # CONTINUATION: what to hand `ainvoke`, given what the durable thread already holds.
        # Interpreting the disposition is checkpoint semantics and lives with the fenced writer.
        _continuation = _fc.plan_continuation(_disposition, initial_state, _materialized)

        async def _graph_input(graph):
            return await _fc.enter_graph(graph, graph_config, _continuation, _materialized,
                                         job_id=job_id, generation=_generation, jlog=jlog)

        async def _invoke_with_heartbeat(graph):
            if ownership is None:
                return await graph.ainvoke(await _graph_input(graph), config=graph_config)
            _stop = asyncio.Event()

            async def _beat():
                _interval = max(1, int(rtcfg.WORKER_HEARTBEAT_SECONDS))
                while not _stop.is_set():
                    try:
                        await asyncio.wait_for(_stop.wait(), timeout=_interval)
                        return                       # stop requested (run finished)
                    except TimeoutError:
                        pass
                    try:
                        async with AsyncSessionLocal() as _hb:
                            _ok = await lease_repo.heartbeat(_hb, ownership)
                            await _hb.commit()
                        if not _ok:
                            jlog.error("Lease LOST - job_id=%s generation=%d token=%s: a newer "
                                       "generation/token owns this job; the terminal fence will make "
                                       "this worker's side effects inert.", job_id,
                                       ownership.execution_generation, ownership.token_hash())
                            return
                    except Exception as _hbx:  # noqa: BLE001 - a transient heartbeat error is retried
                        jlog.warning("heartbeat error job_id=%s: %s - retrying next interval",
                                     job_id, _hbx)

            _hb_task = asyncio.create_task(_beat())
            try:
                # BIND execution ownership for the whole graph run. Every in-graph fence seam
                # (Planner, Builder node + side-effecting tools, native accept, executor gates,
                # Reviewer, checkpoint writes) reads it from this context - so the worker token
                # reaches them WITHOUT ever entering PipelineState, the checkpoint, the export, or
                # any log. The binding is always restored on exit.
                from meshpipeline.application.execution_fence import execution_ownership
                with execution_ownership(ownership, session_factory=AsyncSessionLocal):
                    return await graph.ainvoke(await _graph_input(graph),
                                               config=graph_config)
            finally:
                _stop.set()
                try:
                    await _hb_task
                except Exception:  # noqa: BLE001 - heartbeat teardown never affects the run result
                    pass

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            _pg_dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
            async with AsyncPostgresSaver.from_conn_string(_pg_dsn) as checkpointer:
                await checkpointer.setup()
                _checkpointer_ok = True
                # every authoritative checkpoint WRITE is fenced on execution ownership, and each
                # checkpoint is stamped with the run identity (job/fingerprint/generation/attempt/
                # state schema). Reads stay unfenced so a legitimate restart can still resume.
                graph = build_graph(checkpointer=_fenced_checkpointer(checkpointer))
                final_state = await _invoke_with_heartbeat(graph)
        except Exception as _cp_exc:
            if _checkpointer_ok:
                raise
            # outside dev/test, durable checkpointing is MANDATORY. Rather than silently degrade
            # to in-process MemorySaver (a mid-run restart would then re-run from scratch with no
            # operator signal), FAIL CLOSED - fail the job truthfully, before any expensive work.
            if polcfg.REQUIRE_DURABLE_CHECKPOINTER:
                jlog.error(
                    "Durable checkpointer (AsyncPostgresSaver) could not initialize and "
                    "REQUIRE_DURABLE_CHECKPOINTER is on - refusing to run without persistent "
                    "checkpoints (install psycopg[binary] + libpq5 in the worker image). "
                    "reason=%s: %s", type(_cp_exc).__name__, _cp_exc)
                raise RuntimeError(
                    "durable checkpoint initialization failed and a durable checkpointer is "
                    f"required (ENV={polcfg.ENV}) - refusing to run with in-process checkpoints"
                ) from _cp_exc
            from langgraph.checkpoint.memory import MemorySaver
            _using_memory_saver = True
            jlog.warning(
                "AsyncPostgresSaver unavailable - falling back to MemorySaver "
                "(in-process checkpoints only; permitted because REQUIRE_DURABLE_CHECKPOINTER is off "
                "in this dev/test environment) - reason=%s: %s",
                type(_cp_exc).__name__, _cp_exc,
            )
            graph = build_graph(checkpointer=_fenced_checkpointer(MemorySaver()))
            final_state = await _invoke_with_heartbeat(graph)

        # An ABSENT verdict means the reviewer produced none - it is NOT a rejection. Defaulting
        # this to "FAIL" manufactured a quality judgement for runs the reviewer never concluded
        # (or never reached at all), which the terminal record then reported as review_rejected.
        # THE OUTCOME the graph produced, reduced to the facts a terminal status depends on, and
        # the status it is entitled to. Both rules live in final_result: each exists because a
        # specific way of over-claiming success was possible, and they are policy, not sequencing.
        from meshpipeline.application.final_result import (
            RunOutcome,
            derive_terminal_status,
            layer_coverage_caveat,
        )
        _run_outcome = RunOutcome.from_graph_state(final_state)
        # Authored HERE and only here, from the SAME predicate that grants the status - the
        # graph state's requirement_caveats never carries it, so mid-run routing (the
        # executor caveat branch, the classifier near-miss branch, the reviewer's
        # adjudicated-deviations block) is never exposed to a layer caveat.
        _layer_caveat = layer_coverage_caveat(_run_outcome)
        verdict, api_failure = _run_outcome.reviewer_verdict, _run_outcome.api_failure

        retry_count = _run_outcome.retry_count
        if retry_count > 0:
            async with AsyncSessionLocal() as db:
                await job_repo.update_current_attempt(db, uuid.UUID(job_id), retry_count)
                await db.commit()

        _uploaded_artifacts: list[dict] = []
        # No DB write yet: on success artifacts must be delivered FIRST and the status committed
        # ONCE afterwards, so a client never sees 'succeeded' before the mesh is fetchable.
        _decision = derive_terminal_status(_run_outcome, job_id=job_id, jlog=jlog)
        final_status, _failed_reason = _decision.status, _decision.failed_reason

        # FENCE before ANY terminal side effect. A worker whose generation/token was superseded
        # (its lease taken over while it ran) must produce NOTHING externally observable: it must not
        # deliver artifacts to the store, commit a terminal status, persist a final_result, or publish
        # a terminal event - the current owner does all of that. This read-only pre-check fails fast so
        # a fenced worker does not even upload; the AUTHORITATIVE fence is the row-lock inside the
        # atomic terminal transaction below.
        if ownership is not None:
            async with AsyncSessionLocal() as _fdb:
                _still_owner = await lease_repo.is_current_owner(_fdb, ownership)
            if not _still_owner:
                jlog.warning("Worker FENCED before finalize (generation/token superseded) - producing "
                             "no terminal side effects; the current owner finalizes. job_id=%s", job_id)
                await _worker_engine.dispose()
                return {"job_id": job_id, "status": "fenced", "skipped": "not_owner"}

        # Artifacts are delivered ONLY on a genuine success (verdict PASS with a real
        # visual review). DELIVER FIRST - a delivery failure downgrades the job to a
        # system failure, recorded by the single status commit below.
        if final_status == JobStatus.succeeded:
            # Deliver FIRST, commit status ONCE afterwards: a client must never see `succeeded`
            # before the mesh is fetchable. A run whose mesh cannot reach the user did not succeed,
            # so the downgrade travels back with the delivery outcome rather than being re-derived
            # here. The retry policy and the failure classification belong to the uploader.
            from meshpipeline.application.artifact_uploader import deliver_succeeded_run

            # BIND execution ownership for artifact delivery. The graph span closed above, so the
            # uploader's own progress and failure events would otherwise be published by a worker
            # that cannot say which execution it speaks for - a superseded one included.
            #
            # This is context PROPAGATION, not authorization: the authorization is the
            # `is_current_owner` check above, which a stale worker fails before reaching here. The
            # binding only carries the identity that check just confirmed into the delivery call.
            #
            # It covers the awaited delivery and nothing else. `apply_delivery`, the status commit
            # and the terminal/outbox path stay outside it: terminal authority must be able to
            # speak when the execution no longer owns the job, which is the whole point of it
            # being a separate authority.
            #
            # KNOWN LIMIT, unchanged by this: a takeover that lands between the PostgreSQL check
            # and a Redis event is still possible. Closing that needs the central atomic Redis
            # fence, which is a later batch.
            from meshpipeline.application.execution_fence import execution_ownership
            with execution_ownership(ownership, session_factory=AsyncSessionLocal):
                _delivery = await deliver_succeeded_run(
                    AsyncSessionLocal,
                    job_id=job_id, owner_id=owner_id,
                    workspace=final_state.get("openfoam_workspace", ""),
                    engine=str(final_state.get("engine", "") or ""),
                    delivery_attempt=int(final_state.get("retry_count", 0) or 0),
                    execution_generation=_generation,
                    jlog=jlog, publish=_pub, persist_orphans=_persist_orphans)
            _uploaded_artifacts = _delivery.delivered
            # A run whose mesh could not be delivered did not succeed - the downgrade is a status
            # rule owned by final_result, not two lines of orchestration.
            from meshpipeline.application.final_result import apply_delivery
            _decision = apply_delivery(_decision, _delivery)
            final_status, _failed_reason = _decision.status, _decision.failed_reason

        # THE TERMINAL OUTCOME. Assembly (read the durable artifact rows, apply the readiness
        # policy, render the application-owned verdict) and the ONE atomic fenced commit that
        # persists it both belong to terminal_finalize; this call is the lifecycle step, not the
        # policy. A fenced worker writes and publishes nothing - the current owner finalizes.
        from meshpipeline.application.terminal_finalize import (
            TerminalAssembly,
            assemble_and_finalize,
        )
        _publication = await assemble_and_finalize(
            AsyncSessionLocal,
            TerminalAssembly(
                job_id=job_id, owner_id=owner_id, status=final_status,
                failed_reason=_failed_reason,
                engine=final_state.get("engine", "") or "",
                purpose=final_state.get("purpose", "") or "",
                dimensionality=final_state.get("dimensionality", "") or "",
                requested_mesh_fidelity=req.requested_mesh_fidelity,
                effective_mesh_fidelity=req.effective_mesh_fidelity,
                mesh_fidelity_source=req.mesh_fidelity_source,
                fidelity_policy_version=req.fidelity_policy_version,
                approved_snapshot_id=str(final_state.get("approved_snapshot_id", "") or ""),
                executor_success=bool(final_state.get("executor_success", False)),
                reviewer_verdict=str(final_state.get("reviewer_verdict", "") or ""),
                failed_gate=str(final_state.get("executor_failed_gate", "") or ""),
                api_failure=api_failure,
                attempts=int(final_state.get("retry_count", 0) or 0),
                attempts_max=int(bcfg.BUILDER_MAX_TOTAL_ATTEMPTS),
                pipeline_timed_out=_pb.is_exhausted(_pipeline_deadline),
                requirement_caveats=(list(final_state.get("requirement_caveats") or [])
                                     + ([_layer_caveat] if _layer_caveat else [])),
                pre_composed_message=str(final_state.get("outcome_message") or "").strip()),
            ownership=ownership, lease_repo=lease_repo, job_repo=job_repo, jlog=jlog)
        if _publication.fenced:
            await _worker_engine.dispose()
            return {"job_id": job_id, "status": "fenced", "skipped": "not_owner"}
        # Non-fenced publication always carries the rendered verdict (the fenced branch returned
        # above); binding it explicitly keeps that guarantee visible rather than implied.
        assert _publication.result is not None
        _result = _publication.result
        _final_closing = _publication.closing_message
        _job_created_at, _job_ended_at = _publication.created_at, _publication.ended_at

        # POST-TERMINAL RECORDING. The verdict is already durable and published; these three
        # records - the training capture, the viewer preview and the conversation export - each
        # have their own policy and none may affect the outcome. post_terminal owns them.
        from meshpipeline.application import post_terminal as _pt
        _pt.capture_terminal_record(
            job_id, generation=_generation, final_result=_result.to_dict(),
            terminal_message=_final_closing, geometry_source=_geometry_provenance(final_state),
            agent_model_configs=final_state.get("agent_model_configs", {}), jlog=jlog)

        _pt.copy_viewer_preview(job_id, final_state.get("openfoam_workspace", ""), jlog=jlog)
        _pt.enqueue_conversation_export(job_id, final_state, created_at=_job_created_at,
                                        ended_at=_job_ended_at, jlog=jlog)

        # NOTE: the terminal closing is NOT published directly here any more. It was enqueued into the
        # transactional outbox inside the atomic terminal transaction and delivered by the outbox
        # publisher (fast path above + a durable recovery sweep) - so the user's terminal message
        # survives this worker's death, and there is exactly one publisher of the terminal event.

    except Exception as exc:
        # A run that died still owes the user a truthful terminal record. Classification, the
        # verdict built from DURABLE evidence, the fence, the atomic commit and the direct-closing
        # fallback all belong to terminal_finalize; this is the lifecycle hook, not the policy.
        # Only `Exception` is caught: CancelledError and SystemExit are BaseException and propagate
        # untouched, so a cancelled run never gets a terminal record it has no status for.
        from meshpipeline.application.terminal_finalize import (
            TerminalAssembly as _TA,
        )
        from meshpipeline.application.terminal_finalize import (
            finalize_crash as _finalize_crash,
        )
        from meshpipeline.errors import user_message_for as _umf

        def _direct_closing(_fc):
            _pub(job_id).closing(_umf(_fc), _terminal_event_id(job_id))

        await _finalize_crash(
            AsyncSessionLocal, exc,
            job_id=job_id, owner_id=owner_id,
            assembly_defaults=_TA(
                job_id=job_id, owner_id=owner_id, status=JobStatus.failed, failed_reason=None,
                requested_mesh_fidelity=req.requested_mesh_fidelity,
                effective_mesh_fidelity=req.effective_mesh_fidelity,
                mesh_fidelity_source=req.mesh_fidelity_source,
                fidelity_policy_version=req.fidelity_policy_version,
                attempts_max=int(bcfg.BUILDER_MAX_TOTAL_ATTEMPTS)),
            approved=_approved_payload_for_crash,
            graph=locals().get("graph"), graph_config=locals().get("graph_config"),
            ownership=locals().get("ownership"), lease_repo=locals().get("lease_repo"),
            job_repo=job_repo, jlog=jlog, publish=_direct_closing,
            used_durable_checkpointer=_checkpointer_ok)
        # The crash must still reach Celery and monitoring.
        raise
    finally:
        await _worker_engine.dispose()
        # Eager per-loop cleanup of the GLOBAL engine: any surface that fell back to
        # persistence.session.get_db during this task (orphan persistence, geometry
        # materialization, fence fallback) cached an engine keyed to THIS loop. Dispose it
        # while the loop is still alive so its connections close properly; the sweep in
        # session._get_bundle is only the backstop for paths that never reach this finally.
        from meshpipeline.persistence.session import dispose_engine as _dispose_global
        await _dispose_global()

    return {"job_id": job_id, "status": final_status.value, "verdict": verdict}


