# Responsibility: Move a job workspace between the caller and the remote mesher through object storage.
# Boundaries: transfer only - it is what lets the two sides share no filesystem.
from __future__ import annotations

import io
import json
import pathlib
import tarfile
import tempfile


def storage_client():
    # The project comes from the setting, never from inference. google.auth can only guess one from
    # a gcloud config or a metadata server, and the worker container has neither - so an ADC file
    # that carries no project leaves storage.Client() with nothing and the transfer fails at
    # dispatch, long after mesh-doctor said the credential was fine.
    from google.cloud import storage

    import meshpipeline.settings.providers as provcfg
    project = (provcfg.GCP_PROJECT_ID or "").strip()
    return storage.Client(project=project) if project else storage.Client()


def split_gs(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// uri: {uri}")
    bucket, _, key = uri[len("gs://"):].partition("/")
    return bucket, key


def download_workspace(input_uri: str) -> str:
    from meshpipeline.sandbox.safe_extract import safe_extract_tar
    ws = tempfile.mkdtemp(prefix="cr_ws_")
    in_bucket, in_key = split_gs(input_uri)
    tar_bytes = storage_client().bucket(in_bucket).blob(in_key).download_as_bytes()
    safe_extract_tar(fileobj=io.BytesIO(tar_bytes), dest=ws)
    return ws


#: The returned archive goes up in chunks, each with its own deadline - the input side's rule
#: (cloud_run_client._UPLOAD_CHUNK_BYTES): sent as one request, the storage SDK's 60-second
#: default is a deadline on the WHOLE archive, and a result of several hundred MB (the layered
#: lumens of verify-7fix came back at 566-696 MB) must not race it. The archive is written to a
#: file beside the workspace, not held in memory twice.
RESULT_UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024
RESULT_UPLOAD_TIMEOUT_S = 300


def upload_progress(output_uri: str, stage: dict) -> None:
    """The stage the mesher is at, beside where its result will land, for the waiting worker to
    read (cloud_run_client._poll_gcs_json). Small, and overwritten as the stage moves on."""
    from meshpipeline.artifact_keys import progress_key_beside
    out_bucket, out_key = split_gs(output_uri)
    storage_client().bucket(out_bucket).blob(progress_key_beside(out_key)).upload_from_string(
        json.dumps(stage), content_type="application/json", timeout=30)


def kept_writer(bucket: str, key: str):
    """A binary writer for one kept file's object (engines/bucket_files.keep): a resumable upload
    in chunks, each with its own deadline, so a file of tens of GB is never one request."""
    blob = storage_client().bucket(bucket).blob(key, chunk_size=RESULT_UPLOAD_CHUNK_BYTES)
    return blob.open("wb", content_type="application/gzip", timeout=RESULT_UPLOAD_TIMEOUT_S,
                     ignore_flush=True)


def kept_reader(bucket: str, key: str):
    """A binary reader for one kept file's object, read in chunks (never held whole)."""
    return storage_client().bucket(bucket).blob(key, chunk_size=RESULT_UPLOAD_CHUNK_BYTES).open(
        "rb", timeout=RESULT_UPLOAD_TIMEOUT_S)


def kept_deleter(bucket: str, key: str) -> None:
    storage_client().bucket(bucket).blob(key).delete(timeout=60)


def keep_big_files(output_uri: str, workspace: str, result: dict) -> None:
    """RUN-7: a big result's delivery-only files go to the bucket on their own and stay there
    (engines/bucket_files); the rest is archived as before. What was sent home is measured again,
    so the worker sizes its collection by what it will really hold."""
    from meshpipeline.engines import bucket_files as BF
    from meshpipeline.engines.dispatch import returned_bytes
    out_bucket, out_key = split_gs(output_uri)
    kept = BF.keep(workspace, bucket=out_bucket, output_key=out_key, writer=kept_writer)
    if kept:
        # what the worker will hold: the archive's files and the fetched ones (unpacked at
        # collection); the kept ones never reach it
        here = returned_bytes(workspace)
        fetched = [k for k in kept if k.fetch]
        result["returned_bytes"] = {**here,
                                    "raw": int(here["raw"]) + int(sum(k.bytes for k in fetched)),
                                    "largest": max([int(here["largest"])] + [k.bytes for k in fetched]),
                                    "fetched": int(sum(k.bytes for k in fetched)),
                                    "kept": int(sum(k.bytes for k in kept if not k.fetch)),
                                    "kept_files": len(kept)}


def upload_result(output_uri: str, workspace: str, result: dict) -> None:
    gcs = storage_client()
    out_bucket, out_key = split_gs(output_uri)
    ws = pathlib.Path(workspace)
    if result.get("rc") is not None:
        # only a run that meshed has files to keep; a refusal's empty workspace has none
        try:
            keep_big_files(output_uri, workspace, result)
        except Exception:  # noqa: BLE001 - keeping is an economy: the files then ride home whole
            import logging
            logging.getLogger(__name__).warning("keeping the big files in the bucket failed; the "
                                                "workspace goes home whole", exc_info=True)
    with tempfile.TemporaryDirectory(prefix="mesh_out_", dir=str(ws.parent)) as tmp:
        archive = pathlib.Path(tmp) / "output.tar.gz"
        with tarfile.open(name=str(archive), mode="w:gz") as tf:
            # Same rule as the input side: children only, so no "." member is written. The local
            # worker extracts this through the identical safe_extract, so a "." root here fails
            # the run just as surely - one step later, after the mesh has already been paid for.
            for child in sorted(ws.iterdir()):
                tf.add(str(child), arcname=child.name)
        gcs.bucket(out_bucket).blob(out_key, chunk_size=RESULT_UPLOAD_CHUNK_BYTES) \
            .upload_from_filename(str(archive), content_type="application/gzip",
                                  timeout=RESULT_UPLOAD_TIMEOUT_S)
    from meshpipeline.artifact_keys import result_key_beside
    res_key = result_key_beside(out_key)
    gcs.bucket(out_bucket).blob(res_key).upload_from_string(
        json.dumps(result), content_type="application/json")
