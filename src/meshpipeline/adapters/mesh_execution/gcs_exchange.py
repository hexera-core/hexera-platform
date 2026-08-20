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


def upload_result(output_uri: str, workspace: str, result: dict) -> None:
    gcs = storage_client()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Same rule as the input side: children only, so no "." member is written. The local
        # worker extracts this through the identical safe_extract, so a "." root here fails the
        # run just as surely - one step later, after the mesh has already been paid for.
        for child in sorted(pathlib.Path(workspace).iterdir()):
            tf.add(str(child), arcname=child.name)
    out_bucket, out_key = split_gs(output_uri)
    gcs.bucket(out_bucket).blob(out_key).upload_from_string(
        buf.getvalue(), content_type="application/gzip")
    from meshpipeline.artifact_keys import result_key_beside
    res_key = result_key_beside(out_key)
    gcs.bucket(out_bucket).blob(res_key).upload_from_string(
        json.dumps(result), content_type="application/json")
