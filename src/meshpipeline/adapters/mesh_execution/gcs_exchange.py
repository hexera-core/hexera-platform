# Responsibility: Move a job workspace between the caller and the remote mesher through object storage.
# Boundaries: transfer only - it is what lets the two sides share no filesystem.
from __future__ import annotations

import io
import json
import logging
import pathlib
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor

import meshpipeline.parallel_gzip as parallel_gzip

logger = logging.getLogger(__name__)


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
                         archive: str | pathlib.Path,
                         skip: frozenset[str] = frozenset()) -> int:
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
            # WHAT TRAVELS ON ITS OWN IS NOT PUT IN HERE TWICE. `skip` holds the customer's
            # bundle, which is already a gzip archive: compressing it again gains nothing and
            # costs a whole pass over it at RESULT_ARCHIVE_LEVEL, plus a second copy on disk.
            if child.name in skip:
                continue
            tf.add(str(child), arcname=child.name)
    return out.stat().st_size


def _bundle_beside(ws: pathlib.Path) -> dict | None:
    """The engine's finished bundle, when there is one, as the record that describes it.

    THE BUNDLE IS ALREADY AN ARCHIVE. `engines/bundled_delivery` builds it on the mesher so the
    cells travel once, at BUNDLE_ARCHIVE_LEVEL, verified, with its sha256 in the record. Putting
    that file inside `output.tar.gz` compresses 23 GB of gzip output a second time - which gains
    nothing by definition - and writes it to disk again on the way. It goes as its own object.

    None for every run without one, which is every engine that declares no `packed_only` and
    every failure path (those call this with an empty workspace), and the archive is then written
    exactly as it always was.
    """
    try:
        from meshpipeline.engines.bundled_delivery import bundle_record
        rec = bundle_record(ws)
    except Exception:                 # noqa: BLE001 - an unreadable record sends the old way
        logger.warning("the bundle record could not be read; the archive carries everything",
                       exc_info=True)
        return None
    if not isinstance(rec, dict):
        return None
    name = str(rec.get("bundle") or "")
    # the record's own rule, restated where the name is about to become a path
    if not name or "/" in name or not (ws / name).is_file():
        return None
    return rec


#: ABOVE THIS, A SEQUENTIAL CHUNKED UPLOAD IS THE WRONG SHAPE. `upload_from_filename` with a
#: `chunk_size` is a RESUMABLE upload: one HTTP request per chunk, strictly in order, each paying
#: its own round trip. MEASURED on job f55ef23e, whose deliverable is 23,109,660,219 bytes: the
#: VM->GCS hop took about 842 s, which is 27.4 MB/s, and 23.1 GB in 16 MiB chunks is 1,378
#: sequential requests. The same bundle came back down at 54.7 MB/s on a single-stream
#: `download_to_filename`. Neither figure is the network: a GCE VM writes to GCS in the same
#: region far faster than that, and the whole 139.2-minute run spent 22.4 minutes moving this one
#: file three times.
#:
#: SO A BUNDLE THIS SIZE GOES UP IN PARALLEL. `transfer_manager.upload_chunks_concurrently` is the
#: SDK's own answer - an XML multipart upload, parts in flight together, assembled server side -
#: and `google-cloud-storage==2.18.2` is pinned in both `requirements/runtime.txt` (the mesh
#: image, which is the side that pays the 842 s) and the worker's environment, so it is available
#: wherever this runs.
#:
#: THE SMALL CASE KEEPS THE OLD PATH, deliberately. Multipart has a per-upload cost - it creates
#: an upload id, sends parts, then asks for a compose - and it leaves orphaned parts behind if it
#: dies mid-flight, where a resumable upload leaves nothing. For an archive of a few hundred MB
#: (the layered lumens of verify-7fix came back at 566-696 MB) the sequential path is already
#: fast enough and strictly simpler, so the threshold buys the parallel machinery only where it
#: has been measured to matter.
PARALLEL_UPLOAD_FLOOR_BYTES = 2 * 1024 * 1024 * 1024
#: Parts of this size, so a 23.1 GB bundle is 145 of them rather than 1,378. GCS caps a multipart
#: upload at 10,000 parts, which this clears by two orders of magnitude at any size the mesher can
#: produce.
PARALLEL_UPLOAD_PART_BYTES = 160 * 1024 * 1024
#: How many parts are in flight. The mesher's VM has the cores; this is deliberately modest
#: because the far side of this call is a network and not the CPU, and because an upload that
#: saturates the NIC while the chain still has work to do helps nothing.
PARALLEL_UPLOAD_WORKERS = 8


def _upload(gcs, bucket: str, key: str, path: pathlib.Path) -> None:
    """The archive up to GCS - in parallel parts once it is big enough to be worth it.

    NON-FATAL IN THE PARALLEL DIRECTION ONLY. A failure inside the multipart path falls back to
    the sequential one rather than failing the run: the mesh is the deliverable and is never lost
    over the way its bytes travel. The reverse is not true - if the sequential path fails there is
    nothing to fall back to and the error is the answer.
    """
    size = path.stat().st_size if path.is_file() else 0
    if size >= PARALLEL_UPLOAD_FLOOR_BYTES:
        try:
            from google.cloud.storage import transfer_manager

            blob = gcs.bucket(bucket).blob(key)
            blob.content_type = "application/gzip"
            transfer_manager.upload_chunks_concurrently(
                str(path), blob,
                chunk_size=PARALLEL_UPLOAD_PART_BYTES,
                max_workers=PARALLEL_UPLOAD_WORKERS)
            logger.info("sent %s (%.1f GiB) in parallel parts of %d MiB", key,
                        size / 2 ** 30, PARALLEL_UPLOAD_PART_BYTES // 2 ** 20)
            return
        except Exception as exc:                 # noqa: BLE001 - the mesh never dies for this
            logger.warning("the parallel upload of %s did not take (%s: %s); "
                           "sending it in sequence instead", key, type(exc).__name__, exc)
    gcs.bucket(bucket).blob(key, chunk_size=RESULT_UPLOAD_CHUNK_BYTES) \
        .upload_from_filename(str(path), content_type="application/gzip",
                              timeout=RESULT_UPLOAD_TIMEOUT_S)


def upload_result(output_uri: str, workspace: str, result: dict) -> None:
    gcs = storage_client()
    out_bucket, out_key = split_gs(output_uri)
    ws = pathlib.Path(workspace)
    beside = _bundle_beside(ws)
    from meshpipeline.artifact_keys import bundle_key_beside, result_key_beside

    # THE BUNDLE GOES WHILE THE ARCHIVE IS STILL BEING WRITTEN. It is 23.1 GB against the
    # archive's ~1 GB, and it is a finished, verified, renamed file the moment `build_bundle`
    # returns - so the whole of the archive's gzip pass and its own upload fit inside the
    # bundle's transfer instead of queueing behind it. Nothing after this point touches those
    # bytes: `narrow` deletes the processor trees and `write_result_archive` is told to skip the
    # bundle by name.
    #
    # ONE THREAD AND NOT A POOL, because there is exactly one object and the join has to be
    # unconditional - see where it is awaited.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bundleup") if beside else None
    sending = None
    bundle_key = ""
    if beside is not None and pool is not None:
        bundle_key = bundle_key_beside(out_key)
        sending = pool.submit(_upload, gcs, out_bucket, bundle_key,
                              ws / str(beside["bundle"]))
    try:
        with tempfile.TemporaryDirectory(prefix="mesh_out_", dir=str(ws.parent)) as tmp:
            archive = pathlib.Path(tmp) / "output.tar.gz"
            write_result_archive(
                ws, archive,
                skip=frozenset({str(beside["bundle"])}) if beside else frozenset())
            _upload(gcs, out_bucket, out_key, archive)
        if sending is not None:
            # BEFORE THE RESULT DOCUMENT, always, and this `result()` is what enforces it. The
            # result is what the home side reads to learn there is a second object, so an upload
            # that died must leave a result that does not promise one - the home side would then
            # find the bundle missing from BOTH places and read a delivered mesh as hollow. A
            # failed upload raises HERE, out of `upload_result`, exactly as it did when this was
            # a straight-line call.
            sending.result()
            result = {**result, "bundle_beside": {**beside, "key": bundle_key}}
            logger.info("the %s bundle (%s bytes) travelled beside the archive as %s",
                        beside.get("bundle"), beside.get("bytes"), bundle_key)
    finally:
        # `wait=False` with nothing to cancel: the one future is either joined above or the
        # archive raised, and a run whose archive failed must not be held by 23 GB nobody will
        # read. The process is about to end either way.
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    res_key = result_key_beside(out_key)
    gcs.bucket(out_bucket).blob(res_key).upload_from_string(
        json.dumps(result), content_type="application/json")
