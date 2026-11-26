# Responsibility: Verify the MinIO adapter's conditional create writes with a precondition and reports "already there".
# Boundaries: the minio client is a double; nothing here reaches a network.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.adapters.object_storage.minio import MinioStore
from meshpipeline.contracts.object_storage import StorageError


def _store(execute):
    store = MinioStore.__new__(MinioStore)
    store._bucket = "b"
    store._client = lambda **k: SimpleNamespace(_execute=execute)           # type: ignore[method-assign]
    store._ensure_bucket = lambda client: None                              # type: ignore[method-assign]
    return store


def test_the_write_carries_both_preconditions_and_reports_success(tmp_path):
    seen = {}

    def execute(method, bucket, key, body=None, headers=None):
        seen.update(method=method, bucket=bucket, key=key, body=body, headers=headers)
    f = tmp_path / "m"
    f.write_bytes(b"v")
    assert _store(execute).create_object_if_absent(local_path=f, object_key="w/shard-0") is True
    assert seen["method"] == "PUT" and seen["key"] == "w/shard-0" and seen["body"] == b"v"
    assert seen["headers"]["If-None-Match"] == "*" and seen["headers"]["x-goog-if-generation-match"] == "0"


@pytest.mark.parametrize("code", ["PreconditionFailed", "ConditionalRequestConflict"])
def test_an_existing_object_is_false_not_an_error(tmp_path, code):
    def execute(*a, **k):
        raise _Err(code)
    f = tmp_path / "m"
    f.write_bytes(b"v")
    assert _store(execute).create_object_if_absent(local_path=f, object_key="k") is False


def test_any_other_failure_is_a_storage_error(tmp_path):
    def execute(*a, **k):
        raise _Err("AccessDenied")
    f = tmp_path / "m"
    f.write_bytes(b"v")
    with pytest.raises(StorageError):
        _store(execute).create_object_if_absent(local_path=f, object_key="k")


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
