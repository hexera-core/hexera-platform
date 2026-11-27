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
    STD-4  THE STAGE HAS THE LAST WORD ON A SHELL'S ELEMENTS. The shell line's "Elements: Quads | Triangles" (SHSTAGE-8,
           application/shell_elements) is one more source of the same field, after the notes: rules < settings < notes
           < the stage, for the shell element kind only. Triangles: triangles only, whatever the notes or settings
           said of quads (told as not applied). Quads: quad-dominant, as the rules make it - a "quads only" or a share
           of triangles from the notes or settings stays (Quads covers both); only a "triangles only" gives way. No
           click on the stage (the control left where it started): nothing changes.

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
#: a standard's words for one part, or shell or solid: the run's notes are where those are read (an element or cell
#: count is the standard's own default for every run, STD-1)
ELSEWHERE_WORDS = "put sizes for one part, or shell or solid, in a run's notes"


# #
# the standard as a run carries it
# #

def snapshot(text: str, reading: Mapping | None, *, updated_by: str = "", updated_at: str = "",
             shell_elements: str = "quads", shell_elements_set: bool = False) -> dict | None:
    """What a run is dispatched with: the text, the reading kept at save time, who saved it and when, and the page's
    switch when the user SET it (STD-4: Triangles, or Quads chosen - `shell_elements_set`, 0025; the switch left on
    its default Quads is not carried, so the typed words decide). None with no words and the switch on Quads (a run
    without a standard carries nothing at all)."""
    t = str(text or "").strip()
    tri = pick_of(shell_elements) == TRIANGLES
    if not t and not tri:
        return None
    out: dict = {"text": t[:MAX_CHARS]}
    if tri:
        out["shell_elements"] = TRIANGLES
    elif shell_elements_set and pick_of(shell_elements) == QUADS:
        out["shell_elements"] = QUADS
        out["shell_elements_set"] = True
    if isinstance(reading, Mapping) and isinstance(reading.get("arguments"), str) and reading.get("key"):
        out["reading"] = {"key": str(reading["key"]), "arguments": str(reading["arguments"])}
    if updated_by:
        out["updated_by"] = str(updated_by)
    if updated_at:
        out["updated_at"] = str(updated_at)
    return out


def snapshot_key(std: Mapping | None) -> str:
    """Which saved standard a count was planned with: a digest of its words, kept reading and switch ("" for none)."""
    import hashlib
    if not isinstance(std, Mapping):
        return ""
    return hashlib.sha256(json.dumps({"t": std.get("text"), "r": std.get("reading"), "s": std.get("shell_elements")},
                                     sort_keys=True, default=str).encode()).hexdigest()[:16]


def standard_of(state: Mapping | None) -> dict | None:
    """The standard the run was dispatched with, or None when it has none (STD-3)."""
    got = (state or {}).get(STATE_KEY)
    if not isinstance(got, Mapping) or not (str(got.get("text") or "").strip() or switch_of(got) == TRIANGLES):
        return None
    return dict(got)


def switch_of(std: Mapping | None) -> str:
    """STD-4: the settings page's switch as a standard carries it: "triangles", else "quads" (its default)."""
    return TRIANGLES if pick_of((std or {}).get("shell_elements")) == TRIANGLES else QUADS


def switch_chosen(std: Mapping | None) -> str:
    """STD-4: the page's switch when the user SET it - "triangles" (never the default), "quads" chosen (carried with
    `shell_elements_set`) - else "" (left at its default: the standard's typed words decide the element kind)."""
    got = pick_of((std or {}).get("shell_elements"))
    if got == TRIANGLES:
        return TRIANGLES
    return QUADS if got == QUADS and (std or {}).get("shell_elements_set") is True else ""


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
    other = raw.get("other") if isinstance(raw, dict) else None
    for e in other if isinstance(other, list) else []:
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


#: the answer's lists whose entries quote one line each (the reader's tool): STD-5 sorts them by kind of mesh
_KEEP_LISTS = ("other", "strictness")


def _for_route(arguments: str, route: str) -> tuple[str, list[str]]:
    """STD-5 BEFORE THE READER'S RULES: the entries of the answer whose own line is tagged for other kinds of mesh than
    this run's ("CFD: base cell 10 mm" on a solid run) are set aside first, so the reader's "the later one wins" never
    lets a flow line replace a structural one of the same field (H02: "FEA: global size 4 mm ... / CFD: base cell 10
    mm" lost the 4 mm on a solid run). Returns (the answer for this route, the quotes set aside). Only a LINE's tag
    sorts (the list's whole tag is merge()'s); no route, no tag, or nothing set aside: the answer itself."""
    if route not in (FLOW, "fea"):
        return arguments, []
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return arguments, []
    tag = raw.get("applies_to") if isinstance(raw, dict) else None
    lines_ = tag.get("lines") if isinstance(tag, Mapping) else None
    if not lines_:
        return arguments, []

    def line_kinds(quote: str) -> list[str] | None:
        rq = MR._squash(quote)
        for ln in lines_:
            lq = MR._squash(str((ln or {}).get("quote") or "")) if isinstance(ln, Mapping) else ""
            if lq and rq and (lq in rq or rq in lq):
                return list(ln.get("kinds") or ["any"])
        return None

    gone: list[str] = []
    for k, v in list(raw.items()):
        if k in _KEEP_LISTS or k == "applies_to" or not isinstance(v, list):
            continue
        kept = []
        for e in v:
            q = str(e.get("quote") or "") if isinstance(e, Mapping) else ""
            kinds = line_kinds(q) if q else None
            if kinds is not None and not _fits(kinds, route):
                if q not in gone:
                    gone.append(q)
                continue
            kept.append(e)
        raw[k] = kept
    return (json.dumps(raw), gone) if gone else (arguments, [])


def checked(arguments: str, text: str, *, route: str = "", key: str = "", source: str = "") -> dict:
    """The standard's section from one answer of the model, checked by the reader's rules: the notes' section shape
    ("used", its requirements, what was dropped and not applied) and the unit it names, its words for elsewhere, and
    the solver files it names that every FEA run delivers."""
    arguments, other_kinds = _for_route(arguments, route)
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
        # the model files "Nastran BDF output" as no mesh setting (its prompt: the solver the mesh is for): delivered too
        nm = (req or {}).get("not_mesh") or []
        for e in list(nm):
            got = _solver_files(str(e.get("quote") or ""))
            if got:
                files.append([str(e.get("quote")), *[f"{name} ({ext})" for name, ext in got]])
                nm.remove(e)
        if not nm:
            (req or {}).pop("not_mesh", None)
    elsewhere = _elsewhere(arguments, text)
    if route == "fea":
        # STD-1: on the FEA routes only the run's notes are read for a size in one place (cad/part_words): the
        # standard's is told to go there, never said as set and left unapplied. Its element COUNT stays: it is the
        # default count of every run (engines/fea_driver.element_target, PLAN-fea-27 / SHMESH-23)
        for row in [r for r in req.get("rows") or [] if r.get("field") == "local"]:
            _drop_row(req, row)
            if row.get("quote") and row["quote"] not in elsewhere:
                elsewhere.append(str(row["quote"]))
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied, key=key, source=source)
    sec["stated_unit"] = unit
    sec["elsewhere"] = elsewhere
    sec["solver_files"] = files
    if other_kinds:
        # STD-5: told with merge()'s line, as the lines written for other kinds of mesh
        sec["other_kinds"] = other_kinds
    return sec


# #
# the standard's design intent, handed to the run's AI steps as guidance (STD-6)
# #

RULE_GUIDANCE = "STD-6"
#: how a line of the standard that is no setting is told, in the three places lines are told
GUIDANCE_WORDS = MR.GUIDANCE_WORDS
GUIDANCE_LEAD = MR.GUIDANCE_LEAD


def guidance_split(sec: Mapping | None, route: str) -> tuple[list[dict], list[str]]:
    """STD-6: the standard's lines that are no setting - what the reader filed as no mesh setting of ours ("we care
    about fatigue at the fillets") and as not a mesh setting at all ("this goes into a crash model") - split by STD-5
    into those for this run's kind of mesh ([{"quote", "instead"}], `instead` '' or the reader's hint) and the quotes
    written for other kinds. ([], []) when the standard was not read."""
    s = sec if isinstance(sec, Mapping) else {}
    if s.get("status") != "used":
        return [], []
    req = s.get("requirements") if isinstance(s.get("requirements"), Mapping) else {}
    got: list[dict] = [{"quote": str(q), "instead": ""} for q in s.get("not_applied") or [] if str(q or "").strip()]
    for e in (req or {}).get("not_mesh") or []:
        if isinstance(e, Mapping) and str(e.get("quote") or "").strip():
            hint = str(e.get("instead") or "")
            got.append({"quote": str(e["quote"]), "instead": "" if hint == MR.NOT_MESH_DEFAULT else hint})
    kept: list[dict] = []
    off: list[str] = []
    for g in got:
        if any(k["quote"] == g["quote"] for k in kept) or g["quote"] in off:
            continue
        (kept.append(g) if _fits(_kinds_of({"quote": g["quote"]}, req or {}), route) else off.append(g["quote"]))
    return kept, off


def guidance_line(g: Mapping, *, page: bool = False) -> str:
    """STD-6 in plain words: 'From your settings, kept as guidance for the mesh planning: "..."' (on the settings
    page without its lead); a hint of the reader's ("a time limit is not a setting yet ...") rides along."""
    hint = f" ({g['instead']})" if g.get("instead") else ""
    lead = GUIDANCE_WORDS[:1].upper() + GUIDANCE_WORDS[1:] if page else GUIDANCE_LEAD
    return f'{lead}: "{MR._short(str(g.get("quote") or ""))}"{hint}.'


def route_of(state: Mapping | None) -> str:
    """The route a run's standard is read for: "fea" for a structural run (its saved purpose), else "flow"."""
    from meshpipeline.engines.fea_review import is_fea

    return "fea" if is_fea((state or {}).get("purpose")) else FLOW


def _section_for_guidance(std: Mapping, route: str, state: Mapping) -> dict | None:
    """The standard's section as the run read it, with no model call: the reading kept at save time, else the
    decision cache's saved answer to the same question (a run that read it again at run time); None with neither."""
    text = str(std.get("text") or "").strip()
    kept = kept_reading(std, text)
    if kept is None:
        from meshpipeline.contracts.decision_cache import recorded_answer

        tool = MR.tool()
        rr = recorded_answer("builder", messages=messages(text), tools=[tool], tool_choice=MR._choice(tool),
                             user_id=str((state or {}).get("user_id") or ""))
        kept = MR._args_of(rr) if rr is not None else None
    if kept is None:
        return None
    try:
        return checked(kept, text, route=route)
    except Exception:  # noqa: BLE001 - guidance is a courtesy: a run never fails on it
        logger.warning("meshing standard: guidance not worked out", exc_info=True)
        return None


def guidance(state: Mapping | None, route: str | None = None) -> list[dict]:
    """STD-6: the standard's lines that are no setting and are for this run's kind of mesh ([] with none)."""
    std = standard_of(state)
    if std is None or not str(std.get("text") or "").strip():
        return []
    r = route_of(state) if route is None else route
    sec = _section_for_guidance(std, r, state or {})
    return guidance_split(sec, r)[0] if sec else []


def guidance_notes(state: Mapping | None, route: str | None = None) -> list:
    """STD-6 THE NOTES A RUN'S AI STEPS READ: the run's own notes, with the standard's design intent (guidance())
    as one note of its own put FIRST - labelled as the team's saved settings, so the run's words, after it, win
    where they differ. No such line: the run's notes as stored, the same list (every prompt byte for byte as
    before, STD-3)."""
    own = list((state or {}).get("user_notes") or [])
    lines = guidance(state, route)
    if not lines:
        return (state or {}).get("user_notes") or []
    from meshpipeline.contracts import user_notes as un

    # no capture time: the same standard gives the same note (the decision cache keys a step's round by its prompt)
    return [{"text": "\n".join(str(g["quote"]) for g in lines), "source": un.SOURCE_TEAM, "turn": None, "at": ""},
            *own]


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
    return (await decide_and_base(state, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=wait_s))[0]


async def decide_and_base(state: Mapping, *, route: str = "", job_id: str = "", run=None, enabled: bool = True,
                          wait_s: float = MR.TIMEOUT_S) -> tuple[dict, dict]:
    """decide(), and the same standard merged with NO notes: (what the run applies, what it would apply had the user
    typed nothing). NOTE-26 costs a typed requirement against the second when nothing typed was costed before it.
    The standard is read once for both; no standard: (the notes' section, an empty one)."""
    std = standard_of(state)
    if std is None:
        got = await MR.decide(state, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=wait_s)
        return got, MR.section("none")
    started = time.monotonic()
    user_id = str((state or {}).get("user_id") or "")
    try:
        settings = (await read(std, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=wait_s,
                               user_id=user_id)) if str(std.get("text") or "").strip() else MR.section("none")
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the standard never fails a run
        logger.warning("meshing standard: not read (%s) - job_id=%s", type(exc).__name__, job_id)
        settings = MR.section("failed", source="rules", note=UNREAD_WORDS)
    # STD-4: the page's switch, when the user set it, is the settings' shell element kind (over their own words);
    # left at its default it sets nothing aside - "tria mesh only" typed in the box is built
    chosen = switch_chosen(std)
    if chosen:
        settings = switched(settings, chosen)
    left = max(0.0, float(wait_s) - (time.monotonic() - started))
    notes = await MR.decide(state, route=route, job_id=job_id, run=run, enabled=enabled, wait_s=left)
    return merge(settings, notes, route=route), merge(settings, MR.section("none"), route=route)


# #
# the merge (STD-2)
# #

_KIND_SLOT = {"quads_only": "shell", "triangles_only": "shell", "max_triangle_percent": "tria_percent_max",
              "linear": "order", "quadratic": "order", "free_mesh": "structure", "no_washers": "structure",
              "structured": "structure"}
_SIZE_KEY = {"global": "global_mm", "min_edge": "min_edge_mm", "max_edge": "max_edge_mm", "thickness": "thickness_mm",
             "near_wall": "near_wall_mm"}
#: the flow route's boundary-layer rows (NOTE-10) and the key each sets in the set's "layers"
_LAYER_KEY = {"layers:count": "count", "layers:first_height": "first_mm", "layers:total_thickness": "total_mm",
              "layers:growth": "growth", "layers:y_plus": "y_plus"}
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
    elif field in _LAYER_KEY and isinstance(req.get("layers"), dict):
        req["layers"][_LAYER_KEY[field]] = None
    elif field.startswith("cells:") and isinstance(req.get("cells"), dict):
        req["cells"][field.split(":", 1)[1]] = None
    elif field == "growth":
        req["sizes"]["growth"] = None
    elif field in _KIND_SLOT:
        req["kind"][_KIND_SLOT[field]] = None


def _size_row(req: dict, field: str) -> dict | None:
    return next((r for r in req.get("rows") or [] if r.get("field") == field), None)


def _set_size(row: dict, req: dict, mm: float, words: str) -> None:
    req["sizes"][_SIZE_KEY[str(row["field"])]] = float(mm)
    row["asked"] = words


def _in_unit(notes: dict, unit: str, told: list[str], *, cells: bool = False) -> None:
    """STD-2: the notes' lengths written with no unit are in the unit the standard names (the notes' own unit, when
    they give one, was already used by the reader). The flow route's lengths too (NOTE-10): the size at the walls, a
    boundary layer's first height and its stack, a size in one named place. `cells`: the flow route's words."""
    per = MR.UNITS_MM[unit]
    said = f" (no unit was written: {_UNIT_NAME[unit]}, as your settings say)"
    for row in list(notes.get("rows") or []):
        field = str(row.get("field") or "")
        asked = str(row.get("asked") or "")
        if not asked.endswith(_NO_UNIT) or _MULTIPLE.search(asked):
            continue
        place = None
        if field in _SIZE_KEY:
            value = notes["sizes"].get(_SIZE_KEY[field])
        elif field in ("layers:first_height", "layers:total_thickness"):
            value = (notes.get("layers") or {}).get(_LAYER_KEY[field])
        elif field == "local":
            place = next((e for e in notes.get("local") or [] if e.get("id") == row.get("id")), None)
            value = place.get("mm") if place and place.get("what") == "size" else None
        else:
            continue
        if value is None:
            continue
        mm = float(value) * per
        if not (MR.SIZE_RANGE_MM[0] <= mm <= MR.SIZE_RANGE_MM[1]):
            told.append(f'"{MR._short(row.get("quote"))}": {MR._g(mm)} mm is not '
                        f'{"a cell" if cells else "an element"} size anyone means: not applied')
            _drop_row(notes, row)
            continue
        if field in _SIZE_KEY:
            words = MR._size_words(field, mm, None, unit, float(value), cells=cells)
            _set_size(row, notes, mm, f"{words}{said}")
        elif place is not None:
            place["mm"] = mm
            where = str(place.get("where") or "")
            row["asked"] = f"{MR._local_words(where, 'size', mm, unit, float(value), cells=cells)}{said}"
        else:
            notes["layers"][_LAYER_KEY[field]] = mm
            row["asked"] = f"{MR._layer_words(field.split(':', 1)[1], float(value), mm, unit)}{said}"


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


#: a saved standard applies to the kinds of mesh it is written for (docs/RULES.md)
KINDS_RULE = "STD-5"
#: STD-5: how the kinds of mesh a standard speaks to are said, and this run's kind
_KIND_WORDS = {"shell": "shell", "solid": "solid", "cfd": "flow (CFD)"}


def _kinds_of(row: Mapping, req: Mapping) -> list[str]:
    """STD-5: the kinds of mesh a standard's row speaks to: its own line's tag when the reading gave the line one, else
    the list's; ["any"] with no tag (a standard read before the tag existed applies as it always did)."""
    raw_tag = req.get("applies_to")
    tag: Mapping = raw_tag if isinstance(raw_tag, Mapping) else {}
    rq = MR._squash(str(row.get("quote") or ""))
    for ln in tag.get("lines") or []:
        lq = MR._squash(str(ln.get("quote") or ""))
        if lq and rq and (lq in rq or rq in lq):
            return list(ln.get("kinds") or ["any"])
    return list(tag.get("whole") or ["any"])


def _fits(kinds: list[str], route: str) -> bool:
    """STD-5: whether words for `kinds` of mesh apply to this run's (the flow route: cfd; the FEA route: shell or
    solid; a route not named: every line, as before)."""
    if "any" in kinds or route not in (FLOW, "fea"):
        return True
    return "cfd" in kinds if route == FLOW else bool({"shell", "solid"} & set(kinds))


def _once(rows: list[dict]) -> list[dict]:
    """STD-5: each saved line listed once. One line of the standard can come back twice - as a setting the reader
    read from it and as the same words filed as guidance or another kind of mesh's ("3 boundary layers, growth 1.2"
    and "3 boundary layers, growth 1.2." in one list, 26 Nov) - so lines whose words are the same but for spacing,
    case, quote marks and a closing full stop are one; the first is kept."""
    seen: set[str] = set()
    out = []
    for r in rows:
        k = MR._squash(str(r.get("asked") or r.get("quote") or "")).rstrip(".")
        if k and k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _not_here_words(req: Mapping, not_here: list[dict], route: str, *, everything: bool) -> str:
    """STD-5 in plain words, said once: the standard (or its lines) written for other kinds of mesh, not applied."""
    if not not_here:
        return ""
    raw_tag = req.get("applies_to")
    tag: Mapping = raw_tag if isinstance(raw_tag, Mapping) else {}
    # the kinds of the lines LEFT OUT, never every kind the list mentions ("5 lines ... are written for flow (CFD)
    # meshes", not "for solid and flow (CFD)" on a solid run); none of this run's kind (a line that fits is not here)
    left = sorted({k for r in not_here for k in _kinds_of(r, req) if k in _KIND_WORDS and not _fits([k], route)},
                  key=list(_KIND_WORDS).index)
    kinds = left or [k for k in (tag.get("whole") or []) if k in _KIND_WORDS and not _fits([k], route)]
    names = " and ".join(_KIND_WORDS[k] for k in kinds) or "other"
    here = "this flow mesh" if route == FLOW else "this solid or shell mesh"
    if everything:
        return f"Your saved meshing standard is written for {names} meshes; it was not applied to {here}."
    asked = "; ".join(str(r.get("asked")) for r in not_here[:6]) + ("; and more" if len(not_here) > 6 else "")
    return (f"{len(not_here)} line{'s' if len(not_here) != 1 else ''} of your saved meshing standard "
            f"{'are' if len(not_here) != 1 else 'is'} written for {names} meshes and {'were' if len(not_here) != 1 else 'was'} "
            f"not applied to {here}: {asked}.")


def _gone(line: str, rows: list[dict]) -> bool:
    return any(str(r.get("asked") or "") and str(r.get("asked")) in line for r in rows)


def merge(settings: Mapping | None, notes: Mapping, *, route: str = "") -> dict:
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
        _in_unit(N, str(settings["stated_unit"]), told_n, cells=route == FLOW)
    _reid(S, "s")
    # STD-6: the standard's words that are no setting are guidance for this run's AI steps (guidance_notes), told as
    # kept as guidance - those written for another kind of mesh are STD-5's, told with its line
    g_kept, g_off = guidance_split(settings, route)
    # NOTE-12 is the run's notes': the standard's words that are no mesh setting stay with STD-1's own lines
    S.pop("not_mesh", None)
    for r in [*(S.get("rows") or []), *(S.get("strictness") or [])]:
        r["from"] = "settings"
    for r in [*(N.get("rows") or []), *(N.get("strictness") or [])]:
        r["from"] = "notes"

    # STD-5: the standard's lines for another kind of mesh are not this run's (a structural quality list's global size
    # is no CFD cell size); told once, here and with the result
    not_here = [r for r in S.get("rows") or [] if not _fits(_kinds_of(r, S), route)]
    for r in not_here:
        _drop_row(S, r)
    not_here += [{"asked": f'"{MR._short(q)}"', "quote": q} for q in g_off]
    if s_used:
        not_here += [{"asked": f'"{MR._short(q)}"', "quote": q} for q in settings.get("other_kinds") or []]
    not_here = _once(not_here)
    std_off = bool(not_here) and not S.get("rows") and not g_kept
    said_not_here = _not_here_words(S, not_here, route, everything=std_off)
    S.pop("applies_to", None)
    if said_not_here and not S.get("rows") and not g_kept and not n_used:
        # nothing of it is for this mesh and the notes ask nothing: the run is the run with no standard, and told
        out_none = dict(notes) if notes.get("status") == "none" else dict(MR.section("none"))
        out_none["standard_not_applied"] = said_not_here
        return out_none

    # field by field: a slot the notes set is theirs this run
    taken = {_slot(r, N) for r in N.get("rows") or []}
    overridden = [dict(r) for r in S.get("rows") or [] if _slot(r, S) in taken]
    for r in overridden:
        _drop_row(S, r)
    if route == "fea" and any(str(r.get("field") or "").startswith("cells:") for r in N.get("rows") or []):
        # STD-1: on a solid or a shell ONE count aims the plan (PLAN-fea-27): a count in the run's notes, of any kind,
        # is the run's, and every count of the standard gives way to it - told, never dropped without a word
        for r in [r for r in S.get("rows") or [] if str(r.get("field") or "").startswith("cells:")]:
            told_s.append(f"{r['asked']} - your notes give the element count for this run")
            overridden.append(dict(r))
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
    if out.get("strictness"):
        # NOTE-14: how strict each limit is, the standard's words first and the notes' after (a later sentence refines
        # an earlier one): "we're flexible around the 5 mm minimum" in the notes is the standard's 5 mm minimum's too
        MR.apply_strictness(out)

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
    _flow_across(out, source, not_the_notes)
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
    s_dropped = list(settings.get("dropped") or []) if s_used and not std_off else []
    n_not = list(notes.get("not_applied") or []) if n_used else []
    # (the standard's words no setting matches are STD-6's guidance, told as kept - never as not applied)
    dropped = [*n_dropped, *told_n, *[f"from your settings - {d}" for d in (*s_dropped, *told_s)]]
    not_applied = list(n_not)
    sec = MR.section(status, requirements=out if status == "used" else None, dropped=dropped,
                     not_applied=not_applied, note=str(notes.get("note") or "") if n_used else "",
                     key=str(notes.get("key") or settings.get("key") or ""),
                     source=str(notes.get("source") or "") if n_used else str(settings.get("source") or ""))
    sec["standard"] = {"status": settings.get("status"), "source": settings.get("source"),
                       "key": settings.get("key"), "unit": settings.get("stated_unit"),
                       "overridden": [{"field": r.get("field"), "asked": r.get("asked")} for r in overridden],
                       "from": {str(r["id"]): r["from"] for r in out.get("rows") or []}}
    if g_kept:
        sec["standard"]["guidance"] = [g["quote"] for g in g_kept]
    sec["lines"] = lines(MR.section("none") if std_off else settings, notes, out, told_s=told_s, told_n=told_n,
                         guidance=g_kept, route=route)
    if said_not_here:
        sec["standard"]["not_applied_here"] = said_not_here
        sec["standard_not_applied"] = said_not_here
        sec["lines"] = [said_not_here, *[w for w in sec["lines"]
                                         if not (w.startswith("Not applied (your settings)") and _gone(w, not_here))]]
    if status != "used":
        sec["note"] = " ".join(sec["lines"]) or UNREAD_WORDS
    return sec


def _flow_across(out: dict, source, not_the_notes) -> None:
    """STD-2 x NOTE-10: the flow fields' own rules (engines/mesh_requirements._flow_fields settles them within one
    source) re-applied where the two sources meet - the notes' value is kept and the standard's is told as not
    applied: no boundary layers against a layer setting, a first layer not thinner than the stack, a cell floor not
    under the ceiling, a cell count outside the range."""
    got = out.get("layers")
    lay: dict = got if isinstance(got, dict) else {}
    others = [f for f in _LAYER_KEY if f != "layers:count" and lay.get(_LAYER_KEY[f]) is not None]
    if lay.get("count") == 0 and others:
        if source("layers:count") == "notes":
            for f in others:
                not_the_notes(f, "your notes ask for no boundary layers")
        elif any(source(f) == "notes" for f in others):
            not_the_notes("layers:count", "your notes set the boundary layers")
    first, total = lay.get("first_mm"), lay.get("total_mm")
    if (first is not None and total is not None and float(first) >= float(total)
            and source("layers:first_height") != source("layers:total_thickness")):
        bad = "layers:first_height" if source("layers:first_height") == "settings" else "layers:total_thickness"
        not_the_notes(bad, "it does not fit the boundary layers your notes ask for")
    got = out.get("cells")
    c: dict = got if isinstance(got, dict) else {}
    lo, hi = c.get("floor"), c.get("ceiling")
    if lo is not None and hi is not None and lo >= hi and source("cells:floor") != source("cells:ceiling"):
        not_the_notes("cells:floor" if source("cells:floor") == "settings" else "cells:ceiling",
                      "it does not fit the cell count your notes ask for")
    lo, hi, mid = c.get("floor"), c.get("ceiling"), c.get("target")
    if mid is not None:
        for f, off in (("cells:floor", lo is not None and mid < lo), ("cells:ceiling", hi is not None and mid > hi)):
            if off and source(f) != source("cells:target"):
                not_the_notes(f if source(f) == "settings" else "cells:target",
                              "it does not fit the cell count your notes ask for")


def lines(settings: Mapping, notes: Mapping, merged: Mapping, *, told_s=(), told_n=(),
          guidance: list[dict] | None = None, route: str = "") -> list[str]:
    """Before meshing, in plain words: one line per value in force, said by where it came from, then what was not
    applied - the notes' lines exactly as the reader says them, the standard's marked as the standard's. `guidance`:
    the standard's lines kept as guidance for this run (STD-6; None: every such line, as the settings page says)."""
    s_used, n_used = settings.get("status") == "used", notes.get("status") == "used"
    out: list[str] = []
    if not s_used and settings.get("status") not in (None, "none"):
        out.append(str(settings.get("note") or UNREAD_WORDS))
    if not n_used and notes.get("status") not in (None, "none"):
        out.append(str(notes.get("note") or MR.UNREAD_WORDS))
    rows = list(merged.get("rows") or [])
    unit = settings.get("stated_unit") if s_used else None
    def said(r: Mapping, whose: str) -> str:
        # a row this route cannot take at all is never said as applied: the result's own reason, said now (STD-4)
        why = MR.not_on_route(str(r.get("field") or ""), route)
        if why:
            return f"{MR.NOT_HERE_LEAD} ({'your ' + whose}): {r['asked']} - {why}."
        return f"From your {whose}: {r['asked']}."

    out += [said(r, "settings") for r in rows if r.get("from") == "settings"]
    if unit:
        out.append(f"From your settings: lengths with no unit are in {_UNIT_NAME[str(unit)]}.")
    if s_used:
        for _quote, *files in settings.get("solver_files") or []:
            out.append(f"From your settings: {' and '.join(files)} files - every FEA run delivers them.")
    out += [said(r, "notes") for r in rows if r.get("from") == "notes"]
    out += MR.strictness_words(merged)
    for sec in (settings, notes):
        if sec.get("status") == "used" and sec.get("source") == "saved" and sec.get("note"):
            out.append(str(sec["note"]))
    if n_used:
        out += [f"Not applied: {d}." for d in notes.get("dropped") or []]
        out += [f'Not applied: "{MR._short(q)}" - no mesh setting of ours matches it yet.'
                for q in notes.get("not_applied") or []]
        out += MR.not_mesh_words((notes.get("requirements") or {}).get("not_mesh"))
    out += [f"Not applied: {d}." for d in told_n]
    if s_used:
        out += [f"Not applied (your settings): {d}." for d in settings.get("dropped") or []]
        out += [f"Not applied (your settings): {d}." for d in told_s]
        out += [f'Not applied (your settings): "{MR._short(q)}" - {ELSEWHERE_WORDS}.'
                for q in settings.get("elsewhere") or []]
        # STD-6 (and NOTE-12 for the standard): a line that is no setting ("we care about fatigue at the fillets",
        # "this goes into a crash model") is handed to the run's AI steps as guidance and told so, never lost
        got = guidance_split(settings, "")[0] if guidance is None else guidance
        out += [guidance_line(g) for g in got]
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
        if line.startswith(GUIDANCE_LEAD):
            out.append(GUIDANCE_WORDS[:1].upper() + GUIDANCE_WORDS[1:] + line[len(GUIDANCE_LEAD):])
            continue
        for lead in ("From your settings: ", "Not applied (your settings): ", f"{MR.NOT_MESH_LEAD} (your settings): "):
            if line.startswith(lead):
                line = line[len(lead):]
                line = ((line[:1].upper() + line[1:]) if lead.startswith("From") else f"Not applied: {line}"
                        if lead.startswith("Not applied") else f"{MR.NOT_MESH_LEAD}: {line}")
        out.append(line)
    return out


def told_not_applied(workspace, words: str) -> None:
    """STD-5: the result's line when the saved standard was not applied to this kind of mesh at all (the run asked
    nothing else): kept with the result, said as the settings' own."""
    try:
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(workspace, f"From your settings: {words[:1].lower()}{words[1:]}", by="meshing_standard")
    except Exception:  # noqa: BLE001 - a line not kept never stops a mesh
        logger.debug("STD-5 line not kept", exc_info=True)


def remark(sec: Mapping | None, line: str) -> str:
    """The result's one closing line ("all 5 things you asked for are met"), led by where the things asked came
    from: unchanged with no standard (the result says "From your notes: ..." as it always has)."""
    if not line or not isinstance(sec, Mapping) or not sec.get("standard"):
        return line
    got = set(((sec.get("standard") or {}).get("from") or {}).values())
    # STD-5: the standard (or its lines) written for another kind of mesh, said with the result too
    off = str((sec.get("standard") or {}).get("not_applied_here") or "")
    tail = f" {off}" if off else ""
    if got == {"settings"}:
        return f"From your settings: {line}{tail}"
    if "settings" in got:
        return f"From your settings and notes: {line}{tail}"
    return f"From your notes: {line}{tail}" if off else line


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


# #
# the stage's element choice for a shell (STD-4)
# #

RULE_STAGE = "STD-4"
#: the pipeline state field the stage's pick reaches the run in (application/shell_elements.confirmed)
STAGE_KEY = "fea_shell_elements"
#: the stage's two choices
QUADS, TRIANGLES = "quads", "triangles"
PICKS = (QUADS, TRIANGLES)
#: the element-kind rows of the set (engines/mesh_requirements)
_SHELL_KIND_FIELDS = ("quads_only", "triangles_only", "max_triangle_percent")
#: where a value came from, in the words the user reads
_FROM_WORDS = {"stage": "chosen on the stage", "notes": "from your notes", "settings": "from your settings",
               "default": "the default"}


def switched(settings: dict, kind: str) -> dict:
    """STD-4: the settings' section with the page's switch as its shell element kind - Triangles: triangles only
    (any element kind the words gave set aside); Quads: a "triangles only" the words gave is set aside (told), a
    "quads only" or a share of triangles stays. Quads with nothing to set aside: the section itself."""
    used = settings.get("status") == "used"
    req = copy.deepcopy(dict(settings.get("requirements") or MR.empty())) if used else MR.empty()
    gone = [dict(r) for r in req.get("rows") or [] if r.get("field") in _SHELL_KIND_FIELDS
            and (kind == TRIANGLES or r.get("field") == "triangles_only")]
    if kind != TRIANGLES and not gone:
        return settings
    for r in gone:
        _drop_row(req, r)
    if kind == TRIANGLES:
        req["kind"]["shell"] = TRIANGLES
        req["kind"]["tria_percent_max"] = None
        req["rows"].append({"id": f"r{len(req['rows']) + 1}w", "field": "triangles_only", "quote": "",
                            "asked": MR.KIND_WORDS[TRIANGLES]})
    out = dict(settings) if used else dict(MR.section("used", source="switch"))
    out["requirements"] = req
    out["dropped"] = [*list(settings.get("dropped") or [] if used else []),
                      *[f"{r['asked']} - the Shell elements switch says {kind.capitalize()}" for r in gone]]
    out.setdefault("stated_unit", None)
    out.setdefault("elsewhere", [])
    out.setdefault("solver_files", [])
    return out


def pick_of(value) -> str:
    """A stage pick read strictly: "quads", "triangles", or "" (nothing picked)."""
    v = str(value or "").strip().lower()
    return v if v in PICKS else ""


def _from(row: Mapping) -> str:
    """Where a row came from: a row with no mark is the notes' (a run with no standard)."""
    return str(row.get("from") or "notes")


def stage(sec: dict, pick) -> dict:
    """STD-4: the section with the stage's element pick applied - the SAME field the notes and settings set
    (requirements' kind.shell, its row from "stage"). No pick: the section itself, the same object (nothing changes).
    A section nothing was read into ("none", or the AI not answering) becomes a "used" one holding only the pick."""
    p = pick_of(pick)
    if not p:
        return sec
    used = sec.get("status") == "used"
    before = MR.words(sec)
    req = copy.deepcopy(dict(sec.get("requirements") or MR.empty())) if used else MR.empty()
    req.setdefault("kind", {"shell": None, "tria_percent_max": None, "order": None})
    req.setdefault("rows", [])
    gone = [dict(r) for r in req["rows"] if r.get("field") in _SHELL_KIND_FIELDS
            and (p == TRIANGLES or r.get("field") == "triangles_only")]
    for r in gone:
        _drop_row(req, r)
    told = [f"{r['asked']} ({'your settings' if _from(r) == 'settings' else 'your notes'}) - you chose "
            f"{p.capitalize()} on the stage" for r in gone]
    if p == TRIANGLES:
        req["kind"]["shell"] = TRIANGLES
        req["kind"]["tria_percent_max"] = None
        req["rows"].append({"id": "e1", "field": "triangles_only", "quote": "", "asked": MR.KIND_WORDS[TRIANGLES],
                            "from": "stage"})
    out = MR.section("used", requirements=req, dropped=list(sec.get("dropped") or []) if used else [],
                     not_applied=list(sec.get("not_applied") or []) if used else [],
                     note=str(sec.get("note") or ""), key=str(sec.get("key") or ""),
                     source=str(sec.get("source") or "") if used else "stage")
    for k in ("standard", "stated_unit"):
        if k in sec:
            out[k] = copy.deepcopy(sec[k])
    if isinstance(out.get("standard"), dict):
        came = {rid: v for rid, v in (out["standard"].get("from") or {}).items()
                if rid not in {str(r.get("id")) for r in gone}}
        if p == TRIANGLES:
            came["e1"] = "stage"
        out["standard"]["from"] = came
    gone_lines = {f"From your {'settings' if _from(r) == 'settings' else 'notes'}: {r['asked']}." for r in gone}
    out["lines"] = ([w for w in before if w not in gone_lines] + [f"On the stage: {p}."]
                    + [f"Not applied: {t}." for t in told])
    out["stage"] = {"pick": p, "overridden": [{"field": r.get("field"), "asked": r.get("asked"), "from": _from(r)}
                                              for r in gone]}
    return out


def elements_of(sec: Mapping | None) -> dict:
    """WHICH SHELL ELEMENTS A RUN MAKES AND WHY, from the section in force (after the stage, STD-4): {"kind": "quads" |
    "quads_only" | "quad_share" | "triangles", "from": "stage" | "notes" | "settings" | "default", "words":
    "Triangles, as chosen on the stage."}. Nothing said anywhere: quad-dominant, the default."""
    s = sec if isinstance(sec, Mapping) else {}
    req = (s.get("requirements") or {}) if s.get("status") == "used" else {}
    k = req.get("kind") or {}
    row = next((r for r in req.get("rows") or [] if r.get("field") in _SHELL_KIND_FIELDS), None)
    if k.get("shell") == TRIANGLES:
        kind, said = "triangles", "Triangles"
    elif k.get("shell") == QUADS:
        kind, said = "quads_only", "Quads only"
    elif k.get("tria_percent_max") is not None:
        kind, said = "quad_share", f"Quads, at most {MR._g(float(k['tria_percent_max']))}% triangles"
    else:
        kind, said = "quads", "Quads"
    if row is not None:
        came = _from(row)
    elif (s.get("stage") or {}).get("pick"):
        came = "stage"
    else:
        came = "default"
    return {"kind": kind, "from": came, "words": f"{said}, {_FROM_WORDS[came]}."}


def stage_decisions(sec: Mapping | None, stage_name: str = "notes") -> list[dict]:
    """The why-record entry of the stage's pick (contracts/decision_record.cite shape): [] with no pick."""
    st = (sec or {}).get("stage") if isinstance(sec, Mapping) else None
    if not st:
        return []
    got = elements_of(sec)
    return [{"stage": stage_name, "decided": "shell elements chosen on the stage", "value": st.get("pick"),
             "rule": RULE_STAGE, "inputs": {"pick": st.get("pick"), "overridden": list(st.get("overridden") or []),
                                            "in_force": got["kind"]},
             "words": got["words"]}]


__all__ = ["ELSEWHERE_WORDS", "MAX_CHARS", "OFF_WORDS", "PICKS", "QUADS", "RULE_MERGE", "RULE_NOTHING", "RULE_READ",
           "RULE_STAGE", "SAVE_WAIT_S", "SOURCE", "STAGE_KEY", "STATE_KEY", "TRIANGLES", "UNREAD_WORDS", "checked",
           "switch_chosen", "switch_of",
           "decide", "decide_and_base", "decisions", "elements_of", "kept_reading", "key_of", "lines", "merge", "messages", "pick_of",
           "read", "read_at_save", "remark", "size_remark", "snapshot", "snapshot_key", "stage", "stage_decisions", "standard_of",
           "understood", "words_read"]
