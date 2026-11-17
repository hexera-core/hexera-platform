# Responsibility: Deliver a finished run's artifacts again from a workspace that is still on disk.
# Owns: the guard that decides a terminal run may be re-delivered, and the re-statement of what it delivered.
# Boundaries: it re-runs the product's own delivery; it never changes a job's status and never re-meshes.
# Collaborates with: application/artifact_uploader.py, application/terminal_finalize.py, persistence/lease.py.
#
# WHY THIS EXISTS. Job 0ff6f9e5 meshed a 283,410,955-cell conjugate cold plate in 5 h 07 m, passed
# every gate, and then lost its delivery to a column type: the OpenFOAM case bundle is
# 10,616,027,998 bytes and artifacts.size_bytes was int4, so asyncpg refused the bind parameter
# ("value out of int32 range") on a retry that could never succeed. Delivery is one transaction, so
# the bundle, mesh.msh and viewer_data.json all rolled back together and the run was downgraded to
# `failed` by apply_delivery AFTER it had been granted `succeeded`. The mesh itself is intact in its
# workspace. Re-running the job would cost another five hours on a 64-core VM to rebuild bytes we
# already have, so the deliverable is uploaded again instead - from the same workspace, through the
# same uploader, under the same artifact policy.
#
# WHY IT IS A SEPARATE ENTRY POINT AND NOT A RE-RUN. Two doors are shut, both correctly:
#   - execution_fence.claim_delivery refuses any job already in TERMINAL_STATES before it even
#     classifies the checkpoint, so no worker can pick this job up again.
#   - job_state.LEGAL_SOURCES names no transition OUT of a terminal state, so failed -> pending and
#     failed -> succeeded are illegal by construction.
# Neither is a defect and neither is widened here. This goes AROUND them: it takes no claim, writes
# no status, and touches only the two things that are actually wrong - the artifact rows that never
# landed, and the terminal record's account of them.
from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Nothing was delivered, and why. Each is a REFUSAL by the guard below, not a failure of delivery.
NO_SUCH_JOB = "no_such_job"
NOT_TERMINAL_FAILED = "not_terminal_failed"
WORKSPACE_PURGED = "workspace_purged"
NO_WORKSPACE = "no_workspace"
#: the named workspace is on disk but is not this job's - another run's mesh, under this job's id
FOREIGN_WORKSPACE = "foreign_workspace"
#: The path carries no attempt_<n>, so the attempt that built these bytes is unknown. Guessing
#: it low is the dangerous direction: the artifact CAS reads a lower pair as superseded and
#: registers NOTHING, silently, which is this incident wearing a different hat.
UNNUMBERED_WORKSPACE = "unnumbered_workspace"
NO_TERMINAL_RECORD = "no_terminal_record"
ALREADY_DELIVERED = "already_delivered"
#: The delivery itself ran and did not succeed - the uploader has already said why.
DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class Redelivery:

    job_id: str
    #: why nothing was delivered; "" when the delivery actually ran
    refused: str = ""
    workspace: str = ""
    delivered: list = field(default_factory=list)
    #: the logical artifact classes the job's DURABLE rows carry after this call
    delivered_types: list = field(default_factory=list)
    required_ready: bool = False
    #: whether the terminal record's account of the delivery was re-written. A delivered mesh with
    #: a record that could not be re-stated is still delivered, so this is reported apart from it.
    restated: bool = False

    @property
    def ok(self) -> bool:
        return not self.refused and self.required_ready


def _refused(job_id: str, why: str, detail: str, *, jlog) -> Redelivery:
    jlog.warning("re-delivery REFUSED (%s) for job %s - %s", why, job_id, detail)
    return Redelivery(job_id=job_id, refused=why)


async def redeliver_job(session_factory, job_repo, job_id: str, *, jlog,
                        workspace: str | Path | None = None,
                        publish=None, persist_orphans=None) -> Redelivery:
    """Upload and register a terminal-failed run's deliverables again, from its surviving workspace.

    THE DELIVERY IS THE PRODUCT'S OWN. `deliver_succeeded_run` is called with the same collaborators
    pipeline_run.py:1171 hands it - the same jlog, the same `_pub` publisher factory, the same
    `_persist_orphans` - so the bundle is built, verified against the engine's deliverable contract,
    uploaded, integrity-checked and registered by exactly the code that delivers every other run.
    A hand-written copy of that path would be a second answer to "what did we promise", and the
    first thing it would drift from is the contract check that proves the workspace still holds it.

    Its name says `succeeded_run`, and this run is `failed`. What that function delivers is a
    SUCCEEDED MESH - executor_success, every gate passed - and this run has one; what failed was the
    storing of it. The status the job row carries is not what it reads.
    """
    from meshpipeline.application.artifact_policy import required_ready as _ready
    from meshpipeline.application.terminal_finalize import read_delivered_types
    from meshpipeline.persistence.models import JobStatus

    jid = uuid.UUID(str(job_id))
    async with session_factory() as db:
        row = await job_repo.get_internal(db, jid)
        if row is None:
            return _refused(job_id, NO_SUCH_JOB, "no such job row", jlog=jlog)
        status = getattr(row, "status", None)
        owner_id = str(getattr(row, "owner_id", "") or "")
        generation = int(getattr(row, "execution_generation", 0) or 0)
        purged = bool(getattr(row, "workspace_purged", False))
        record = getattr(row, "final_result", None)

    # ONLY A TERMINAL-FAILED RUN. A succeeded job is the case this guard exists for: its delivery
    # already landed, its artifact rows are the ones a customer is downloading, and re-uploading
    # over them would put a second set of bytes under a live link for no gain. `cancelled` is
    # refused for the stronger reason - the owner asked for the run to end, and every read of a
    # cancelled job withholds its artifacts, so a delivery onto one would be rows nobody may see.
    # A job still in flight is refused because its own worker is the one that delivers it.
    if status is not JobStatus.failed:
        return _refused(job_id, NOT_TERMINAL_FAILED,
                        f"status is {getattr(status, 'value', status)!r}, not failed", jlog=jlog)
    if purged:
        # The purge has already deleted the tree; whatever is at the path now is not this run's.
        return _refused(job_id, WORKSPACE_PURGED, "the job row says its workspace was purged",
                        jlog=jlog)

    if not isinstance(record, dict) or not record:
        return _refused(job_id, NO_TERMINAL_RECORD,
                        "the job carries no terminal record, so what it promised is unknown",
                        jlog=jlog)
    # THE ENGINE COMES FROM THE RECORD, never from the dispatch payload. The ladder can switch
    # engines mid-run (engine_ladder names the one that built the delivered mesh), and the uploader
    # resolves the deliverable contract from the engine the EXECUTION settled on - two engines share
    # a deliverable marker, so guessing it from the workspace is what that contract exists to stop.
    engine = str(record.get("engine") or "")
    purpose = str(record.get("purpose") or "")
    if not engine:
        return _refused(job_id, NO_TERMINAL_RECORD,
                        "the terminal record names no engine, so no deliverable contract applies",
                        jlog=jlog)

    ws = Path(workspace) if workspace else _attempt_workspace(job_id)
    if ws is None or not ws.is_dir():
        return _refused(job_id, NO_WORKSPACE, f"no attempt workspace on disk at {ws}", jlog=jlog)
    # AND IT MUST BE THIS JOB'S. The worst thing this entry point could do is hand one customer
    # another's mesh, and an operator typing a path is the only way in: the deliverable contract
    # would not catch it, because a workspace from a different run of the SAME engine satisfies
    # every check it makes. The layout puts the job id in the path
    # (WORKSPACE_BASE/<job>/generation_<g>/attempt_<n>, agents/builder/workspace._setup_workspace),
    # so the id being a component of it is what "belongs to this job" means on disk.
    if str(job_id) not in ws.resolve().parts:
        return _refused(job_id, FOREIGN_WORKSPACE,
                        f"{ws} is not this job's attempt workspace", jlog=jlog)

    # THE ATTEMPT COMES FROM THE DIRECTORY HOLDING THE BYTES, and deliberately not from the job
    # row. `SimulationJob.current_attempt` is the number a USER is shown (models.py:354) and
    # the uploader's own contract warns against it by name (artifact_uploader.py:419): what it
    # wants is the caller's retry_count, which production reads from the pipeline state
    # (pipeline_run.py:1176) and which can differ from the column in both directions - the
    # column lags an in-flight attempt, and engine_fallback.attempts_made can count past turns
    # where no mesher started. Here there is a better source than either: these bytes are in
    # attempt_<n>, the builder named that directory retry_count + 1 (snappy/drivers._attempt_of),
    # so n - 1 IS the retry_count the lost delivery carried. Not a substitute for it - the same
    # number, read off the thing being delivered.
    attempt = _delivery_attempt_of(ws)
    if attempt is None:
        return _refused(job_id, UNNUMBERED_WORKSPACE,
                        f"{ws} has no attempt_<n> component, so the attempt that built it is "
                        "unknown", jlog=jlog)

    # ALREADY DELIVERED is decided on the DURABLE ROWS, not on the record's own `required_ready`.
    # The record is exactly the thing this incident proved can be wrong about delivery, and it is
    # re-stated at the end of this call from these same rows; reading it here would let a stale
    # `false` authorise an upload over artifacts that are already on the shelf.
    delivered_types = await read_delivered_types(session_factory, job_id, jlog=jlog)
    if _ready(engine, delivered_types, purpose):
        return _refused(job_id, ALREADY_DELIVERED,
                        f"its required deliverables are already ready ({delivered_types})",
                        jlog=jlog)

    # THE PIPELINE'S OWN COLLABORATORS when the caller named none - the same `_pub` factory and the
    # same durable orphan recorder pipeline_run.py:1171 hands the delivery. Bound to NEW names, not
    # back onto the parameters: `publish = publish or _pub` is a self-referential assignment, and
    # devtools/quality/publication_authority - the static reader that proves every publication site
    # has an authority - follows an assigned name's value and recurses on itself until the stack
    # ends, taking the whole hygiene suite's collection with it.
    if publish is None or persist_orphans is None:
        from meshpipeline.application.pipeline_run import _persist_orphans, _pub
        pub = _pub if publish is None else publish
        orphans = _persist_orphans if persist_orphans is None else persist_orphans
    else:
        pub, orphans = publish, persist_orphans

    jlog.info("re-delivering job %s from %s (engine=%s generation=%d retry_count=%d) - the run "
              "stays failed; only its artifacts and its account of them change", job_id, ws,
              engine, generation, attempt)

    from meshpipeline.application.artifact_uploader import deliver_succeeded_run
    delivery = await deliver_succeeded_run(
        session_factory, job_id=str(job_id), owner_id=owner_id, workspace=str(ws), engine=engine,
        # THE RUN'S OWN COORDINATES, not new ones. The artifact CAS compares
        # (execution_generation, delivery_attempt) against the incumbent row: claiming a higher
        # pair would let these rows outrank a genuine later execution's, and claiming a lower one
        # would read as superseded and register nothing at all, without erroring. Both are read
        # off this run - the generation from its row, the attempt from the directory the bytes
        # are in - so they are the pair the lost delivery carried.
        delivery_attempt=attempt, execution_generation=generation,
        jlog=jlog, publish=pub, persist_orphans=orphans,
        fence_commit=_terminal_fence(jid, status))
    if not delivery.succeeded:
        return _refused(job_id, DELIVERY_FAILED, f"the uploader could not deliver: {delivery.error}",
                        jlog=jlog)

    # WHAT IS NOW ON THE SHELF, read back from the durable rows rather than taken from the report:
    # the report says what this call uploaded, and the question an operator is asking is whether the
    # job's required deliverables are ready - which the rows answer and a report cannot.
    delivered_types = await read_delivered_types(session_factory, job_id, jlog=jlog)
    ready = _ready(engine, delivered_types, purpose)
    restated = await _restate_delivery(session_factory, job_repo, jid,
                                       delivered_types=delivered_types, jlog=jlog)
    return Redelivery(job_id=job_id, workspace=str(ws), delivered=list(delivery.delivered),
                      delivered_types=delivered_types, required_ready=ready, restated=restated)


def _attempt_workspace(job_id: str):
    # The same resolver the API uses to find a finished run's workspace (job_service, reached from
    # api/v1/simulation.py:455): the last generation_<g>/attempt_<n> that holds a mesh_manifest.json.
    # A run that produced no manifest produced no deliverable either, so there is nothing to find.
    from meshpipeline.application.job_service import final_attempt_workspace
    return final_attempt_workspace(str(job_id))


def _delivery_attempt_of(ws) -> int | None:
    """The retry_count that produced `ws`, or None if the path does not say.

    The layout is WORKSPACE_BASE/<job>/generation_<g>/attempt_<n> and attempt.prepare numbers
    it retry_count + 1, so the first attempt of a run is attempt_1 and carries retry_count 0.
    """
    for part in reversed(Path(ws).resolve().parts):
        if part.startswith("attempt_"):
            tail = part[len("attempt_"):]
            # A 1-based name, so attempt_0 is not a thing the builder writes; refuse it rather
            # than return -1, which the CAS would compare as superseded-by-everything.
            return int(tail) - 1 if tail.isdigit() and int(tail) >= 1 else None
    return None


def _terminal_fence(job_id: uuid.UUID, expected):
    """The registration fence for a job no execution owns.

    THE PRODUCTION FENCE CANNOT BE REUSED, and copying its shape would be worse than useless.
    `lock_ownership_for_commit` holds THIS WORKER'S claim under the job row's lock and refuses if
    the generation or token moved. A terminal job has no live claim at all - no token, no lease -
    so that check has nothing to hold and would refuse every time (or, with no ownership bound,
    return without checking anything, which is a fence in name only).

    What is actually worth serialising here is different, and this is it: the guard above decided
    on a job that was `failed` and unpurged, and between that read and this commit someone can
    re-run the job (a re-dispatch takes pending, then running) or the purge can delete the tree. The
    job row is locked and re-read in the registration's OWN transaction, so the two orders are the
    only two possible: either this commits first and the re-run starts beside rows that are already
    durable, or the re-run commits first and this raises - and the rows roll back with the session,
    exactly as a fenced worker's do, because `deliver_succeeded_run` already treats StaleWorkerFenced
    as "not mine to deliver": it persists what it stored for the reconciler and re-raises without
    downgrading anything.
    """
    async def _fence(db) -> None:
        # `_locked_job` rather than a second SELECT ... FOR UPDATE written here: it is the one
        # definition of "the job row as the database holds it under its lock", and the two things
        # that make it correct (populate_existing, so the identity map cannot answer with a read
        # taken BEFORE the lock, and key_share, so a foreign-key insert is not blocked behind it)
        # are reasoning that belongs with the lease, not restated from memory in a second place.
        from meshpipeline.application.execution_fence import StaleWorkerFenced
        from meshpipeline.persistence.lease import _locked_job
        row = (await db.execute(_locked_job(job_id))).scalar_one_or_none()
        if (row is None or getattr(row, "status", None) is not expected
                or bool(getattr(row, "workspace_purged", False))):
            logger.error("re-delivery FENCED at artifact registration - job %s is no longer the "
                         "terminal-failed run with a workspace that this call was guarded against "
                         "(status=%s purged=%s)", job_id,
                         getattr(getattr(row, "status", None), "value", None),
                         getattr(row, "workspace_purged", None))
            raise StaleWorkerFenced("re-delivery artifact registration", str(job_id))
    return _fence


async def _restate_delivery(session_factory, job_repo, job_id: uuid.UUID, *,
                            delivered_types: list, jlog) -> bool:
    """Re-state what the terminal record says about DELIVERY, from the durable rows. Nothing else.

    Returns whether the record was re-written. A record that cannot be read is logged and left
    alone: the mesh is delivered either way, and losing a delivery over its own paperwork is the
    shape of failure this whole entry point exists to undo.

    The record of this run says `missing_outputs: [mesh_bundle, viewer_data]`, `delivered_types: []`
    and `required_ready: false`. Each was true when it was written and all three are now false:
    delivery_manifest.build reads them verbatim into the parcel a customer is shown, and
    evidence_gaps turns the third into "the required mesh deliverables are not all ready" over a
    mesh that is sitting in the store. So they are re-stated here.

    `build_terminal_result` IS NOT USED, although it is the renderer everywhere else, and the reason
    is worth stating because the obvious call is the wrong one. The failure branch it renders
    through (`build_final_result`, its second `return FinalResult(...)`) hardcodes
    `required_ready=False, delivered_types=[]` and `missing_outputs=required_output_classes(engine)`
    for every non-succeeded record, ignoring the arguments it was handed. Re-rendering
    through it would therefore reproduce, verbatim, the three fields this call exists to correct -
    and widening that branch to take a delivery is not a fix either: every genuinely failed run DOES
    have nothing delivered, and the hardcoding is what keeps that true without asking. This run is
    the one shape it was never written for, a failed run holding a complete delivery.

    What is NOT touched, deliberately: the status, the outcome code, the failure category, the
    cause, the detail and `finalized_at`. The run failed, the review really did not conclude, and it
    really ended at 21:56:37Z. This re-states what is on the shelf; it does not re-finalize the run.

    THE CLOSING MESSAGE DOES CHANGE, and that is the point rather than a side effect. This
    paragraph once claimed the reverse on two counts, that the renderer touches `required_ready`
    nowhere but its succeeded branch and that a failed record therefore reads identically either
    side of this write. Neither holds now. `final_result._holds_a_complete_delivery` reads
    `required_ready`, `delivered_types` and `missing_outputs` in the FAILURE half of the
    renderer, and the three fields written below are the only way it can come back true; the
    test that pinned the old wording now pins the new
    (tests/unit/application/test_a_mesh_still_on_disk_is_delivered_again_without_re_running_the_job.py). So this write is
    what turns the message over. Before it the panel said "No downloadable mesh deliverable is
    available" and sent the user back for a five-hour re-run, beside three downloads and a drawn
    mesh; after it the message leads with "Mesh generation completed; the run did not." and says
    the mesh is stored and that re-running fetches nothing new. What does NOT change is anything
    about the RUN - the status, the category and the recorded cause are still rendered, because
    they are all still true.
    """
    from meshpipeline.application.artifact_policy import (
        optional_warnings,
        required_output_classes,
        required_ready,
    )
    from meshpipeline.application.final_result import FinalResult

    async with session_factory() as db:
        record = await job_repo.get_final_result(db, job_id)
    if not isinstance(record, dict) or not record:
        jlog.warning("re-delivery: no terminal record to re-state for job %s - the artifacts are "
                     "delivered and the record stays as it is", job_id)
        return False
    try:
        fr = FinalResult.from_dict(record)
    except Exception as exc:  # noqa: BLE001 - an unreadable record never loses a delivered mesh
        jlog.error("re-delivery: the terminal record for job %s cannot be read (%s) - the "
                   "artifacts are delivered and the record stays as it is", job_id, exc)
        return False

    # The SAME policy the terminal assembly applies (the two lines build_terminal_result opens
    # with), asked with the run's own purpose: an FEA run's required set is not a CFD run's, and
    # `required_ready` is already asked with the purpose there. `missing_outputs` is asked with it
    # too, which the failure branch does not do - a literal list and a policy answer have
    # disagreed in this file before.
    types = sorted({str(t) for t in (delivered_types or [])})
    ready = required_ready(fr.engine, types, fr.purpose)
    restated = dataclasses.replace(
        fr,
        required_ready=ready,
        delivered_types=types,
        optional_warnings=optional_warnings(types) if ready else [],
        missing_outputs=[c for c in required_output_classes(fr.engine, fr.purpose)
                         if c not in types])
    async with session_factory() as db:
        await job_repo.set_final_result(db, job_id, restated.to_dict())
        await db.commit()
    jlog.info("re-delivery: terminal record re-stated for job %s - delivered_types=%s "
              "required_ready=%s missing_outputs=%s (status untouched)",
              job_id, types, ready, restated.missing_outputs)
    return True


__all__ = ["Redelivery", "redeliver_job", "NO_SUCH_JOB", "NOT_TERMINAL_FAILED", "WORKSPACE_PURGED",
           "NO_WORKSPACE", "FOREIGN_WORKSPACE", "NO_TERMINAL_RECORD", "ALREADY_DELIVERED",
           "DELIVERY_FAILED"]
