# Responsibility: The parts checklist before approval (NOTE-4): the user's words about particular parts ("3-4 mm on the frame rail") tied to the parts of the uploaded model while the user can still see and fix it - the geometry check's parts (table and pictures, measured where the labelled surface exists), a worker step that asks the vision model once per notes, the rows the request card shows, the part the user picks to fix a row, and the corrected reading handed to the run.
# Boundaries: reads and writes the check's own objects only (sessions/<id>/geometry_check/...); the model call runs on the geometry worker through contracts/geometry_check (never on a request), and cad/part_words checks every entry. Nothing here ever fails a check, a brief or a run: what cannot be read applies nothing.
# Collaborates with: application/geometry_check.py (measures the parts, fea_measurements), application/intake_brief.py (the rows on the card), api/v1/geometry.py (the poll and the pick), application/pipeline_run.py (the reading in the run's state), engines/fea_driver.py and engines/snappy/drivers.py (use it), adapters/pipeline_execution/celery.py (the worker task).
"""THE PARTS CHECKLIST, BEFORE APPROVAL (NOTE-5).

1. The geometry check measures an assembly (or a multi-body flow model) and keeps its parts: the facts table
   cad/part_words reads (`parts_seen`) and the pictures (each part its own colour and number), stored beside the
   check (`store_parts`).
2. The card is drawn (`view`, off the event loop): the notes and the table are hashed; a reading of that key is
   shown, else one is queued (`enqueue_reading`) and the card says "Reading your notes…" until it lands.
3. The worker reads it (`run_reading`): cad/part_words.ask on the stored table and pictures; stored with its key.
4. The user may fix a row (`pick`): the parts they pick for one phrase, kept with the key, checked by the same
   rules (cad/part_words.picked).
5. The run takes the reading as the user left it (`for_run`) when its key is still the notes'.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RULE = "NOTE-5"

#: where the parts live in the check's FEA measurement record (facts[cad.fea_measure.FACT_KEY])
PARTS_KEY = "part_words"
#: the stored reading and the user's picks, beside the check
MATCHES = "part_matches.json"
PICKS = "part_picks.json"
#: the pictures, in the check's folder (fixed names, so the privacy sweep knows them)
PICTURE_DIR = "parts"
PICTURE_NAMES = ("parts_iso", "parts_top", "parts_front", "parts_alone_1", "parts_alone_2",
                 "card_a", "card_a_ids", "card_b", "card_b_ids")
#: the console's card views (render/part_pictures.CARD_VIEWS): a neutral shaded picture and its id picture each
CARD_VIEWS = ("card_a", "card_b")
CARD_PICTURES = frozenset(n for v in CARD_VIEWS for n in (v, f"{v}_ids"))
#: a reading queued this long ago and not landed is queued again (the model may take 3 minutes, the queue more)
READING_STALE_S = 600.0
#: the parts listed in the picker at most (the largest first beyond it)
MAX_LISTED = 60
ROUTE_FEA, ROUTE_CFD = "fea", "cfd"


def _key(name: str, session_id: str) -> str:
    from meshpipeline.application.geometry_check import check_object_key
    return check_object_key(session_id, name)


def picture_keys(session_id: str) -> list[str]:
    """Every picture key this step may have stored for the session (the privacy sweep deletes them)."""
    return [_key(f"{PICTURE_DIR}/{n}.png", session_id) for n in PICTURE_NAMES]


def _read(session_id: str, name: str) -> dict | None:
    from meshpipeline.contracts.object_storage import get_object_store
    try:
        got = json.loads(get_object_store().get_bytes(object_key=_key(name, session_id)))
    except Exception:  # noqa: BLE001 - absent or unreadable is "none yet"
        return None
    return got if isinstance(got, dict) else None


def _write(session_id: str, name: str, payload: dict) -> None:
    from meshpipeline.application.geometry_check import _store_json
    _store_json(_key(name, session_id), payload)


# #
# 1. the parts, measured by the geometry check
# #

def store_parts(session_id: str, record: Mapping, surface, *, route: str = ROUTE_FEA) -> dict | None:
    """The parts of a measured model of two or more parts, kept for the checklist: the facts table and the
    pictures, uploaded beside the check. Returns what to keep in the record (PARTS_KEY), or None (one part, or it
    could not be drawn). Never raises."""
    try:
        from meshpipeline.cad import part_words as PW
        table = PW.part_table(PW.check_record(record) if route == ROUTE_FEA else record, surface)
        if len(table) < 2:
            return None
        from meshpipeline.contracts.object_storage import get_object_store
        from meshpipeline.render.part_pictures import render_parts
        store = get_object_store()
        pictures = []
        with tempfile.TemporaryDirectory(prefix="part_pictures_") as tmp:
            views = []
            for path in render_parts(surface, [r["id"] for r in table], Path(tmp), card=True):
                if path.suffix == ".json":
                    # where each visible part's number goes on a card view, and what that view hides
                    views.append({"name": path.stem, **json.loads(path.read_text())})
                    continue
                key = _key(f"{PICTURE_DIR}/{path.name}", session_id)
                store.upload_file(local_path=path, object_key=key)
                pictures.append({"name": path.stem, "object_key": key})
        return {"route": route, "table": table, "pictures": pictures, "views": views}
    except Exception as exc:  # noqa: BLE001 - no checklist, never a failed check
        logger.warning("part matches: the parts were not kept for the checklist (%s: %s)", type(exc).__name__,
                       str(exc)[:200])
        return None


def copy_parts(seen: Mapping | None, session_id: str) -> dict | None:
    """A measurement reused from an earlier check of the same file: its pictures copied to this session's folder
    (the earlier session's may be erased); the table alone when they cannot be. Never raises."""
    if not seen or not seen.get("table"):
        return None
    from meshpipeline.contracts.object_storage import get_object_store
    store = get_object_store()
    pictures = []
    for p in seen.get("pictures") or []:
        key = _key(f"{PICTURE_DIR}/{p.get('name')}.png", session_id)
        if p.get("object_key") == key:
            pictures.append(dict(p))
            continue
        try:
            data = store.get_bytes(object_key=str(p.get("object_key")))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fh.write(data)
                tmp = Path(fh.name)
            try:
                store.upload_file(local_path=tmp, object_key=key)
            finally:
                tmp.unlink(missing_ok=True)
            pictures.append({"name": p.get("name"), "object_key": key})
        except Exception as exc:  # noqa: BLE001 - fewer pictures
            logger.info("part matches: picture %s not copied (%s)", p.get("name"), type(exc).__name__)
    return {**dict(seen), "pictures": pictures}


def parts_seen(check: Mapping | None) -> dict | None:
    """The kept parts of a stored check, or None."""
    from meshpipeline.cad.fea_measure import FACT_KEY
    facts = (check or {}).get("facts") or {}
    for got in ((facts.get(FACT_KEY) or {}).get(PARTS_KEY), facts.get(PARTS_KEY)):
        if isinstance(got, dict) and got.get("table"):
            return got
    return None


# #
# 2. the card
# #

def notes_of(user_notes, request_txt) -> str:
    from meshpipeline.cad import part_words as PW
    return PW.notes_text({"user_notes": list(user_notes or []), "request_txt": request_txt or ""})


def key_of(notes: str, table: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"n": notes, "t": table}, sort_keys=True).encode()).hexdigest()[:20]


def view(session: Any, *, owner_id: str = "") -> dict | None:
    """What the request card shows about the notes and the parts (blocking: call it off the event loop):
    None (no notes, no parts, or the notes name no part), {"status": "waiting"} (the check is still measuring),
    {"status": "reading"} (queued here when the notes or the parts changed), or {"status": "done", ...} with
    the rows. Never raises."""
    try:
        return _view(session, owner_id=owner_id)
    except Exception as exc:  # noqa: BLE001 - the card is drawn without the rows
        logger.warning("part matches: the checklist was not drawn (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def _view(session: Any, *, owner_id: str = "") -> dict | None:
    from meshpipeline.application.geometry_check import read_check
    sid = str(getattr(session, "id", "") or "")
    notes = notes_of(getattr(session, "user_notes", None), getattr(session, "request_txt", None))
    if not sid or not notes.strip():
        return None
    check = read_check(sid)
    seen = parts_seen(check)
    if seen is None:
        status = str((check or {}).get("status") or "")
        return {"status": "waiting"} if status in ("pending", "scouted") else None
    key = key_of(notes, seen["table"])
    stored = _read(sid, MATCHES)
    if stored is None or stored.get("key") != key or (
            stored.get("status") == "reading" and time.time() - float(stored.get("at") or 0) > READING_STALE_S):
        queued = enqueue_reading(sid, key, getattr(session, "user_notes", None),
                                 getattr(session, "request_txt", None),
                                 owner_id=owner_id or str(getattr(session, "owner_id", "") or ""))
        return {"status": "reading"} if queued else None
    if stored.get("status") == "reading":
        return {"status": "reading"}
    reading = with_picks(sid, stored, seen["table"], notes)
    return rows_of(reading, seen, sid)


def with_picks(session_id: str, stored: Mapping, table: list[dict], notes: str) -> dict:
    """The stored reading with the user's picks for the same key applied (checked again)."""
    from meshpipeline.cad import part_words as PW
    reading = dict(stored.get("section") or {})
    picks = _read(session_id, PICKS)
    if reading.get("status") == "used" and picks and picks.get("key") == stored.get("key") and picks.get("picks"):
        reading = PW.picked(reading, picks["picks"], table, notes)
    return reading


def _num(pid: str) -> str:
    return str(pid).rsplit("_", 1)[-1]


def parts_words(ids: list[str]) -> str:
    """"part 9", "parts 2, 3 and 5", "parts 1-4 and 7" (runs of three or more joined)."""
    nums = sorted({int(_num(p)) for p in ids if _num(p).isdigit()})
    if not nums:
        return ""
    runs: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        runs.extend([f"{nums[i]}–{nums[j]}"] if j - i >= 2 else [str(n) for n in nums[i:j + 1]])
        i = j + 1
    if len(nums) == 1:
        return f"part {runs[0]}"
    return "parts " + (", ".join(runs[:-1]) + " and " + runs[-1] if len(runs) > 1 else runs[0])


def _mm(v) -> str:
    x = float(v)
    return f"{x:.3g}"


def size_words(e: Mapping) -> str:
    ask = str(e.get("ask") or "size")
    if ask == "size":
        lo, hi = e.get("min_mm"), e.get("max_mm")
        if lo is None or hi is None:
            return ""
        return f"{_mm(lo)} mm" if abs(float(lo) - float(hi)) < 1e-12 else f"{_mm(lo)}–{_mm(hi)} mm"
    return {"refine": "finer", "coarsen": "coarser", "name": "named"}.get(ask, "")


def _short(quote: str, n: int = 60) -> str:
    q = " ".join(str(quote or "").split())
    return q if len(q) <= n else q[: n - 1].rstrip() + "…"


def rows_of(reading: Mapping, seen: Mapping, session_id: str) -> dict | None:
    """The card's rows from a checked reading: one per phrase that holds ("frame rail → part 9 → 3–4 mm"), one
    line per phrase the model could not place ("Couldn't tell which part is 'X'"); None when the notes name no
    part (or the AI step did not answer: the run reads them again)."""
    from meshpipeline.render.part_pictures import colour_of
    if reading.get("status") != "used":
        return None
    table = list(seen.get("table") or [])
    rows = []
    for e in reading.get("entries") or []:
        ids = [*e.get("parts", []), *e.get("rest_parts", [])]
        rows.append({"phrase": e.get("phrase"), "words": _short(e.get("quote") or ""),
                     "quote": e.get("quote") or "", "parts": ids,
                     "parts_text": (f"the other {len(ids)} part{'s' if len(ids) != 1 else ''}"
                                    if e.get("rest_parts") and not e.get("parts") else parts_words(ids)),
                     "size": size_words(e), "picked": e.get("why") == "picked by you"})
    unsure = [{"phrase": u.get("phrase"), "words": _short(u.get("quote") or ""), "quote": u.get("quote") or "",
               "text": f"Couldn't tell which part is “{_short(u.get('quote') or '', 40)}”", "size": size_words(u)}
              for u in reading.get("unsure") or []]
    if not rows and not unsure:
        return None
    parts = [{"id": r["id"], "n": _num(r["id"]), "name": readable(r.get("name") or ""), "colour": colour_of(i)}
             for i, r in enumerate(table)]
    if len(parts) > MAX_LISTED:
        keep = {r["id"] for r in sorted(table, key=lambda r: -float(r.get("volume_mm3") or 0))[:MAX_LISTED]}
        parts = [p for p in parts if p["id"] in keep]
    # order: the part under each id of the card's id picture (the table's order)
    out: dict[str, Any] = {"status": "done", "rows": rows, "unsure": unsure, "parts": parts,
                           "order": [r["id"] for r in table]}
    # THE CARD'S VIEWS (render/part_pictures CARD_VIEWS): served through the API (GET .../check/parts/<name>.png)
    # so the console can read the id picture under the cursor; the first view always, the second only when the
    # first hides a part
    stored = {p.get("name") for p in seen.get("pictures") or []}
    views = [{k: v[k] for k in ("name", "w", "h", "tags", "hidden") if k in v} for v in seen.get("views") or []
             if v.get("name") in stored and f"{v.get('name')}_ids" in stored]
    if views:
        out["views"] = views if views[0].get("hidden") else views[:1]
    return out


_JUNK_NAME = re.compile(r"^(?:p|pt|part|body|solid|shape|component|comp|feature|brep|item|mesh|object|obj|unnamed|"
                        r"default|copy|instance|occurrence|occ)?[\s_\-.#:]*\d*(?:\s*[(\[<]\d+[)\]>])?$", re.I)


def readable(name: str) -> str:
    """A CAD part name shown to the user only when it reads as a name ("Bolt M8", "Rail"); "p5 (3)", "Body1",
    "Solid_12" and the like say nothing and are left out."""
    n = " ".join(str(name or "").split())
    letters = re.sub(r"[^A-Za-z]", "", n)
    if len(letters) < 3 or _JUNK_NAME.match(n) or re.fullmatch(r"[A-Za-z]{0,2}[\d\W_]+", n):
        return ""
    return n[:40]


def card_picture(session_id: str, name: str) -> bytes | None:
    """The bytes of one of the card's pictures (CARD_PICTURES), or None. Never raises."""
    if name not in CARD_PICTURES:
        return None
    from meshpipeline.contracts.object_storage import get_object_store
    try:
        return get_object_store().get_bytes(object_key=_key(f"{PICTURE_DIR}/{name}.png", session_id))
    except Exception:  # noqa: BLE001 - absent: the rows alone
        return None


def enqueue_reading(session_id: str, key: str, user_notes, request_txt, *, owner_id: str = "") -> bool:
    """Mark the reading of `key` as begun and hand it to the geometry worker. False when nothing runs it."""
    from meshpipeline.contracts.geometry_check import enqueue_part_words
    try:
        _write(session_id, MATCHES, {"key": key, "status": "reading", "at": time.time()})
        return enqueue_part_words(session_id=session_id, key=key, owner_id=owner_id,
                                  user_notes=json.loads(json.dumps(list(user_notes or []), default=str)),
                                  request_txt=str(request_txt or ""))
    except Exception as exc:  # noqa: BLE001 - no reading before approval; the run reads the notes itself
        logger.warning("part matches: the reading was not queued (%s) - session=%s", type(exc).__name__, session_id)
        return False


# #
# 3. the worker's step
# #

def _route_ask(route: str) -> tuple[str, str, bool]:
    """(the user-notes consumer, the mesh's purpose, whether the AI step is on) for the route."""
    from meshpipeline.engines import fea_part_sizes as PS
    from meshpipeline.engines.fea_ai import FEA_AI
    return PS.CONSUMER, PS.PURPOSE, bool(FEA_AI)


def run_reading(*, session_id: str, key: str, user_notes=None, request_txt: str = "", owner_id: str = "") -> dict:
    """THE WORKER'S STEP: the model asked once on the check's parts table and pictures; the checked reading stored
    with its key - unless the notes changed meanwhile (a newer reading is queued; this one is dropped). Never
    raises: a reading that fails is stored as such (the card then shows nothing; the run reads again)."""
    from meshpipeline.application.geometry_check import read_check
    from meshpipeline.cad import part_words as PW
    from meshpipeline.contracts.object_storage import get_object_store

    seen = parts_seen(read_check(session_id))
    if seen is None:
        return {"status": "none"}
    state = {"user_notes": list(user_notes or []), "request_txt": request_txt or "", "user_id": owner_id}
    consumer, purpose, enabled = _route_ask(str(seen.get("route") or ROUTE_FEA))
    started = time.time()
    if not enabled:
        section = PW.section("none", note="the AI step is off")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="part_words_") as tmp:
                store = get_object_store()
                pictures: list[Path] = []
                for p in seen.get("pictures") or []:
                    if p.get("name") in CARD_PICTURES:
                        continue
                    dest = Path(tmp) / f"{p.get('name')}.png"
                    try:
                        store.download_file(object_key=str(p.get("object_key")), destination=dest)
                        pictures.append(dest)
                    except Exception as exc:  # noqa: BLE001 - one picture fewer
                        logger.info("part matches: picture %s not read (%s)", p.get("name"), type(exc).__name__)
                section = asyncio.run(PW.ask(Path(tmp), list(seen["table"]), pictures, state, consumer=consumer,
                                             purpose=purpose, job_id=f"parts-{session_id}"))
                section["pictures"] = [p["object_key"] for p in seen.get("pictures") or []]
        except Exception as exc:  # noqa: BLE001 - nothing read; the run asks again
            logger.warning("part matches: the reading failed (%s) - session=%s", type(exc).__name__, session_id)
            section = PW.section("failed", note=PW.UNREAD_WORDS)
    now = _read(session_id, MATCHES)
    if now is not None and now.get("key") not in (None, key):
        logger.info("part matches: the notes changed while they were read - this reading dropped - session=%s",
                    session_id)
        return {"status": "superseded"}
    _write(session_id, MATCHES, {"key": key, "status": "done", "at": time.time(), "section": section,
                                 "seconds": round(time.time() - started, 1)})
    logger.info("part matches: read in %.1fs - %d row(s) - session=%s", time.time() - started,
                len(section.get("entries") or []), session_id)
    return {"status": "done", "entries": len(section.get("entries") or [])}


# #
# 4. the user's pick, 5. the run
# #

def pick(session: Any, phrase: int, parts: list[str]) -> dict | None:
    """Keep the parts the user picked for one phrase (for the reading of the notes as they stand), and answer
    with the card's rows. None when there is no reading to fix. Ids not in the table are dropped."""
    from meshpipeline.application.geometry_check import read_check
    sid = str(session.id)
    seen = parts_seen(read_check(sid))
    stored = _read(sid, MATCHES)
    if seen is None or stored is None or stored.get("status") != "done":
        return None
    notes = notes_of(getattr(session, "user_notes", None), getattr(session, "request_txt", None))
    key = key_of(notes, seen["table"])
    if stored.get("key") != key:
        return None
    ids = {str(r["id"]) for r in seen["table"]}
    picks = _read(sid, PICKS) or {}
    kept = dict(picks.get("picks") or {}) if picks.get("key") == key else {}
    kept[str(int(phrase))] = [str(p) for p in parts if str(p) in ids]
    _write(sid, PICKS, {"key": key, "picks": kept, "at": time.time()})
    return rows_of(with_picks(sid, stored, seen["table"], notes), seen, sid)


def for_run(session_id: str, user_notes, request_txt) -> dict | None:
    """The reading the user saw before approving, with their picks, for the run's state (cad/part_words.from_stored
    takes it); None when there is none for the notes as they stand. Never raises."""
    try:
        from meshpipeline.application.geometry_check import read_check
        seen = parts_seen(read_check(session_id))
        stored = _read(session_id, MATCHES)
        if seen is None or stored is None or stored.get("status") != "done":
            return None
        notes = notes_of(user_notes, request_txt)
        if stored.get("key") != key_of(notes, seen["table"]):
            return None
        reading = with_picks(session_id, stored, seen["table"], notes)
        return reading if reading.get("status") == "used" else None
    except Exception as exc:  # noqa: BLE001 - the run reads the notes itself
        logger.warning("part matches: the reading was not handed to the run (%s)", type(exc).__name__)
        return None


__all__ = ["CARD_PICTURES", "CARD_VIEWS", "card_picture", "readable", "MATCHES", "PARTS_KEY", "PICKS", "PICTURE_NAMES", "ROUTE_CFD", "ROUTE_FEA", "copy_parts",
           "enqueue_reading", "for_run", "key_of", "notes_of", "parts_seen", "parts_words", "pick", "picture_keys",
           "rows_of", "run_reading", "size_words", "store_parts", "view", "with_picks"]
