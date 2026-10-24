# Responsibility: Run a mesh job on Cloud Run and bring its workspace back.
# Owns: job dispatch, the configuration check, the returned Operation reference, cancelling a run nobody will collect, and fail-closed archive extraction.
# Boundaries: it moves work and results; whether a submission may happen at all is decided above it.
# Collaborates with: exchange_coordinates.py to name its objects; native_submission.py alone may submit through it.
from __future__ import annotations

import io
import json
import logging
import re
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution.exchange_coordinates import (
    coordinates_for,
)
from meshpipeline.contracts.mesh_execution import (
    RC_INFRASTRUCTURE,
    RC_TIMED_OUT,
    RUN_NOT_STARTED_TAG,
    RUN_UNCOLLECTED_TAG,
    SubmissionIndeterminate,
    abandonment_reason,
)
from meshpipeline.settings.env import ConfigurationError

logger = logging.getLogger(__name__)


class CloudRunMeshExecutor:
    # The PROVIDER executor. It no longer satisfies `MeshExecutor` directly: its `run` requires
    # the operation identity, because the exchange must land in a namespace a replacement worker
    # can re-derive. `ClaimingMeshExecutor` is what satisfies the outer port, and it is the only
    # thing that may call this one.

    def run(self, workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
        from meshpipeline.application.mesh_execution_shadow import run_with_shadow_profile
        return run_with_shadow_profile(
            workspace, engine,
            lambda: run_mesh_remote(workspace, engine=engine, timeout=timeout,
                                    operation_key=operation_key))

    def collect(self, workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
        # A PREVIOUS worker's submission was durably accepted. Its exchange objects live under
        # coordinates derived from the same operation key, so the result is read from there
        # rather than a second mesh run being started.
        return collect_mesh_remote(workspace, engine=engine, timeout=timeout,
                                   operation_key=operation_key)


def require_cloudrun_config() -> None:
    # WHERE the job runs, WHERE the exchange lives, and WHICH job - the three facts a dispatch
    # cannot proceed without. The credential is deliberately not one of them: _access_token()
    # resolves it through google.auth.default(), which reads a mounted key file under compose and
    # the instance's own identity on a GCE worker, where no key file exists by design. Demanding
    # GOOGLE_APPLICATION_CREDENTIALS here refused every dispatch from the worker fleet - the check
    # failed a configuration the token path would have accepted.
    missing = [name for name, val in (
        ("GCP_PROJECT_ID", provcfg.GCP_PROJECT_ID),
        ("GCP_MESH_BUCKET", provcfg.GCP_MESH_BUCKET),
        ("CLOUDRUN_JOB", provcfg.CLOUDRUN_JOB),
    ) if not val]
    if missing:
        raise ConfigurationError(
            "Cloud Run compute is required for meshing but is not configured - missing: "
            + ", ".join(missing)
        )


def _tar_dir(path: Path) -> bytes:
    # The workspace's CHILDREN, never the directory itself. arcname="." emits a member literally
    # named "." alongside "./case" and friends, and safe_extract._safe_member_path rejects "." as
    # an empty name - so the archive this produced could never be opened by the extractor on the
    # other side. Adding the children yields "case", "case/geom.stl" with no root entry, which is
    # the same content the extractor already expects to see once it has stripped a leading "./".
    #
    # When the workspace DECLARES its submission payload (contracts.mesh_execution's
    # NATIVE_PAYLOAD_FACT), the archive carries exactly the files that declaration resolves to.
    # A retry pass shares its attempt's workspace with the pass before it, whose collected
    # outputs (polyMesh, VTK, feature-edge meshes, logs) live right beside the revised case -
    # tarring every child shipped a built mesh back to the mesher and ballooned the upload past
    # what the transport survives. The same enumerator scopes the claim's payload digest, so
    # what is hashed and what is uploaded stay one set by construction. No declaration means
    # every child, exactly as before.
    from meshpipeline.contracts.mesh_execution import submission_payload_files
    root = Path(path)
    payload = submission_payload_files(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if payload is None:
            for child in sorted(root.iterdir()):
                tf.add(str(child), arcname=child.name)
        else:
            for f in payload:
                tf.add(str(f), arcname=f.relative_to(root).as_posix())
    return buf.getvalue()


#: Upload transport for the submission archive. Without an explicit chunk size the storage SDK
#: sends the whole archive as ONE resumable request whose sockets carry its default 60-second
#: timeout - a deadline on the entire payload, which a legitimately large case on a slow uplink
#: cannot meet ("The write operation timed out", killing every big submission at the same
#: moment). A chunked resumable upload turns that into a deadline PER CHUNK, so the permitted
#: time scales with the payload instead of racing it, and a transient stall costs one chunk,
#: not the upload. 16 MiB is a multiple of the SDK's required 256 KiB granule; 300s/chunk
#: clears a sub-1 Mbps uplink with margin.
_UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024
_UPLOAD_TIMEOUT_S = 300


class _Abandoned(Exception):
    """The run stopped waiting before the remote finished; the message says why."""


def _relay_progress(bucket, progress_key: str, seen: dict) -> None:
    """The stage the remote mesher last said it was at (runtime/mesh_runner's beacon), handed
    to whoever started this mesh (contracts/mesh_progress) when it has moved on. Never raises."""
    try:
        blob = bucket.get_blob(progress_key)
        if blob is None or blob.generation == seen.get("generation"):
            return
        seen["generation"] = blob.generation
        from meshpipeline.contracts.mesh_progress import report
        report(json.loads(blob.download_as_bytes()))
    except Exception as exc:  # noqa: BLE001 - the live line is a courtesy, never the run's
        logger.debug("mesh progress: unreadable (%s)", type(exc).__name__)


def _poll_gcs_json(bucket, key: str, deadline_s: float, progress_key: str | None = None) -> dict | None:
    blob = bucket.blob(key)
    start = time.time()
    seen: dict = {}
    while time.time() - start < deadline_s:
        if blob.exists():
            return json.loads(blob.download_as_bytes())
        if progress_key:
            _relay_progress(bucket, progress_key, seen)
        # Asked between polls, AFTER the result check: a result that has already landed is
        # collected, never thrown away over a cancel that came a moment too late.
        reason = abandonment_reason()
        if reason:
            raise _Abandoned(reason)
        time.sleep(10)
    return None


def _access_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request as GRequest
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GRequest())
    return creds.token


#: The `:run` endpoint returns a long-running OPERATION, not the finished execution. Named for
#: what it is: calling it an execution id would invite a later caller to look up a resource that
#: does not exist yet.
_OPERATION_NAME = re.compile(
    r"\Aprojects/[^/]+/locations/[^/]+/operations/[A-Za-z0-9._~-]+\Z")
_EXECUTION_NAME = re.compile(
    r"\Aprojects/[^/]+/locations/[^/]+/jobs/[^/]+/executions/[A-Za-z0-9._~-]+\Z")


@dataclass(frozen=True)
class CloudRunOperationReference:

    #: `projects/{p}/locations/{l}/operations/{id}` - the only thing a later reconciliation has
    operation_name: str
    #: `projects/{p}/locations/{l}/jobs/{j}/executions/{e}` - what a cancel names, or "" when the
    #: acceptance did not say. RunJob's operation metadata IS the Execution, so the run request's
    #: own answer carries it; reading it there keeps the cancel to `run.executions.cancel`, which
    #: the narrowest invoker role (roles/run.jobsExecutorWithOverrides, apply-iam.sh) holds -
    #: looking the operation up later would need `run.operations.get`, which that role does not.
    #: Kept in memory only: provider_reference stays the operation name.
    execution_name: str = ""

    @property
    def location(self) -> str:
        return self.operation_name.split("/")[3]

    @property
    def execution_id(self) -> str:
        # the short name an operator types: `gcloud run jobs executions describe <this>`
        return self.execution_name.rsplit("/", 1)[-1] if self.execution_name else ""


def _operation_reference(response, *, engine: str) -> CloudRunOperationReference:
    # A 2xx with an unreadable body is NOT success: Cloud Run may well have accepted the run, and
    # saying "not submitted" here is the one answer that could duplicate the work.
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - the body's content never reaches a log or an error
        raise SubmissionIndeterminate(
            f"{engine}: the run request returned {response.status_code} with an unreadable body; "
            "the provider may have accepted it") from exc
    name = str((body or {}).get("name", "") or "").strip()
    if not name:
        raise SubmissionIndeterminate(
            f"{engine}: the run request returned {response.status_code} with no operation name; "
            "the provider may have accepted it")
    if not _OPERATION_NAME.match(name):
        raise SubmissionIndeterminate(
            f"{engine}: the run request returned an operation name that is not a Cloud Run "
            "long-running operation resource; the provider may have accepted it")
    expected_location = str(provcfg.GCP_REGION or "").strip()
    # Optional, never a reason to doubt the acceptance: the operation name alone proves it, so a
    # missing or unrecognised metadata block only means this run cannot be cancelled by name.
    meta = body.get("metadata") if isinstance(body, dict) else None
    execution = str(meta.get("name", "") or "").strip() if isinstance(meta, dict) else ""
    reference = CloudRunOperationReference(
        name, execution if _EXECUTION_NAME.match(execution) else "")
    # The project segment is deliberately NOT compared: the API may canonicalise a project id to
    # its numeric project number, so an equality check there would reject valid acceptances.
    if expected_location and reference.location != expected_location:
        raise SubmissionIndeterminate(
            f"{engine}: the run was accepted in {reference.location}, not the configured region; "
            "the provider may have accepted it")
    return reference


def _trigger_job(*, input_uri: str, output_uri: str, engine: str,
                 timeout: int) -> CloudRunOperationReference:
    import requests as _rq
    name = f"projects/{provcfg.GCP_PROJECT_ID}/locations/{provcfg.GCP_REGION}/jobs/{provcfg.CLOUDRUN_JOB}"
    body = {"overrides": {"containerOverrides": [{"env": [
        {"name": "INPUT_URI", "value": input_uri},
        {"name": "OUTPUT_URI", "value": output_uri},
        {"name": "ENGINE", "value": engine},
        {"name": "TIMEOUT", "value": str(timeout)},
    ]}]}}
    try:
        resp = _rq.post(f"https://run.googleapis.com/v2/{name}:run",
                        json=body, headers={"Authorization": f"Bearer {_access_token()}"},
                        timeout=60)
    except _rq.exceptions.RequestException as exc:
        # The request left this process and no answer came back. Whether Cloud Run accepted it is
        # unknowable from here, and guessing "no" is what duplicates a 25-minute job.
        raise SubmissionIndeterminate(
            f"{engine}: the run request failed in transport ({type(exc).__name__}); the provider "
            "may have accepted it") from None
    resp.raise_for_status()
    return _operation_reference(resp, engine=engine)


def cancel_execution(operation: CloudRunOperationReference, *, engine: str, why: str) -> bool:
    """Stop a Cloud Run execution whose result nobody will collect. True when Cloud Run took the
    cancel.

    BEST EFFORT, and it never raises: by the time this is called the run has already been given
    up (cancelled by its owner, handed back, or past its deadline), and failing that over a cancel
    that did not land would only lose the reason it was given up. A cancel that does not land
    leaves the execution to finish on its own, which is what happened before this existed - so it
    is logged loudly enough to find, never escalated. The provider's message is never logged; the
    status code and the execution's short name are what an operator acts on.

    An acceptance that did not name its execution is not looked up: that needs run.operations.get,
    which the narrowest invoker role does not have, and RunJob always names it."""
    import requests as _rq
    short = operation.execution_id
    if not short:
        logger.warning("Cloud Run execution for %s NOT cancelled (%s): the run's acceptance did "
                       "not name its execution; it finishes on its own", engine, why)
        return False
    try:
        resp = _rq.post(f"https://run.googleapis.com/v2/{operation.execution_name}:cancel",
                        json={}, headers={"Authorization": f"Bearer {_access_token()}"},
                        timeout=30)
    except Exception as exc:  # noqa: BLE001 - see the docstring: best effort, never raised
        logger.warning("Cloud Run execution %s for %s NOT cancelled (%s): %s; it finishes on its "
                       "own", short, engine, why, type(exc).__name__)
        return False
    if 200 <= resp.status_code < 300:
        logger.warning("Cloud Run execution %s for %s cancelled: %s", short, engine, why)
        return True
    logger.warning("Cloud Run execution %s for %s NOT cancelled (%s): HTTP %s; it finishes on its "
                   "own", short, engine, why, resp.status_code)
    return False


def _fail(engine: str, detail: str) -> dict:
    logger.error("Cloud Run mesh FAILED for %s: %s", engine, detail)
    return {"rc": -3, "timed_out": False,
            "log_tail": f"{RUN_NOT_STARTED_TAG} {engine}: {detail}"}


RESULT_TIMEOUT_MARKER = "[CLOUD_RUN_TIMEOUT]"


def _timed_out(engine: str, deadline_s: float, operation) -> dict:
    """The run was DISPATCHED - this worker's submission was accepted, or a previous worker's was
    and this one is collecting it - and no result came back by the deadline. That is a run that
    ran out of time, not one that never started: job 470c3eb9's snappyHexMesh ran the full 50
    minutes twice, each time reported as "INFRASTRUCTURE failure - the mesh run never started",
    and the planner, told to resubmit the plan unchanged, did exactly that.

    TIMED OUT, so every judge reads it before the exit code and asks for a smaller mesh. The
    operation reference travels back so the claim records the submission as accepted: a
    replacement worker then collects this run's late result instead of starting another."""
    minutes = max(0, int(round(float(deadline_s) / 60.0)))
    logger.warning("Cloud Run mesh TIMED OUT for %s: no result within %ss", engine, deadline_s)
    return {"rc": RC_TIMED_OUT, "timed_out": True,
            "provider_reference": operation.operation_name if operation else "",
            "log_tail": (f"{RESULT_TIMEOUT_MARKER} {engine}: the mesh run ran out of time - no "
                         f"result after {minutes} min. It was dispatched and did not finish in "
                         "time; the mesh is too big or too slow for the budget.")}


#: Compressed bytes a returned workspace carries per mesh cell, with margin. Measured: a 7 M-cell
#: snappy passage came back as 557 MB (80 B a cell, job 3cd77f85), and VMTK's Fluent aorta as
#: 560 MB (lab, 2026-10-04) - both past the generic 512 MiB extraction ceiling, so meshes the cell
#: budget allows were built, paid for and lost on the way back.
RESULT_BYTES_PER_CELL = 100
#: ...and per cell the RESULT DOCUMENT says the mesh has, measured on what came back: compressed,
#: the whole workspace; raw, all of it and its largest single file. A 6.7 M-cell cfMesh polyMesh is
#: 1,236 MB raw (its faces file 699 MB) and 218 MB gzipped (cf_lab tw011_v3); the volume export
#: beside it is most of the rest - the verify-7fix layered lumens came back at 566-696 MB. Each
#: with room: these bound a mesh the run has already built and paid for, not a guess.
RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL = 200
RESULT_RAW_BYTES_PER_MEASURED_CELL = 800
#: AN FEA MESH comes home as a quadratic deck AND its Nastran and Ansys files AND gmsh's own mesh
#: (mesh once, export many): about 1 KB raw an element (SC8UU on dev-areen, 1.5 M elements: .inp
#: 290 MB, .bdf 405 MB, .cdb about 500 MB). Past the flow-mesh allowance above, a valid mesh of a
#: few million elements was built and then refused on the way back. When the mesher measured what
#: it sent (dispatch.returned_bytes) that measurement sizes the limits, with margin; else these.
FEA_RESULT_ARCHIVE_BYTES_PER_ELEMENT = 700
FEA_RESULT_RAW_BYTES_PER_ELEMENT = 1600
FEA_RESULT_FILE_BYTES_PER_ELEMENT = 600
#: The margin on a measured size, and the ceiling no result document can lift the limits past.
FEA_RESULT_MARGIN = 1.15
FEA_RESULT_MAX_BYTES = 48 * 1024 ** 3
RESULT_FILE_BYTES_PER_MEASURED_CELL = 400


def result_extraction_limits(result: dict | None = None):
    """The extraction limits for a RETURNED mesh workspace: the generic limits, with the archive
    ceiling sized from the cell budget - CELL_HARD_LIMIT cells at RESULT_BYTES_PER_CELL (800 MB
    at the default 8 M) - and, when the remote's result document says how many cells the mesh
    has, from that count too (the archive, the expanded total and the largest file each at their
    RESULT_*_PER_MEASURED_CELL). Never below the generic limits; entries, depth, ratio and member
    types are unchanged."""
    from dataclasses import replace

    import meshpipeline.settings.policy as polcfg
    from meshpipeline.sandbox.safe_extract import default_limits
    base = default_limits()
    by_budget = int(polcfg.CELL_HARD_LIMIT) * RESULT_BYTES_PER_CELL
    q = (result or {}).get("quality") if isinstance(result, dict) else None
    try:
        cells = max(0, int((q or {}).get("cells") or 0)) if isinstance(q, dict) else 0
    except (TypeError, ValueError):
        cells = 0
    # the count is the remote's word: bounded, so no document can lift the limits without end
    cells = min(cells, 2 * int(polcfg.CELL_HARD_LIMIT))
    archive = max(base.max_archive_bytes, by_budget, cells * RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL)
    raw = max(base.max_total_uncompressed_bytes, cells * RESULT_RAW_BYTES_PER_MEASURED_CELL)
    largest = max(base.max_file_bytes, cells * RESULT_FILE_BYTES_PER_MEASURED_CELL)
    measured = (result or {}).get("returned_bytes") if isinstance(result, dict) else None
    fea = (isinstance(measured, dict) and bool(measured.get("fea"))) or (
        isinstance(q, dict) and (str(q.get("quality_bar") or "") == "fea" or isinstance(q.get("fea"), dict)))
    if fea:
        # AN FEA MESH IS NEVER LOST TO SIZE (Areen: only a broken mesh fails): its own allowance, and
        # the mesher's measurement of what it sent when there is one - bounded, so no document can
        # lift the limits without end
        archive = max(archive, cells * FEA_RESULT_ARCHIVE_BYTES_PER_ELEMENT)
        raw = max(raw, cells * FEA_RESULT_RAW_BYTES_PER_ELEMENT)
        largest = max(largest, cells * FEA_RESULT_FILE_BYTES_PER_ELEMENT)
        if isinstance(measured, dict):
            try:
                m_raw, m_big = int(measured.get("raw") or 0), int(measured.get("largest") or 0)
            except (TypeError, ValueError):
                m_raw = m_big = 0
            raw = max(raw, int(m_raw * FEA_RESULT_MARGIN))
            archive = max(archive, int(m_raw * FEA_RESULT_MARGIN))      # gzip never grows text much
            largest = max(largest, int(m_big * FEA_RESULT_MARGIN))
        archive, raw, largest = (min(v, FEA_RESULT_MAX_BYTES) for v in (archive, raw, largest))
    return replace(base, max_archive_bytes=archive, max_total_uncompressed_bytes=raw,
                   max_file_bytes=largest)


#: one spelling, owned by the contract the executor's account reads it through
RESULT_UNCOLLECTED_MARKER = RUN_UNCOLLECTED_TAG
ABANDONED_MARKER = "[CLOUD_RUN_ABANDONED]"


def _uncollected(engine: str, result: dict, exc: BaseException) -> dict:
    """The remote run FINISHED and wrote its result document, but its output could not be
    brought back (download or bounded extraction failed). Still RC_INFRASTRUCTURE - the plan did
    not fail and the claim semantics are unchanged - but the text says what happened and carries
    the remote's own summary, so a reader can tell "too big to return" from "never dispatched".
    A 7 M-cell blade-row passage (job 3cd77f85) came back as 557 MB, tripped the 512 MiB
    extraction cap, and was reported as 'the mesh run never started' - the one repair that
    applied (a smaller mesh) was the one the wording forbade."""
    q = result.get("quality") or {}
    summary = (f"remote rc={result.get('rc')} cells={q.get('cells')} "
               f"faces={q.get('faces')} timed_out={result.get('timed_out')}")
    detail = f"{type(exc).__name__}: {exc}"
    logger.error("Cloud Run mesh result NOT COLLECTED for %s (%s): %s", engine, summary, detail)
    return {"rc": -3, "timed_out": False,
            "remote_quality": dict(q),
            "log_tail": (f"{RESULT_UNCOLLECTED_MARKER} {engine}: the mesh ran ({summary}) but "
                         f"its result could not be collected - {detail}")}


#: The share of a worker's free disk one result may use (the rest is the job's own and the OS's).
WORKER_DISK_SHARE = 0.85
#: How many times a result's archive the collection writes besides its extracted files: the archive
#: itself while it is extracted, and the delivery bundle built from the extracted files afterwards.
COLLECTION_ARCHIVE_COPIES = 2
TOO_BIG_MARKER = "[CLOUD_RUN_RESULT_TOO_BIG_FOR_WORKER]"


def _is_fea_result(result: dict | None) -> bool:
    measured = (result or {}).get("returned_bytes") if isinstance(result, dict) else None
    q = (result or {}).get("quality") if isinstance(result, dict) else None
    return (isinstance(measured, dict) and bool(measured.get("fea"))) or (
        isinstance(q, dict) and (str(q.get("quality_bar") or "") == "fea" or isinstance(q.get("fea"), dict)))


def collection_need(archive_bytes: int, result: dict | None) -> int:
    """The disk a result's collection needs on the worker: its archive twice (downloaded, then the
    delivery bundle) and its files extracted - as the mesher measured them, else three times the
    archive (text meshes compress about that much)."""
    measured = (result or {}).get("returned_bytes") if isinstance(result, dict) else None
    try:
        raw = int((measured or {}).get("raw") or 0)
    except (TypeError, ValueError):
        raw = 0
    return int(COLLECTION_ARCHIVE_COPIES * archive_bytes + (raw or 3 * archive_bytes))


def worker_room(bucket, out_key: str, result: dict | None, where) -> dict | None:
    """None when this worker has room to collect the result (or it is not an FEA result); else the
    refusal - RC_INFRASTRUCTURE, the exchange objects kept for a worker with room - with the numbers
    the user is told (gigabytes needed and free)."""
    import shutil

    if not _is_fea_result(result):
        return None
    try:
        blob = bucket.blob(out_key)
        blob.reload()
        size = int(blob.size or 0)
        free = int(shutil.disk_usage(str(where)).free)
    except Exception:  # noqa: BLE001 - a size we cannot read is collected as before
        return None
    need = collection_need(size, result)
    if need <= WORKER_DISK_SHARE * free:
        return None
    facts = {"need_bytes": need, "free_bytes": free, "archive_bytes": size}
    logger.error("Cloud Run mesh result too big for this worker: needs %.1f GB, %.1f GB free (%s)",
                 need / 1e9, free / 1e9, out_key)
    return {"rc": RC_INFRASTRUCTURE, "timed_out": False, "too_big_for_worker": facts,
            "remote_quality": dict((result or {}).get("quality") or {}),
            "log_tail": (f"{TOO_BIG_MARKER} the mesh was built, but collecting it needs about "
                         f"{need / 1e9:.1f} GB of disk and this worker has {free / 1e9:.1f} GB free; the "
                         "result is kept for a worker with room")}


#: A VM boots and pulls the mesh image before it meshes: the wait for its result is this much longer.
VM_START_S = 1800


def _stop(operation, *, engine: str, why: str) -> None:
    """Stop a run nobody will collect: cancel the Cloud Run execution, or delete the VM."""
    from meshpipeline.adapters.mesh_execution.gce_vm import VmReference, delete_vm
    if isinstance(operation, VmReference):
        delete_vm(operation, why=why)
    elif operation is not None:
        cancel_execution(operation, engine=engine, why=why)


def run_mesh_remote(workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
    return _exchange(workspace, engine=engine, timeout=timeout, operation_key=operation_key,
                     submit=True)


def collect_mesh_remote(workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
    return _exchange(workspace, engine=engine, timeout=timeout, operation_key=operation_key,
                     submit=False)


def _exchange(workspace, *, engine: str, timeout: int, operation_key: str,
              submit: bool) -> dict:
    require_cloudrun_config()
    # DETERMINISTIC coordinates, derived from the operation identity. A replacement worker
    # re-derives exactly these, which is what lets it read an earlier submission's result
    # instead of starting a second one.
    coords = coordinates_for(operation_key)
    in_key = coords.input_object_key
    out_key = coords.output_object_key
    res_key = coords.result_object_key
    bucket_name = provcfg.GCP_MESH_BUCKET
    bucket = None
    operation: Any = None           # a Cloud Run operation, or a VM (gce_vm.VmReference)
    # THE CLEANUP INVARIANT. The exchange objects are this run's ONLY recovery path: a replacement
    # worker re-derives these exact coordinates and collects the result a previous worker already
    # paid for, instead of submitting a second mesh. So they may be released only once every local
    # step that could still fail has succeeded - the result read, the OUTPUT downloaded, the archive
    # safely extracted, and the returned value built.
    #
    # This used to be `result_is_final(result)`, which asked only whether a result dict had been
    # parsed. That is true the instant the result JSON is read, several fallible steps too early:
    # a corrupt output tar, a tripped safe_extract limit or a local OSError would fail the run AND
    # delete the objects, turning a recoverable 25-minute mesh into an unrecoverable one - and the
    # replacement would then burn the whole (timeout + 600s) deadline before failing too.
    #
    # It is deliberately NOT keyed on the remote's rc. The mesh job uploads the output tar and THEN
    # the result JSON (adapters/mesh_execution/gcs_exchange.upload_result), so a result document
    # implies a collectable workspace, and a failing remote run returns its workspace and logs the
    # same way a successful one does. rc is the mesh's verdict, not evidence about the exchange.
    collection_complete = False
    result = None                     # set once the remote's result document has been read
    # WHERE IT RUNS (contracts/mesh_machine): the Cloud Run mesh job, or a VM sized to fit the plan -
    # decided by the driver before meshing and written in the workspace; none means Cloud Run
    from meshpipeline.contracts import mesh_machine
    machine = mesh_machine.read(workspace)
    on_vm = bool(machine and machine.get("where") == "gce")
    wait_s = timeout + 600 + (VM_START_S if on_vm else 0)
    try:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
        ws = Path(workspace)
        bucket = storage_client().bucket(bucket_name)
        if submit:
            in_blob = bucket.blob(in_key, chunk_size=_UPLOAD_CHUNK_BYTES)
            in_blob.upload_from_string(_tar_dir(ws), content_type="application/gzip",
                                       timeout=_UPLOAD_TIMEOUT_S)
            # A large upload takes minutes; a run given up meanwhile starts nothing.
            reason = abandonment_reason()
            if reason:
                raise _Abandoned(reason)
            if on_vm:
                from meshpipeline.adapters.mesh_execution.gce_vm import trigger_vm
                operation = trigger_vm(
                    input_uri=f"gs://{bucket_name}/{in_key}", output_uri=f"gs://{bucket_name}/{out_key}",
                    engine=engine, timeout=timeout, operation_key=operation_key, decision=machine or {})
            else:
                operation = _trigger_job(
                    input_uri=f"gs://{bucket_name}/{in_key}",
                    output_uri=f"gs://{bucket_name}/{out_key}",
                    engine=engine, timeout=timeout)

        from meshpipeline.artifact_keys import progress_key_beside
        result = _poll_gcs_json(bucket, res_key, deadline_s=wait_s,
                                progress_key=progress_key_beside(out_key))
        if result is None:
            # Reached only once the run is dispatched (a refused or ambiguous submission raised
            # above), so this is a run out of time, never one that did not start.
            #
            # THIS WORKER'S deadline, not the execution's: the job's own task timeout is set at
            # deploy and runs well past it. The execution already had its whole mesh budget plus
            # ten minutes, and the driver answers a timeout with a SMALLER mesh, not by waiting
            # longer - so it is stopped rather than left running unread. The reference still
            # travels back (the claim records the run as accepted, exactly as before); the one
            # price is that a same-identity replay collecting it waits out its own deadline for a
            # late result that will not come, and then reads the same timeout.
            _stop(operation, engine=engine, why=f"no result within the {wait_s}s deadline")
            return _timed_out(engine, wait_s, operation)
        # A recognised mesh result, checked rather than assumed. Anything else - a JSON scalar, a
        # list, a document without the verdict - is an exchange we do not understand, and the one
        # safe reading of that is "not collected", so the objects stay for someone who can.
        if not isinstance(result, dict) or "rc" not in result:
            return _fail(engine, "the result document is not a recognised mesh result")

        # bounded, fail-closed extraction of the returned workspace tar (size/entry/ratio/type
        # limits) - never unbounded extractall, even for our own tar. Streamed to a file beside
        # the workspace, not held in memory: the archive of a mesh at the cell budget is most of
        # a gigabyte, and holding it (twice, as bytes and as a buffer) was the worker's peak.
        # ws holds the staged inputs, the plan and the builder's records, so a refused archive
        # must leave it untouched: safe_extract stages the members beside it and moves them over
        # only after the whole archive extracted, and an uncollected result keeps the workspace.
        import tempfile

        from meshpipeline.sandbox.safe_extract import safe_extract_tar
        # ROOM ON THIS WORKER, measured before a byte is written (an FEA result only: its limits are
        # sized by what was built, up to FEA_RESULT_MAX_BYTES, and a worker's disk is finite). A result
        # that cannot fit is refused in words and KEPT in the exchange - never half-written, never lost
        _room = worker_room(bucket, out_key, result, ws.parent)
        if _room is not None:
            return _room
        with tempfile.TemporaryDirectory(prefix="mesh_result_", dir=str(ws.parent)) as _tmp:
            _archive = Path(_tmp) / "result.tar.gz"
            bucket.blob(out_key).download_to_filename(str(_archive))
            safe_extract_tar(path=_archive, dest=str(ws), limits=result_extraction_limits(result))
        # Built BEFORE the exchange is released: this is the last thing that can raise, so the
        # objects must still exist while it happens.
        # The reference travels back so the submission authority can record what was accepted.
        collected = {**result,
                     "provider_reference": operation.operation_name if operation else "",
                     # WHERE IT RAN, and why (the why record reads it)
                     "ran_on": machine or {"where": "cloud_run", "machine": None,
                                           "memory_gib": mesh_machine.CLOUD_RUN_GIB}}
        logger.info("%s ran on %s - rc=%s timed_out=%s", engine,
                    f"a {machine.get('machine')} VM" if on_vm and machine else "Cloud Run Job",
                    result.get("rc"), result.get("timed_out"))
        collection_complete = True
        return collected
    except (ConfigurationError, SubmissionIndeterminate):
        # An ambiguous submission is NOT a mesh failure: only the caller holding the claim may
        # decide what to record, and it must never read this as "nothing was submitted".
        raise
    except _Abandoned as stop:
        # GIVEN UP, not failed: the job was cancelled or moved, or the worker is going away. Not
        # a mesh verdict, so the infrastructure code - and the execution it was waiting on is
        # stopped, because nobody will collect it. The exchange objects stay, like any run that
        # was not collected; a submission given up before its trigger has no execution to stop.
        logger.warning("Cloud Run mesh for %s given up before it finished: %s", engine, stop)
        _stop(operation, engine=engine, why=str(stop))
        return {"rc": RC_INFRASTRUCTURE, "timed_out": False,
                "log_tail": f"{ABANDONED_MARKER} {engine}: {stop}"}
    except Exception as exc:  # noqa: BLE001 - any failure is a LOUD mesh failure, not a local run
        if isinstance(result, dict) and "rc" in result:
            return _uncollected(engine, result, exc)
        return _fail(engine, f"{type(exc).__name__}: {exc}")
    finally:
        if on_vm and collection_complete:
            # THE VM IS DONE once its result is collected: deleted now, not left to its own self-delete
            # or its lifetime limit (an idle big VM is billed by the hour)
            _stop(operation, engine=engine, why="its result was collected")
        if bucket is not None and collection_complete:
            # EXACTLY the three keys this operation owns, each deleted by name. Never a prefix
            # delete: the coordinates are derived per operation key, and a prefix sweep here could
            # take another job's or another generation's exchange with it.
            for _k in (in_key, out_key, res_key):
                try:
                    bucket.blob(_k).delete()
                except Exception as _exc:  # noqa: BLE001 - best-effort; the run already succeeded
                    # Never downgrade a collected result because tidying failed, and never log the
                    # object path or the provider's message - the failure TYPE is what an operator
                    # acts on. A leftover object is reclaimable; a lost result is not.
                    logger.warning("exchange cleanup failed for %s after a complete collection "
                                   "(%s) - the object remains and can be reclaimed",
                                   engine, type(_exc).__name__)
