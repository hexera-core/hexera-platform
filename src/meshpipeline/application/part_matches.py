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
#: the parts model the console turns in 3D (render/part_model), beside the pictures
MODEL = "model.json"
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
    return [*(_key(f"{PICTURE_DIR}/{n}.png", session_id) for n in PICTURE_NAMES), _key(f"{PICTURE_DIR}/{MODEL}", session_id)]


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
        # the parts, and (FEA) the areas inside them (NOTE-6): a single part with no area to name keeps nothing
        table, _faces_of, shown = PW.table_of(PW.check_record(record) if route == ROUTE_FEA else record, surface)
        if len(table) < 2:
            return None
        from meshpipeline.contracts.object_storage import get_object_store
        from meshpipeline.render.part_pictures import render_parts
        store = get_object_store()
        pictures = []
        ids = [r["id"] for r in table]
        with tempfile.TemporaryDirectory(prefix="part_pictures_") as tmp:
            views = []
            # THE MODEL THE USER TURNS (the checklist's 3D view): the same surface, each triangle's part; the
            # pictures stay for the vision model (and as the console's fallback)
            model = _model_file(shown, ids, Path(tmp), inside={str(r["id"]): str(r.get("in_part") or "")
                                                                for r in table if r.get("feature")})
            for path in [*render_parts(shown, ids, Path(tmp), card=True), *([model] if model else [])]:
                if path.suffix == ".json" and path.name != MODEL:
                    # where each visible part's number goes on a card view, and what that view hides
                    views.append({"name": path.stem, **json.loads(path.read_text())})
                    continue
                key = _key(f"{PICTURE_DIR}/{path.name}", session_id)
                store.upload_file(local_path=path, object_key=key)
                if path.name == MODEL:
                    continue
                pictures.append({"name": path.stem, "object_key": key})
        out = {"route": route, "table": table, "pictures": pictures, "views": views}
        if model:
            out["model"] = _key(f"{PICTURE_DIR}/{MODEL}", session_id)
        return out
    except Exception as exc:  # noqa: BLE001 - no checklist, never a failed check
        logger.warning("part matches: the parts were not kept for the checklist (%s: %s)", type(exc).__name__,
                       str(exc)[:200])
        return None


def _model_file(surface, part_ids: list[str], tmp: Path, *, inside: dict[str, str] | None = None) -> Path | None:
    """The parts model (render/part_model) written to `tmp`, or None when it cannot be made (the pictures alone).
    `inside`: the part each area is in."""
    try:
        from meshpipeline.render.part_model import model_payload
        path = tmp / MODEL
        path.write_text(json.dumps(model_payload(surface, part_ids, inside=inside), separators=(",", ":")))
        return path
    except Exception as exc:  # noqa: BLE001 - the console shows the pictures instead
        logger.warning("part matches: the parts model was not made (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def model_bytes(session_id: str) -> bytes | None:
    """The stored parts model (JSON bytes) the console turns, or None. Never raises."""
    from meshpipeline.contracts.object_storage import get_object_store
    try:
        return get_object_store().get_bytes(object_key=_key(f"{PICTURE_DIR}/{MODEL}", session_id))
    except Exception:  # noqa: BLE001 - absent: the pictures instead
        return None


def copy_parts(seen: Mapping | None, session_id: str) -> dict | None:
    """A measurement reused from an earlier check of the same file: its pictures copied to this session's folder
    (the earlier session's may be erased); the table alone when they cannot be. Never raises."""
    if not seen or not seen.get("table"):
        return None
    from meshpipeline.contracts.object_storage import get_object_store
    store = get_object_store()
    pictures = []
    out = {k: v for k, v in dict(seen).items() if k != "model"}
    model = {"name": MODEL[:-5], "object_key": seen.get("model"), "file": MODEL} if seen.get("model") else None
    for p in [*(seen.get("pictures") or []), *([model] if model else [])]:
        file = str(p.get("file") or f"{p.get('name')}.png")
        key = _key(f"{PICTURE_DIR}/{file}", session_id)
        if p.get("object_key") == key:
            if p is model:
                out["model"] = key
            else:
                pictures.append(dict(p))
            continue
        try:
            data = store.get_bytes(object_key=str(p.get("object_key")))
            with tempfile.NamedTemporaryFile(suffix=Path(file).suffix, delete=False) as fh:
                fh.write(data)
                tmp = Path(fh.name)
            try:
                store.upload_file(local_path=tmp, object_key=key)
            finally:
                tmp.unlink(missing_ok=True)
            if p is model:
                out["model"] = key
            else:
                pictures.append({"name": p.get("name"), "object_key": key})
        except Exception as exc:  # noqa: BLE001 - fewer pictures
            logger.info("part matches: picture %s not copied (%s)", p.get("name"), type(exc).__name__)
    return {**out, "pictures": pictures}


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
    """What the request card shows about the notes and the parts (blocking: call it off the event loop). Drawing
    the card never starts a reading - the user's Finalize notes does (`finalize`), or their yes (`before_run`):
    None (no words, or no parts to tie them to), {"status": "waiting"} (the check is still measuring),
    {"status": "ready"} (words not read yet: the card offers Finalize notes), {"status": "reading"} or
    {"status": "done", "rows": [...]} (no rows: the words name no part). While the notes changed since the last
    reading, its rows stay on the card ("stale"). Never raises."""
    try:
        return _view(session, owner_id=owner_id, start=False)
    except Exception as exc:  # noqa: BLE001 - the card is drawn without the rows
        logger.warning("part matches: the checklist was not drawn (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def finalize(session: Any, *, owner_id: str = "") -> dict | None:
    """THE USER FINALIZED THEIR NOTES: the reading of the words as they stand now (the first answer and every note)
    is started - unless it already runs or is done - and the card's view is answered. Never raises."""
    try:
        return _view(session, owner_id=owner_id, start=True)
    except Exception as exc:  # noqa: BLE001 - nothing read; the run reads the notes itself
        logger.warning("part matches: the notes were not read (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def before_run(session: Any, *, owner_id: str = "") -> str:
    """Whether a yes can start the run now: "go" (nothing to read, or the words as they stand are read) or "read"
    (they are not read yet: the reading is started now and the yes waits for it). Never raises: "go" when unsure."""
    got = finalize(session, owner_id=owner_id)
    return "read" if got and got.get("status") == "reading" else "go"


def _view(session: Any, *, owner_id: str, start: bool) -> dict | None:
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
    was = stored or {}
    current = stored is not None and was.get("key") == key
    lapsed = current and was.get("status") == "reading" and (
        time.time() - float(was.get("at") or 0) > READING_STALE_S)
    if start and (not current or lapsed):
        if enqueue_reading(sid, key, getattr(session, "user_notes", None), getattr(session, "request_txt", None),
                           owner_id=owner_id or str(getattr(session, "owner_id", "") or ""), previous=stored):
            stored, current = _read(sid, MATCHES), True
    if current and stored is not None and stored.get("status") == "done":
        got = rows_of(with_picks(sid, stored, seen["table"], notes), seen, sid)
        return got or {"status": "done", "rows": [], "unsure": []}
    # NOT READ FOR THESE WORDS (yet): the last reading's rows stay on the card until the new one lands
    status = "reading" if current and stored is not None and stored.get("status") == "reading" else "ready"
    out: dict[str, Any] = {"status": status}
    last = stored if stored is not None and stored.get("status") == "done" else (stored or {}).get("previous")
    if isinstance(last, dict) and last.get("status") == "done":
        old = rows_of(with_picks(sid, last, seen["table"], notes), seen, sid)
        if old:
            out = {**old, "status": status, "stale": True}
    return out


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


def parts_words(ids: list[str], names: dict[str, str] | None = None) -> str:
    """"part 9", "parts 2, 3 and 5", "parts 1-4 and 7" (runs of three or more joined); an area inside a part
    (NOTE-6) by its own name ("4 holes, 8.5 mm across"), two at most, then how many."""
    names = names or {}
    areas = [names[p] for p in ids if p in names]
    ids = [p for p in ids if p not in names]
    said = _part_words(ids) if ids else ""
    if areas:
        # one area by its name; several alike ("2 bosses ...", "2 bosses ...") or long names as a count
        uniq = list(dict.fromkeys(areas))
        if len(uniq) == 1 and len(areas) == 1:
            shown = uniq[0]
        elif len(areas) == 2 and len(uniq) == 2 and len(uniq[0]) + len(uniq[1]) <= 44:
            shown = f"{uniq[0]} and {uniq[1]}"
        else:
            shown = f"{len(areas)} areas"
        return f"{said}, {shown}" if said else shown
    return said


def _where(e: Mapping, ids: list[str], names: dict[str, str], one_part: bool) -> str:
    """Where a row's size goes, in words: "parts 9-12", "4 holes, 8.5 mm across", "the other 6 parts", and on a
    single part "the rest of the part" / "the whole part"."""
    if e.get("rest_parts") and not e.get("parts"):
        if one_part:
            return "the rest of the part"
        return f"the other {len(ids)} part{'s' if len(ids) != 1 else ''}"
    if one_part and [p for p in ids if p not in names] == ["Part_1"]:
        areas = [p for p in ids if p in names]
        return "the whole part" + (f", {parts_words(areas, names)}" if areas else "")
    return parts_words(ids, names)


def _part_words(ids: list[str]) -> str:
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
    if len(runs) > 4:
        # a long scatter of parts ("parts 5–8, 11–14, 19, 62, ...") is a count: the model shows which
        return f"{len(nums)} parts"
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
    from meshpipeline.render.part_pictures import colour_of, number
    if reading.get("status") != "used":
        return None
    table = list(seen.get("table") or [])
    names = {str(r["id"]): str(r.get("name") or "") for r in table if r.get("feature")}
    one_part = sum(1 for r in table if not r.get("feature")) == 1
    rows = []
    for e in reading.get("entries") or []:
        ids = [*e.get("parts", []), *e.get("rest_parts", [])]
        rows.append({"phrase": e.get("phrase"), "words": _short(e.get("quote") or ""),
                     "quote": e.get("quote") or "", "parts": ids,
                     "parts_text": _where(e, ids, names, one_part),
                     "size": size_words(e), "picked": e.get("why") == "picked by you"})
    unsure = [{"phrase": u.get("phrase"), "words": _short(u.get("quote") or ""), "quote": u.get("quote") or "",
               "text": f"Couldn't tell which part is “{_short(u.get('quote') or '', 40)}”", "size": size_words(u)}
              for u in reading.get("unsure") or []]
    if not rows and not unsure:
        return None
    # WHAT A ROW COVERS on the picture: its parts, and the areas inside them no other row sized (a part's size holds
    # on its holes and fillets too, unless they were named for their own) - so "the rest of the part" lights the
    # whole rest of it
    inside = {str(r["id"]): str(r.get("in_part") or "") for r in table if r.get("feature")}
    claimed = {p for r in rows for p in r["parts"]}
    for r in rows:
        lit = [*r["parts"], *[f for f, p in inside.items() if p in r["parts"] and f not in claimed]]
        if len(lit) > len(r["parts"]):
            r["lit"] = lit
    parts = [{"id": r["id"], "n": number(r["id"]),
              "name": (str(r.get("name") or "") if r.get("feature") else readable(r.get("name") or "")),
              "colour": colour_of(i)}
             for i, r in enumerate(table)]
    if len(parts) > MAX_LISTED:
        keep = {r["id"] for r in sorted(table, key=lambda r: -float(r.get("volume_mm3") or 0))[:MAX_LISTED]}
        parts = [p for p in parts if p["id"] in keep]
    # order: the part under each id of the card's id picture (the table's order)
    out: dict[str, Any] = {"status": "done", "rows": rows, "unsure": unsure, "parts": parts,
                           "order": [r["id"] for r in table]}
    # the part each area is inside: a click on an area picks its part unless the row is about areas
    if inside:
        out["inside"] = {f: p for f, p in inside.items() if p}
    # THE CARD'S VIEWS (render/part_pictures CARD_VIEWS): served through the API (GET .../check/parts/<name>.png)
    # so the console can read the id picture under the cursor; the first view always, the second only when the
    # first hides a part
    stored = {p.get("name") for p in seen.get("pictures") or []}
    views = [{k: v[k] for k in ("name", "w", "h", "tags", "hidden", "rings") if k in v} for v in seen.get("views") or []
             if v.get("name") in stored and f"{v.get('name')}_ids" in stored]
    if views:
        out["views"] = views if views[0].get("hidden") else views[:1]
    # THE MODEL THE USER TURNS (GET .../check/parts/model): the console draws it in 3D, the pictures its fallback
    if seen.get("model"):
        out["model"] = True
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


def enqueue_reading(session_id: str, key: str, user_notes, request_txt, *, owner_id: str = "",
                    previous: Mapping | None = None) -> bool:
    """Mark the reading of `key` as begun and hand it to the geometry worker. False when nothing runs it.
    `previous`: the last reading, whose rows the card keeps showing until this one lands."""
    from meshpipeline.contracts.geometry_check import enqueue_part_words
    try:
        last = previous if (previous or {}).get("status") == "done" else (previous or {}).get("previous")
        _write(session_id, MATCHES, {"key": key, "status": "reading", "at": time.time(),
                                     **({"previous": dict(last)} if isinstance(last, Mapping) else {})})
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
    if route == ROUTE_CFD:
        from meshpipeline.engines.snappy import part_levels as PL
        return PL.CONSUMER, PL.PURPOSE, True
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


__all__ = ["CARD_PICTURES", "CARD_VIEWS", "MODEL", "before_run", "finalize", "card_picture", "model_bytes", "readable", "MATCHES", "PARTS_KEY", "PICKS", "PICTURE_NAMES", "ROUTE_CFD", "ROUTE_FEA", "copy_parts",
           "enqueue_reading", "for_run", "key_of", "notes_of", "parts_seen", "parts_words", "pick", "picture_keys",
           "rows_of", "run_reading", "size_words", "store_parts", "view", "with_picks"]
