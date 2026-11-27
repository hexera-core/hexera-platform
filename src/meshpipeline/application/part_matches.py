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

ON THE FLOW (CFD) ROUTE the notes are read before approval whatever the model is (NOTES-CFD-1): a single part, or
parts the notes never name, still gets Finalize notes. The worker then reads the notes as the run's CFD reader
does (engines/cfd_requirements: engines/mesh_requirements, route "flow", through engines/meshing_standard) and the
card shows its lines ("From your notes: ...", "Not applied: ..."); a model of several parts gets them beside its
rows. The run reads the notes again for itself (the same words, the same reading).
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
    # the user's own words only: request_txt (the intake's summary) is never read (cad/part_words.notes_text)
    return PW.notes_text({"user_notes": list(user_notes or [])})


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


def finalize(session: Any, *, owner_id: str = "", standard: Mapping | None = None) -> dict | None:
    """THE USER FINALIZED THEIR NOTES: the reading of the user's notes as they stand now
    is started - unless it already runs or is done - and the card's view is answered. `standard`: the team's saved
    meshing standard (application/meshing_standard_service.for_reading), handed to the reading for the cost of an
    FEA solid's requirements (NOTE-26). Never raises."""
    try:
        return _view(session, owner_id=owner_id, start=True, standard=standard)
    except Exception as exc:  # noqa: BLE001 - nothing read; the run reads the notes itself
        logger.warning("part matches: the notes were not read (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None


def with_standard(standard: Mapping | None) -> dict:
    """finalize's `standard` argument, given only when there is one (a session with no team standard reads as before)."""
    return {"standard": dict(standard)} if isinstance(standard, Mapping) else {}


def before_run(session: Any, *, owner_id: str = "") -> str:
    """Whether a yes can start the run now: "go" (nothing to read, or the words as they stand are read) or "read"
    (they are not read yet: the reading is started now and the yes waits for it). Never raises: "go" when unsure."""
    got = finalize(session, owner_id=owner_id)
    return "read" if got and got.get("status") == "reading" else "go"


def _view(session: Any, *, owner_id: str, start: bool, standard: Mapping | None = None) -> dict | None:
    from meshpipeline.application.geometry_check import read_check
    sid = str(getattr(session, "id", "") or "")
    notes = notes_of(getattr(session, "user_notes", None), getattr(session, "request_txt", None))
    if not sid or not notes.strip():
        return None
    check = read_check(sid)
    seen = parts_seen(check)
    if seen is None:
        status = str((check or {}).get("status") or "")
        if status in ("pending", "scouted"):
            return {"status": "waiting"}
        if not flow_route(check, session):
            return None
        # NOTES-CFD-1: no parts to tie the words to, and a flow mesh: the notes themselves are still read
        seen = NOTES_ONLY
    key = key_of(notes, seen["table"])
    stored = _read(sid, MATCHES)
    was = stored or {}
    current = stored is not None and was.get("key") == key
    lapsed = current and was.get("status") == "reading" and (
        time.time() - float(was.get("at") or 0) > READING_STALE_S)
    if start and (not current or lapsed):
        if enqueue_reading(sid, key, getattr(session, "user_notes", None), getattr(session, "request_txt", None),
                           owner_id=owner_id or str(getattr(session, "owner_id", "") or ""), previous=stored,
                           notes_only=seen is NOTES_ONLY, standard=standard):
            stored, current = _read(sid, MATCHES), True
    if current and stored is not None and stored.get("status") == "done":
        own = getattr(session, "user_notes", None)
        got = with_notes(with_faces(rows_of(with_picks(sid, stored, seen["table"], notes), seen, sid), own), own) or {
            "status": "done", "rows": [], "unsure": []}
        got = with_counts(got, own, flow=seen.get("route") == ROUTE_CFD) or got
        got = with_cost(got, stored.get("cost"))
        if stored.get("cost_pending"):
            # NOTE-26: the cost is still being worked out - the console keeps asking, and its line follows the reply
            got = {**got, "cost_pending": True}
        if str((stored.get("section") or {}).get("status") or "") == "failed":
            # the model step did not answer: said so plainly (the run reads the notes again)
            got = {**got, "failed": True}
        lines = notes_lines(stored, notes=notes, facts=_acted_facts(session))
        if seen.get("route") == ROUTE_CFD:
            # NOTE-24: a count the checklist shows as its own row is not listed again as the reader's line
            lines = listed_once(lines, got.get("rows"))
            # a flow mesh: the run's own CFD reader reads every note again, so what is not in this checklist is
            # still kept for the run (the reply says it is kept, never that it was unreadable -
            # agents/intake/plan_talk.reply_for); `notes_only`: no parts to tie words to at all
            got = {**got, "flow": True, **({"notes_only": True} if seen is NOTES_ONLY else {})}
        return {**got, "lines": lines} if lines else got
    # NOT READ FOR THESE WORDS (yet): the last reading's rows stay on the card until the new one lands
    status = "reading" if current and stored is not None and stored.get("status") == "reading" else "ready"
    out: dict[str, Any] = {"status": status}
    last = stored if stored is not None and stored.get("status") == "done" else (stored or {}).get("previous")
    if isinstance(last, dict) and last.get("status") == "done":
        old = with_notes(with_faces(rows_of(with_picks(sid, last, seen["table"], notes), seen, sid),
                                    getattr(session, "user_notes", None)), getattr(session, "user_notes", None))
        if old:
            old = with_counts(old, getattr(session, "user_notes", None), flow=seen.get("route") == ROUTE_CFD) or old
            out = {**old, "status": status, "stale": True}
    return out


#: NOTES-CFD-1: what stands in for the parts on a flow model the notes can name no part of (one part, or none kept)
NOTES_ONLY: dict = {"route": ROUTE_CFD, "table": []}


def flow_route(check: Mapping | None, session: Any) -> bool:
    """Whether the session's mesh is a flow (CFD) mesh as the stage reads it: the user's words settled a flow purpose
    and the file is not a whole thermal model (which has its own panel)."""
    from meshpipeline.application import fea_check
    return fea_check.stage_route(dict(check or {}), getattr(session, "messages", None)) == fea_check.STAGE_FLOW


def notes_lines(stored: Mapping | None, *, notes: str = "", facts: Mapping | None = None) -> list[str]:
    """NOTES-CFD-1: the CFD reader's lines for a reading - "From your notes: ..." for each requirement it read, then
    what it could not apply - as the run says them (engines/mesh_requirements.words); none when it read nothing.
    `notes` and `facts` (engines/notes_acted, NOTE-15): what the product acts on outside the reading - the engine the
    notes name, the conditions confirmed - is said as acted on, never "not applied"."""
    from meshpipeline.engines import mesh_requirements as MR
    sec = (stored or {}).get("notes")
    if not isinstance(sec, Mapping):
        return []
    if notes:
        from meshpipeline.engines import notes_acted as NA
        sec = NA.apply(sec, notes, facts)
    return [str(w) for w in MR.words(sec)]


def _acted_facts(session: Any) -> dict:
    """NOTE-15 on the request card: the engine the session meshes with (the confirmed or proposed selection, else
    the session's own), its purpose and the conditions its request carries. Never raises."""
    from meshpipeline.engines import notes_acted as NA
    try:
        gate = getattr(session, "intake_gate", None) or {}
        engine = str((gate.get("selection") or {}).get("engine") or getattr(session, "mesh_engine", "") or "")
        return NA.facts_of({"purpose": str(getattr(session, "purpose", "") or ""),
                            "request_txt": str(getattr(session, "request_txt", "") or "")}, engine)
    except Exception:  # noqa: BLE001 - no facts: only the engine the notes name, as asked
        return {}


def read_notes(state: Mapping, session_id: str) -> dict:
    """NOTES-CFD-1: the notes read as the run's CFD reader reads them (engines/cfd_requirements.read_for_attempt:
    engines/meshing_standard.decide on the flow route) - one model call, on the worker. Never raises."""
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines import meshing_standard as MS
    try:
        return asyncio.run(MS.decide(state, route=MR.FLOW, job_id=f"notes-{session_id}"))
    except Exception as exc:  # noqa: BLE001 - the run reads the notes itself
        logger.warning("part matches: the notes were not read (%s) - session=%s", type(exc).__name__, session_id)
        return MR.section("failed", note=MR.UNREAD_WORDS)


def with_picks(session_id: str, stored: Mapping, table: list[dict], notes: str) -> dict:
    """The stored reading with the user's picks for the same key applied (checked again)."""
    from meshpipeline.cad import part_words as PW
    reading = dict(stored.get("section") or {})
    picks = _read(session_id, PICKS)
    if reading.get("status") == "used" and picks and picks.get("key") == stored.get("key") and picks.get("picks"):
        reading = PW.picked(reading, picks["picks"], table, notes)
    return reading


def with_faces(got: dict | None, user_notes) -> dict | None:
    """NOTE-17 on the checklist before approval: a row read from words sent with faces picked on the model shows
    those faces as its place ("2 mm · the face you picked (Face 12 · round Ø15 mm)"), as the run applies it
    (engines/mesh_requirements.bind_picks, cad/part_words.with_picks), not the parts or areas the reader tied the
    words to: the one rule both call is mesh_requirements.picked_for. A phrase the reader could not place is placed by
    the pick. Left as read: words that say the whole mesh. A face row is not re-tied to parts. Pure."""
    from meshpipeline.contracts.user_notes import own_notes
    from meshpipeline.engines import mesh_requirements as MR
    if not got:
        return got
    notes = [n for n in own_notes(user_notes) if n.get("faces")]
    if not notes:
        return got

    def faces_of(r: Mapping) -> list | None:
        return MR.picked_for(str(r.get("quote") or ""), notes)

    def face_row(r: Mapping, faces: list) -> dict:
        row = {k: v for k, v in r.items() if k not in ("lit", "text", "picked", "rest")}
        return {**row, "parts": [], "picked": True, "parts_text": MR.picked_where(faces),
                "faces": [{"id": str(f.get("id") or ""), "label": str(f.get("label") or f.get("id") or "")}
                          for f in faces]}

    rows, unsure = [], []
    for r in got.get("rows") or []:
        f = faces_of(r)
        rows.append(face_row(r, f) if f else r)
    for u in got.get("unsure") or []:
        f = faces_of(u) if u.get("size") else None
        if f:
            rows.append(face_row(u, f))
        else:
            unsure.append(u)
    return {**got, "rows": whole_or_rest(rows), "unsure": unsure}


def with_notes(got: dict | None, user_notes) -> dict | None:
    """NOTE-19: each row and each phrase not placed carries `note`, the id of the user's note it was read from (the
    newest note its quote stands in, by the reader's own word-for-word test, cad/part_words.quoted_in - the test that
    let the row onto the card), so a reply finds the rows of its own message by id, not by matching words again.
    Pure; a row no note holds is left without one."""
    from meshpipeline.cad import part_words as PW
    from meshpipeline.contracts.user_notes import own_notes, said
    if not got or not (got.get("rows") or got.get("unsure")):
        return got
    # a click that answers earlier words (NOTE-22) holds them: the row they make on its faces is that message's
    notes = [(str(n.get("id") or ""), said(n)) for n in own_notes(user_notes)]

    def stamp(r: Mapping) -> dict:
        quote = str(r.get("quote") or "")
        hit = next((i for i, text in reversed(notes) if PW.quoted_in(quote, text)), "")
        return {**r, "note": hit} if hit else dict(r)
    return {**got, "rows": [stamp(r) for r in got.get("rows") or []],
            "unsure": [stamp(u) for u in got.get("unsure") or []]}


#: NOTE-24: an element or cell count typed at the plan step is a row of the checklist
COUNT_RULE = "NOTE-24"


def with_counts(got: dict | None, user_notes, *, flow: bool) -> dict | None:
    """NOTE-24: the count the user's notes state for the whole mesh ("keep it under 1.5 million elements") as a row of
    the checklist - the newest note that states one (the run's readers keep the latest), stamped with its id - where
    the run's own reader applies it: every flow mesh (CFDREQ-3), and the solid and shell meshes while their AI step
    reads the notes (AI-fea-4, PLAN-fea-27, SHMESH-23; with it off the run applies no count, so none is shown). Pure
    but for that switch."""
    from meshpipeline.agents.intake import plan_talk as PT
    from meshpipeline.contracts.user_notes import own_notes
    if got is None:
        return got
    if not flow:
        from meshpipeline.engines.fea_ai import FEA_AI
        if not FEA_AI:
            return got
    rows: list[dict] = []
    for n in reversed(own_notes(user_notes)):
        rows = PT.count_rows(n, flow=flow, note_id=str(n.get("id") or ""))
        if rows:
            break
    return {**got, "rows": [*(got.get("rows") or []), *rows]} if rows else got


def with_cost(got: dict, cost: Mapping | None) -> dict:
    """NOTE-26: the cost of the checklist's sizes and count kept with the reading (application/plan_cost) on the view,
    and the count row (NOTE-24) showing the element estimate the run would plan with them. Pure."""
    if not got or not isinstance(cost, Mapping):
        return got
    after = int((cost.get("after") or {}).get("elements") or 0)
    if not after:
        return got              # (a reading with nothing to cost keeps only its marker, plan_cost.NOTHING)
    from meshpipeline.agents.intake import plan_talk as PT
    rows = [({**r, "estimate": f"Planned: {PT.count_words(after)} elements"} if r.get("count") and after else r)
            for r in got.get("rows") or []]
    return {**got, "rows": rows, "cost": dict(cost)}


#: NOTE-24: the CFD reader's line for a whole-mesh count ("From your notes: about 2 million cells.", or the team's
#: settings' "From your settings: at most 8 million cells."), and its bound
_COUNT_LINE = re.compile(r"^From your (?:notes|settings): (about|at most|at least) [\d.,]+(?: million)? "
                         r"(?:cells|elements)\b")
_COUNT_BOUND = {"target": "about", "ceiling": "at most", "floor": "at least"}


def listed_once(lines: list[str], rows) -> list[str]:
    """NOTE-24 on a flow mesh: ONE SETTING IS LISTED ONCE. A count typed at the plan step is a row of the checklist
    (`with_counts`, read in code, as typed) and the CFD reader reads it too ("From your notes: about 2 million
    cells."), so the checklist listed the same setting twice; a team's saved count of the same bound (Workspace >
    Meshing, engines/meshing_standard) beside it is the same setting again. The row stays - it holds the typed value,
    which wins - and the reader's count lines of that bound are left out. Pure."""
    bounds = {_COUNT_BOUND.get(str((r.get("count") or {}).get("what"))) for r in rows or ()
              if isinstance(r, Mapping) and r.get("count")}
    if not bounds:
        return list(lines)
    return [w for w in lines if not ((m := _COUNT_LINE.match(str(w))) and m.group(1) in bounds)]


def note_id(user_notes, note: Mapping) -> str:
    """The id among the user's own notes of one kept note (a chat note by its source and turn, else by its text)."""
    from meshpipeline.contracts.user_notes import own_notes
    for n in reversed(own_notes(user_notes)):
        if note.get("turn") is not None and n.get("source") == note.get("source") and n.get("turn") == note.get("turn"):
            return str(n.get("id") or "")
    for n in reversed(own_notes(user_notes)):
        if str(n.get("text") or "") == str(note.get("text") or "") and n.get("source") == note.get("source"):
            return str(n.get("id") or "")
    return ""


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


WHOLE_PART, REST_OF_PART = "the whole part", "the rest of the part"


def whole_or_rest(rows: list[dict]) -> list[dict]:
    """On a single part, a size the reader put on everything else (`rest`) is "the rest of the part" only when another
    row of the checklist sizes a place of its own (an area, a picked face); alone it is "the whole part" (the C5
    knuckle, 26 Nov: "3 mm elements on the rest of the part" for the first size typed). Pure."""
    others = any(not r.get("rest") and not r.get("count") and r.get("size") for r in rows)
    return [({**r, "parts_text": REST_OF_PART if others else WHOLE_PART} if r.get("rest") else r) for r in rows]


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


def _unsure_text(u: Mapping) -> str:
    """The card's line for a phrase not placed: which part was unclear, or (NOTE-21) which one of several places."""
    quote = _short(u.get("quote") or "", 40)
    n = u.get("which_one")
    if isinstance(n, int) and n > 1:
        noun = str(u.get("noun") or "place")
        nouns = noun + ("es" if noun.endswith(("ch", "sh", "s", "x")) else "s")
        return f"Not set yet: “{quote}” - there are {n} {nouns}, click the one you mean"
    if u.get("none_of"):
        # NOTE-23: a kind of place the part does not have - none found, the face asked for
        noun = str(u["none_of"])
        nouns = noun + ("es" if noun.endswith(("ch", "sh", "s", "x")) else "s")
        return f"Not set yet: “{quote}” - no {nouns} on this part, click the face you mean"
    return f"Couldn't tell which part is “{quote}”"


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
                     "size": size_words(e), "picked": e.get("why") == "picked by you",
                     **({"rest": True} if one_part and e.get("rest_parts") and not e.get("parts") else {})})
    unsure = [{"phrase": u.get("phrase"), "words": _short(u.get("quote") or ""), "quote": u.get("quote") or "",
               "text": _unsure_text(u), "size": size_words(u),
               # NOTE-21: one place named, several it could be - how many, and what they are
               **({"which_one": int(u["which_one"]), "noun": str(u.get("noun") or "")}
                  if isinstance(u.get("which_one"), int) else {}),
               # NOTE-23: a kind of place the part does not have - which, and what it does have
               **({"none_of": str(u["none_of"]), "noun": str(u["none_of"]), "found": str(u.get("found") or "")}
                  if u.get("none_of") else {})}
              for u in reading.get("unsure") or []]
    if not rows and not unsure:
        return None
    rows = whole_or_rest(rows)
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
                    previous: Mapping | None = None, notes_only: bool = False,
                    standard: Mapping | None = None) -> bool:
    """Mark the reading of `key` as begun and hand it to the geometry worker. False when nothing runs it.
    `previous`: the last reading, whose rows the card keeps showing until this one lands. `notes_only`: a flow model
    with no parts to tie the words to (NOTES-CFD-1): the worker reads the notes alone."""
    from meshpipeline.contracts.geometry_check import enqueue_part_words
    try:
        last = previous if (previous or {}).get("status") == "done" else (previous or {}).get("previous")
        _write(session_id, MATCHES, {"key": key, "status": "reading", "at": time.time(),
                                     **({"notes_only": True} if notes_only else {}),
                                     **({"previous": dict(last)} if isinstance(last, Mapping) else {})})
        return enqueue_part_words(session_id=session_id, key=key, owner_id=owner_id,
                                  user_notes=json.loads(json.dumps(list(user_notes or []), default=str)),
                                  request_txt=str(request_txt or ""),
                                  # NOTE-26: the team's standard, for the cost of an FEA solid's requirements
                                  **({"meshing_standard": json.loads(json.dumps(dict(standard), default=str))}
                                     if isinstance(standard, Mapping) else {}))
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


def read_fea_notes(state: Mapping, session_id: str) -> tuple[dict, dict] | None:
    """NOTE-26: the notes read as the solid run reads them (engines/fea_driver.read_requirements: meshing_standard
    .decide on the FEA route, then fea_driver.requirements_section) - and the team's standard alone - for the cost of
    what was typed. One model call for the notes (the standard's reading kept at save time is reused), on the worker.
    None when they could not be read (the cost is then not said). Never raises."""
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import meshing_standard as MS
    from meshpipeline.engines.fea_ai import FEA_AI
    try:
        read, base = asyncio.run(MS.decide_and_base(state, route="fea", job_id=f"notes-{session_id}",
                                                    enabled=FEA_AI))
        return FD.requirements_section(read, state), FD.requirements_section(base, state)
    except Exception as exc:  # noqa: BLE001 - no cost said; the run reads the notes itself
        logger.warning("part matches: the notes were not read for the cost (%s) - session=%s", type(exc).__name__,
                       session_id)
        return None


def run_reading(*, session_id: str, key: str, user_notes=None, request_txt: str = "", owner_id: str = "",
                meshing_standard: Mapping | None = None) -> dict:
    """THE WORKER'S STEP: the model asked once on the check's parts table and pictures; the checked reading stored
    with its key - unless the notes changed meanwhile (a newer reading is queued; this one is dropped). Never
    raises: a reading that fails is stored as such (the card then shows nothing; the run reads again).
    `meshing_standard`: the team's saved standard as a run is dispatched with it (engines/meshing_standard.snapshot),
    for the cost of an FEA solid's requirements (NOTE-26)."""
    from meshpipeline.application.geometry_check import read_check
    from meshpipeline.cad import part_words as PW
    from meshpipeline.contracts.object_storage import get_object_store

    seen = parts_seen(read_check(session_id))
    if seen is None:
        # NOTES-CFD-1: a flow model with no parts to tie the words to - the notes alone, as the card asked
        if not (_read(session_id, MATCHES) or {}).get("notes_only"):
            return {"status": "none"}
        seen = NOTES_ONLY
    # session_id: the user's own pictures with their notes are read from it (NOTE-16, contracts/user_pictures)
    state = {"user_notes": list(user_notes or []), "request_txt": request_txt or "", "user_id": owner_id,
             "session_id": str(session_id or "")}
    route = str(seen.get("route") or ROUTE_FEA)
    consumer, purpose, enabled = _route_ask(route)
    started = time.time()
    # NOTES-CFD-1: on the flow route the notes themselves are read too, as the run's CFD reader reads them - in a
    # thread of its own, beside the parts reading (the two model calls are independent; one after the other they
    # cost their sum, side by side the longer of the two)
    notes_job = fea_job = None
    if route == ROUTE_CFD:
        import concurrent.futures
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes-read")
        notes_job = pool.submit(read_notes, state, session_id)
        pool.shutdown(wait=False)
    elif seen is not NOTES_ONLY:
        # NOTE-26: the cost plans with what the run plans with - the notes read with the team's standard, as the run
        # reads them - so they are read too, beside the parts reading (side by side: the longer of the two). EVERY
        # reading of an FEA part keeps them, sized or not: the reading after it plans "before" from them (the C5
        # knuckle, 27 Nov: a reading made before the stage had sized the part kept none, and the first requirement's
        # count line had no "before", so it was never said)
        import concurrent.futures
        fea_state: dict[str, Any] = dict(state)
        if isinstance(meshing_standard, Mapping):
            fea_state["meshing_standard"] = dict(meshing_standard)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fea-notes-read")
        fea_job = pool.submit(read_fea_notes, fea_state, session_id)
        pool.shutdown(wait=False)
    if seen is NOTES_ONLY:
        section = PW.section("none", note="no parts to tie the words to")
    elif not enabled:
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
    notes_sec = notes_job.result() if notes_job is not None else None
    # NOTE-26: the notes read with the team's standard, kept with the reading so the reading after it can plan the
    # plan as it stood before it (plan_cost.before_of) - notes that could not be read are no cost at all (one without
    # the standard and the picked faces would be the wrong number)
    fea_notes = fea_job.result() if fea_job is not None else None
    fea_inputs = None
    if fea_notes is not None:
        from meshpipeline.application import plan_cost
        fea_inputs = plan_cost.inputs(section, fea_notes[0], user_notes)
    now = _read(session_id, MATCHES)
    if now is not None and now.get("key") not in (None, key):
        logger.info("part matches: the notes changed while they were read - this reading dropped - session=%s",
                    session_id)
        return {"status": "superseded"}
    previous = (now or {}).get("previous")
    # THE REPLY NEVER WAITS FOR THE COST (NOTE-26): the reading is kept now, the cost after it - its line follows the
    # reply as its own message when it lands (agents/intake/plan_reply.count_owed)
    _write(session_id, MATCHES, {"key": key, "status": "done", "at": time.time(), "section": section,
                                 **({"notes": notes_sec} if notes_sec is not None else {}),
                                 **({"fea_inputs": fea_inputs, "cost_pending": True} if fea_inputs is not None else {}),
                                 "seconds": round(time.time() - started, 1)})
    logger.info("part matches: read in %.1fs - %d row(s) - session=%s", time.time() - started,
                len(section.get("entries") or []), session_id)
    if fea_notes is not None:
        from meshpipeline.application import plan_cost
        ai_state = {k: v for k, v in fea_state.items() if k != "user_notes"}
        cost = plan_cost.for_reading(session_id, section, user_notes, asked=fea_notes[0], base_asked=fea_notes[1],
                                     previous=previous if isinstance(previous, Mapping) else None, ai_state=ai_state)
        keep_cost(session_id, key, cost)
    return {"status": "done", "entries": len(section.get("entries") or [])}


def keep_cost(session_id: str, key: str, cost: Mapping | None) -> bool:
    """NOTE-26: the cost of a reading kept with it - only while the notes are still the ones it read (a newer
    requirement's reading has its own: a stale count is never kept, so never said). False when it was not kept."""
    now = _read(session_id, MATCHES)
    if now is None or now.get("key") != key or now.get("status") != "done":
        logger.info("part matches: the notes changed while they were costed - this cost dropped - session=%s",
                    session_id)
        return False
    got = {k: v for k, v in now.items() if k != "cost_pending"}
    if cost is not None:
        got["cost"] = dict(cost)
    _write(session_id, MATCHES, got)
    return True


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
    return with_faces(rows_of(with_picks(sid, stored, seen["table"], notes), seen, sid),
                      getattr(session, "user_notes", None))


def notes_for_run(session_id: str, user_notes, request_txt) -> dict | None:
    """NOTES-CFD-1: the CFD reader's reading of the notes made before approval (run_reading's "notes"), when it is
    of the notes as they stand; None otherwise. No model call - what the card already read. Never raises."""
    try:
        from meshpipeline.application.geometry_check import read_check
        stored = _read(session_id, MATCHES)
        if stored is None or stored.get("status") != "done" or not isinstance(stored.get("notes"), Mapping):
            return None
        seen = parts_seen(read_check(session_id)) or NOTES_ONLY
        if stored.get("key") != key_of(notes_of(user_notes, request_txt), seen["table"]):
            return None
        return dict(stored["notes"])
    except Exception as exc:  # noqa: BLE001 - the run reads the notes itself
        logger.warning("part matches: the notes reading was not handed to the run (%s)", type(exc).__name__)
        return None


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
        if reading.get("status") != "used":
            return None
        args = str((stored.get("cost") or {}).get("ai_args") or "")
        # NOTE-26: the AI step's decision made when these words were costed goes with them, so the run plans the count
        # the user was told (engines/fea_driver.plan_step_ai; checked again on the run's own part)
        return {**reading, "ai_args": args} if args else reading
    except Exception as exc:  # noqa: BLE001 - the run reads the notes itself
        logger.warning("part matches: the reading was not handed to the run (%s)", type(exc).__name__)
        return None


#: NOTE-26: the reading whose count line was said (one line per reading, never one for notes since changed)
COUNT_SAID = "part_cost_said.json"


def count_owed(session: Any) -> dict | None:
    """NOTE-26: the cost of the reading of the notes as they stand, when it has landed and its line is not said yet:
    {"key", "cost"}; None otherwise (still costing, nothing to say, said, or the notes changed). Never raises."""
    try:
        from meshpipeline.application.geometry_check import read_check
        sid = str(getattr(session, "id", "") or "")
        seen = parts_seen(read_check(sid))
        stored = _read(sid, MATCHES)
        if seen is None or stored is None or stored.get("status") != "done" or not stored.get("cost"):
            return None
        notes = notes_of(getattr(session, "user_notes", None), getattr(session, "request_txt", None))
        if stored.get("key") != key_of(notes, seen["table"]):
            return None
        if (_read(sid, COUNT_SAID) or {}).get("key") == stored["key"]:
            return None
        return {"key": stored["key"], "cost": dict(stored["cost"])}
    except Exception as exc:  # noqa: BLE001 - no line said
        logger.warning("part matches: the count line was not looked up (%s)", type(exc).__name__)
        return None


def count_said(session_id: str, key: str) -> None:
    """NOTE-26: the count line of the reading `key` was said (or had nothing to say): it is never said again."""
    _write(session_id, COUNT_SAID, {"key": key, "at": time.time()})


__all__ = ["CARD_PICTURES", "CARD_VIEWS", "MODEL", "NOTES_ONLY", "before_run", "finalize", "flow_route", "notes_lines",
           "read_notes", "card_picture", "model_bytes", "readable", "MATCHES", "PARTS_KEY", "PICKS", "PICTURE_NAMES", "ROUTE_CFD", "ROUTE_FEA", "copy_parts",
           "enqueue_reading", "for_run", "key_of", "notes_of", "parts_seen", "parts_words", "pick", "picture_keys",
           "rows_of", "run_reading", "size_words", "store_parts", "view", "with_faces", "with_picks"]
