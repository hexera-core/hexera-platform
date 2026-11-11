# Responsibility: THE TEAM'S MESHING STANDARD (docs/RULES.md STD-1..3): an organisation's standing mesh requirements, written the way an engineer writes the notes and read by the SAME reader (engines/mesh_requirements: its prompt, its tool, its checks) into the SAME typed set - then merged, once per run, with that run's notes: our rules < the settings < the notes, field by field, with where every value came from kept and told ("From your settings: ..." / "From your notes: ...").
# Boundaries: the ONE place the merge happens; every route calls decide() where it read the notes before (engines/fea_driver.read_requirements today; the flow route's engines/cfd_requirements.read_for_attempt next). It writes no mesher setting and changes no rule: a route applies the merged set exactly as it applies the notes' set. A model that is off, slow, wrong or down never fails a run: the reading kept when the standard was saved, else the decision cache's, else nothing of the standard applied, and said. No standard: decide() returns the notes' own section, untouched - byte for byte the run it always was.
# Collaborates with: engines/mesh_requirements (the reader, its words and its report rows), contracts/user_notes (the standard is shown to the model as a note of its own source), application/meshing_standard_service.py (save-time reading, the settings page's lines, the dispatch snapshot), engines/fea_driver.py (calls decide and remark).
"""THE TEAM'S MESHING STANDARD.

    STD-1  ONE READER. The standard is read by engines/mesh_requirements' own prompt, tool and checks, shown as a note
           of its own ("your team's meshing standard, from settings") with no route in the question, so the reading
           made when it is SAVED is the reading every run uses: kept with the standard (the model's answer and the key
           of the question it answered) and re-checked by the rules at run time with no model call. The question
           changed (a new prompt or tool) or nothing kept (the model was down at save time): the run reads it, through
           the same route and decision cache as the notes, with the notes' own fallbacks.
    STD-2  RULES < SETTINGS < NOTES, FIELD BY FIELD. A field is a size of one kind (global, shortest edge, ...), one
           element-kind choice (quads or triangles, the share of triangles, the order), a quality limit per measure,
           element shape and direction (and, on the flow route, each of its own fields and each named place). What
           the run's notes set wins for that run; every other field the standard sets stays. Lengths the notes give
           with no unit take the unit the standard names. A shortest edge written as a multiple of the global size
           follows the global size in force. Where a value of the standard contradicts one of the notes (a shortest
           edge over the notes' global size, quads only against a share of triangles) the notes' value is kept and
           the standard's is told as not applied. Every row of the set says where it came from.
    STD-3  NOTHING IN EITHER, NOTHING CHANGES. With no standard the notes' section is returned as it was read: every
           plan, mesh and record is byte for byte the run before the standard existed.

A per-part size stays the notes' own (engines/fea_part_sizes reads the notes' phrases against the parts): a part the
notes size keeps its size, and a global size from the standard covers the rest (engines/fea_requirements.whole_sizes).
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.engines import mesh_requirements as MR

logger = logging.getLogger(__name__)

RULE_READ = "STD-1"
RULE_MERGE = "STD-2"
RULE_NOTHING = "STD-3"
#: the pipeline state field and the dispatch argument that carry the standard into a run
STATE_KEY = "meshing_standard"
#: the source the standard's text is shown to the model under (contracts/user_notes.SOURCE_SETTINGS)
SOURCE = "settings"
#: the most of a standard kept: every word of it is read (the reader shows the model MR.MAX_CHARS)
MAX_CHARS = MR.MAX_CHARS
#: how long the reading at save time may take before the text is saved unread (it is read at run time)
SAVE_WAIT_S = 60.0

#: what is said when the standard could not be read for a run
UNREAD_WORDS = ("Your team's meshing settings were not applied this run: the AI step that reads them gave no "
                "answer.")
OFF_WORDS = ("Your team's meshing settings were not applied this run: the AI step that reads them is switched "
             "off.")
#: a standard's words for one part, a count, or shell or solid: the run's notes are where those are read
ELSEWHERE_WORDS = "put sizes for one part, an element count, or shell or solid in a run's notes"


# #
# the standard as a run carries it
# #

def snapshot(text: str, reading: Mapping | None, *, updated_by: str = "", updated_at: str = "") -> dict | None:
    """What a run is dispatched with: the text, the reading kept at save time, who saved it and when. None with no
    words (a run without a standard carries nothing at all)."""
    t = str(text or "").strip()
    if not t:
        return None
    out: dict = {"text": t[:MAX_CHARS]}
    if isinstance(reading, Mapping) and isinstance(reading.get("arguments"), str) and reading.get("key"):
        out["reading"] = {"key": str(reading["key"]), "arguments": str(reading["arguments"])}
    if updated_by:
        out["updated_by"] = str(updated_by)
    if updated_at:
        out["updated_at"] = str(updated_at)
    return out


def standard_of(state: Mapping | None) -> dict | None:
    """The standard the run was dispatched with, or None when it has none (STD-3)."""
    got = (state or {}).get(STATE_KEY)
    if not isinstance(got, Mapping) or not str(got.get("text") or "").strip():
        return None
    return dict(got)


def _state(text: str, user_id: str = "") -> dict:
    return {"user_notes": [{"text": str(text), "source": SOURCE}], "user_id": str(user_id or "")}


def messages(text: str) -> list[dict]:
    """The question the reader asks about the standard: the notes' own, with the standard as the one note and no
    route (STD-1: the same question at save time and on every route, so one reading serves them all)."""
    return MR.messages_for(_state(text), route="")


def key_of(text: str) -> str:
    """The key of the question about these words (what a kept reading is matched by)."""
    return MR._key(messages(text), MR.tool())


def words_read(text: str) -> str:
    """The words the reader's checks hold the answer to (the standard as the model is shown it)."""
    return MR.notes_text(_state(text))


#: whether the reader's check takes the route (the flow branch's NOTE-10 adds it): asked once, so this module calls
#: the reader the way the reader is written today and the way it is about to be
_ROUTED = "route" in inspect.signature(MR.check).parameters


def _check(arguments: str, text: str, route: str) -> tuple[dict, list[str], list[str]]:
    extra: dict[str, str] = {"route": route} if _ROUTED else {}
    return MR.check(arguments, words_read(text), **extra)


#: the words that name a length unit once for all, for the stated unit the model reads (it gives no quote)
_UNIT_SAID = {"mm": r"\bmm\b|millimet", "cm": r"\bcm\b|centimet", "m": r"\bmetres?\b|\bmeters?\b|\bunits?\s*[:=]?\s*m\b",
              "in": r"\binch|\binches\b|\bunits?\s*[:=]?\s*in\b", "um": r"\bum\b|µm|μm|micron"}
_UNIT_NAME = {"mm": "millimetres", "cm": "centimetres", "m": "metres", "in": "inches", "um": "microns"}


def _stated_unit(arguments: str, text: str) -> str | None:
    """The length unit the standard gives once for all its numbers, when the model read one AND the standard says
    it in words (the model gives no quote for it, so the rules look for the word)."""
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return None
    unit = str((raw or {}).get("stated_unit") or "none").strip().lower() if isinstance(raw, dict) else "none"
    if unit not in MR.UNITS_MM or not re.search(_UNIT_SAID[unit], str(text or ""), re.I):
        return None
    return unit


def _elsewhere(arguments: str, text: str) -> list[str]:
    """The standard's words for one part, an element count, or shell or solid (the reader's "elsewhere"): a run's
    notes are where those are read, so the standard tells them back as not applied rather than drop them."""
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return []
    said = words_read(text)
    norm, squashed = MR._norm(said), MR._squash(said)
    out: list[str] = []
    for e in (raw.get("other") if isinstance(raw, dict) and isinstance(raw.get("other"), list) else []):
        if not isinstance(e, dict) or str(e.get("kind")) != "elsewhere":
            continue
        quote = " ".join(str(e.get("quote") or "").split())[:400]
        if quote and MR._quoted(quote, norm, squashed) and quote not in out:
            out.append(quote)
    return out[:20]


#: the solver files every FEA run delivers (engines/fea_writers.SOLVERS and the shell's): a standard that names one
#: is told it is delivered, never "not applied" (the user picks the file at download)
_SOLVERS = (("Nastran", r"\bnastran\b|\bbdf\b", ".bdf"), ("Abaqus", r"\babaqus\b", ".inp"),
            ("CalculiX", r"\bcalculix\b|\bccx\b", ".inp"), ("Ansys", r"\bansys\b|\bmapdl\b|\bcdb\b", ".cdb"))
#: words that make a line about a solver a CHECK of that solver's, not the file it reads ("Nastran's own limits")
_NOT_A_FILE = re.compile(r"check|limit|quality|criteri|warp|aspect|jacobian|angle|skew|taper", re.I)
FLOW = "flow"


def _solver_files(quote: str) -> list[tuple[str, str]]:
    """[(solver, file extension)] a short line names as the files it wants ("Nastran BDF output"), else []."""
    q = str(quote or "")
    if len(q.split()) > 6 or _NOT_A_FILE.search(q):
        return []
    return [(name, ext) for name, rx, ext in _SOLVERS if re.search(rx, q, re.I)]


def checked(arguments: str, text: str, *, route: str = "", key: str = "", source: str = "") -> dict:
    """The standard's section from one answer of the model, checked by the reader's rules: the notes' section shape
    ("used", its requirements, what was dropped and not applied) and the unit it names, its words for elsewhere, and
    the solver files it names that every FEA run delivers."""
    req, dropped, not_applied = _check(arguments, text, route)
    unit = _stated_unit(arguments, text)
    if unit:
        # "Units: inches" is said as the standard's unit, not again as a thing no setting matches
        not_applied = [q for q in not_applied if not (len(q.split()) <= 5 and re.search(_UNIT_SAID[unit], q, re.I))]
    files: list[list[str]] = []
    if route != FLOW:
        for q in list(not_applied):
            got = _solver_files(q)
            if got:
                files.append([q, *[f"{name} ({ext})" for name, ext in got]])
                not_applied.remove(q)
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied, key=key, source=source)
    sec["stated_unit"] = unit
    sec["elsewhere"] = _elsewhere(arguments, text)
    sec["solver_files"] = files
    return sec


# #
# the reading: kept at save time, reused at run time (STD-1)
# #

async def read_at_save(text: str, *, user_id: str = "", wait_s: float = SAVE_WAIT_S) -> dict | None:
    """The reading made when the standard is saved: {"key", "arguments", "read_at"}, or None when the model gave no
    usable answer in time (the standard is saved anyway and read at the start of each run). Never raises."""
    t = str(text or "").strip()
    if not t:
        return None
    from meshpipeline.contracts import model_inference as llm_router

    msgs, tool = messages(t), MR.tool()
    try:
        rr = await asyncio.wait_for(llm_router.call_builder_model(
            msgs, tools=[tool], tool_choice=MR._choice(tool), job_id="", user_id=str(user_id or "")),
            timeout=float(wait_s))
    except Exception as exc:  # noqa: BLE001 - an unread standard is still saved
        logger.warning("meshing standard: not read at save time (%s)", type(exc).__name__)
        return None
    if getattr(rr, "failure_marker", ""):
        logger.warning("meshing standard: not read at save time (%s)", rr.failure_marker)
        return None
    args = MR._args_of(rr)
    if args is None:
        return None
    return {"key": MR._key(msgs, tool), "arguments": args,
            "read_at": datetime.now(UTC).isoformat(timespec="seconds")}


def kept_reading(std: Mapping | None, text: str | None = None) -> str | None:
    """The answer kept with the standard when it is the answer to THIS question (same words, same prompt and tool),
    else None (the run reads it again)."""
    reading = (std or {}).get("reading")
    t = str(text if text is not None else (std or {}).get("text") or "").strip()
    if not t or not isinstance(reading, Mapping) or not isinstance(reading.get("arguments"), str):
        return None
    return str(reading["arguments"]) if str(reading.get("key") or "") == key_of(t) else None


async def read(std: Mapping, *, route: str = "", job_id: str = "", run=None, enabled: bool = True,
               wait_s: float = MR.TIMEOUT_S, user_id: str = "") -> dict:
    """The standard's section for this run: from the reading kept at save time (no model asked), else asked as the
    notes are (the builder route; the decision cache replays it), else its saved round, else nothing of it applied
    ("failed" / "invalid" / "no_time" / "off"), said. Never raises (but a fenced worker's stale publish)."""
    text = str(std.get("text") or "").strip()
    msgs, tool = messages(text), MR.tool()
    key = MR._key(msgs, tool)
    kept = kept_reading(std, text)
    if kept is not None:
        return checked(kept, text, route=route, key=key, source="kept")

    def answered(rr, source: str) -> dict | None:
        args = MR._args_of(rr) if rr is not None else None
        return None if args is None else checked(args, text, route=route, key=key, source=source)

    def without(status: str, why: str) -> dict:
        from meshpipeline.contracts.decision_cache import recorded_answer

        saved = answered(recorded_answer("builder", messages=msgs, tools=[tool], tool_choice=MR._choice(tool),
                                         job_id=job_id, user_id=user_id), "saved")
        if saved is not None:
            saved["note"] = (f"The AI was not available just now ({why}); its saved reading of your team's meshing "
                             "settings was used.")
            return saved
        logger.warning("meshing standard: no reading (%s) and none saved - not applied - job_id=%s", why, job_id)
        return MR.section(status, key=key, source="rules (nothing saved)", note=UNREAD_WORDS)

    if not enabled:
        return MR.section("off", key=key, source="rules", note=OFF_WORDS)
    if wait_s < MR.MIN_WAIT_S:
        return without("no_time", "no time to spare")
    from meshpipeline.contracts import execution_guard as _fence
    from meshpipeline.contracts import model_inference as llm_router

    await _fence.assert_still_owner("meshing standard")
    try:
        rr = await asyncio.wait_for(llm_router.call_builder_model(
            msgs, tools=[tool], tool_choice=MR._choice(tool), job_id=job_id, user_id=user_id),
            timeout=min(MR.TIMEOUT_S, float(wait_s)))
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the AI never fails a run
        return without("failed", type(exc).__name__)
    note = getattr(run, "note_plan_round", None)
    if callable(note):
        note(rr)
    if getattr(rr, "failure_marker", ""):
        return without("failed", str(rr.failure_marker))
    got = answered(rr, "model")
    return got if got is not None else without("invalid", "it gave no reading")


async def decide(state: Mapping, *, route: str = "", job_id: str = "", run=None, enabled: bool = True,
                 wait_s: float = MR.TIMEOUT_S) -> dict:
    """THE ONE CALL every route makes where it read the notes: the notes' section (engines/mesh_requirements.decide,
    exactly as before) merged with the team's standard. No standard: the notes' section itself (STD-3)."""
    std = standard_of(state)
    if std is None:
        return await MR.decide(state, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=wait_s)
    started = time.monotonic()
    user_id = str((state or {}).get("user_id") or "")
    try:
        settings = await read(std, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=wait_s,
                              user_id=user_id)
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the standard never fails a run
        logger.warning("meshing standard: not read (%s) - job_id=%s", type(exc).__name__, job_id)
        settings = MR.section("failed", source="rules", note=UNREAD_WORDS)
    left = max(0.0, float(wait_s) - (time.monotonic() - started))
    notes = await MR.decide(state, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=left)
    return merge(settings, notes)


# #
# the merge (STD-2)
# #

_KIND_SLOT = {"quads_only": "shell", "triangles_only": "shell", "max_triangle_percent": "tria_percent_max",
              "linear": "order", "quadratic": "order"}
_SIZE_KEY = {"global": "global_mm", "min_edge": "min_edge_mm", "max_edge": "max_edge_mm", "thickness": "thickness_mm"}
_MULTIPLE = re.compile(r"\(([0-9.eE+-]+) x the global size\)")
_NO_UNIT = " (no unit was written: read as millimetres)"


def _entry(req: Mapping, list_key: str, rid: str) -> Mapping:
    return next((e for e in req.get(list_key) or [] if isinstance(e, Mapping) and e.get("id") == rid), {})


def _slot(row: Mapping, req: Mapping) -> tuple:
    """The FIELD a row sets: two rows of one slot are one field, and the notes' wins."""
    field, rid = str(row.get("field") or ""), str(row.get("id") or "")
    if field.startswith("limit:"):
        lim = _entry(req, "limits", rid)
        return ("limit", lim.get("measure"), lim.get("shape"), lim.get("op"))
    if field == "local":
        place = _entry(req, "local", rid)
        return ("local", " ".join(str(place.get("where") or "").casefold().split()))
    if field in _KIND_SLOT:
        return ("kind", _KIND_SLOT[field])
    return ("row", field)


def _reid(req: dict, prefix: str) -> None:
    """The standard's rows and entries numbered s1, s2, ... (the notes' keep r1, r2, ...): no two rows of the merged
    set share an id."""
    new = {str(r["id"]): f"{prefix}{i}" for i, r in enumerate(req.get("rows") or [], start=1)}
    for r in req.get("rows") or []:
        r["id"] = new[str(r["id"])]
    for k, v in req.items():
        if k != "rows" and isinstance(v, list):
            for e in v:
                if isinstance(e, dict) and str(e.get("id")) in new:
                    e["id"] = new[str(e["id"])]


def _drop_row(req: dict, row: Mapping) -> None:
    """Take a row and the value it set out of a set."""
    field, rid = str(row.get("field") or ""), row.get("id")
    req["rows"] = [r for r in req.get("rows") or [] if r.get("id") != rid]
    for k, v in req.items():
        if k != "rows" and isinstance(v, list):
            req[k] = [e for e in v if not (isinstance(e, Mapping) and e.get("id") == rid)]
    if field in _SIZE_KEY:
        req["sizes"][_SIZE_KEY[field]] = None
    elif field == "growth":
        req["sizes"]["growth"] = None
    elif field in _KIND_SLOT:
        req["kind"][_KIND_SLOT[field]] = None


def _size_row(req: dict, field: str) -> dict | None:
    return next((r for r in req.get("rows") or [] if r.get("field") == field), None)


def _set_size(row: dict, req: dict, mm: float, words: str) -> None:
    req["sizes"][_SIZE_KEY[str(row["field"])]] = float(mm)
    row["asked"] = words


def _in_unit(notes: dict, unit: str, told: list[str]) -> None:
    """STD-2: the notes' lengths written with no unit are in the unit the standard names (the notes' own unit, when
    they give one, was already used by the reader)."""
    per = MR.UNITS_MM[unit]
    for row in list(notes.get("rows") or []):
        field = str(row.get("field") or "")
        asked = str(row.get("asked") or "")
        if field not in _SIZE_KEY or not asked.endswith(_NO_UNIT) or _MULTIPLE.search(asked):
            continue
        value = float(notes["sizes"][_SIZE_KEY[field]])
        mm = value * per
        if not (MR.SIZE_RANGE_MM[0] <= mm <= MR.SIZE_RANGE_MM[1]):
            told.append(f'"{MR._short(row.get("quote"))}": {MR._g(mm)} mm is not an element size anyone means: not '
                        "applied")
            _drop_row(notes, row)
            continue
        words = MR._size_words(field, mm, None, unit, value)
        _set_size(row, notes, mm, f"{words} (no unit was written: {_UNIT_NAME[unit]}, as your settings say)")


def _follow_global(req: dict, glob: float | None) -> None:
    """A shortest or longest edge written as a multiple of the global size follows the global size in force."""
    if glob is None:
        return
    for field in ("min_edge", "max_edge"):
        row = _size_row(req, field)
        m = _MULTIPLE.search(str(row.get("asked") or "")) if row else None
        if row is None or m is None:
            continue
        k = float(m.group(1))
        _set_size(row, req, k * glob, MR._size_words(field, k * glob, k, "x_global", k))


def merge(settings: Mapping | None, notes: Mapping) -> dict:
    """THE MERGED SECTION a route applies (STD-2): the notes' section shape, every row marked with where it came
    from ("settings" / "notes"), the lines said before meshing worked out once here (mesh_requirements.words reads
    them), and the merge itself kept for the why record. No standard: the notes' section, the same object (STD-3)."""
    if not settings or settings.get("status") == "none":
        return notes  # type: ignore[return-value]
    s_used, n_used = settings.get("status") == "used", notes.get("status") == "used"
    S = copy.deepcopy(dict(settings.get("requirements") or MR.empty())) if s_used else MR.empty()
    N = copy.deepcopy(dict(notes.get("requirements") or MR.empty())) if n_used else MR.empty()
    told_s: list[str] = []          # the standard's values not applied because of the notes, and why
    told_n: list[str] = []          # the notes' values the standard's unit made unbuildable
    if s_used and n_used and settings.get("stated_unit"):
        _in_unit(N, str(settings["stated_unit"]), told_n)
    _reid(S, "s")
    for r in S.get("rows") or []:
        r["from"] = "settings"
    for r in N.get("rows") or []:
        r["from"] = "notes"

    # field by field: a slot the notes set is theirs this run
    taken = {_slot(r, N) for r in N.get("rows") or []}
    overridden = [dict(r) for r in S.get("rows") or [] if _slot(r, S) in taken]
    for r in overridden:
        _drop_row(S, r)
    out = copy.deepcopy(S)
    for k, v in N.items():
        if k == "rows":
            continue
        if isinstance(v, dict):
            base = dict(out.get(k) or {})
            out[k] = {**base, **{sub: x for sub, x in v.items() if x is not None}}
        elif isinstance(v, list):
            out[k] = [*list(out.get(k) or []), *copy.deepcopy(v)]
        elif v is not None:
            out[k] = v
    out["rows"] = [*list(S.get("rows") or []), *copy.deepcopy(list(N.get("rows") or []))]

    # a multiple of the global size follows the global size in force
    if N["sizes"].get("global_mm") is not None:
        _follow_global(out, out["sizes"].get("global_mm"))

    def source(field: str) -> str | None:
        row = _size_row(out, field)
        return str(row["from"]) if row else None

    def not_the_notes(field: str, why: str) -> None:
        row = _size_row(out, field)
        if row is not None and row.get("from") == "settings":
            told_s.append(f"{row['asked']} - {why}")
            _drop_row(out, row)

    # contradictions between the two: the notes' value is kept, the standard's told (within one source the reader
    # already settled them)
    s = out["sizes"]
    lo, hi, glob = s.get("min_edge_mm"), s.get("max_edge_mm"), s.get("global_mm")
    if lo is not None and hi is not None and lo >= hi and source("min_edge") != source("max_edge"):
        bad = "min_edge" if source("min_edge") == "settings" else "max_edge"
        not_the_notes(bad, f"it does not fit the {'longest' if bad == 'min_edge' else 'shortest'} edge your notes "
                           "ask for")
    lo, hi, glob = s.get("min_edge_mm"), s.get("max_edge_mm"), s.get("global_mm")
    for field, off in (("min_edge", lo is not None and glob is not None and lo > glob),
                       ("max_edge", hi is not None and glob is not None and hi < glob)):
        if off and source(field) != source("global"):
            if source(field) == "settings":
                not_the_notes(field, f"it does not fit the {MR._g(glob)} mm global size your notes ask for")
            else:
                not_the_notes("global", "it does not fit the edge lengths your notes ask for")
    k = out["kind"]
    shell_row = next((r for r in out["rows"] if r.get("field") in ("quads_only", "triangles_only")), None)
    share_row = next((r for r in out["rows"] if r.get("field") == "max_triangle_percent"), None)
    if shell_row is not None and share_row is not None and shell_row["from"] != share_row["from"]:
        # quads only, triangles only and a share of triangles are one choice: the notes' is this run's
        loser = shell_row if shell_row["from"] == "settings" else share_row
        overridden.append(dict(loser))
        _drop_row(out, loser)
    if k.get("shell") == "quads" and k.get("tria_percent_max") is not None:
        # the reader's own rule: quads with a share of triangles is the share
        k["shell"] = None
        out["rows"] = [r for r in out["rows"] if r.get("field") != "quads_only"]

    if s_used or n_used:
        status = "used"
    else:
        # neither was read: the standard's reason when the notes asked nothing, else the notes'
        status = str(settings.get("status") if notes.get("status") in (None, "none") else notes.get("status"))
    n_dropped = list(notes.get("dropped") or []) if n_used else []
    s_dropped = list(settings.get("dropped") or []) if s_used else []
    n_not = list(notes.get("not_applied") or []) if n_used else []
    s_not = list(settings.get("not_applied") or []) if s_used else []
    dropped = [*n_dropped, *told_n, *[f"from your settings - {d}" for d in (*s_dropped, *told_s)]]
    not_applied = [*n_not, *[f"from your settings - {q}" for q in s_not]]
    sec = MR.section(status, requirements=out if status == "used" else None, dropped=dropped,
                     not_applied=not_applied, note=str(notes.get("note") or "") if n_used else "",
                     key=str(notes.get("key") or settings.get("key") or ""),
                     source=str(notes.get("source") or "") if n_used else str(settings.get("source") or ""))
    sec["standard"] = {"status": settings.get("status"), "source": settings.get("source"),
                       "key": settings.get("key"), "unit": settings.get("stated_unit"),
                       "overridden": [{"field": r.get("field"), "asked": r.get("asked")} for r in overridden],
                       "from": {str(r["id"]): r["from"] for r in out.get("rows") or []}}
    sec["lines"] = lines(settings, notes, out, told_s=told_s, told_n=told_n)
    if status != "used":
        sec["note"] = " ".join(sec["lines"]) or UNREAD_WORDS
    return sec


def lines(settings: Mapping, notes: Mapping, merged: Mapping, *, told_s=(), told_n=()) -> list[str]:
    """Before meshing, in plain words: one line per value in force, said by where it came from, then what was not
    applied - the notes' lines exactly as the reader says them, the standard's marked as the standard's."""
    s_used, n_used = settings.get("status") == "used", notes.get("status") == "used"
    out: list[str] = []
    if not s_used and settings.get("status") not in (None, "none"):
        out.append(str(settings.get("note") or UNREAD_WORDS))
    if not n_used and notes.get("status") not in (None, "none"):
        out.append(str(notes.get("note") or MR.UNREAD_WORDS))
    rows = list(merged.get("rows") or [])
    unit = settings.get("stated_unit") if s_used else None
    out += [f"From your settings: {r['asked']}." for r in rows if r.get("from") == "settings"]
    if unit:
        out.append(f"From your settings: lengths with no unit are in {_UNIT_NAME[str(unit)]}.")
    if s_used:
        for _quote, *files in settings.get("solver_files") or []:
            out.append(f"From your settings: {' and '.join(files)} files - every FEA run delivers them.")
    out += [f"From your notes: {r['asked']}." for r in rows if r.get("from") == "notes"]
    for sec in (settings, notes):
        if sec.get("status") == "used" and sec.get("source") == "saved" and sec.get("note"):
            out.append(str(sec["note"]))
    if n_used:
        out += [f"Not applied: {d}." for d in notes.get("dropped") or []]
        out += [f'Not applied: "{MR._short(q)}" - no mesh setting of ours matches it yet.'
                for q in notes.get("not_applied") or []]
    out += [f"Not applied: {d}." for d in told_n]
    if s_used:
        out += [f"Not applied (your settings): {d}." for d in settings.get("dropped") or []]
        out += [f"Not applied (your settings): {d}." for d in told_s]
        out += [f'Not applied (your settings): "{MR._short(q)}" - no mesh setting of ours matches it yet.'
                for q in settings.get("not_applied") or []]
        out += [f'Not applied (your settings): "{MR._short(q)}" - {ELSEWHERE_WORDS}.'
                for q in settings.get("elsewhere") or []]
    return out


# #
# what the settings page shows, what the result's line leads with, the why record
# #

def understood(text: str, reading: Mapping | None) -> list[str]:
    """THE SETTINGS PAGE'S CONFIRMATION: what the reader understood, one plain line per item, exactly as a run says
    it ("Global element size 10 mm.", "Aspect ratio under 4 (up to 1% of elements between 4 and 6)."), then the
    "Not applied" lines with the user's own words quoted. [] when there is nothing kept to show."""
    t = str(text or "").strip()
    args = reading.get("arguments") if isinstance(reading, Mapping) else None
    if not t or not isinstance(args, str):
        return []
    sec = checked(args, t, key=str((reading or {}).get("key") or ""), source="kept")
    merged = copy.deepcopy(dict(sec.get("requirements") or MR.empty()))
    for r in merged.get("rows") or []:
        r["from"] = "settings"
    out = []
    for line in lines(sec, MR.section("none"), merged):
        for lead in ("From your settings: ", "Not applied (your settings): "):
            if line.startswith(lead):
                line = line[len(lead):]
                line = (line[:1].upper() + line[1:]) if lead.startswith("From") else f"Not applied: {line}"
        out.append(line)
    return out


def remark(sec: Mapping | None, line: str) -> str:
    """The result's one closing line ("all 5 things you asked for are met"), led by where the things asked came
    from: unchanged with no standard (the result says "From your notes: ..." as it always has)."""
    if not line or not isinstance(sec, Mapping) or not sec.get("standard"):
        return line
    got = set(((sec.get("standard") or {}).get("from") or {}).values())
    if got == {"settings"}:
        return f"From your settings: {line}"
    if "settings" in got:
        return f"From your settings and notes: {line}"
    return line


def size_remark(sec: Mapping | None, line: str) -> str:
    """The plan's line about a size for the whole mesh ("You asked for 4-12 mm elements on the whole mesh: ..."), led
    by where the sizes came from; unchanged with no standard, for any other line, or when the notes set them all."""
    if not line or "the whole mesh" not in line or not isinstance(sec, Mapping) or not sec.get("standard"):
        return line
    rows = ((sec.get("requirements") or {}).get("rows") or []) if isinstance(sec.get("requirements"), Mapping) else []
    got = {str(r.get("from")) for r in rows if r.get("field") in ("global", "min_edge", "max_edge")}
    if got == {"settings"}:
        return f"From your settings: {line}"
    return f"From your settings and notes: {line}" if "settings" in got else line


def decisions(sec: Mapping | None, stage: str = "notes") -> list[dict]:
    """The why-record entry of the merge (contracts/decision_record.cite shape): [] with no standard."""
    std = (sec or {}).get("standard") if isinstance(sec, Mapping) else None
    if not std:
        return []
    from meshpipeline.contracts.decision_record import SOURCE_AI_LEVER

    came = dict(std.get("from") or {})
    n_s = sum(1 for v in came.values() if v == "settings")
    return [{"stage": stage, "decided": "meshing standard merged with the notes", "value": n_s,
             "rule": RULE_MERGE if std.get("status") == "used" else RULE_READ, "source": SOURCE_AI_LEVER,
             "inputs": {"settings": {"status": std.get("status"), "from": std.get("source"), "key": std.get("key"),
                                     "unit": std.get("unit")},
                        "from": came, "overridden": list(std.get("overridden") or [])},
             "words": (f"{n_s} requirement{'' if n_s == 1 else 's'} from your team's meshing settings, "
                       f"{len(came) - n_s} from your notes; your notes win field by field.")}]


__all__ = ["ELSEWHERE_WORDS", "MAX_CHARS", "OFF_WORDS", "RULE_MERGE", "RULE_NOTHING", "RULE_READ", "SAVE_WAIT_S",
           "SOURCE", "STATE_KEY", "UNREAD_WORDS", "checked", "decide", "decisions", "kept_reading", "key_of", "lines",
           "merge", "messages", "read", "read_at_save", "remark", "size_remark", "snapshot", "standard_of", "understood",
           "words_read"]
