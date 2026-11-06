# Responsibility: Own the user's own notes for a run: the one optional question, the notes kept word for word, how every AI step is shown them, and who on every route reads them.
# Owns: the question's wording, how an answer and a quoted message become a note, the notes file every workspace gets, the one-line remarks a step may leave for the result, and the consumer registry the parity test holds every route to.
# Boundaries: pure over plain data and the workspace's own files. Notes are GUIDANCE for how to mesh, never acceptance criteria: nothing here checks, gates or fails anything (NOTE-3), and a note is never rewritten.
# Collaborates with: agents/intake (asks once, captures), agents/builder (user_notes.txt, the builder's prompt), engines/snappy/planner.py and engines/fea_ai.py (read them), agents/reviewer (context only), application/final_result.py (the "From your notes" lines).
"""THE USER'S OWN NOTES (docs/RULES.md NOTE-1..3).

A customer often knows things about their simulation that help the mesh: a region that needs finer
cells, a layer count, an element type, the solver they will use - or a whole pasted spec sheet. The
intake's summary (request_txt) is a model's rewrite and can squash a number or drop a condition, so
the user's words are also kept exactly as typed:

    note = {"id": "n1", "text": <verbatim>, "source": "chat" | "console", "turn": <user turn | None>,
            "at": <ISO time captured>}

Every AI step on every route is shown them as guidance for how to mesh (NOTE-2). They are not
acceptance criteria: the reviewer reads them as context and never fails a mesh over one, and
nothing a note asks for can fail a run (NOTE-3) - what cannot be done is simply left, and a step
may say so in one plain line on the result ("From your notes: ...").
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The rules this module is the code of (docs/RULES.md, NOTE family).
RULE_VERBATIM = "NOTE-1"      # asked once; the user's words kept as typed
RULE_EVERY_ROUTE = "NOTE-2"   # every route's AI steps read them, as guidance
RULE_NEVER_FAILS = "NOTE-3"   # a note is never a check: it fails no review and no run

#: THE ONE QUESTION, asked by the application before the first summary - once per conversation,
#: never repeated, never a reason to hold a submission.
QUESTION = ("Anything specific about this simulation we should know? For example, a region that "
            "needs finer cells, a layer count, an element type, or a solver you'll use. You can "
            "paste a spec. Say 'no' to skip.")

SOURCE_CHAT = "chat"
SOURCE_CONSOLE = "console"

#: The file every attempt's workspace gets (verbatim notes, for any step that reads the workspace)
NOTES_FILE = "user_notes.txt"
#: The one-line remarks steps leave for the result ("From your notes: ...")
REMARKS_FILE = "user_notes_remarks.json"

#: The most of one note kept (a pasted spec sheet fits many times over)
MAX_NOTE_CHARS = 20_000
#: The most of the notes one prompt is shown; the rest stays in user_notes.txt
PROMPT_CHARS = 8_000
#: The most of one remark kept for the result
MAX_REMARK_CHARS = 400


# #
# THE CONSUMERS (NOTE-2) - who reads the notes, on every route. The parity test
# (tests/unit/hygiene/test_user_notes_every_route.py) fails when a route of the purpose map or an
# implemented engine has no entry here, or when a consumer named here reads the notes nowhere
# (each read names its consumer: `consumer="..."`).
# #

CONSUMERS: dict[str, str] = {
    "intake": "agents/intake: asks the question once, keeps the user's words, and sets a note "
              "that fits a declared field (engine_params, mesh_fidelity, patches, extents) there",
    "builder_workspace": "agents/builder/workspace.py: user_notes.txt in every attempt's "
                         "workspace, model loop or deterministic driver",
    "builder_model": "agents/builder/attempt.py: the model-led builder's first message carries them",
    "snappy_planner": "engines/snappy/planner.py: the plan's request carries them",
    "fea_ai": "engines/fea_ai.py: the FEA judgement calls are shown them",
    "reviewer": "agents/reviewer/visual.py: shown as context, never as criteria",
    "delivery": "application/final_result.py: a step's 'From your notes' line on the result",
    "chat": "agents/intake/chat_facts.py: every chat answer, before and after the run, is given "
            "them - a question about what the mesh will do is answered with what the notes ask",
}
#: Every route reads the notes through these.
COMMON_CONSUMERS = ("intake", "builder_workspace", "reviewer", "delivery", "chat")
#: Each route of application/purpose_map.ROUTES, and the AI steps that read the notes on it.
ROUTE_CONSUMERS: dict[str, tuple[str, ...]] = {
    "flow": (*COMMON_CONSUMERS, "builder_model", "snappy_planner"),
    "fea": (*COMMON_CONSUMERS, "fea_ai", "builder_model"),
    # an ECXML model is built by snapgrid's deterministic driver: no AI step in the build, so the
    # intake sets what fits a field and the reviewer reads them beside the result
    "electronics": COMMON_CONSUMERS,
}
#: Each implemented engine (engines/registry.engine_names), and the build step that reads them.
ENGINE_CONSUMERS: dict[str, tuple[str, ...]] = {
    "cfmesh": ("builder_model",),
    "gmsh": ("builder_model", "fea_ai"),
    # Netgen is built by its deterministic plan (engines/netgen/plan.py): no AI step in the build
    "netgen": ("intake",),
    "snapgrid": ("intake",),
    "snappy": ("snappy_planner",),
    "snappy_multiregion": ("builder_model",),
    "vmtk": ("builder_model",),
}


def _reader(consumer: str) -> None:
    """Every read names its consumer (CONSUMERS) - the parity test finds each reader by it."""
    if consumer not in CONSUMERS:
        raise KeyError(f"{consumer!r} is not a declared user-notes consumer")


# #
# capture (NOTE-1)
# #

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _own(messages: Iterable[Any]) -> list[dict]:
    """The conversation as the user and the assistant said it - the application's synthetic
    nudges left out."""
    return [m for m in messages or [] if isinstance(m, dict) and not m.get("_synthetic")
            and m.get("role") in ("user", "assistant")]


def asked(messages: Iterable[Any]) -> bool:
    """Whether the application already asked its question in this conversation."""
    return any(m.get("role") == "assistant" and str(m.get("content") or "").strip() == QUESTION
               for m in _own(messages))


def answer(messages: Iterable[Any]) -> tuple[int, str] | None:
    """(user turn, the user's words) answering the question - the first user message after it -
    or None when it was not asked or not answered yet."""
    turn, seen = 0, False
    for m in _own(messages):
        if m.get("role") == "user":
            turn += 1
            if seen:
                return turn, str(m.get("content") or "")
        elif str(m.get("content") or "").strip() == QUESTION:
            seen = True
    return None


def answers_now(messages: Iterable[Any]) -> bool:
    """Whether the user's latest message answers the question (the question is the last thing
    the assistant said)."""
    own = _own(messages)
    return (len(own) >= 2 and own[-1].get("role") == "user"
            and str(own[-2].get("content") or "").strip() == QUESTION)


#: "No" in the words people use for it - the whole message, nothing else in it.
_SKIP = re.compile(
    r"^(?:no|nope|nah|none|nothing|n/?a|skip|not really|all good|no notes?|nothing specific|"
    r"nothing else|no thanks?|no thank you|that'?s all|that'?s it|no that'?s (?:all|it)|"
    r"nothing thanks?|none thanks?|no (?:go ahead|proceed|continue|carry on|all good)|"
    r"no nothing(?: specific| else)?|go ahead|go on|carry on|proceed|continue|ok|okay|fine)"
    r"[.! ]*$")


def is_skip(text: Any) -> bool:
    """Whether an answer to the question says there is nothing to add."""
    t = re.sub(r"[\s,]+", " ", str(text or "").casefold().replace("’", "'")).strip()
    return bool(t) and bool(_SKIP.match(t))


def make(text: str, *, source: str, turn: int | None = None, at: str = "") -> dict:
    """One note: the user's words exactly as typed (only cut past MAX_NOTE_CHARS)."""
    return {"text": str(text)[:MAX_NOTE_CHARS], "source": source,
            "turn": int(turn) if turn is not None else None, "at": at or _now()}


def capture(messages: Iterable[Any], quotes: Sequence[Any] = (), *,
            consumer: str = "intake") -> tuple[list[dict], list[str]]:
    """The conversation's notes, and the quotes that matched no message of the user's.

    Two ways in, both the user's WHOLE message verbatim: the answer to the question (unless it
    says no), and every message the intake quoted as saying something specific. A quote that
    matches nothing the user wrote is not the user's words, so it is not kept - it is returned."""
    _reader(consumer)
    turns, turn = [], 0
    for m in _own(messages):
        if m.get("role") == "user":
            turn += 1
            turns.append((turn, str(m.get("content") or "")))
    picked: dict[int, str] = {}
    got = answer(messages)
    if got is not None and got[1].strip() and not is_skip(got[1]):
        picked[got[0]] = got[1]
    unmatched: list[str] = []
    for q in quotes or ():
        quote = _norm(q)
        if not quote:
            continue
        hit = next(((t, text) for t, text in turns if quote in _norm(text)), None)
        if hit is None:
            unmatched.append(str(q))
        elif not is_skip(hit[1]):
            picked[hit[0]] = hit[1]
    return [make(text, source=SOURCE_CHAT, turn=t) for t, text in sorted(picked.items())], unmatched


def clean(raw: Any) -> list[dict]:
    """Stored notes as notes: anything that is not one is left out, and the ids are numbered."""
    out = []
    for n in raw or ():
        if isinstance(n, Mapping) and str(n.get("text") or "").strip():
            out.append({"text": str(n["text"])[:MAX_NOTE_CHARS],
                        "source": str(n.get("source") or SOURCE_CHAT),
                        "turn": n.get("turn") if isinstance(n.get("turn"), int) else None,
                        "at": str(n.get("at") or "")})
    return [{"id": f"n{i}", **n} for i, n in enumerate(out, start=1)]


def merge(existing: Any, new: Any) -> list[dict]:
    """The notes so far, then each new one not already among them. A note is never dropped and
    never reworded; the first capture keeps its time."""
    out, seen = [], set()
    for n in [*clean(existing), *clean(new)]:
        key = (n["source"], n["turn"] if n["source"] == SOURCE_CHAT else None, _norm(n["text"]))
        if key not in seen:
            seen.add(key)
            out.append(n)
    return clean(out)


# #
# reading them (NOTE-2)
# #

def _where(note: Mapping) -> str:
    if note.get("source") == SOURCE_CONSOLE:
        return "typed in the notes box"
    return f"said in the chat, message {note['turn']}" if note.get("turn") else "said in the chat"


def render(notes: Any, *, limit: int | None = None) -> str:
    """The notes as a model reads them: each verbatim between markers. `limit` cuts the text (the
    full notes stay in user_notes.txt)."""
    rows = clean(notes)
    if not rows:
        return ""
    text = "\n\n".join(f"[{n['id']}] ({_where(n)})\n<<<\n{n['text']}\n>>>" for n in rows)
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"\n... (cut here; the full notes are in {NOTES_FILE})"
    return text


#: How every step is told to use them - the same words everywhere.
GUIDANCE = (
    "Use them as GUIDANCE for how to mesh: where something in them fits a setting you control "
    "(a region to refine, a layer count, an element type, a size or budget), use it within that "
    "setting's limits. They are not acceptance criteria. Loads, supports, materials, time "
    "histories and the outputs they want are solver settings - nothing to do in the mesh. What "
    "you cannot do (for example removing holes from the CAD) you leave and carry on: a note never "
    "stops or fails the mesh. Never invent a value the user did not give.")


def prompt_block(notes: Any, *, consumer: str, limit: int = PROMPT_CHARS) -> str:
    """The notes block a step's prompt carries ('' with no notes)."""
    _reader(consumer)
    text = render(notes, limit=limit)
    if not text:
        return ""
    return ("THE USER'S OWN NOTES - word for word, as they typed them:\n" + text + "\n\n"
            + GUIDANCE)


def write_file(workspace: Any, notes: Any, *, consumer: str = "builder_workspace") -> bool:
    """Write user_notes.txt into the workspace (removed when there are none, so a carried file
    never speaks for another run). True when written."""
    _reader(consumer)
    path = Path(workspace) / NOTES_FILE
    text = render(notes)
    try:
        if text:
            path.write_text("THE USER'S OWN NOTES - word for word, never reworded.\n\n" + text
                            + "\n\n" + GUIDANCE + "\n", encoding="utf-8")
            return True
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("user notes: could not write %s", path, exc_info=True)
    return False


def context_block(notes: Any, *, consumer: str, limit: int = PROMPT_CHARS) -> str:
    """The notes for a step that JUDGES (the reviewer): context about what the user cares about,
    stated as never a criterion (NOTE-3). '' with no notes."""
    _reader(consumer)
    text = render(notes, limit=limit)
    if not text:
        return ""
    return ("THE USER'S OWN NOTES (CONTEXT ONLY - not acceptance criteria). Word for word, what the "
            "user told us about their simulation; the builder used them as guidance:\n" + text
            + "\n\nRead them to understand what matters to this user. Never fail an axis, ask for a "
              "rebuild or call the mesh the wrong problem because something in a note was not done - "
              "a note is never a requirement you judge against. You may mention one in a finding as "
              "context.")


def chat_block(notes: Any, *, consumer: str = "chat", limit: int = PROMPT_CHARS) -> str:
    """The notes for the step that TALKS to the user about their run (the chat, before and after
    it): what they asked for, which every step that builds the mesh is given. '' with no notes.
    The knuckle of the 2 Nov dry run had a note asking for its bolt holes and fillets, and the chat
    answered that the requirements "do not specify" them - it had never been shown the note."""
    _reader(consumer)
    text = render(notes, limit=limit)
    if not text:
        return ""
    return ("THE USER'S OWN NOTES - word for word, as they typed them. They are part of what the user "
            "asked for, and every step that builds the mesh is given them as guidance:\n" + text
            + "\n\nWhen the user asks what the mesh will do about something a note mentions (a region "
              "to refine, a layer count, a hole, a fillet), say what the note asks and that the steps "
              "that build the mesh are given it; never say the requirements do not mention it. A note "
              "is guidance, not a promise: do not claim a size or a count the note does not give.")


def workspace_block(workspace: Any, *, consumer: str, limit: int = PROMPT_CHARS) -> str:
    """The notes block for a step that holds only the attempt's workspace: user_notes.txt as
    written there ('' when the run has no notes)."""
    _reader(consumer)
    try:
        text = (Path(workspace) / NOTES_FILE).read_text(encoding="utf-8").strip() if workspace else ""
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + f"\n... (cut here; the full notes are in {NOTES_FILE})"
    return text


# #
# the result's line (best effort, never a check)
# #

#: The optional field a step answers with: one plain line, read as written.
REMARK_DESCRIPTION = (
    "OPTIONAL, only when the user left notes: ONE short plain-English line for the user on what "
    "you did with them, e.g. 'used 10 prism layers on the hull; the small side holes stay (this "
    "mesher does not remove features)'. Leave it out if you did nothing with them.")


def record_remark(workspace: Any, remark: Any, *, by: str) -> None:
    """Keep a step's one-line remark in the workspace (replacing that step's earlier one). Best
    effort: a remark that cannot be kept is lost, and nothing else is affected (NOTE-3)."""
    import json
    text = " ".join(str(remark or "").split())[:MAX_REMARK_CHARS]
    if not workspace or not text:
        return
    path = Path(workspace) / REMARKS_FILE
    try:
        rows = [r for r in read_remarks(workspace, raw=True) if r.get("by") != by]
        path.write_text(json.dumps([*rows, {"by": by, "text": text}], indent=1), encoding="utf-8")
    except OSError:
        logger.warning("user notes: could not keep %s's remark", by, exc_info=True)


def read_remarks(workspace: Any, *, raw: bool = False) -> list:
    """The remarks the steps left in this workspace: their lines, or (raw) the stored rows."""
    import json
    if not workspace:
        return []
    try:
        rows = json.loads((Path(workspace) / REMARKS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("text") or "").strip()] \
        if isinstance(rows, list) else []
    return rows if raw else [str(r["text"]) for r in rows]


def given_remark(engines: Sequence[str]) -> str:
    """The result's line when no step of the delivered attempt said what it did with the notes: that
    they went to every attempt - every engine the run tried, in order, as the user knows them - and
    that no step reported a change for them. Never a claim that something was done."""
    seen: list[str] = []
    for e in engines or ():
        name = str(e or "").strip()
        if name and name not in seen:
            seen.append(name)
    if len(seen) > 1:
        where = f"every attempt of this run ({', then '.join(seen)})"
    elif seen:
        where = f"every step that built this mesh with {seen[0]}"
    else:
        where = "every step that built this mesh"
    return f"they went to {where}; no step reported a specific change for them."


def result_lines(remarks: Sequence[Any], *, consumer: str = "delivery") -> list[str]:
    """The result's 'From your notes' lines, one per remark ([] with none)."""
    _reader(consumer)
    return [f"From your notes: {str(r).strip()}" for r in remarks or () if str(r).strip()]


__all__ = ["COMMON_CONSUMERS", "CONSUMERS", "ENGINE_CONSUMERS", "GUIDANCE", "NOTES_FILE",
           "QUESTION", "REMARKS_FILE", "REMARK_DESCRIPTION", "ROUTE_CONSUMERS", "RULE_EVERY_ROUTE",
           "RULE_NEVER_FAILS", "RULE_VERBATIM", "SOURCE_CHAT", "SOURCE_CONSOLE", "answer",
           "answers_now", "asked", "capture", "chat_block", "clean", "context_block", "is_skip", "make", "merge",
           "prompt_block",
           "read_remarks", "record_remark", "render", "result_lines", "workspace_block",
           "write_file"]
