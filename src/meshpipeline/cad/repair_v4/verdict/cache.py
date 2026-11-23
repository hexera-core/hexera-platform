# Responsibility: The verdict cache - never mesh the same STEP under the same panel and checker twice.
# Owns: the key, `VERDICT_CODE_VERSION`, the store protocol, a directory store and a GCS store.
# Boundaries: knows nothing about meshing; it stores and returns panel results as JSON.
"""Cache of panel results, keyed by (STEP sha256, panel config hash, verdict code version).

Meshing is the expensive layer, so that is what is cached; layers 1 and 3 are recomputed because
they depend on more than the STEP (the original's unit, the original shape, the declared edits).

VERDICT_CODE_VERSION MUST be bumped by any change that can alter a panel result for the same file:
a member's options, the pass threshold, SICN or error classification, a member added or removed.
The version is in the key AND re-checked on read, and a mismatch is a miss, never a warning: a
stale hit after a checker change would corrupt every number downstream.

Results with a `timeout` or `crashed` status are never stored: they may be the worker's bad
afternoon rather than the file's, and caching one would make it permanent.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

VERDICT_CODE_VERSION = "crs-v4.verdict.1"
CACHEABLE_STATUSES = ("pass", "fail")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def panel_config_hash(members: Sequence[Any], timeout_s: float) -> str:
    """Names, per-member `config` (when a member exposes one) and the time limit, order-independent."""
    entries = sorted(json.dumps([m.name, getattr(m, "config", None)], sort_keys=True, default=repr) for m in members)
    return hashlib.sha256(json.dumps([entries, float(timeout_s)]).encode()).hexdigest()[:16]


class CacheStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, text: str) -> None: ...


class DirStore:
    """A directory of JSON files; also what tests use."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(self, key: str) -> str | None:
        path = self.root / key
        return path.read_text() if path.is_file() else None

    def put(self, key: str, text: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(path)


class GcsStore:
    """Objects under `gs://bucket/prefix` - the eval bucket."""

    def __init__(self, uri: str) -> None:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import split_gs

        self._bucket, self._prefix = split_gs(uri)

    def _blob(self, key: str) -> Any:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client

        return storage_client().bucket(self._bucket).blob(f"{self._prefix.rstrip('/')}/{key}")

    def get(self, key: str) -> str | None:
        blob = self._blob(key)
        return blob.download_as_text() if blob.exists() else None

    def put(self, key: str, text: str) -> None:
        self._blob(key).upload_from_string(text, content_type="application/json")


def _default_factory(data: dict) -> Any:
    import importlib

    panel = importlib.import_module("meshpipeline.cad.repair_v4.verdict.panel")
    located = [
        panel.Located(**{**item, "xyz": tuple(item["xyz"]) if item.get("xyz") else None})
        for item in data.get("located", [])
    ]
    return panel.MemberResult(**{**data, "located": located})


class VerdictCache:
    """Panel results in a `CacheStore`. `result_factory` rebuilds one member result from its dict."""

    def __init__(self, store: CacheStore, *, result_factory: Callable[[dict], Any] | None = None) -> None:
        self.store = store
        self._factory = result_factory or _default_factory

    @staticmethod
    def key(step_sha: str, config_hash: str, version: str = VERDICT_CODE_VERSION) -> str:
        return f"verdict/{version}/{step_sha}/{config_hash}.json"

    def get(self, step_sha: str, config_hash: str, *, version: str = VERDICT_CODE_VERSION) -> list | None:
        text = self.store.get(self.key(step_sha, config_hash, version))
        if text is None:
            return None
        try:
            body = json.loads(text)
            if (body["version"], body["step_sha256"], body["config_hash"]) != (version, step_sha, config_hash):
                return None  # whatever the key says, a record that does not carry ours is a miss
            return [self._factory(item) for item in body["panel"]]
        except (ValueError, KeyError, TypeError):
            return None  # unreadable or foreign entry: re-mesh rather than trust it

    def put(
        self, step_sha: str, config_hash: str, panel: Sequence[Any], *, version: str = VERDICT_CODE_VERSION
    ) -> bool:
        """Store `panel`; False (and nothing stored) if any result is not a definite pass or fail."""
        if any(r.status not in CACHEABLE_STATUSES for r in panel):
            return False
        body = {
            "version": version,
            "step_sha256": step_sha,
            "config_hash": config_hash,
            "panel": [dataclasses.asdict(r) for r in panel],
        }
        self.store.put(self.key(step_sha, config_hash, version), json.dumps(body))
        return True
