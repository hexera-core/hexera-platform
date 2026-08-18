# Responsibility: Run a mesh job on Cloud Run and bring its workspace back.
# Owns: job dispatch, the configuration check, the returned Operation reference, and fail-closed archive extraction.
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
from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate
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
    missing = [name for name, val in (
        ("GCP_PROJECT_ID", provcfg.GCP_PROJECT_ID),
        ("GCP_MESH_BUCKET", provcfg.GCP_MESH_BUCKET),
        ("CLOUDRUN_JOB", provcfg.CLOUDRUN_JOB),
        ("GOOGLE_APPLICATION_CREDENTIALS", provcfg.GOOGLE_APPLICATION_CREDENTIALS),
    ) if not val]
    if missing:
        raise ConfigurationError(
            "Cloud Run compute is required for meshing but is not configured - missing: "
            + ", ".join(missing)
        )


def _tar_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(path), arcname=".")
    return buf.getvalue()


def _poll_gcs_json(bucket, key: str, deadline_s: float) -> dict | None:
    blob = bucket.blob(key)
    start = time.time()
    while time.time() - start < deadline_s:
        if blob.exists():
            return json.loads(blob.download_as_bytes())
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


@dataclass(frozen=True)
class CloudRunOperationReference:

    #: `projects/{p}/locations/{l}/operations/{id}` - the only thing a later reconciliation has
    operation_name: str

    @property
    def location(self) -> str:
        return self.operation_name.split("/")[3]


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
    reference = CloudRunOperationReference(name)
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


def _fail(engine: str, detail: str) -> dict:
    logger.error("Cloud Run mesh FAILED for %s: %s", engine, detail)
    return {"rc": -3, "timed_out": False,
            "log_tail": f"[CLOUD_RUN_FAILED] {engine}: {detail}"}


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
    try:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
        ws = Path(workspace)
        bucket = storage_client().bucket(bucket_name)
        if submit:
            bucket.blob(in_key).upload_from_string(_tar_dir(ws),
                                                   content_type="application/gzip")
            operation = _trigger_job(
                input_uri=f"gs://{bucket_name}/{in_key}",
                output_uri=f"gs://{bucket_name}/{out_key}",
                engine=engine, timeout=timeout)

        result = _poll_gcs_json(bucket, res_key, deadline_s=timeout + 600)
        if result is None:
            return _fail(engine, f"no result within {timeout + 600}s deadline")
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
    except Exception as exc:  # noqa: BLE001 - any failure is a LOUD mesh failure, not a local run
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
