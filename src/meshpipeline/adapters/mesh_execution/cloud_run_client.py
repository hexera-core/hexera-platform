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

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution.exchange_coordinates import (
    coordinates_for,
)
from meshpipeline.contracts.mesh_execution import (
    RC_INFRASTRUCTURE,
    RC_TIMED_OUT,
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
        return run_mesh_remote(workspace, engine=engine, timeout=timeout,
                               operation_key=operation_key)

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


def _poll_gcs_json(bucket, key: str, deadline_s: float) -> dict | None:
    blob = bucket.blob(key)
    start = time.time()
    while time.time() - start < deadline_s:
        if blob.exists():
            return json.loads(blob.download_as_bytes())
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
            "log_tail": f"[CLOUD_RUN_FAILED] {engine}: {detail}"}


RESULT_TIMEOUT_MARKER = "[CLOUD_RUN_TIMEOUT]"


def _timed_out(engine: str, deadline_s: float,
               operation: CloudRunOperationReference | None) -> dict:
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


RESULT_UNCOLLECTED_MARKER = "[CLOUD_RUN_RESULT_UNCOLLECTED]"
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
    operation = None
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
            operation = _trigger_job(
                input_uri=f"gs://{bucket_name}/{in_key}",
                output_uri=f"gs://{bucket_name}/{out_key}",
                engine=engine, timeout=timeout)

        result = _poll_gcs_json(bucket, res_key, deadline_s=timeout + 600)
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
            if operation is not None:
                cancel_execution(operation, engine=engine,
                                 why=f"no result within the {timeout + 600}s deadline")
            return _timed_out(engine, timeout + 600, operation)
        # A recognised mesh result, checked rather than assumed. Anything else - a JSON scalar, a
        # list, a document without the verdict - is an exchange we do not understand, and the one
        # safe reading of that is "not collected", so the objects stay for someone who can.
        if not isinstance(result, dict) or "rc" not in result:
            return _fail(engine, "the result document is not a recognised mesh result")

        out_bytes = bucket.blob(out_key).download_as_bytes()
        # bounded, fail-closed extraction of the returned workspace tar (size/entry/ratio/type
        # limits) - never unbounded extractall, even for our own tar.
        from meshpipeline.sandbox.safe_extract import safe_extract_tar
        safe_extract_tar(fileobj=io.BytesIO(out_bytes), dest=str(ws))
        # Built BEFORE the exchange is released: this is the last thing that can raise, so the
        # objects must still exist while it happens.
        # The reference travels back so the submission authority can record what was accepted.
        collected = {**result,
                     "provider_reference": operation.operation_name if operation else ""}
        logger.info("%s ran on Cloud Run Job - rc=%s timed_out=%s",
                    engine, result.get("rc"), result.get("timed_out"))
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
        if operation is not None:
            cancel_execution(operation, engine=engine, why=str(stop))
        return {"rc": RC_INFRASTRUCTURE, "timed_out": False,
                "log_tail": f"{ABANDONED_MARKER} {engine}: {stop}"}
    except Exception as exc:  # noqa: BLE001 - any failure is a LOUD mesh failure, not a local run
        if isinstance(result, dict) and "rc" in result:
            return _uncollected(engine, result, exc)
        return _fail(engine, f"{type(exc).__name__}: {exc}")
    finally:
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
