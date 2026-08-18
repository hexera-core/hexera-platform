# Responsibility: Declare how the product reads and writes durable objects.
# Owns: the store Protocol, the stored-object value, and key normalisation shared by every backend.
# Boundaries: no bucket names, credentials or lifecycle policy - those belong to the adapters and to deployment.
# Collaborates with: adapters/object_storage/ for the local and hosted implementations.
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    pass


class ObjectNotFound(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    size_bytes: int
    content_type: str | None = None
    checksum: str | None = None       # provider checksum (e.g. md5 hex), for idempotent retries


# An object key is a forward-slash relative path within the bucket. It must never let a
# caller (or a crafted job/user id) escape the bucket namespace or produce a malformed
# object name - so we reject absolute paths, `..`, backslashes, and empty/relative dots.
_BAD_SEGMENT = {"", ".", ".."}


def normalize_key(object_key: str) -> str:
    if not object_key or not isinstance(object_key, str):
        raise StorageError(f"empty object key: {object_key!r}")
    if object_key.startswith("/"):
        raise StorageError(f"object key must be relative, not absolute: {object_key!r}")
    if "\\" in object_key:
        raise StorageError(f"object key must not contain backslashes: {object_key!r}")
    if re.match(r"^[a-zA-Z]:", object_key):          # windows drive
        raise StorageError(f"object key must not be a drive path: {object_key!r}")
    segments = object_key.split("/")
    for seg in segments:
        if seg in _BAD_SEGMENT:
            raise StorageError(f"illegal segment {seg!r} in object key: {object_key!r}")
    return "/".join(segments)


@runtime_checkable
class ObjectStore(Protocol):

    def upload_file(self, *, local_path: Path, object_key: str,
                    content_type: str | None = None,
                    metadata: Mapping[str, str] | None = None) -> StoredObject: ...

    def download_file(self, *, object_key: str, destination: Path) -> None: ...

    #: Read a SMALL object into memory. Used for application payloads (the viewer/quality JSON)
    #: that a request must return synchronously; large artifacts still go through download_file
    #: or a signed URL so nothing unbounded is buffered in a request.
    def get_bytes(self, *, object_key: str) -> bytes: ...

    def create_download_url(self, *, object_key: str, expires_in: timedelta) -> str: ...

    def delete_object(self, *, object_key: str) -> None: ...

    def exists(self, *, object_key: str) -> bool: ...


# runtime-injected accessor
# The concrete store is SELECTED from config by adapters.object_storage.build_object_store and
# INJECTED here by runtime composition. Product/application call get_object_store() (this
# neutral accessor) - they never select a provider through the adapter factory.
_store: ObjectStore | None = None


def set_object_store(store: ObjectStore | None) -> None:
    global _store
    _store = store


def get_object_store() -> ObjectStore:
    if _store is None:
        raise StorageError(
            "no object store configured - runtime composition must call set_object_store()")
    return _store
