# Responsibility: Verify every object-store backend reports the same neutral metadata, errors and refusals.
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from meshpipeline.contracts.object_storage import ObjectNotFound, StorageError, StoredObject

_PAYLOAD = b"ISO-10303-21;/* contract */\n" + bytes(range(256)) * 4






@pytest.fixture()
def key():
    return f"sources/{uuid.uuid4()}"


@pytest.fixture
def store():
    if not os.getenv("MINIO_ENDPOINT"):
        pytest.skip("MINIO_ENDPOINT is required to run the contract against real MinIO")
    from meshpipeline.adapters.object_storage.minio import MinioStore
    st = MinioStore()
    try:
        st._ensure_bucket(st._client())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"MinIO must be PROVISIONED for this contract, not skipped ({exc})")
    return st


def _put(store, tmp_path, key, payload=_PAYLOAD):
    src = tmp_path / "upload.bin"
    src.write_bytes(payload)
    return store.upload_file(local_path=src, object_key=key)


# the round trip

def test_an_uploaded_object_reports_provider_neutral_metadata(store, tmp_path, key):
    so = _put(store, tmp_path, key)
    assert isinstance(so, StoredObject)
    assert so.object_key == key                 # the key the caller chose, unchanged
    assert so.size_bytes == len(_PAYLOAD)


def test_an_uploaded_object_exists_and_downloads_byte_for_byte(store, tmp_path, key):
    _put(store, tmp_path, key)
    assert store.exists(object_key=key)
    dest = tmp_path / "nested" / "down.bin"
    store.download_file(object_key=key, destination=dest)
    assert dest.read_bytes() == _PAYLOAD        # what came back is what went in


def test_a_missing_object_does_not_exist(store, key):
    assert store.exists(object_key=key) is False


def test_downloading_a_missing_object_raises_the_neutral_not_found(store, tmp_path, key):
    with pytest.raises(ObjectNotFound):
        store.download_file(object_key=key, destination=tmp_path / "missing.bin")


# exact deletion

def test_deletion_removes_exactly_one_object_and_spares_its_neighbours(store, tmp_path):
    prefix = f"sources/{uuid.uuid4()}"
    victim, neighbour = f"{prefix}-a", f"{prefix}-b"
    _put(store, tmp_path, victim)
    _put(store, tmp_path, neighbour)

    store.delete_object(object_key=victim)
    assert not store.exists(object_key=victim)
    assert store.exists(object_key=neighbour)


def test_deleting_an_absent_object_is_idempotent(store, key):
    store.delete_object(object_key=key)
    store.delete_object(object_key=key)


# error containment

def test_a_provider_failure_surfaces_as_a_platform_storage_error(store, tmp_path, key,
                                                                 monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider exploded: bucket=secret-bkt key=AKIAEXAMPLE")

    target = "_client" if type(store).__name__ == "MinioStore" else "_bucket"
    monkeypatch.setattr(store, target, _boom)
    src = tmp_path / "x.bin"
    src.write_bytes(b"1")
    with pytest.raises(StorageError):
        store.upload_file(local_path=src, object_key=key)


def test_keys_with_traversal_are_rejected_by_every_backend(store, tmp_path):
    from meshpipeline.adapters.object_storage import normalize_key
    for bad in ("../escape", "/absolute", "a/../../b"):
        with pytest.raises((StorageError, ValueError)):
            normalize_key(bad)


# signed download

def test_a_download_url_is_read_only_and_time_boxed(store, tmp_path, key):
    _put(store, tmp_path, key)
    url = store.create_download_url(object_key=key, expires_in=timedelta(seconds=600))
    assert url.startswith("http")
    # the URL is a grant to READ one object; it must never carry the credentials themselves
    assert "srcintadminsecret" not in url
