# Responsibility: Own the approval gate: propose requirements, read the user's answer, and record the transition.
# Owns: intent classification, the durable approval snapshot, deferral, invalidation, and the confirm transaction.
# Boundaries: one live approval at a time.
from __future__ import annotations

import enum
import re
import time
import uuid

APPROVAL_TTL_S = 1800   # a snapshot the user never answers goes stale

AWAITING = "awaiting_confirmation"
APPROVED = "approved"
INVALIDATED = "invalidated"
DISPATCHED = "dispatched"

# Deterministic confirmation grammar. Deliberately WHOLE-MESSAGE: "yes" approves, but
# "yes, but make the far-field 50 chords" does NOT - it is agreement WITH A CHANGE, and the old
# keyword consent classifier dispatched exactly that kind of message with the stale requirements.
# Anything that is not a bare approval or a bare hedge goes back to intake as a correction.
_FILLER = {"please", "ok", "okay", "thanks", "thank", "you", "alright", "right",
           "great", "perfect", "then", "now", "just", "lets", "let", "us", "s", "and"}

_APPROVE = {
    "yes", "y", "yep", "yeah", "sure", "yes sure", "yes proceed", "proceed", "yes proceed with mesh generation",
    "proceed with mesh generation", "proceed exactly as shown", "yes proceed exactly as shown",
    "exactly as shown", "approve", "approve this", "approve this configuration", "approved",
    "i approve", "i approve this", "use these requirements", "use these", "confirm", "confirmed",
    "i confirm", "go", "go ahead", "yes go ahead", "start", "start the mesh",
    "start mesh generation", "yes start the mesh", "run it", "yes run it", "do it", "yes do it",
    "correct proceed", "yes correct", "that is correct proceed", "looks good proceed",
    "yes looks good", "yes that is right", "that is right proceed", "yes approve",
    "yes confirmed", "yes confirm", "confirm dispatch", "yes dispatch",
}

_HEDGE = {
    "maybe", "perhaps", "probably", "possibly", "i think so", "i think", "not sure",
    "i am not sure", "im not sure", "unsure", "hmm", "hm", "i guess", "i suppose",
    "maybe yes", "probably yes", "i dont know", "dont know", "no idea",
}

APPROVE_INTENT = "approve"
HEDGE_INTENT = "ambiguous"
CORRECTION_INTENT = "correction"


def _normalise(text: str) -> str:
    t = re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold())
    words = [w for w in t.split() if w not in _FILLER]
    return " ".join(words)


def classify(message: str) -> str:
    n = _normalise(message)
    if not n:
        return HEDGE_INTENT
    if n in _APPROVE:
        return APPROVE_INTENT
    if n in _HEDGE:
        return HEDGE_INTENT
    return CORRECTION_INTENT


CLARIFICATION = (
    "I need a clear answer before spending anything: reply \"yes, proceed\" to run exactly the "
    "configuration shown above, or tell me what to change and I will re-check it and show you a "
    "new summary."
)


def create(*, owner_id: str, session_id: str, selection_id: str, token_id: str,
           canonical: dict, fingerprint: str, payload: dict, summary: str,
           proposal_revision: str, proposal_msg_count: int,
           intent_canonical: dict | None = None, intent_fingerprint: str = "") -> dict:
    now = time.time()
    return {
        "id": uuid.uuid4().hex,
        "owner": owner_id,
        "session": session_id,
        "selection_id": selection_id,
        "token_id": token_id,
        "canonical": canonical,
        "fingerprint": fingerprint,
        "intent_canonical": intent_canonical or {},
        "intent_fingerprint": intent_fingerprint,
        "payload": payload,
        "summary": summary,
        "proposal_revision": proposal_revision,
        "proposal_msg_count": int(proposal_msg_count),
        # The ONE message this approval is answerable by.
        "expected_confirmation_msg_count": int(proposal_msg_count) + 1,
        "created_at": now,
        "expires_at": now + APPROVAL_TTL_S,
        "status": AWAITING,
    }


def is_live(approval: dict | None) -> bool:
    if not approval:
        return False
    return approval.get("status") == AWAITING and time.time() <= approval.get("expires_at", 0)


def verify(approval: dict | None, *, owner_id: str, session_id: str, selection: dict | None,
           user_msg_count: int) -> tuple[bool, str]:
    from meshpipeline.agents.intake import engine_selection as es

    if not approval:
        return False, "there is nothing awaiting your approval"
    if approval.get("status") == DISPATCHED:
        return False, "that configuration has already been dispatched"
    if approval.get("status") != AWAITING:
        return False, "that configuration is no longer awaiting approval"
    if approval.get("owner") != owner_id or approval.get("session") != session_id:
        return False, "that configuration belongs to a different session or owner"
    if time.time() > approval.get("expires_at", 0):
        return False, "that summary has expired - I will re-check and show you a fresh one"
    if es.state_of(selection) != es.CONFIRMED or not selection:
        return False, "the engine selection behind that summary is no longer confirmed"
    if approval.get("selection_id") != selection.get("id"):
        return False, "the engine changed after that summary was shown"
    # The approval survives EXACTLY the expected confirmation message and nothing later.
    if int(user_msg_count) != int(approval.get("expected_confirmation_msg_count", -1)):
        return False, ("that summary is stale - you have said something else since, so I will "
                       "re-check and show you a fresh one")
    return True, ""


def defer(approval: dict | None) -> dict | None:
    if not approval:
        return None
    return {**approval,
            "expected_confirmation_msg_count": int(approval.get("expected_confirmation_msg_count", 0)) + 1,
            "deferred_count": int(approval.get("deferred_count", 0)) + 1}


def invalidate(approval: dict | None, reason: str) -> dict | None:
    if not approval:
        return None
    return {**approval, "status": INVALIDATED, "invalidated_reason": reason,
            "invalidated_at": time.time()}


def builder_payload(approval: dict) -> dict:
    return dict(approval.get("payload") or {})


# #
# THE APPROVAL TRANSACTION - turning a live approval snapshot into a dispatched run.
# Extracted from api/v1/chat.py::_confirm_pending_approval, where ~263 lines of application
# transaction sat inside an HTTP route: the row lock, the compare-and-set on the approval snapshot,
# quota admission, job creation, source and unit resolution, the retention check, the state
# transition, the builder handoff and two fingerprint bindings.
# This module already owned the approval LIFECYCLE (classify/create/verify/defer/invalidate); it now
# owns the transaction that lifecycle exists to gate. There is deliberately no application/approval.py:
# a second approval authority is what the audit refused.
# NOTHING HERE IMPORTS FASTAPI. Outcomes are typed values; mapping them to status codes and response
# bodies is the route's job and only the route's.
# ORDERING, unchanged and load-bearing:
#   lock the session row -> CAS the snapshot -> quotas -> create the job -> resolve source + unit ->
#   REFUSE A PURGED SOURCE -> link -> mark dispatched -> disarm consent -> COMMIT -> bind -> dispatch
# The purge check precedes every object-store read and the whole dispatch, so no run is created for
# bytes that no longer exist.
# #

from dataclasses import dataclass  # noqa: E402


class ConfirmStatus(str, enum.Enum):

    dispatched = "dispatched"              # a new run was created and handed off
    already_running = "already_running"    # idempotent: this session already has a job
    already_dispatched = "already_dispatched"
    refused = "refused"                    # the snapshot no longer verifies (user-recoverable)
    no_geometry = "no_geometry"
    no_confirmed_unit = "no_confirmed_unit"
    source_expired = "source_expired"      # retention purge - the bytes are gone
    quota_exceeded = "quota_exceeded"
    inconsistent = "inconsistent"          # payload/approval binding failed - internal defect
    dispatch_failed = "dispatch_failed"    # the job exists but nothing is running


@dataclass(frozen=True)
class ConfirmOutcome:

    status: ConfirmStatus
    message: str = ""
    job_id: uuid.UUID | None = None
    filename: str = ""
    #: True only when this call is what created and dispatched the run.
    dispatched: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (ConfirmStatus.dispatched, ConfirmStatus.already_running,
                               ConfirmStatus.already_dispatched)


class ApprovalTransactionError(RuntimeError):

    def __init__(self, outcome: ConfirmOutcome) -> None:
        super().__init__(outcome.message or outcome.status.value)
        self.outcome = outcome


def _already_running(job_id) -> ConfirmOutcome:
    return ConfirmOutcome(
        ConfirmStatus.already_running,
        f"Mesh generation is already running for this session. Job ID: {job_id}",
        job_id=job_id)


async def _open_transaction(db, *, session, session_repo, owner_id: str, session_id,
                            source_ref_for_session, interpretation_ref_for_session,
                            source_repo, job_service, job_repo, logger):
    # Lock the session row for the whole create+link transaction. A concurrent consent
    # blocks here until we commit (with job_id set), then sees the link and returns the
    # no-re-dispatch outcome - so two rapid "yes" messages cannot both create a paid job.
    locked = await session_repo.get_for_update(db, session_id)
    if locked is not None and locked.job_id:
        await db.commit()
        logger.info("approval: session %s already linked to job %s (locked check) - idempotent "
                    "no-op", session_id, locked.job_id)
        raise ApprovalTransactionError(_already_running(locked.job_id))

    # COMPARE-AND-SET, under the lock: only a snapshot still `awaiting_confirmation`, still bound to
    # this owner/session/selection, unexpired, and answered by exactly the expected message may
    # proceed. The loser of a race arrives here after the winner committed a linked job.
    gate = dict(getattr(locked, "intake_gate", None) or {})
    snapshot_in, selection = gate.get("approval"), gate.get("selection")
    if (snapshot_in or {}).get("status") == DISPATCHED:
        await db.commit()
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.already_dispatched, "That configuration has already been dispatched.",
            job_id=(snapshot_in or {}).get("job_id")))

    user_msgs = len([m for m in (getattr(locked, "messages", None) or [])
                     if isinstance(m, dict) and m.get("role") == "user"])
    ok, why = verify(snapshot_in, owner_id=owner_id, session_id=str(session_id),
                     selection=selection, user_msg_count=user_msgs)
    if not ok:
        gate["approval"] = invalidate(snapshot_in, why)
        await session_repo.set_intake_gate(db, session_id, gate)
        await db.commit()
        logger.warning("approval: refused - %s - session=%s", why, session_id)
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.refused,
            f"I did not start anything: {why}. Tell me what you would like to do and I will "
            "re-check it and show you a fresh summary."))

    snapshot: dict = dict(snapshot_in or {})          # verified live above
    try:
        await job_service.check_quotas(db, owner_id)
    except ValueError as exc:
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.quota_exceeded, str(exc))) from exc

    job = await job_repo.create(db, owner_id=owner_id)

    # The job inherits the SESSION's source, resolved inside this transaction and scoped to the
    # owner, so a session naming another tenant's upload never links. No directory is renamed: the
    # durable copy is the stored object.
    source_ref = await source_ref_for_session(db, session, owner_id)
    if source_ref is None:
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.no_geometry,
            "No geometry is associated with this session. Upload a file first."))
    # The scale travels with the bytes, resolved in the SAME transaction and scoped to the same
    # owner. A source whose unit was never confirmed cannot be meshed at all - the size would have
    # to be assumed - so it is refused here, in the conversation, not as an opaque failure later.
    interp_ref = await interpretation_ref_for_session(db, session, owner_id)
    if interp_ref is None:
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.no_confirmed_unit,
            "This geometry has no confirmed unit yet, so its physical size is unknown. Confirm "
            "what the file's units represent, then try again."))
    # RETENTION EXPIRY is answered HERE, before any object-store read and before dispatch: the user
    # is in the conversation right now and can act on it, and no job should run for bytes that no
    # longer exist.
    row = await source_repo.get_for_owner(db, uuid.UUID(source_ref.source_id), owner_id)
    if row is not None and getattr(row, "purged_at", None) is not None:
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.source_expired,
            "This geometry was removed after its retention period. Upload the file again to "
            "start a new run."))

    job.geometry_source_id = uuid.UUID(source_ref.source_id)
    job.geometry_interpretation_id = uuid.UUID(interp_ref.interpretation_id)
    await session_repo.link_job(db, session_id, job.id)
    # awaiting_confirmation -> dispatched, in the SAME transaction as the job link, so the two can
    # never disagree and no second confirmation can re-enter this path.
    gate["approval"] = {**snapshot, "status": DISPATCHED,
                        "job_id": str(job.id), "dispatched_at": time.time()}
    await session_repo.set_intake_gate(db, session_id, gate)
    # Disarm consent: clear request_txt so a later affirmative message is not re-classified as
    # consent for an already-started job.
    await session_repo.set_request_txt(db, session_id, "")
    await db.commit()
    return job.id, snapshot, source_ref, interp_ref


def _build_dispatch_payload(*, job_id, owner_id: str, session_id, session, snapshot: dict,
                            source_ref, interp_ref) -> dict:
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
    from meshpipeline.application.dispatch_contract import build as build_dispatch

    approved = builder_payload(snapshot)
    intent = snapshot.get("intent_canonical", {}) or {}
    return build_dispatch(**{
        "job_id": str(job_id),
        "geometry_source": source_ref.to_payload(),
        "geometry_interpretation": interp_ref.to_payload(),
        "owner_id": owner_id,
        "session_id": str(session_id),
        "request_txt": approved.get("request_txt", ""),
        "review_brief_txt": approved.get("review_brief_txt", ""),
        "intake_patches": list(approved.get("patches") or []),
        "dimensionality": approved.get("dimensionality", ""),
        "purpose": approved.get("purpose", ""),
        "input_kind": approved.get("input_kind", ""),
        # The mesh-detail preference EXACTLY as approved - replayed from the approval record, never
        # re-derived from prose and never read off a session column a later turn may have moved.
        "requested_mesh_fidelity": intent.get("requested_mesh_fidelity"),
        "effective_mesh_fidelity": intent.get("effective_mesh_fidelity", ""),
        "mesh_fidelity_source": intent.get("mesh_fidelity_source", ""),
        "fidelity_policy_version": intent.get("fidelity_policy_version", ""),
        "mesh_engine": approved.get("mesh_engine", ""),
        "domain": approved.get("domain", ""),
        "engine_params": dict(approved.get("engine_params") or {}),
        "intake_events": list(session.llm_metadata or []),
        "approved_snapshot_id": str(snapshot.get("id")),
        # THE typed, fingerprinted approved patch contract - built ONCE from the approved snapshot's
        # own declared patches, so the EXACT boundary set is verifiable at reconstruction and at
        # graph admission, before the builder.
        "approved_patch_contract": ApprovedPatchContract.build(
            approved.get("patches") or []).to_dict(),
        # the COMPLETE approved-intent fingerprint from the DURABLE approval record.
        "approved_intent_fingerprint": snapshot.get("intent_fingerprint") or "",
        # THE TYPED DOMAIN REQUEST the gate measures against. A pre-v5 approval carries no
        # strictness bit and predates near-miss delivery entirely - it executes as STRICT, so
        # a later default can never relax an approval's blocking contract.
        "requested_extents": approved.get("requested_extents"),
        "reference_length_m": approved.get("reference_length_m"),
        "flow_axis": approved.get("flow_axis"),
        "requirements_strict": (
            bool(approved.get("requirements_strict"))
            if int((intent or {}).get("schema_version") or 0) >= 5 else True),
    })


_INCONSISTENT = ("Internal consistency error: the approved configuration and the run payload do "
                 "not match. Nothing was started.")


def assert_payload_matches_approval(payload: dict, snapshot: dict, source_ref, *, logger) -> None:
    from meshpipeline.agents.intake import admission_token as at

    snapshot_id = str(snapshot.get("id"))
    builder_canonical = at.canonical_payload(
        payload["mesh_engine"], payload["purpose"], payload["input_kind"],
        payload["dimensionality"], payload["intake_patches"], payload["engine_params"])
    if at.fingerprint(builder_canonical) != snapshot.get("fingerprint"):
        logger.error("approval: builder payload does not match the approved snapshot %s - "
                     "refusing to dispatch", snapshot_id)
        raise ApprovalTransactionError(ConfirmOutcome(ConfirmStatus.inconsistent, _INCONSISTENT))

    stored_intent = snapshot.get("intent_canonical") or {}
    stored_fp = snapshot.get("intent_fingerprint") or ""
    if not stored_fp:
        logger.error("approval: approval snapshot %s carries no complete approved-intent "
                     "fingerprint - refusing to dispatch", snapshot_id)
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.inconsistent,
            "Internal consistency error: the approved configuration is incomplete. Nothing was "
            "started."))
    run_intent = at.approved_intent_canonical(
        engine=payload["mesh_engine"], purpose=payload["purpose"],
        input_kind=payload["input_kind"], dimensionality=payload["dimensionality"],
        patches=payload["intake_patches"], engine_params=payload["engine_params"],
        requested_mesh_fidelity=payload["requested_mesh_fidelity"],
        effective_mesh_fidelity=payload["effective_mesh_fidelity"],
        mesh_fidelity_source=payload["mesh_fidelity_source"],
        fidelity_policy_version=payload["fidelity_policy_version"],
        requested_extents=payload.get("requested_extents"),
        reference_length_m=payload.get("reference_length_m"),
        flow_axis=payload.get("flow_axis"),
        requirements_strict=bool(payload.get("requirements_strict") or False),
        request_txt=payload["request_txt"], source_ref=source_ref)
    if at.fingerprint(run_intent) != stored_fp:
        drifted = sorted(k for k in set(run_intent) | set(stored_intent)
                         if run_intent.get(k) != stored_intent.get(k))
        logger.error("approval: the run's complete approved intent does not match the DURABLE "
                     "approval record %s - drifted field(s): %s - refusing to dispatch",
                     snapshot_id, drifted)
        raise ApprovalTransactionError(ConfirmOutcome(ConfirmStatus.inconsistent, _INCONSISTENT))


async def confirm_pending_approval(session, session_repo, owner_id: str, session_id, *,
                                   logger, sessions=None, job_service=None, job_repo=None,
                                   source_repo=None, dispatch=None) -> ConfirmOutcome:
    from meshpipeline.application.geometry_materializer import (
        interpretation_ref_for_session,
        source_ref_for_session,
    )
    from meshpipeline.application.job_service import JobService
    from meshpipeline.application.pipeline_run import dispatch as dispatch_pipeline
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db

    sessions = sessions or get_db
    job_service = job_service or JobService()
    job_repo = job_repo or JobRepository()
    source_repo = source_repo or GeometrySourceRepository()
    dispatch = dispatch or dispatch_pipeline

    # Fast path only; the AUTHORITATIVE check is under the row lock.
    if session.job_id:
        logger.info("approval: session %s already linked to job %s - no re-dispatch",
                    session_id, session.job_id)
        return _already_running(session.job_id)
    if not getattr(session, "geometry_source_id", None):
        return ConfirmOutcome(ConfirmStatus.no_geometry,
                              "No geometry is associated with this session. Upload a file first.")

    try:
        async with sessions() as db:
            job_id, snapshot, source_ref, interp_ref = await _open_transaction(
                db, session=session, session_repo=session_repo, owner_id=owner_id,
                session_id=session_id,
                source_ref_for_session=source_ref_for_session,
                interpretation_ref_for_session=interpretation_ref_for_session,
                source_repo=source_repo, job_service=job_service, job_repo=job_repo, logger=logger)
    except ApprovalTransactionError:
        raise
    except Exception as exc:
        logger.error("approval: failed to create job: %s", exc)
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.dispatch_failed, "Failed to create simulation job")) from exc

    try:
        payload = _build_dispatch_payload(
            job_id=job_id, owner_id=owner_id, session_id=session_id, session=session,
            snapshot=snapshot, source_ref=source_ref, interp_ref=interp_ref)
        assert_payload_matches_approval(payload, snapshot, source_ref, logger=logger)
        # Persist the run snapshot on the job record, then launch the configured backend
        # (celery locally; a one-shot hosted pipeline job). Reconstructable from the job id alone.
        async with sessions() as db:
            await dispatch(db, str(job_id), payload)
        logger.info("approval: dispatched job_id=%s snapshot=%s intake_events=%d",
                    job_id, snapshot.get("id"), len(session.llm_metadata or []))
    except ApprovalTransactionError:
        raise
    except Exception as exc:
        # The job row already exists, so silence would leave it looking launched forever. Record the
        # launch failure durably (compare-and-set, so a terminal result is never overwritten) and
        # tell the user the truth: nothing is running.
        logger.error("approval: failed to enqueue task: %s", exc)
        try:
            async with sessions() as db2:
                await job_repo.mark_launch_failed(db2, job_id, f"{type(exc).__name__}: {exc}"[:2000])
        except Exception as rec_exc:               # noqa: BLE001 - never mask the original failure
            logger.error("approval: could not record launch failure: %s", rec_exc)
        raise ApprovalTransactionError(ConfirmOutcome(
            ConfirmStatus.dispatch_failed,
            "Could not start mesh generation - the run was not launched and nothing is running. "
            "Your approved configuration was kept; please try again.", job_id=job_id)) from exc

    # ACCEPTED FOR ASYNCHRONOUS LAUNCH - not "started by the worker", which this request cannot
    # know. The distinction matters: the old wording claimed execution had begun even when the
    # worker later refused the payload.
    return ConfirmOutcome(
        ConfirmStatus.dispatched,
        f"Mesh generation accepted and queued for {source_ref.original_filename}. "
        f"Job ID: {job_id}",
        job_id=job_id, filename=source_ref.original_filename, dispatched=True)
