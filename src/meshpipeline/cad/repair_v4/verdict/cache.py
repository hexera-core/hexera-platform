# Responsibility: The verdict cache - never mesh the same STEP under the same panel and checker twice.
# Owns: the key, `VERDICT_CODE_VERSION`, the store protocol, a directory store and a GCS store.
# Boundaries: knows nothing about meshing; it stores and returns panel results as JSON.
"""Cache of panel results, keyed by (STEP sha256, panel config hash, verdict code version).

Meshing is the expensive layer, so that is what is cached; layers 1 and 3 are recomputed because
they depend on more than the STEP (the original's unit, the original shape, the declared edits).

VERDICT_CODE_VERSION MUST be bumped by any change that can alter a panel result for the same file:
a member's options, the pass threshold, SICN or error classification, a member added or removed (a member's own `config` and the mesher library versions are keyed
automatically, so those need no bump).
The version is in the key AND re-checked on read, and a mismatch is a miss, never a warning: a
stale hit after a checker change would corrupt every number downstream.

Results with a `timeout` or `crashed` status are never stored: they may be the worker's bad
afternoon rather than the file's, and caching one would make it permanent.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
import weakref
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from meshpipeline.settings.cad_repair import verdict_cache_uri

#: .2: a retry that ended fail or crashed after a timeout is the timeout (panel.classify_after_timeout); a
#: cached .1 "fail" for such a file is what the retry happened to hit, so .1 entries must not be read.
#: .2 (lane C5): members now LOCATE failures they used to leave unlocated (gmsh's printed node triples, a low-quality
#: mesh's worst tets, the face Netgen names). Pass/fail is unchanged, but `located` is part of the cached panel result,
#: and a .1 entry would hand the search the old, unlocated answer.
#: .3: BOTH of the above landed independently, and each ALONE bumped .1 -> .2. Merged, a .2 entry written by
#: either one is unreadable by the other: the timeout-rule code never stored `located`, so a .2 entry from it
#: would hand the search the old unlocated answer, which is the very thing the lane C5 note forbids. The rule at
#: the top of this file is per result-altering change, so two of them merged is one further bump.
VERDICT_CODE_VERSION = "crs-v4.verdict.4"  # .4: clean = valid + panel; layer 3 reports and no longer gates
logger = logging.getLogger(__name__)
CACHEABLE_STATUSES = ("pass", "fail")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PanelConfigError(ValueError):
    """A panel member does not declare its `config`. Fail closed: a member whose options are not in
    the key could change them and be served a stale verdict, with nothing to flag it."""


def panel_config_hash(members: Sequence[Any], timeout_s: float, versions: dict[str, str]) -> str:
    """Hash of every member's name and `config` (REQUIRED, JSON-able), the time limit and the mesher
    library versions (`versions`, from `mesher_versions`). Order-independent over members."""
    entries = []
    for member in members:
        if getattr(member, "config", None) is None:  # missing, or None, which would key on nothing
            raise PanelConfigError(f"panel member {getattr(member, 'name', member)!r} has no `config`")
        entries.append(json.dumps([member.name, member.config], sort_keys=True))
    payload = [sorted(entries), float(timeout_s), dict(sorted(versions.items()))]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


_VERSIONS: weakref.WeakKeyDictionary[Any, dict[str, str]] = weakref.WeakKeyDictionary()


def mesher_versions(pool: Any) -> dict[str, str]:
    """gmsh / netgen / OCP versions as the workers see them (asked once per pool)."""
    cached = _VERSIONS.get(pool)
    if cached is not None:
        return cached
    result = pool.call("mesher_versions", {}, timeout_s=60.0)
    if not result.ok or result.value is None:
        from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

        raise CheckerUnavailable(f"mesher_versions: {result.status}: {result.error}")
    _VERSIONS[pool] = dict(result.value)
    return _VERSIONS[pool]


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


class MemoryStore:
    """An in-process store: what the engine's default cache is when no durable one is configured. Thread-safe
    (the search confirms candidates on a thread pool)."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        with self._lock:
            self._data[key] = text


class GcsStore:
    """Objects under `gs://bucket/prefix` - the eval bucket."""

    def __init__(self, uri: str) -> None:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import split_gs

        self._bucket, self._prefix = split_gs(uri)

    def _blob(self, key: str) -> Any:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client

        return storage_client().bucket(self._bucket).blob(f"{self._prefix.rstrip('/')}/{key}")

    def get(self, key: str) -> str | None:
        # NB: blob.exists() then download_as_text() with no error handling - a 403 (no get/list grant)
        # RAISES here rather than reading as a miss; never exercised live (owner decision, task 8d).
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
        # lane B15: what this cache ANSWERED, so a wave can say whether its arms shared one verdict or each paid
        # for its own meshing. A hit is a panel served; an `unstable` is a panel deliberately not kept (a timeout
        # or a crash is not a verdict); an `error` is a store that could not answer, which `assemble` turns into a
        # miss and which would otherwise be indistinguishable from a file that simply never repeated.
        self._counts = {"hits": 0, "misses": 0, "stored": 0, "unstable": 0, "errors": 0}
        self._counts_lock = threading.Lock()

    def _count(self, what: str) -> None:
        with self._counts_lock:
            self._counts[what] += 1

    def note_error(self) -> None:
        """A store that raised. Called by `assemble`'s wrappers, which own the decision to treat it as a miss."""
        self._count("errors")

    def stats(self) -> dict[str, int]:
        with self._counts_lock:
            return dict(self._counts)

    @staticmethod
    def key(step_sha: str, config_hash: str, version: str = VERDICT_CODE_VERSION) -> str:
        return f"verdict/{version}/{step_sha}/{config_hash}.json"

    def get(self, step_sha: str, config_hash: str, *, version: str = VERDICT_CODE_VERSION) -> list | None:
        key_ = self.key(step_sha, config_hash, version)
        text = self.store.get(key_)
        if text is None:
            self._count("misses")
            return None
        try:
            body = json.loads(text)
            if (body["version"], body["step_sha256"], body["config_hash"]) != (version, step_sha, config_hash):
                logger.warning("verdict cache miss: record under %s carries a different version/sha/config", key_)
                self._count("misses")
                return None  # whatever the key says, a record that does not carry ours is a miss
            panel = [self._factory(item) for item in body["panel"]]
            self._count("hits")
            return panel
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # Re-mesh rather than trust it, but say why: a schema mismatch with the panel's result
            # type would otherwise disable the cache without anyone seeing it.
            logger.warning("verdict cache miss: unreadable entry %s: %s: %s", key_, type(exc).__name__, exc)
            self._count("misses")
            return None

    def put(
        self, step_sha: str, config_hash: str, panel: Sequence[Any], *, version: str = VERDICT_CODE_VERSION
    ) -> bool:
        """Store `panel`; False (and nothing stored) if any result is not a definite pass or fail."""
        if any(r.status not in CACHEABLE_STATUSES for r in panel):
            self._count("unstable")
            return False
        body = {
            "version": version,
            "step_sha256": step_sha,
            "config_hash": config_hash,
            "panel": [dataclasses.asdict(r) for r in panel],
        }
        self.store.put(self.key(step_sha, config_hash, version), json.dumps(body))
        self._count("stored")
        return True


#: where the engine's verdict cache lives when it must outlive the process: `gs://bucket/prefix` or a directory.
#: Unset: one in-memory cache per process, which is enough for the staged healer and the search of ONE file to
#: share their meshing.
CACHE_URI_ENV = "CRS_V4_VERDICT_CACHE_URI"
_DEFAULT: dict[str, VerdictCache] = {}
_DEFAULT_LOCK = threading.Lock()


def default_verdict_cache() -> VerdictCache:
    """The cache the engine's judges use (`search.default_judge`, `stages`), one per (process, `CACHE_URI_ENV`).
    Both judges MUST get the same one: the staged healer's final state is the search's start state, and the second
    judgement of it must not mesh again."""
    uri = verdict_cache_uri()
    with _DEFAULT_LOCK:
        if uri not in _DEFAULT:
            if not uri:  # a process-local cache looks exactly like a working one: say so, once per process
                logger.warning("%s is unset or blank: the verdict cache is in-memory and per-process, so "
                               "verdicts will NOT be shared across files, workers or runs", CACHE_URI_ENV)
            store: CacheStore = (GcsStore(uri) if uri.startswith("gs://") else DirStore(uri)) if uri else MemoryStore()
            _DEFAULT[uri] = VerdictCache(store)
        return _DEFAULT[uri]


def reset_default_verdict_cache() -> None:
    """Forget the process-wide caches (tests; a long-lived worker that wants a cold start)."""
    with _DEFAULT_LOCK:
        _DEFAULT.clear()
