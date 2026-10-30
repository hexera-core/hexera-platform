# Responsibility: Move a job workspace between the caller and the remote mesher through object storage.
# Boundaries: transfer only - it is what lets the two sides share no filesystem.
from __future__ import annotations

import io
import json
import pathlib
import tarfile
import tempfile

import meshpipeline.parallel_gzip as parallel_gzip


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
#: How the returned archive is compressed. NOT tarfile's level-9 default, which is what this was
#: and what most of a long return tail is. Measured on the two-scale coupon's own workspace: level
#: 9 wrote its 1,120,883,923-byte workspace as 244,089,415 bytes in 557.8 s where level 6 wrote
#: 244,704,856 in 121.2 s, both on one thread - 4.6 times the time to save 0.25% of the archive.
#: It is worst on the
#: polyMesh, which is most of the bytes: 2.0 MB/s against level 6's 8.3 on a 177 MB `faces` and
#: 0.7 against 16.9 on `owner`, for 0.196 of raw against 0.197 and 0.081 against 0.081.
#: Job 575f76e6 (execution
#: hexera-mesh-semi-vh29x, 2026-10-29) logged its return scope at 13:56 and the worker was still
#: waiting past 14:20 for 689,484,779 bytes; the mesh was never the slow part.
#: The measure that decides what rides home (engines/return_scope.MEASURE_LEVEL) is this same
#: level and still reads HIGH against it, because it compresses every file on its own, carries no
#: dictionary across members and charges each one a full uncompressed tar header.
#: The figures above are one thread each, which is how the level was chosen. The archive is no
#: longer written on one: parallel_gzip spreads it over every core the job has, and the same
#: workspace archived in 7.4 s.
RESULT_ARCHIVE_LEVEL = 6


def upload_progress(output_uri: str, stage: dict) -> None:
    """The stage the mesher is at, beside where its result will land, for the waiting worker to
    read (cloud_run_client._poll_gcs_json). Small, and overwritten as the stage moves on."""
    from meshpipeline.artifact_keys import progress_key_beside
    out_bucket, out_key = split_gs(output_uri)
    storage_client().bucket(out_bucket).blob(progress_key_beside(out_key)).upload_from_string(
        json.dumps(stage), content_type="application/json", timeout=30)


def write_result_archive(workspace: str | pathlib.Path,
                         archive: str | pathlib.Path) -> int:
    """Write the returned archive exactly as the mesher sends it, and say how big it came out.

    Separate from the upload, and named, because the measure that decides what goes INTO it
    (engines/return_scope) has to be held against the real thing: an estimate that reads lower
    than this archive is the one failure that loses a built mesh. Measuring against a tarfile
    default instead of against this has been wrong twice - first its level-9 compresslevel, then
    its single-threaded deflate stream.
    """
    from meshpipeline.engines.determinism import job_cpus
    ws = pathlib.Path(workspace)
    out = pathlib.Path(archive)
    # the cores THE JOB has, never os.cpu_count() - see parallel_gzip.workers
    cores = parallel_gzip.workers(job_cpus())
    with out.open("wb") as fh, \
            parallel_gzip.ParallelGzipWriter(fh, level=RESULT_ARCHIVE_LEVEL,
                                             worker_count=cores) as gz, \
            tarfile.open(fileobj=gz, mode="w|") as tf:
        # Same rule as the input side: children only, so no "." member is written. The local
        # worker extracts this through the identical safe_extract, so a "." root here fails
        # the run just as surely - one step later, after the mesh has already been paid for.
        for child in sorted(ws.iterdir()):
            tf.add(str(child), arcname=child.name)
    return out.stat().st_size


def upload_result(output_uri: str, workspace: str, result: dict) -> None:
    gcs = storage_client()
    out_bucket, out_key = split_gs(output_uri)
    ws = pathlib.Path(workspace)
    with tempfile.TemporaryDirectory(prefix="mesh_out_", dir=str(ws.parent)) as tmp:
        archive = pathlib.Path(tmp) / "output.tar.gz"
        write_result_archive(ws, archive)
        gcs.bucket(out_bucket).blob(out_key, chunk_size=RESULT_UPLOAD_CHUNK_BYTES) \
            .upload_from_filename(str(archive), content_type="application/gzip",
                                  timeout=RESULT_UPLOAD_TIMEOUT_S)
    from meshpipeline.artifact_keys import result_key_beside
    res_key = result_key_beside(out_key)
    gcs.bucket(out_bucket).blob(res_key).upload_from_string(
        json.dumps(result), content_type="application/json")
