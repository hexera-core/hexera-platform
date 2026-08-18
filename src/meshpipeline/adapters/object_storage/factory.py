# Responsibility: Build the process-wide object store.
# Boundaries: construction only; the composition root binds the result to the contract.
from __future__ import annotations

from meshpipeline.contracts.object_storage import ObjectStore

_instance: ObjectStore | None = None


def build_object_store() -> ObjectStore:
    global _instance
    if _instance is None:
        from meshpipeline.adapters.object_storage.minio import MinioStore
        _instance = MinioStore()
    return _instance


def _reset_for_tests() -> None:
    global _instance
    _instance = None
