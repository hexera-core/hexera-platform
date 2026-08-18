# Responsibility: Verify the mesh exchange is released only after a complete local collection.
# Boundaries: it drives the real collection path and stands in only for google.cloud.storage.

# Nothing production is stubbed here: the archive bytes are real tar.gz, the extraction is the
# shipped safe_extract_tar, the workspace effects are real files on disk, and the cleanup decision
# is the one cloud_run_client makes. Only the GCS client is a double, because reaching a provider
# is exactly what this mechanism exists to avoid.
from __future__ import annotations

import gzip
import io
import json
import tarfile

import pytest

import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for

#: A well-formed operation identity, and a SECOND one whose objects must never be touched.
KEY = "a" * 64
OTHER_KEY = "b" * 64

BUCKET = "mesh-exchange-bkt"
_CONFIG = {"GCP_PROJECT_ID": "proj", "GCP_MESH_BUCKET": BUCKET, "CLOUDRUN_JOB": "job",
           "GOOGLE_APPLICATION_CREDENTIALS": "/secrets/sa.json", "GCP_REGION": "us-central1"}

#: deadline_s inside the client is `timeout + 600`. -600 makes it 0, so the poll loop never
#: iterates and returns immediately. No sleep is introduced anywhere in this suite.
_IMMEDIATE_DEADLINE = -600


class _Fault(Exception):
    pass


class _FakeBlob:
    def __init__(self, bucket: _FakeBucket, key: str) -> None:
        self._b, self.key = bucket, key

    def exists(self) -> bool:
        return self.key in self._b.objects

    def download_as_bytes(self) -> bytes:
        self._b.downloads.append(self.key)
        fault = self._b.download_faults.get(self.key)
        if fault is not None:
            if self._b.transient.get(self.key):
                # a transient fault fires ONCE: the same valid bytes succeed on the retry, which is
                # what makes the replacement collector's success meaningful rather than a new upload
                self._b.download_faults.pop(self.key)
            raise fault
        if self.key not in self._b.objects:
            raise _Fault(f"404 no such object: {self.key}")
        return self._b.objects[self.key]

    def upload_from_string(self, data, content_type=None) -> None:
        self._b.objects[self.key] = data if isinstance(data, bytes) else data.encode()

    def delete(self) -> None:
        self._b.deleted.append(self.key)
        if self._b.delete_fault is not None:
            raise self._b.delete_fault
        self._b.objects.pop(self.key, None)


class _FakeBucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.deleted: list[str] = []
        self.downloads: list[str] = []
        self.download_faults: dict[str, Exception] = {}
        self.transient: dict[str, bool] = {}
        self.delete_fault: Exception | None = None

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _traversal_tar_gz() -> bytes:
    # a genuine escaping member - safe_extract_tar must reject it, and nothing may be deleted
    return _tar_gz({"../escaped.txt": b"outside"})


def _seeded(coords, *, output: bytes, result: dict | bytes | None = None) -> dict[str, bytes]:
    other = coordinates_for(OTHER_KEY)
    objects = {
        coords.input_object_key: _tar_gz({"case.foam": b"in"}),
        coords.output_object_key: output,
        # NEIGHBOURS that must survive: another operation's exchange, and keys that merely share
        # this one's prefix. A prefix delete would take all four.
        other.input_object_key: b"other-in",
        other.output_object_key: b"other-out",
        other.result_object_key: b'{"rc": 0}',
        coords.output_object_key + ".bak": b"backup",
        coords.result_object_key + ".old": b"previous",
    }
    if result is not None:
        objects[coords.result_object_key] = (
            result if isinstance(result, bytes) else json.dumps(result).encode())
    return objects


@pytest.fixture
def bucket(monkeypatch):
    for k, v in _CONFIG.items():
        monkeypatch.setattr(provcfg, k, v)
    holder: dict[str, _FakeBucket] = {}

    class _FakeClient:
        # The project is passed explicitly, never inferred. A worker container has no gcloud
        # config and no metadata server, so a client built without one fails at dispatch rather
        # than at startup - long after the credential check said everything was fine.
        def __init__(self, project=None):
            assert project == _CONFIG["GCP_PROJECT_ID"], \
                f"the exchange client was built with project={project!r}"

        def bucket(self, name):
            assert name == BUCKET, f"the client addressed an unexpected bucket: {name!r}"
            return holder["b"]

    monkeypatch.setattr("google.cloud.storage.Client", _FakeClient)
    # A resubmission is the thing this whole mechanism exists to avoid. Any call is a test failure,
    # in every case below - not only in the recovery one.
    monkeypatch.setattr(crc, "_trigger_job",
                        lambda **kw: pytest.fail("the collector submitted a new provider job"))

    def _install(objects: dict[str, bytes]) -> _FakeBucket:
        holder["b"] = _FakeBucket(objects)
        return holder["b"]

    return _install


def _collect(workspace, *, timeout=60):
    # THE production collection entry point, unmodified.
    return crc.collect_mesh_remote(workspace, engine="cfmesh", timeout=timeout, operation_key=KEY)


def _exchange_keys(coords) -> set[str]:
    return {coords.input_object_key, coords.output_object_key, coords.result_object_key}


# the cleanup decision matrix

def test_a_poll_timeout_preserves_every_exchange_object(bucket, tmp_path):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"})))   # NO result object
    out = _collect(tmp_path, timeout=_IMMEDIATE_DEADLINE)
    assert out["rc"] != 0 and "no result" in out["log_tail"]
    assert b.deleted == []
    assert _exchange_keys(coords) - set(b.objects) == {coords.result_object_key}


def test_an_unreadable_result_object_preserves_every_exchange_object(bucket, tmp_path):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"}), result={"rc": 0}))
    b.download_faults[coords.result_object_key] = _Fault("503 backend error")
    out = _collect(tmp_path)
    assert out["rc"] != 0
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)


@pytest.mark.parametrize("document", [b"not json at all", b'"a bare string"', b"[1,2,3]",
                                      b'{"no_verdict": true}'])
def test_a_malformed_or_unrecognised_result_preserves_every_exchange_object(document, bucket,
                                                                            tmp_path):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"}), result=document))
    out = _collect(tmp_path)
    assert out["rc"] != 0
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)


def test_a_missing_output_object_preserves_every_exchange_object(bucket, tmp_path):
    # remote success, but the collectable output is not there
    coords = coordinates_for(KEY)
    objects = _seeded(coords, output=b"", result={"rc": 0, "timed_out": False})
    objects.pop(coords.output_object_key)
    b = bucket(objects)
    out = _collect(tmp_path)
    assert out["rc"] != 0
    assert b.deleted == []
    assert {coords.input_object_key, coords.result_object_key} <= set(b.objects)


def test_an_output_download_failure_preserves_every_exchange_object(bucket, tmp_path):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"}),
                       result={"rc": 0, "timed_out": False}))
    b.download_faults[coords.output_object_key] = _Fault("connection reset")
    out = _collect(tmp_path)
    assert out["rc"] != 0
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)


@pytest.mark.parametrize("bad", [
    pytest.param(b"\x1f\x8b" + b"\x00" * 40, id="corrupt-gzip"),
    pytest.param(_tar_gz({"mesh.msh": b"m" * 4096})[:80], id="truncated"),
    pytest.param(_traversal_tar_gz(), id="path-traversal"),
])
def test_an_unsafe_or_broken_archive_preserves_every_exchange_object(bad, bucket, tmp_path):
    # Retrying these exact bytes cannot succeed - the point is only that nothing is DESTROYED, so
    # an operator still has the input and the result to diagnose from.
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=bad, result={"rc": 0, "timed_out": False}))
    out = _collect(tmp_path)
    assert out["rc"] != 0
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_local_filesystem_failure_during_extraction_preserves_every_exchange_object(
        bucket, tmp_path, monkeypatch):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"}),
                       result={"rc": 0, "timed_out": False}))
    real_open = io.open

    def _no_space(path, mode="r", *a, **k):
        if "w" in str(mode):
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", _no_space)
    out = _collect(tmp_path)
    monkeypatch.undo()
    assert out["rc"] != 0
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)


def test_a_complete_collection_deletes_exactly_the_owned_exchange_objects(bucket, tmp_path):
    coords = coordinates_for(KEY)
    other = coordinates_for(OTHER_KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"solver"}),
                       result={"rc": 0, "timed_out": False, "log_tail": "ok"}))
    out = _collect(tmp_path)

    assert out["rc"] == 0 and out["provider_reference"] == ""
    assert (tmp_path / "mesh.msh").read_bytes() == b"solver"       # real workspace effect
    # exactly three deletions, one per owned key, each once
    assert sorted(b.deleted) == sorted(_exchange_keys(coords))
    assert len(b.deleted) == 3 and len(set(b.deleted)) == 3
    assert not (_exchange_keys(coords) & set(b.objects))
    # every neighbour survives - no prefix delete happened
    for survivor in (*_exchange_keys(other), coords.output_object_key + ".bak",
                     coords.result_object_key + ".old"):
        assert survivor in b.objects, f"cleanup destroyed an unrelated object: {survivor}"


def test_a_terminal_remote_failure_with_a_collectable_workspace_is_still_collected_and_cleaned(
        bucket, tmp_path):
    # The mesh job uploads the output tar BEFORE the result JSON, so a result document implies a
    # collectable workspace even when the mesh itself failed. rc is the mesh's verdict, not
    # evidence about the exchange - this is the pre-existing contract, held unchanged.
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"log/foamLog": b"FOAM FATAL"}),
                       result={"rc": 1, "timed_out": False, "log_tail": "mesh failed"}))
    out = _collect(tmp_path)
    assert out["rc"] == 1 and out["log_tail"] == "mesh failed"
    assert (tmp_path / "log" / "foamLog").exists()
    assert sorted(b.deleted) == sorted(_exchange_keys(coords))


def test_a_cleanup_failure_does_not_disguise_the_collected_result(bucket, tmp_path, caplog):
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"solver"}),
                       result={"rc": 0, "timed_out": False, "log_tail": "ok"}))
    b.delete_fault = _Fault("permission denied on s3cr3t-bucket/AKIAEXAMPLE")
    out = _collect(tmp_path)
    assert out["rc"] == 0, "a tidy-up failure downgraded a result that was already collected"
    assert (tmp_path / "mesh.msh").exists()
    assert len(b.deleted) == 3, "cleanup stopped at the first failure instead of trying each key"
    logged = " ".join(r.getMessage() for r in caplog.records)
    for leak in ("s3cr3t-bucket", "AKIAEXAMPLE", coords.input_object_key, BUCKET):
        assert leak not in logged, f"the cleanup log leaked {leak!r}"


# the recovery guarantee

def test_a_replacement_collector_recovers_without_a_second_submission(bucket, tmp_path):
    coords = coordinates_for(KEY)
    archive = _tar_gz({"mesh.msh": b"solver", "system/controlDict": b"cd"})
    b = bucket(_seeded(coords, output=archive,
                       result={"rc": 0, "timed_out": False, "log_tail": "ok"}))

    # 1-3. the first collector finds the existing result, then fails locally on the output
    b.download_faults[coords.output_object_key] = _Fault("connection reset by peer")
    b.transient[coords.output_object_key] = True
    first_ws = tmp_path / "worker-1"
    first_ws.mkdir()
    first = _collect(first_ws)
    assert first["rc"] != 0, "the first collector reported success despite failing to collect"

    # 4. every exact exchange object survives
    assert b.deleted == []
    assert _exchange_keys(coords) <= set(b.objects)
    assert b.objects[coords.output_object_key] == archive, "the output bytes were altered"

    # 5-6. a replacement collects the SAME provider result into a FRESH workspace
    second_ws = tmp_path / "worker-2"
    second_ws.mkdir()
    second = _collect(second_ws)
    assert second["rc"] == 0 and second["log_tail"] == "ok"
    assert (second_ws / "mesh.msh").read_bytes() == b"solver"
    assert (second_ws / "system" / "controlDict").exists()

    # 7. only now is the exchange released
    assert sorted(b.deleted) == sorted(_exchange_keys(coords))
    # 8. no resubmission - _trigger_job would have failed the test; assert the shape too
    assert second["provider_reference"] == "", "a collection minted a new provider reference"


def test_the_first_collectors_partial_workspace_is_not_reused_by_the_replacement(bucket, tmp_path):
    # Production owns this cleanup, and owns it harder than "delete what I wrote": safe_extract_tar
    # rmtree()s the whole DESTINATION on refusal, so a refused collection removes the workspace
    # directory itself. Nothing partial can survive for a later collector to mistake for output.
    # That is also exactly why the INPUT object must be preserved - it is what a replacement
    # rebuilds the workspace from, and this batch is what stops it being deleted.
    coords = coordinates_for(KEY)
    good = _tar_gz({"mesh.msh": b"solver"})
    b = bucket(_seeded(coords, output=_tar_gz({"../escaped.txt": b"x"}),
                       result={"rc": 0, "timed_out": False}))
    ws1 = tmp_path / "w1"
    ws1.mkdir()
    (ws1 / "staged.foam").write_bytes(b"pre-existing")
    assert _collect(ws1)["rc"] != 0
    assert not ws1.exists(), "the refused extraction left a workspace a later collector could read"
    assert not (tmp_path / "escaped.txt").exists()
    assert b.deleted == []
    assert coords.input_object_key in b.objects, (
        "the input object - the only thing a replacement can rebuild the workspace from - was lost")

    b.objects[coords.output_object_key] = good            # operator replaces the bad output
    ws2 = tmp_path / "w2"
    ws2.mkdir()
    assert _collect(ws2)["rc"] == 0
    assert (ws2 / "mesh.msh").exists()
    assert sorted(b.deleted) == sorted(_exchange_keys(coords))


def test_the_collector_never_downloads_the_input_object(bucket, tmp_path):
    # The input is kept for a REPLACEMENT's benefit, not read on the collect path; downloading it
    # would be a fallible step with nothing to gain.
    coords = coordinates_for(KEY)
    b = bucket(_seeded(coords, output=_tar_gz({"mesh.msh": b"m"}),
                       result={"rc": 0, "timed_out": False}))
    _collect(tmp_path)
    assert coords.input_object_key not in b.downloads


@pytest.mark.parametrize("bad", [
    pytest.param(b"\x1f\x8b" + b"\x00" * 40, id="corrupt-gzip"),
    pytest.param(_tar_gz({"mesh.msh": b"m" * 4096})[:80], id="truncated"),
    pytest.param(_traversal_tar_gz(), id="path-traversal"),
])
def test_the_unsafe_archive_fixtures_are_genuinely_unsafe(bad, tmp_path):
    # Vacuity guard for the preservation cases above: if a fixture ever became a VALID archive,
    # those tests would still pass while proving nothing about a rejected collection. Asserted
    # against the real extractor, so it also pins that these are the shapes it refuses.
    from meshpipeline.sandbox.safe_extract import WorkspaceExtractionError, safe_extract_tar
    dest = tmp_path / "probe"
    with pytest.raises(WorkspaceExtractionError):
        safe_extract_tar(fileobj=io.BytesIO(bad), dest=str(dest))


def test_a_valid_fixture_extracts_so_the_preservation_cases_are_not_vacuous(tmp_path):
    # The other half: the archive the success cases use really does extract, so "collection
    # succeeded" is a distinguishable outcome rather than the only reachable one.
    from meshpipeline.sandbox.safe_extract import safe_extract_tar
    dest = tmp_path / "ok"
    safe_extract_tar(fileobj=io.BytesIO(_tar_gz({"mesh.msh": b"solver"})), dest=str(dest))
    assert (dest / "mesh.msh").read_bytes() == b"solver"
    assert gzip.decompress(_tar_gz({"mesh.msh": b"solver"}))[:8] != b""
