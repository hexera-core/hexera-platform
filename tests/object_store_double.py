# Responsibility: Be THE in-memory object store for the unit tier.
# Boundaries: one double, honouring the contract's semantics, so no test invents a second with different behaviour.
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from meshpipeline.contracts.object_storage import (
    ObjectNotFound,
    StoredObject,
    normalize_key,
)


class InMemoryObjectStore:

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._content_types: dict[str, str | None] = {}

    # the ObjectStore protocol

    def upload_file(self, *, local_path: Path, object_key: str,
                    content_type: str | None = None,
                    metadata: Mapping[str, str] | None = None) -> StoredObject:
        key = normalize_key(object_key)
        payload = Path(local_path).read_bytes()
        self._objects[key] = payload
        self._content_types[key] = content_type
        return StoredObject(object_key=key, size_bytes=len(payload), content_type=content_type)

    def download_file(self, *, object_key: str, destination: Path) -> None:
        payload = self.get_bytes(object_key=object_key)
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)

    def get_bytes(self, *, object_key: str) -> bytes:
        key = normalize_key(object_key)
        if key not in self._objects:
            raise ObjectNotFound(f"no such object: {key}")
        return self._objects[key]

    def create_download_url(self, *, object_key: str, expires_in: timedelta) -> str:
        key = normalize_key(object_key)
        if key not in self._objects:
            raise ObjectNotFound(f"no such object: {key}")
        # Shaped like a signed URL and deliberately unroutable: a test that accidentally fetches
        # it fails loudly instead of reaching something real.
        return f"memory://object-store/{key}?expires_in={int(expires_in.total_seconds())}"

    def delete_object(self, *, object_key: str) -> None:
        key = normalize_key(object_key)
        self._objects.pop(key, None)
        self._content_types.pop(key, None)

    def exists(self, *, object_key: str) -> bool:
        return normalize_key(object_key) in self._objects

    # test conveniences

    def keys(self) -> list[str]:
        return sorted(self._objects)

    def put_bytes(self, *, object_key: str, payload: bytes) -> None:
        self._objects[normalize_key(object_key)] = payload
