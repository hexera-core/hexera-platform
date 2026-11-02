# Responsibility: Never check the same bytes twice for the same person - an upload whose digest an earlier
# check of theirs already read is answered from that check (its scout, its FEA measurement), copied into
# the new session.
# Boundaries: keyed on the upload's sha256 and the owner, plus whatever else the reading depends on (the
# confirmed unit, the clean verdict, the measurement's version). It stores small pointers and copies
# objects through the object store; it decides nothing about the geometry and never fails a check - a
# pointer it cannot follow is a miss, and the check runs as it always did.
# Collaborates with: application/geometry_check.py (asks before it measures, remembers after),
# contracts/object_storage.py.
"""THE DIGEST INDEX.

Shared dev, 1 Nov: the same IGES landing gear was checked twice back to back (geometry-check-05a8aeee and
-c3282eaf, the same 279.95 x 689.75 x 380 mm box), ten minutes each. The bytes were identical, so every
reading was too. Now a check that lands records where its result lives, under

    geometry_index/<owner digest>/<upload sha256>.json   {"scout": {<unit key>: session}, "fea": {<key>: session}}

and the next check of the same bytes by the same owner copies it (its skin and pictures re-stored under the
new session) instead of reading the file again. Per owner on purpose: a check is never answered from
somebody else's session. The pointer names a session; when that session's objects are gone (it was
deleted), the pointer is a miss.
"""
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bumped when what a stored scout means changes, so an older record is never reused under a new rule.
SCOUT_REUSE_VERSION = 1
#: Where a landed scout keeps its own record (beside the check, so it lives and goes with the session).
SCOUTED_COPY = "scouted.json"
FEA_COPY = "fea_measure.json"


def _owner(owner_id: str) -> str:
    return hashlib.sha256(str(owner_id).encode()).hexdigest()[:32]


def index_key(owner_id: str, sha256: str) -> str:
    return f"geometry_index/{_owner(owner_id)}/{sha256}.json"


def _check_key(session_id: str, name: str) -> str:
    return f"sessions/{session_id}/geometry_check/{name}"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


def unit_key(interpretation: dict | None) -> str:
    """What a scout's reading depends on besides the bytes: the unit it was told, if any."""
    scale = (interpretation or {}).get("scale_to_metres") if isinstance(interpretation, dict) else None
    return f"v{SCOUT_REUSE_VERSION}-" + ("file" if scale is None else f"{float(scale):.12g}")


def fea_key(sha256: str, interpretation: dict | None, scale_to_m: float, validity) -> str:
    """What an FEA measurement depends on: the bytes, the unit, the clean verdict (its suspects read it)
    and the measurement's own version."""
    from meshpipeline.cad.fea_measure import VERSION

    reasons = (validity or {}).get("reasons") if isinstance(validity, dict) else None
    return f"v{VERSION}-{_digest([sha256, unit_key(interpretation), float(scale_to_m), reasons])}"


def _store():
    from meshpipeline.contracts.object_storage import get_object_store
    return get_object_store()


def _read_json(key: str) -> dict | None:
    from meshpipeline.contracts.object_storage import ObjectNotFound

    try:
        got = json.loads(_store().get_bytes(object_key=key))
    except ObjectNotFound:
        return None
    return got if isinstance(got, dict) else None


def _write_json(key: str, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(value, fh)
        tmp = Path(fh.name)
    try:
        _store().upload_file(local_path=tmp, object_key=key, content_type="application/json")
    finally:
        tmp.unlink(missing_ok=True)


def _point(owner_id: str, sha256: str, kind: str, key: str, session_id: str) -> None:
    ikey = index_key(owner_id, sha256)
    index = _read_json(ikey) or {}
    index.setdefault(kind, {})[key] = session_id
    _write_json(ikey, index)


def _pointed(owner_id: str, sha256: str, kind: str, key: str) -> str | None:
    index = _read_json(index_key(owner_id, sha256)) or {}
    got = (index.get(kind) or {}).get(key)
    return str(got) if got else None


# ------------------------------------------------------------------------------ the scout ----
def remember_scout(session_id: str, owner_id: str, record: dict, interpretation: dict | None) -> None:
    """A landed, full scout (not a preview, not one that failed): keep its record beside it and point the
    digest at it. Never raises."""
    try:
        sha = str((record.get("source") or {}).get("sha256") or "")
        if not sha or not owner_id or record.get("status") not in (None, "scouted") or record.get("preview"):
            return
        _write_json(_check_key(session_id, SCOUTED_COPY), {"unit_key": unit_key(interpretation), "record": record})
        _point(owner_id, sha, "scout", unit_key(interpretation), session_id)
    except Exception as exc:  # noqa: BLE001 - a courtesy for the next upload of the same bytes
        logger.info("geometry reuse: the scout was not indexed (%s)", type(exc).__name__)


def reuse_scout(session_id: str, owner_id: str, source: dict, interpretation: dict | None) -> dict | None:
    """The scout of an earlier check of the same bytes by the same owner, under the same unit, re-stored for
    `session_id` (its skin and pictures copied under the new session's keys) - or None, and the scout runs.
    Never raises."""
    try:
        sha = str((source or {}).get("sha256") or "")
        if not sha or not owner_id:
            return None
        uk = unit_key(interpretation)
        earlier = _pointed(owner_id, sha, "scout", uk)
        if not earlier or earlier == session_id:
            return None
        kept = _read_json(_check_key(earlier, SCOUTED_COPY)) or {}
        record = kept.get("record")
        if kept.get("unit_key") != uk or not isinstance(record, dict) \
                or (record.get("source") or {}).get("sha256") != sha:
            return None
        return _copied(record, earlier, session_id, source)
    except Exception as exc:  # noqa: BLE001 - a miss: the scout runs
        logger.info("geometry reuse: no earlier scout used (%s)", type(exc).__name__)
        return None


def _copied(record: dict, earlier: str, session_id: str, source: dict) -> dict | None:
    """`record` with every object it names copied from `earlier`'s folder to `session_id`'s. None when one
    of them is gone."""
    from meshpipeline.contracts.object_storage import ObjectNotFound

    out = {k: v for k, v in json.loads(json.dumps(record)).items()
           if k not in ("status", "written_at", "seconds", "session_id", "preview", "preview_reason")}
    old, new = f"sessions/{earlier}/geometry_check/", f"sessions/{session_id}/geometry_check/"
    keys: list[str] = []
    if out.get("skin_key"):
        keys.append(out["skin_key"])
    keys += [s["object_key"] for s in out.get("snapshots") or [] if s.get("object_key")]
    if isinstance(out.get("upright_sheet"), dict) and out["upright_sheet"].get("object_key"):
        keys.append(out["upright_sheet"]["object_key"])
    thermal = (out.get("facts") or {}).get("thermal_model")
    if isinstance(thermal, dict) and thermal.get("object_key"):
        keys.append(thermal["object_key"])
    store = _store()
    with tempfile.TemporaryDirectory(prefix="geometry_reuse_") as tmp:
        for k, key in enumerate(keys):
            if not key.startswith(old):
                return None
            local = Path(tmp) / f"o{k}"
            try:
                store.download_file(object_key=key, destination=local)
            except ObjectNotFound:
                return None
            store.upload_file(local_path=local, object_key=new + key[len(old):])
    text = json.dumps(out).replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
    out = json.loads(text)
    # the new upload's own record of where its bytes are (the same digest, another source row)
    out["source"] = dict(source)
    out["reused_from"] = {"session_id": earlier}
    return out


# ------------------------------------------------------------------------ the FEA measurement ----
def remember_fea(owner_id: str, sha256: str, key: str, record: dict, *, session_id: str = "") -> None:
    try:
        if not owner_id or not sha256 or not session_id or not record.get("measured"):
            return
        sid = session_id
        _write_json(_check_key(sid, FEA_COPY), {"key": key, "record": record})
        _point(owner_id, sha256, "fea", key, sid)
    except Exception as exc:  # noqa: BLE001 - a courtesy
        logger.info("geometry reuse: the FEA measurement was not indexed (%s)", type(exc).__name__)


def known_fea(owner_id: str, sha256: str, key: str) -> dict | None:
    try:
        sid = _pointed(owner_id, sha256, "fea", key)
        if not sid:
            return None
        kept = _read_json(_check_key(sid, FEA_COPY)) or {}
        record = kept.get("record")
        return record if kept.get("key") == key and isinstance(record, dict) else None
    except Exception as exc:  # noqa: BLE001 - a miss
        logger.info("geometry reuse: no earlier FEA measurement used (%s)", type(exc).__name__)
        return None


__all__ = ["fea_key", "index_key", "known_fea", "remember_fea", "remember_scout", "reuse_scout", "unit_key"]
