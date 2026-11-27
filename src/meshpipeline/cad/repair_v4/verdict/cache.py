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
import os
import threading
import time
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
#: .4: clean = valid + panel; layer 3 reports and no longer gates
#: .5 (lane D17): gmsh_prod's `located` faces are fingerprinted without gmsh's bounding-box padding
#: (`mesh_child.box_mm`). Pass/fail, min SICN and element counts are unchanged (checked field by field), but `located`
#: is part of the cached panel result, and a .4 entry would hand the search gmsh_prod's old fingerprints, which
#: matched almost no workspace face (1 of 15 on the measured file).
#: .6 (lane D17, a SECOND and INDEPENDENT cause, not a fix to .5): every member fingerprinted a located face on its
#: OWN grid (`quantum_for` of the mesher's diagonal), while the workspace names faces on ITS grid (`quantum_for` of
#: `BRepBndLib.Add` over the whole shape, a looser box). When the two diagonals straddle a power of two the grids differ
#: 2x and every fingerprint misses, for every member, mm members included (measured: 2 of 12 dev files). `Located` now
#: carries the face's kernel-equivalent box and `sites.find_sites` names it on the workspace's grid. .5 fixed gmsh's
#: padding (gmsh_prod only); .6 fixes the grid (all members). Pass/fail unchanged; `located` gained a field.
#: .7 (lane D19): Netgen's STEP reader keeps only the FIRST root of a file; when that root holds no solid (a loose
#: shell ahead of the bodies) netgen_occ failed "STEP file holds no solids" on a readable part. The member now reads
#: every root on that one failure, so a cached .6 step_unreadable for such a file is wrong. Files Netgen already
#: read are meshed exactly as before.
#: .8 (lane D20, owner ruling): netgen_occ meshes EVERY root of a multi-root STEP, not only on a no-solids read; a
#: cached .7 netgen result for a multi-root file judged its first body only.
#: .9 (lane D28): a gmsh member's `located` also carries the intersection POINTS and invalid SURFACES its 3D back-end
#: prints or logs (never in the exception). Pass/fail unchanged; `located` is in the cached panel result.
VERDICT_CODE_VERSION = "crs-v4.verdict.9"
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


def panel_config_hash(members: Sequence[Any], timeout_s: float, versions: dict[str, str],
                      extra: dict | None = None) -> str:
    """Hash of every member's name and `config` (REQUIRED, JSON-able), the time limit and the mesher
    library versions (`versions`, from `mesher_versions`). Order-independent over members. `extra` (lane C16: the
    full tier's doomed-panel cap) is hashed only when given, so a key without it is byte-identical to before."""
    entries = []
    for member in members:
        if getattr(member, "config", None) is None:  # missing, or None, which would key on nothing
            raise PanelConfigError(f"panel member {getattr(member, 'name', member)!r} has no `config`")
        entries.append(json.dumps([member.name, member.config], sort_keys=True))
    payload: list[Any] = [sorted(entries), float(timeout_s), dict(sorted(versions.items()))]
    if extra:
        payload.append(dict(sorted(extra.items())))
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


class LeaseStore(Protocol):
    """A store that can make a LEASE (D23 single flight): create a key only if it does not exist, and delete it."""

    def create_if_absent(self, key: str, text: str) -> bool: ...
    def delete(self, key: str) -> None: ...


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

    def create_if_absent(self, key: str, text: str) -> bool:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return True

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


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

    def create_if_absent(self, key: str, text: str) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = text
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class GcsStore:
    """Objects under `gs://bucket/prefix` - the eval bucket."""

    def __init__(self, uri: str) -> None:
        from meshpipeline.contracts.gcs_buckets import split_gs

        self._bucket, self._prefix = split_gs(uri)

    def _blob(self, key: str) -> Any:
        from meshpipeline.contracts import gcs_buckets

        return gcs_buckets.bucket(self._bucket).blob(f"{self._prefix.rstrip('/')}/{key}")

    def get(self, key: str) -> str | None:
        # NB: blob.exists() then download_as_text() with no error handling - a 403 (no get/list grant)
        # RAISES here rather than reading as a miss; never exercised live (owner decision, task 8d).
        blob = self._blob(key)
        return blob.download_as_text() if blob.exists() else None

    def put(self, key: str, text: str) -> None:
        self._blob(key).upload_from_string(text, content_type="application/json")

    def create_if_absent(self, key: str, text: str) -> bool:
        """GCS's atomic create: if_generation_match=0 succeeds only when no object exists."""
        from meshpipeline.contracts import gcs_buckets

        try:
            self._blob(key).upload_from_string(text, content_type="application/json", if_generation_match=0)
            return True
        except gcs_buckets.precondition_failed():
            return False

    def delete(self, key: str) -> None:
        from meshpipeline.contracts import gcs_buckets

        try:
            self._blob(key).delete()
        except gcs_buckets.not_found():
            pass


def _default_factory(data: dict) -> Any:
    import importlib

    panel = importlib.import_module("meshpipeline.cad.repair_v4.verdict.panel")
    located = [
        panel.Located(**{**item, "xyz": tuple(item["xyz"]) if item.get("xyz") else None,
                         "box": tuple(item["box"]) if item.get("box") else None})
        for item in data.get("located", [])
    ]
    return panel.MemberResult(**{**data, "located": located})


#: D23 single flight: a claimer's lease lives this long unless renewed, and is renewed this often while it meshes
LEASE_HEARTBEAT_TTL_S = 180.0
LEASE_HEARTBEAT_EVERY_S = 60.0


class VerdictCache:
    """Panel results in a `CacheStore`. `result_factory` rebuilds one member result from its dict."""

    def __init__(self, store: CacheStore, *, result_factory: Callable[[dict], Any] | None = None) -> None:
        self.store = store
        self._factory = result_factory or _default_factory
        # lane B15: what this cache ANSWERED, so a wave can say whether its arms shared one verdict or each paid
        # for its own meshing. A hit is a panel served; an `unstable` is a panel deliberately not kept (a timeout
        # or a crash is not a verdict); an `error` is a store that could not answer, which `assemble` turns into a
        # miss and which would otherwise be indistinguishable from a file that simply never repeated.
        self._counts = {"hits": 0, "misses": 0, "stored": 0, "unstable": 0, "errors": 0, "leased": 0, "waited": 0}
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
    def lease_key(step_sha: str, config_hash: str, version: str = VERDICT_CODE_VERSION) -> str:
        return f"lease/{version}/{step_sha}/{config_hash}.json"

    def single_flight(self, step_sha: str, config_hash: str, compute: Callable[[], list], *, lease_s: float,
                      poll_s: float = 10.0, clock: Callable[[], float] = time.time,
                      sleep: Callable[[float], None] = time.sleep) -> list:
        """The panel for (sha, config), computed by ONE unit of a wave (D23). Arms on different VMs judge the same
        input at the same time; the first to CREATE the lease (an atomic create-if-absent) meshes and stores, the
        others poll for the stored panel. A waiter computes itself when the lease is released with no panel stored
        (the claimer got a timeout or a crash, which is never cached) or has expired (the claimer died). Bounded:
        no wait outlives `lease_s` plus one claimer's expiry, and nobody is ever served a partial panel. A store
        without leases, or one that raises, falls back to computing, as a miss always has."""
        hit = self.get(step_sha, config_hash)
        if hit is not None:
            return hit
        store = self.store
        if not (hasattr(store, "create_if_absent") and hasattr(store, "delete")):
            return compute()
        lease = self.lease_key(step_sha, config_hash)
        give_up = clock() + lease_s
        while True:
            owner = f"{os.uname().nodename}:{os.getpid()}:{threading.get_ident()}"
            try:
                claimed = store.create_if_absent(lease, json.dumps({"owner": owner,
                                                                    "expires_at": clock() + LEASE_HEARTBEAT_TTL_S}))
            except Exception as exc:  # noqa: BLE001 - a store that cannot lease is a miss, as before
                logger.warning("verdict lease unavailable (%s: %s): computing", type(exc).__name__, exc)
                return compute()
            if claimed:
                self._count("leased")
                # HEARTBEAT: the lease lives a few minutes and is renewed while we mesh, so a claimer that is KILLED
                # (its own file wall, a VM going away) frees the input within LEASE_HEARTBEAT_TTL_S, not lease_s
                stop = threading.Event()

                def beat(stop: threading.Event = stop, owner: str = owner) -> None:
                    while not stop.wait(LEASE_HEARTBEAT_EVERY_S):
                        try:
                            store.put(lease, json.dumps({"owner": owner,
                                                         "expires_at": clock() + LEASE_HEARTBEAT_TTL_S}))
                        except Exception:  # noqa: BLE001 - a missed beat only shortens the lease
                            pass

                beater = threading.Thread(target=beat, daemon=True, name="verdict_lease")
                beater.start()
                try:
                    panel = compute()
                    try:
                        self.put(step_sha, config_hash, panel)
                    except Exception as exc:  # noqa: BLE001 - the measurement stands; only its memo is lost
                        logger.warning("verdict cache write failed: %s: %s", type(exc).__name__, exc)
                    return panel
                finally:
                    stop.set()
                    beater.join(5)
                    try:
                        store.delete(lease)
                    except Exception:  # noqa: BLE001 - an orphaned lease expires on its own
                        pass
            # someone else is computing it: wait for their panel, their release, or their expiry
            while True:
                sleep(poll_s)
                hit = self.get(step_sha, config_hash)
                if hit is not None:
                    self._count("waited")
                    return hit
                try:
                    text = store.get(lease)
                except Exception:  # noqa: BLE001
                    text = None
                if text is None:
                    break  # released without a stored panel: try to claim it ourselves
                try:
                    expired = float(json.loads(text).get("expires_at", 0)) < clock()
                except (ValueError, TypeError, AttributeError):
                    expired = True
                if expired:
                    try:
                        store.delete(lease)  # the claimer died; the next create decides who takes over
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if clock() > give_up:
                    logger.warning("verdict lease on %s outlived our own bound: computing", lease)
                    return compute()

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
