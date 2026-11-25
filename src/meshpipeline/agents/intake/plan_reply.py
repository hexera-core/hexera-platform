# Responsibility: The plan step's answer to a requirement, written like a person (NOTE-19): the intake's model writes
#       one or two short sentences from the FACTS the checklist really produced for the message, a guard in code checks
#       them against those facts, and the plain sentence (agents/intake/plan_talk.reply_for) stands in when it fails.
# Owns: the facts handed to the model, the reply writer's prompt (its own block registry), the guard, the fallback.
# Boundaries: the model is the intake's own (contracts/model_inference.call_intake_model: its client, model and
#       settings); nothing here changes the plan or the notes. The reply's source is logged, never shown.
"""MODEL-WRITTEN REPLIES UNDER A GUARD (NOTE-19).

A requirement typed at the plan step is answered the way a friendly colleague answers the person in chat, in the
first person: "Sure, I've set 0.5 mm elements on Face 23." The words are the model's; what they may say is not:

- the model is given the user's message (its words, that a picture or file came with it, the faces picked) and the
  FACTS: what will be applied (size, finer/coarser, limit), where (face labels, feature names), whether the place was
  the user's click or the system's guess, what was not used (a number only in a picture) and what could not be
  placed; and the plain sentence that says exactly that (plan_talk.reply_for);
- the GUARD (`check`) refuses a reply that holds a number the facts do not, that names no place when something was
  applied (a face id, the picked faces by their count or as "those faces", a feature's noun or size, "the whole
  mesh" all count), that gives a guessed place as certain, that claims something was applied when nothing was,
  that cites a rule id, that runs past `MAX_CHARS`, or that exclaims; which check refused it is logged;
- refused, failed, or slower than `TIMEOUT_S`: the plain sentence is said instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from meshpipeline.agents.intake import plan_talk as PT

logger = logging.getLogger(__name__)

RULE = "NOTE-19"

#: the longest a reply may be, in characters
MAX_CHARS = 300
#: how long the model may take before the plain sentence is said
TIMEOUT_S = 6.0
#: where a reply came from (logged for debugging, never shown)
SOURCE_MODEL, SOURCE_FALLBACK, SOURCE_FIXED = "model", "fallback", "fixed"


# #
# the facts
# #

def facts(note: Mapping, view: Mapping | None, *, note_id: str = "") -> dict[str, Any]:
    """What the system really produced for this message, as the model is told it. Pure."""
    text = str(note.get("text") or "")
    faces = [f for f in note.get("faces") or () if isinstance(f, Mapping)]
    files = [a for a in note.get("attachments") or () if isinstance(a, Mapping)]
    pictures = PT.pictures_of(note)
    rows, unsure = PT.mine(note, view, note_id=note_id) if view is not None else ([], [])
    applied = []
    for r in rows:
        where, how = PT.row_place(r)
        applied.append({"what": PT.row_what(r), "where": where,
                        "place": {"picked": "they clicked it on the model",
                                  "guessed": "YOUR guess from their words: say it as yours ('I took that as ...') "
                                             "and that they can click it on the model if they meant another spot",
                                  "whole": "the whole model"}[how]})
    picture = ""
    if pictures:
        picture = ("the picture is kept with the note and shown, but you do not read pictures yourself: the place "
                   "comes from their words or a face they click. Say this once, briefly")
    return {
        "message": {"words": text, "is_a_question": "?" in text, "pictures": len(pictures),
                    "files": len(files) - len(pictures),
                    "faces_picked": [str(f.get("label") or f.get("id") or "") for f in faces]},
        "applied": applied,
        "flow_lines": PT.lines_mine(view, text),
        "could_not_place": [str(u.get("words") or u.get("quote") or "") for u in unsure],
        "picture": picture,
        "no_checklist": view is None and bool(text.strip()),
        # nothing reads it before the run on this route: kept, read when the mesh is built - never "couldn't read"
        "kept_for_the_run": PT.NO_CHECKLIST in PT.reply_for(note, view, note_id=note_id),
        # the plain sentence that says exactly this (plan_talk.reply_for): the guard's source of truth
        "plain_version_that_is_exactly_right": PT.reply_for(note, view, note_id=note_id),
    }


# #
# the prompt: its own block registry, composed in order
# #

def _block_voice() -> str:
    return ("You are the meshing assistant, talking with an engineer in chat while they set up a mesh. Answer THE "
            "PERSON, the way a friendly colleague would: first person ('I've set', 'I'll go finer', 'I took that "
            "as'), contractions, everyday words. Start with a short, natural acknowledgement that fits what they "
            "said: a question like 'can you do X?' gets 'Sure' or 'Yep, done'; a statement gets 'Got it', 'Okay', "
            "'All right' or 'Sounds good'. Then say what you set and where, in your own words. One or two short "
            "sentences (three only when a picture came too). Vary your wording; do not open the way your last reply "
            "did (it is given). No exclamation marks, no emoji, no 'Certainly', no 'Great question', no 'As an AI'.\n")


def _block_facts_only() -> str:
    return ("Say ONLY what the FACTS say. Name the place naturally: 'Face 23', 'that face', 'the 3 faces you "
            "picked', 'the 22 mm hole', 'the whole mesh'. Never mention mesh quality, element or cell counts, time, "
            "or anything that will happen beyond the facts. Never use a number that is not in the facts. If nothing "
            "was applied, do not say or suggest that anything was set: say plainly what you couldn't take and what "
            "they can do. If kept_for_the_run is true, nothing reads it before the run: say you've kept it (with the "
            "size and face from their message), that you'll read it when the mesh is built and confirm on the "
            "result whether this mesher could apply it; never say you couldn't read it. Never cite rule ids.\n")


def _block_guess_and_picture() -> str:
    return ("If a place is your guess, say so as yours ('I took that as the 22 mm hole') and that they can click it "
            "on the model if they meant another spot. If some words could not be placed, say which and that they can "
            "click the spot on the model and send it again. If a picture came, say once, briefly, that you keep it "
            "with the note but don't read pictures yourself, so the place comes from their words or a click.\n")


def _block_examples() -> str:
    return ("Examples of the voice (the facts differ each time; never copy their numbers):\n"
            "They: 'can you do a 0.5 mm mesh sizing in here?' (Face 23 picked; applied 0.5 mm on Face 23) -> "
            "'Sure, I've set 0.5 mm elements on Face 23.'\n"
            "They: '1 mm on these too' (3 faces picked; applied 1 mm on them) -> 'Got it, those 3 faces get 1 mm "
            "elements as well.'\n"
            "They: 'in that big hole I want a refined mesh, see the picture' (picture; finer at the hole, 22 mm "
            "across, your guess) -> 'Okay, I'll go finer in that hole. I took it as the 22 mm one, so click it on "
            "the model if you meant another spot. I keep the picture with your note, but I don't read pictures "
            "myself.'\n"
            "They: 'keep skewness under 0.8' (applied to the whole model) -> 'Will do, I'll hold skewness under 0.8 "
            "across the whole mesh.'\n"
            "They: 'make it look nice' (nothing applied) -> 'I couldn't find a mesh setting in that. Give me a size, "
            "or click a face and tell me what you want there.'\n")


def _block_language() -> str:
    return ("Answer in the language the user wrote in. Plain text only: no markdown, no lists, no quotes around the "
            "whole reply. At most 300 characters.\n")


#: THE REPLY WRITER'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text it gets
PLAN_REPLY_PROMPT_BLOCKS: tuple = (
    ("voice", "answer the person: first person, contractions, a natural acknowledgement that fits what they said, "
              "then what is set and where; one or two sentences; varied; no exclamation marks, emoji or filler",
     _block_voice),
    ("facts_only", "only what the facts say; the place in its natural short form; no quality, counts, time or "
                   "promises; no number or claim the facts do not hold; no rule ids", _block_facts_only),
    ("guess_and_picture", "a guess said as mine with how to correct it; words not placed said; a picture: kept, "
                          "not read, once", _block_guess_and_picture),
    ("examples", "four or five example exchanges in that voice", _block_examples),
    ("language", "the user's own language; plain text; at most 300 characters", _block_language),
)


def compose_system() -> str:
    return "".join(build() for _name, _purpose, build in PLAN_REPLY_PROMPT_BLOCKS)


def messages_for(note: Mapping, view: Mapping | None, *, note_id: str = "", last_reply: str = "") -> list[dict]:
    """What the model is sent: the composed system text, then the message and the facts (with the plain sentence
    that says exactly what happened), and our last reply (so the opener differs)."""
    body = {"facts": facts(note, view, note_id=note_id), "your_last_reply": last_reply}
    return [{"role": "system", "content": compose_system()},
            {"role": "user", "content": "Write the reply to this message.\n" + json.dumps(body, ensure_ascii=False)}]


# #
# the guard
# #

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_RULE_ID = re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*-\d+\b")
#: a claim that something is set - allowed only when something was (first person included: "I've set", "I'll go")
_CLAIM = re.compile(r"\b(?:applied|applying|added|in place|done|i(?:'| ha)ve (?:set|made|added|put|gone|applied|"
                    r"noted|got)|i set|i(?:'| wi)ll (?:go|use|set|make|hold|keep|put|add|apply)|is set|are set|"
                    r"now set|set to|set at|set on|will use|now using|now uses|sized|gets?|getting)\b", re.I)
_BANNED = re.compile(r"!|\bcertainly\b|\bas an ai\b|\bgreat question\b|[\U0001F300-\U0001FAFF☀-➿]", re.I)
#: a guessed place said as one: the reply says it is a guess, or how to correct it
_GUESS_SAID = re.compile(r"\b(?:click|pick|guess\w*|took (?:that|it|this) as|assum\w*|another (?:spot|place|one)|"
                         r"somewhere else|elsewhere|different (?:spot|place|one)|meant)\b", re.I)
#: a requirement kept for the run is said as kept, never as unreadable
_UNREAD = re.compile(r"\b(?:couldn'?t|could not|can'?t|cannot|unable|didn'?t|did not)\b", re.I)
_KEPT = re.compile(r"\b(?:kept|keep(?:ing)?|noted|saved|hold(?:ing)? on to|held)\b", re.I)
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
#: the picked faces, named the way people name them
_PICKED_FACES = re.compile(r"\b(?:th(?:at|is|ose|ese|e) faces?|faces? you (?:picked|selected|clicked|chose)|"
                           r"(?:picked|selected|clicked) faces?|both faces|each face|all \w+ faces)\b", re.I)
_WHOLE = re.compile(r"\b(?:whole|everywhere|entire|all over|overall|global\w*|throughout|rest of)\b", re.I)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n
            for n in _NUM.findall(str(text or ""))}


def _places(f: Mapping) -> list[str]:
    """The words that name where each applied fact is, in their natural short forms: a face id ("Face 23"), a
    feature's noun ("hole" of "the hole (22 mm across)") or the feature itself, its size ("22 mm")."""
    out: list[str] = []
    for a in f.get("applied") or ():
        where = str(a.get("where") or "")
        out += re.findall(r"\bFace \d+\b", where)
        for piece in re.split(r",\s*|\s+and\s+", re.sub(r"\([^)]*\)", "", where)):
            piece = re.sub(r"^(?:the|a|an)\s+", "", piece.strip(), flags=re.I).strip()
            if len(piece) >= 3:
                out.append(piece)
                noun = piece.split()[-1]
                if len(noun) >= 3 and not noun.isdigit():
                    out.append(noun)
        out += [f"{m} mm" for m in re.findall(r"\((?:[^)]*?)(\d+(?:\.\d+)?) ?mm", where)]
    for label in (f.get("message") or {}).get("faces_picked") or ():
        out += re.findall(r"\bFace \d+\b", str(label))
    return out


def _place_named(text: str, f: Mapping) -> bool:
    low = text.casefold()
    if any(p.casefold() in low for p in _places(f)):
        return True
    applied = list(f.get("applied") or ())
    picked = (f.get("message") or {}).get("faces_picked") or []
    if picked and any("faces you picked" in str(a.get("where") or "") or re.search(r"\bFace \d+", str(a.get("where")))
                      for a in applied):
        n = len(picked)
        if _PICKED_FACES.search(text) or (n > 1 and re.search(rf"\b(?:{n}|{_COUNT_WORDS.get(n, n)})\b", low)
                                          and "face" in low):
            return True
    if any(str(a.get("place") or "") == "the whole model" for a in applied) and _WHOLE.search(text):
        return True
    return False


def check(reply: str, f: Mapping, *, last_reply: str = "") -> str:
    """Why a model-written reply may not be said ("" when it may), against the facts `f`. The checks, in order:
    empty; longer than MAX_CHARS; a rule id; an exclamation, filler or emoji; a number the facts do not hold; something
    applied but no place named (in any natural short form); a guessed place not said as a guess; a claim when nothing
    was applied. (`last_reply` is kept for callers; variety is asked of the prompt, not refused here.)"""
    text = " ".join(str(reply or "").split())
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return "too long"
    if _RULE_ID.search(text):
        return "a rule id"
    if _BANNED.search(text):
        return "an exclamation, filler or emoji"
    allowed = _numbers(json.dumps(f, ensure_ascii=False))
    allowed |= {str(len(a)) for a in ((f.get("message") or {}).get("faces_picked") or [],) if a}
    stray = sorted(_numbers(text) - allowed)
    if stray:
        return f"a number not in the facts ({', '.join(stray)})"
    applied = list(f.get("applied") or ()) or list(f.get("flow_lines") or ())
    if f.get("applied"):
        if not _place_named(text, f):
            return "no place named"
        if any(str(a.get("place") or "").startswith("YOUR guess") for a in f["applied"]) \
                and not _GUESS_SAID.search(text):
            return "a guessed place not said as a guess"
    if not applied and _CLAIM.search(text):
        return "claims something was applied when nothing was"
    if f.get("kept_for_the_run") and (_UNREAD.search(text) or not _KEPT.search(text)):
        return "a kept requirement not said as kept"
    return ""


# #
# the reply
# #

async def _ask(messages: list[dict], *, job_id: str, user_id: str) -> str:
    from meshpipeline.contracts import model_inference as llm_router
    got = await llm_router.call_intake_model(messages=messages, job_id=job_id, user_id=user_id, name="PlanReply")
    if not getattr(got, "ok", True):
        raise RuntimeError(str(getattr(got, "failure_marker", "") or "no answer"))
    return str(getattr(got, "assistant_text", "") or "").strip()


async def write(note: Mapping, view: Mapping | None, *, note_id: str = "", last_reply: str = "",
                session_id: str = "", user_id: str = "", timeout_s: float = TIMEOUT_S) -> tuple[str, str]:
    """(the reply, its source). The model writes it from the facts; the guard checks it; refused, failed or slow,
    the plain sentence is said. Never raises."""
    got = await written(note, view, note_id=note_id, last_reply=last_reply, session_id=session_id, user_id=user_id,
                        timeout_s=timeout_s)
    return got["text"], got["source"]


async def written(note: Mapping, view: Mapping | None, *, note_id: str = "", last_reply: str = "",
                  session_id: str = "", user_id: str = "", timeout_s: float = TIMEOUT_S) -> dict[str, str]:
    """`write`, with what happened on the way: {"text": what is said, "source", "raw": the model's reply as given,
    "why": the guard's refusal or the failure ("" when said as written)}. Never raises."""
    plain = PT.reply_for(note, view, note_id=note_id)
    if PT.fixed_reply(note, view):
        return {"text": plain, "source": SOURCE_FIXED, "raw": "", "why": ""}
    f = facts(note, view, note_id=note_id)
    try:
        said = await asyncio.wait_for(
            _ask(messages_for(note, view, note_id=note_id, last_reply=last_reply),
                 job_id=f"plan-reply-{session_id}", user_id=user_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain sentence (NOTE-19)
        logger.info("plan reply: the plain sentence (%s, %s) - session=%s", RULE, type(exc).__name__, session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "raw": "", "why": f"no answer ({type(exc).__name__})"}
    said = " ".join(said.strip().strip('"“”').split())
    why = check(said, f, last_reply=last_reply)
    if why:
        logger.info("plan reply: the model's reply refused by the check '%s' (%s) - session=%s", why, RULE,
                    session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "raw": said, "why": why}
    logger.info("plan reply: written by the model (%s) - session=%s", RULE, session_id)
    return {"text": said, "source": SOURCE_MODEL, "raw": said, "why": ""}


#: one answer at a time per conversation (the send and the card's poll can both find a reply owed)
_LOCKS: dict[str, asyncio.Lock] = {}


def _own_turns(messages: Sequence[Any]) -> int:
    from meshpipeline.contracts.user_notes import system_written
    return sum(1 for m in messages or () if isinstance(m, Mapping) and m.get("role") == "user"
               and not m.get("_synthetic") and not system_written(m.get("content")))


def owed_note(session: Any) -> dict | None:
    """The requirement a reply is still owed to: the conversation ends on the user's message, the plan still stands,
    and that message was kept as the user's chat note (agents/intake/message._keep_requirement). None otherwise."""
    from meshpipeline.agents.intake import approval as ap
    from meshpipeline.contracts.user_notes import SOURCE_CHAT
    if getattr(session, "job_id", None) is not None:
        return None
    if not ap.is_live((getattr(session, "intake_gate", None) or {}).get("approval")):
        return None
    messages = list(getattr(session, "messages", None) or [])
    if not messages or not isinstance(messages[-1], Mapping) or messages[-1].get("role") != "user":
        return None
    turn = _own_turns(messages)
    return next((dict(n) for n in reversed(list(getattr(session, "user_notes", None) or []))
                 if isinstance(n, Mapping) and n.get("source") == SOURCE_CHAT and n.get("turn") == turn), None)


async def answer_owed(session: Any, view: Mapping | None, *, owner_id: str, session_repo, db_factory,
                      organization_id: str = "", write_fn=None) -> str:
    """The reply owed to the conversation's last requirement, written now from `view` and kept in the conversation
    (once: the conversation is read again before it is kept). "" when none is owed. Never raises."""
    from meshpipeline.application import part_matches as PM
    sid = str(getattr(session, "id", "") or "")
    note = owed_note(session)
    if note is None or not sid:
        return ""
    lock = _LOCKS.setdefault(sid, asyncio.Lock())
    async with lock:
        try:
            nid = PM.note_id(getattr(session, "user_notes", None), note)
            reply, source = await (write_fn or write)(
                note, view, note_id=nid, last_reply=last_reply_of(getattr(session, "messages", None) or ()),
                session_id=sid, user_id=owner_id)
            async with db_factory() as db:
                fresh = await session_repo.get_for_owner(db, session.id, owner_id,
                                                         organization_id=organization_id)
                if fresh is None or owed_note(fresh) != note:
                    return ""          # answered meanwhile, or the conversation moved on
                await session_repo.append_message(db, session.id, "assistant", reply)
                await db.commit()
            logger.info("plan reply: said (%s, source=%s) - session=%s", RULE, source, sid)
            return reply
        except Exception as exc:  # noqa: BLE001 - the card still shows the list; the next poll tries again
            logger.warning("plan reply: not said (%s: %s) - session=%s", type(exc).__name__, str(exc)[:200], sid)
            return ""


def last_reply_of(messages: Sequence[Any]) -> str:
    """Our last line in the conversation (its opener is not used again)."""
    for m in reversed(list(messages or ())):
        if isinstance(m, Mapping) and m.get("role") == "assistant":
            return str(m.get("content") or "")
    return ""


__all__ = ["MAX_CHARS", "PLAN_REPLY_PROMPT_BLOCKS", "RULE", "SOURCE_FALLBACK", "SOURCE_FIXED", "SOURCE_MODEL",
           "TIMEOUT_S", "check", "compose_system", "facts", "last_reply_of", "messages_for", "write"]
