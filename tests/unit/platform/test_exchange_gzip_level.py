# Responsibility: Verify both mesh-exchange archives (the payload the worker sends, the workspace the
# mesh job returns) are written at the exchange's gzip level, not tarfile's level-9 default.
from __future__ import annotations

import io
import tarfile

from meshpipeline.contracts.mesh_execution import EXCHANGE_GZIP_LEVEL


class _Blob:
    def __init__(self, sink, key):
        self._sink, self._key = sink, key

    def upload_from_string(self, data, content_type=None):
        self._sink[self._key] = (data, content_type)

    def upload_from_filename(self, path, content_type=None, timeout=None):
        with open(path, "rb") as fh:
            self._sink[self._key] = (fh.read(), content_type)


class _FakeGcsClient:
    def __init__(self, sink):
        self._sink = sink

    def bucket(self, name):
        sink = self._sink

        class _Bucket:
            def blob(self, key, chunk_size=None):
                return _Blob(sink, key)
        return _Bucket()


def _spy(monkeypatch, module):
    seen: list[dict] = []
    real = tarfile.open

    def open_(*a, **k):
        if "w" in str(k.get("mode", a[1] if len(a) > 1 else "")):
            seen.append(dict(k))
        return real(*a, **k)

    monkeypatch.setattr(module.tarfile, "open", open_)
    return seen


def test_the_level_is_fast_and_reads_back_like_any_gzip():
    assert EXCHANGE_GZIP_LEVEL == 6


def test_the_payload_archive_is_written_at_the_exchange_level(monkeypatch, tmp_path):
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "blockMeshDict").write_text("x\n" * 1000)
    seen = _spy(monkeypatch, crc)
    data = crc._tar_dir(tmp_path)
    assert seen and seen[-1].get("compresslevel") == EXCHANGE_GZIP_LEVEL
    assert data[8] == 0          # gzip XFL: 2 would mean level 9, 4 level 1
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        assert "system/blockMeshDict" in tf.getnames()


def test_the_returned_archive_is_written_at_the_exchange_level(monkeypatch, tmp_path):
    from google.cloud import storage

    import meshpipeline.adapters.mesh_execution.gcs_exchange as gx
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "snappyHexMesh.log").write_text("Finished meshing in = 1 s.\n" * 100)
    sink: dict = {}
    monkeypatch.setattr(storage, "Client", lambda *a, **k: _FakeGcsClient(sink))
    seen = _spy(monkeypatch, gx)
    gx.upload_result("gs://b/jobs/j/output.tar.gz", str(ws), {"rc": None})
    assert seen and seen[-1].get("compresslevel") == EXCHANGE_GZIP_LEVEL
    data, _ = sink["jobs/j/output.tar.gz"]
    assert data[8] == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        assert "snappyHexMesh.log" in tf.getnames()
