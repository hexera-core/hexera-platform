# Responsibility: Verify the unit tier composes an in-memory store and ignores ambient object-store variables.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]

#: The affected tests, named exactly - not a whole file - so this keeps pointing at the behaviour
#: it was written for even as neighbours are added.
_AFFECTED = [
    "tests/unit/api/test_api_upload.py::test_valid_step_file_accepted",
    "tests/unit/api/test_api_upload.py::test_filename_with_special_chars_is_sanitized",
]

#: What a shell looks like after running the integration stack, plus a bucket name that would fail
#: settings validation outright. Both shapes broke the tier before.
_HOSTILE = {
    "MINIO_ENDPOINT": "127.0.0.1:1",          # a port nothing listens on
    "MINIO_ACCESS_KEY": "ambient-access",
    "MINIO_SECRET_KEY": "ambient-secret",
    "MINIO_BUCKET": "z",                       # too short: rejected by settings validation
}


def _run(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env}
    env["PYTHONPATH"] = str(_REPO / "src")
    # PYTEST_ADDOPTS carries this run's own options, and CI sets it to --randomly-seed=<n>. The
    # child below disables pytest-randomly (-p no:randomly), so inheriting that flag hands it an
    # option nothing can parse and the child dies on its command line rather than on the thing
    # under test - reporting a hermeticity failure that never ran.
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *_AFFECTED, "-q", "-p", "no:randomly"],
        cwd=_REPO, capture_output=True, text=True, timeout=300, env=env)


@pytest.mark.parametrize("label,extra", [
    ("clean", {}),                 # nothing added: whatever conftest pins is what runs
    ("hostile", _HOSTILE),         # a shell that has been driving the integration stack
])
def test_the_affected_upload_tests_ignore_ambient_object_store_variables(label, extra):
    proc = _run(extra)

    assert proc.returncode == 0, (
        f"[{label}] ambient object-store variables changed the result:\n"
        f"{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    assert "2 passed" in proc.stdout, f"[{label}] unexpected outcome: {proc.stdout[-2000:]}"


def test_the_unit_tier_composes_an_in_memory_store():
    from tests.object_store_double import InMemoryObjectStore

    from meshpipeline.contracts.object_storage import get_object_store

    assert isinstance(get_object_store(), InMemoryObjectStore)


def test_the_double_honours_the_object_key_contract():
    from tests.object_store_double import InMemoryObjectStore

    from meshpipeline.contracts.object_storage import ObjectNotFound, StorageError

    store = InMemoryObjectStore()
    for illegal in ("/absolute", "..", "a/../b", "back\\slash", ""):
        with pytest.raises(StorageError):
            store.exists(object_key=illegal)

    with pytest.raises(ObjectNotFound):
        store.get_bytes(object_key="sources/absent")

    store.put_bytes(object_key="sources/x", payload=b"bytes")
    assert store.exists(object_key="sources/x")
    assert store.get_bytes(object_key="sources/x") == b"bytes"
    store.delete_object(object_key="sources/x")
    assert not store.exists(object_key="sources/x")
