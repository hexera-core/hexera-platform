"""NOTE-17: PICKING A FACE ON THE MODEL. The geometry check keeps a face map of every upload (cad/face_map, stored as
`faces.npz` beside the check); a click on the settled stage is resolved here to the face under it, named the way the
engines name it (Face_k, Part_k_Face_j, Region_k), with its plain label for the chip and its triangles for the
highlight. On send, the picked faces are verified against the same map (an unknown or changed face is dropped, never
guessed)."""
from __future__ import annotations

import base64
import logging
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

#: The check's facts key that says a face map is kept: {"token", "faces", "triangles", "basis"}.
FACT_KEY = "face_map"
OBJECT_NAME = "faces.npz"
#: Picks allowed on one message or note.
MAX_PICKS = 20
_CACHE_SIZE = 4
_cache: OrderedDict = OrderedDict()
_lock = threading.Lock()


def object_key(session_id: str) -> str:
    from meshpipeline.application.geometry_check import check_object_key
    return check_object_key(str(session_id), OBJECT_NAME)


def keep(session_id: str, fm) -> dict | None:
    """Store a face map for the session's check; what the facts keep (FACT_KEY), or None. Never raises."""
    try:
        from meshpipeline.cad import face_map as FM
        from meshpipeline.contracts.object_storage import get_object_store

        with tempfile.TemporaryDirectory(prefix="face_map_") as tmp:
            path = Path(tmp) / OBJECT_NAME
            path.write_bytes(FM.to_bytes(fm))
            get_object_store().upload_file(local_path=path, object_key=object_key(session_id))
        return {"token": uuid.uuid4().hex, "faces": len(fm.names), "triangles": int(len(fm.tris)),
                "basis": fm.basis}
    except Exception as exc:  # noqa: BLE001 - no face pick, never a failed check
        logger.warning("face pick: the face map was not kept (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def _load(session_id: str, token: str):
    from meshpipeline.cad import face_map as FM
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    key = (str(session_id), token)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        fm = FM.from_bytes(get_object_store().get_bytes(object_key=object_key(session_id)))
    except ObjectNotFound:
        return None
    with _lock:
        _cache[key] = fm
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return fm


def current(session_id: str):
    """(the session's face map, the skin's unit over the map's) or (None, 1.0) when the check kept none."""
    from meshpipeline.application.geometry_check import read_check

    stored = read_check(str(session_id)) or {}
    facts = stored.get("facts") or {}
    kept = facts.get(FACT_KEY)
    if not isinstance(kept, dict) or not kept.get("token"):
        return None, 1.0
    fm = _load(str(session_id), str(kept["token"]))
    if fm is None:
        return None, 1.0
    now = float(facts.get("scale_to_m") or fm.scale_to_m or 1.0)
    return fm, (now / fm.scale_to_m if fm.scale_to_m > 0 else 1.0)


def _b64(arr) -> str:
    return base64.b64encode(arr.tobytes()).decode("ascii")


def face_at(session_id: str, point_m, normal=None) -> dict | None:
    """The face under a click: {id, label, sig, highlight: {points_b64 (float32 x3, metres), tris_b64 (uint32 x3)}},
    or {"id": None} when the click is on no face. None when the session has no face map."""
    from meshpipeline.cad import face_map as FM

    fm, scale = current(session_id)
    if fm is None:
        return None
    k = FM.face_at(fm, point_m, normal, scale=scale)
    if k is None:
        return {"id": None}
    pts, tris = FM.highlight(fm, k, scale=scale)
    return {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k, scale=scale),
            "highlight": {"points_b64": _b64(pts), "tris_b64": _b64(tris), "units": "m"}}


def verify(session_id: str, faces: list) -> tuple[list[dict], list[str]]:
    """The picked faces sent with a message or note, re-read against the session's face map: (kept, dropped labels).
    Kept faces carry the map's own label and signature (the client's words are never trusted for either)."""
    from meshpipeline.cad import face_map as FM

    fm, scale = current(session_id)
    kept: list[dict] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for f in list(faces or [])[:MAX_PICKS]:
        fid = str((f or {}).get("id") or "") if isinstance(f, dict) else str(getattr(f, "id", "") or "")
        said = str((f or {}).get("label") or fid) if isinstance(f, dict) else str(getattr(f, "label", "") or fid)
        if not fid or fid in seen:
            continue
        seen.add(fid)
        if fm is None or fid not in fm.names:
            dropped.append(said)
            continue
        k = fm.names.index(fid)
        kept.append({"id": fid, "label": FM.label(fm, k), "sig": FM.signature(fm, k, scale=scale)})
    return kept, dropped


def refused(dropped: list[str]) -> str:
    """The one plain line a message or note is refused with when a face picked with it is not on the model."""
    names = [str(d) for d in dropped if str(d)] or ["A picked face"]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    if len(names) == 1:
        return f"{shown} is not on the model. Click the face on the model again, then send."
    return f"{shown} are not on the model. Click the faces on the model again, then send."
