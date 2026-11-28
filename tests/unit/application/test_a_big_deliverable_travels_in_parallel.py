# Responsibility: a deliverable too big to move in one stream moves in parallel parts.
# Boundaries: `gcs_exchange._upload` and `cloud_run_client._fetch`.
# Collaborates with: google.cloud.storage.transfer_manager, the SDK's own concurrent helpers.
"""Job f55ef23e spent 22.4 of its 139.2 minutes moving one file three times.

Its deliverable is 23,109,660,219 bytes. Measured, from the worker log and the workspace mtimes:

    VM -> GCS      ~842 s     27.4 MB/s    sequential resumable upload, 16 MiB chunks = 1,377
                                           separate HTTP requests, strictly in order
    GCS -> worker   422.61 s  54.7 MB/s    one ranged GET read end to end
    worker -> MinIO  78.84 s  293 MB/s     the local hop, for comparison

Neither of the first two is the network. The local hop runs at 293 MB/s on the same machine, and a
GCE VM writes to same-region GCS far faster than 27 MB/s. The shape of the transfer is the cost: a
resumable upload pays a round trip per chunk and a single stream does not fill the link.

Both sides now use the SDK's own concurrent helpers above a size floor, and fall back to the old
path on any failure - the mesh is the deliverable and is never lost over the way its bytes travel.
The floor exists because multipart has a per-upload cost and leaves orphaned parts if it dies,
which is not worth paying for the few-hundred-MB archives this chain also moves.
"""
from __future__ import annotations

import pathlib

import pytest

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gcs_exchange as G

#: The real deliverable's size, which is what the floor has to admit.
DELIVERED_BYTES = 23_109_660_219
#: And the size of an archive this chain also moves, which it must not.
SMALL_ARCHIVE_BYTES = 696 * 1024 * 1024


# ------------------------------------------------------------------ the numbers

def test_the_floor_admits_the_real_deliverable_and_not_a_small_archive():
    assert DELIVERED_BYTES >= G.PARALLEL_UPLOAD_FLOOR_BYTES
    assert SMALL_ARCHIVE_BYTES < G.PARALLEL_UPLOAD_FLOOR_BYTES
    assert DELIVERED_BYTES >= C._PARALLEL_DOWNLOAD_FLOOR_BYTES
    assert SMALL_ARCHIVE_BYTES < C._PARALLEL_DOWNLOAD_FLOOR_BYTES


def test_the_part_count_is_far_inside_the_providers_limit():
    """GCS caps a multipart upload at 10,000 parts. 23.1 GB in 160 MiB parts is 145."""
    parts = -(-DELIVERED_BYTES // G.PARALLEL_UPLOAD_PART_BYTES)
    assert parts == 138, parts
    assert parts < 10_000
    # and it is an order of magnitude fewer requests than the sequential path made
    sequential = -(-DELIVERED_BYTES // G.RESULT_UPLOAD_CHUNK_BYTES)
    assert sequential == 1378, sequential
    assert parts * 9 < sequential


def test_the_sequential_chunk_size_is_unchanged():
    """The fallback path is the old path, to the byte: a size change there would alter the
    behaviour of every archive below the floor, which this edit is not about."""
    assert G.RESULT_UPLOAD_CHUNK_BYTES == 16 * 1024 * 1024
    assert G.RESULT_UPLOAD_TIMEOUT_S == 300


# ------------------------------------------------------------------ the upload chooses

class _Blob:
    def __init__(self, key, chunk_size=None):
        self.name = key
        self.chunk_size = chunk_size
        self.content_type = None
        self.sequential = None

    def upload_from_filename(self, path, content_type=None, timeout=None):
        self.sequential = {"path": path, "content_type": content_type, "timeout": timeout}


class _Bucket:
    def __init__(self, seen):
        self._seen = seen

    def blob(self, key, chunk_size=None):
        b = _Blob(key, chunk_size)
        self._seen.append(b)
        return b


class _Gcs:
    def __init__(self):
        self.seen: list = []

    def bucket(self, _name):
        return _Bucket(self.seen)


def written(tmp_path: pathlib.Path, size: int) -> pathlib.Path:
    p = tmp_path / "bundle.tar.gz"
    with p.open("wb") as fh:
        fh.truncate(size)        # sparse: the size is the subject, the bytes are not
    return p


def test_a_big_bundle_goes_up_in_parallel(tmp_path, monkeypatch):
    """THE REGRESSION. Before this it was 1,377 sequential requests."""
    calls = []

    class _TM:
        @staticmethod
        def upload_chunks_concurrently(path, blob, chunk_size=None, max_workers=None):
            calls.append({"path": path, "chunk_size": chunk_size, "workers": max_workers,
                          "content_type": blob.content_type})

    monkeypatch.setitem(__import__("sys").modules, "google.cloud.storage.transfer_manager", _TM)
    gcs = _Gcs()
    G._upload(gcs, "b", "k", written(tmp_path, DELIVERED_BYTES))
    assert len(calls) == 1, "the parallel path did not run"
    assert calls[0]["chunk_size"] == G.PARALLEL_UPLOAD_PART_BYTES
    assert calls[0]["workers"] == G.PARALLEL_UPLOAD_WORKERS
    assert calls[0]["content_type"] == "application/gzip", "the type must still be set"
    assert all(b.sequential is None for b in gcs.seen), "it also sent it sequentially"


def test_a_small_archive_still_goes_up_the_old_way(tmp_path):
    gcs = _Gcs()
    G._upload(gcs, "b", "k", written(tmp_path, SMALL_ARCHIVE_BYTES))
    sent = [b for b in gcs.seen if b.sequential]
    assert len(sent) == 1, "the small archive did not take the sequential path"
    assert sent[0].chunk_size == G.RESULT_UPLOAD_CHUNK_BYTES
    assert sent[0].sequential["content_type"] == "application/gzip"
    assert sent[0].sequential["timeout"] == G.RESULT_UPLOAD_TIMEOUT_S


def test_a_parallel_upload_that_fails_still_sends_the_bundle(tmp_path, monkeypatch):
    """THE WHOLE POINT OF THE FALLBACK. A mesh that took two hours is never lost because a
    transfer optimisation did not take."""
    class _TM:
        @staticmethod
        def upload_chunks_concurrently(*_a, **_k):
            raise RuntimeError("multipart refused")

    monkeypatch.setitem(__import__("sys").modules, "google.cloud.storage.transfer_manager", _TM)
    gcs = _Gcs()
    G._upload(gcs, "b", "k", written(tmp_path, DELIVERED_BYTES))
    sent = [b for b in gcs.seen if b.sequential]
    assert len(sent) == 1, "the bundle was not sent at all after the parallel path failed"
    assert sent[0].chunk_size == G.RESULT_UPLOAD_CHUNK_BYTES


def test_a_missing_file_reaches_the_real_call_rather_than_failing_on_the_size(tmp_path):
    """`_upload` sizes the file to decide which path to take, and a path that is not there must
    not turn that decision into the error. It takes the sequential path - `is_file()` is False, so
    the size reads 0, which is under the floor - and the SDK raises there, where the error is
    about the upload and not about a stat.

    The first draft of this asserted `pytest.raises` and passed nothing but my own fake, which
    does not open the file at all. That was a test of the stub.
    """
    gcs = _Gcs()
    G._upload(gcs, "b", "k", tmp_path / "not-there.tar.gz")
    sent = [b for b in gcs.seen if b.sequential]
    assert len(sent) == 1, "the decision did not reach a call at all"
    assert sent[0].chunk_size == G.RESULT_UPLOAD_CHUNK_BYTES
    assert sent[0].sequential["path"].endswith("not-there.tar.gz")


# ------------------------------------------------------------------ the download chooses

class _DlBlob:
    def __init__(self, size, name="k"):
        self.name = name
        self.size = size
        self.plain = None

    def reload(self):
        pass

    def download_to_filename(self, dest):
        self.plain = dest


def test_a_big_bundle_comes_down_in_parallel(monkeypatch):
    calls = []

    class _TM:
        @staticmethod
        def download_chunks_concurrently(blob, dest, chunk_size=None, max_workers=None):
            calls.append({"dest": dest, "chunk_size": chunk_size, "workers": max_workers})

    monkeypatch.setitem(__import__("sys").modules, "google.cloud.storage.transfer_manager", _TM)
    b = _DlBlob(DELIVERED_BYTES)
    C._fetch(b, "/tmp/out.tar.gz")
    assert len(calls) == 1, "the parallel path did not run"
    assert calls[0]["chunk_size"] == C._PARALLEL_DOWNLOAD_PART_BYTES
    assert calls[0]["workers"] == C._PARALLEL_DOWNLOAD_WORKERS
    assert b.plain is None, "it also downloaded it in one stream"


def test_a_small_object_comes_down_in_one_stream():
    b = _DlBlob(SMALL_ARCHIVE_BYTES)
    C._fetch(b, "/tmp/out.tar.gz")
    assert b.plain == "/tmp/out.tar.gz"


def test_a_parallel_download_that_fails_still_fetches_the_bundle(monkeypatch):
    class _TM:
        @staticmethod
        def download_chunks_concurrently(*_a, **_k):
            raise RuntimeError("ranges refused")

    monkeypatch.setitem(__import__("sys").modules, "google.cloud.storage.transfer_manager", _TM)
    b = _DlBlob(DELIVERED_BYTES)
    C._fetch(b, "/tmp/out.tar.gz")
    assert b.plain == "/tmp/out.tar.gz", "nothing was fetched after the parallel path failed"


def test_an_unsizeable_object_comes_down_in_one_stream():
    """`download_chunks_concurrently` needs the size. A blob that will not say takes the path
    that does not need it, rather than guessing."""
    class _Mute(_DlBlob):
        def reload(self):
            raise RuntimeError("no metadata")

    b = _Mute(None)
    C._fetch(b, "/tmp/out.tar.gz")
    assert b.plain == "/tmp/out.tar.gz"


def test_a_blob_reporting_no_size_comes_down_in_one_stream():
    b = _DlBlob(None)
    C._fetch(b, "/tmp/out.tar.gz")
    assert b.plain == "/tmp/out.tar.gz"
