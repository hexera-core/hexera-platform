# Responsibility: The plan step's answer to a requirement, written like a person (NOTE-19): the intake's model writes
#       one or two short sentences from the FACTS the checklist really produced for the message, a guard in code checks
#       them against those facts, and the plain sentence (agents/intake/plan_talk.reply_for) stands in when it fails.
# Owns: the facts handed to the model, the reply writer's prompt (its own block registry), the guard, the fallback.
# Boundaries: the model is the intake's own (contracts/model_inference.call_intake_model: its client, model and
#       settings); nothing here changes the plan or the notes. The reply's source is logged, never shown.
"""MODEL-WRITTEN REPLIES UNDER A GUARD (NOTE-19).

A requirement typed at the plan step is answered the way a sharp, friendly engineer colleague would answer in chat:
"All right - 1 mm on Face 32 (round Ø6 mm)." The words are the model's; what they may say is not:

- the model is given the user's message (its words, that a picture or file came with it, the faces picked) and the
  FACTS: what will be applied (size, finer/coarser, limit), where (face labels, feature names), whether the place was
  the user's click or the system's guess, what was not used (a number only in a picture) and what could not be
  placed; and the plain sentence that says exactly that (plan_talk.reply_for);
- the GUARD (`check`) refuses a reply that holds a number the facts do not, that names no place when something was
  applied, that claims something was applied when nothing was, that cites a rule id, that runs past `MAX_CHARS`, that
  exclaims or opens with the same words as our last reply;
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
                        "place": {"picked": "the user's own click on the model",
                                  "guessed": "the system's guess from the words: the user can click the model to "
                                             "correct it",
                                  "whole": "the whole model"}[how]})
    not_used = []
    if pictures and text.strip():
        not_used.append("a number that is only in the picture is not used (the user must type it)")
    if pictures and not text.strip():
        not_used.append("the picture alone sets nothing: it only helps show where typed words point")
    return {
        "message": {"words": text, "pictures": len(pictures), "files": len(files) - len(pictures),
                    "faces_picked": [str(f.get("label") or f.get("id") or "") for f in faces]},
        "applied": applied,
        "flow_lines": PT.lines_mine(view, text),
        "could_not_place": [str(u.get("words") or u.get("quote") or "") for u in unsure],
        "not_used": not_used,
        "no_checklist": view is None and bool(text.strip()),
        # the plain sentence that says exactly this (plan_talk.reply_for): the guard's source of truth
        "plain_version_that_is_exactly_right": PT.reply_for(note, view, note_id=note_id),
    }


# #
# the prompt: its own block registry, composed in order
# #

def _block_voice() -> str:
    return ("You answer one chat message from an engineer who is setting up a mesh. Write the way a sharp, friendly "
            "engineer colleague answers in chat: one or two short sentences, plain everyday words. Acknowledge what "
            "they said naturally and vary how you open (never the same opener as your last reply, which you are "
            "given). No 'Certainly', no 'Great question', no 'As an AI', no emoji, no exclamation marks.\n")


def _block_facts_only() -> str:
    return ("Say ONLY what the FACTS say. Say plainly what is now set and where (name the place as the facts name it: "
            "the face label, the feature, or 'the N faces you picked'). Never mention mesh quality, element or cell "
            "counts, time, or anything that will happen beyond the facts. Never use a number that is not in the "
            "facts. If nothing was applied, do not say or suggest that anything was set. Never cite rule ids.\n")


def _block_guess_and_unused() -> str:
    return ("If a place was the system's guess, say so in a few words and that they can click the model to correct "
            "it. If something could not be placed, say which words and that they can click the place on the model "
            "and send it again. If something was not used (a number only in a picture), say it in a few words.\n")


def _block_language() -> str:
    return ("Answer in the language the user wrote in. Plain text only: no markdown, no lists, no quotes around the "
            "whole reply. Keep face labels exactly as given. At most 300 characters.\n")


#: THE REPLY WRITER'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text it gets
PLAN_REPLY_PROMPT_BLOCKS: tuple = (
    ("voice", "one or two short sentences, like an engineer colleague in chat; varied opener; no filler, no emoji, "
              "no exclamation marks", _block_voice),
    ("facts_only", "only what the facts say: what is set and where; no quality, counts, time or promises; no number "
                   "or claim the facts do not hold; no rule ids", _block_facts_only),
    ("guess_and_unused", "a guessed place is said with how to correct it; what could not be placed or used is said",
     _block_guess_and_unused),
    ("language", "the user's own language; plain text; face labels as given; at most 300 characters",
     _block_language),
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
_CLAIM = re.compile(r"\b(?:applied|applying|added|in place|i(?:'| ha)ve set|i set|is set|are set|now set|set to|"
                    r"set at|set on|will use|now using|now uses|sized|using)\b", re.I)
_BANNED = re.compile(r"!|\bcertainly\b|\bas an ai\b|\bgreat question\b|[\U0001F300-\U0001FAFF☀-➿]", re.I)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n
            for n in _NUM.findall(str(text or ""))}


def _places(f: Mapping) -> list[str]:
    """The words that name where each applied fact is: its face names, its features, 'faces you picked'."""
    out: list[str] = []
    for a in f.get("applied") or ():
        where = str(a.get("where") or "")
        out += re.findall(r"\bFace \d+\b", where)
        if "faces you picked" in where:
            out += ["faces you picked", "faces you selected", "picked faces", "selected faces"]
        if "whole" in where or "rest" in where:
            out += ["whole", "everywhere", "rest of"]
        for piece in re.split(r",\s*|\s+and\s+", re.sub(r"\([^)]*\)", "", where)):
            piece = re.sub(r"^(?:the|a|an)\s+", "", piece.strip(), flags=re.I).strip()
            if len(piece) >= 3:
                out.append(piece)
    for label in (f.get("message") or {}).get("faces_picked") or ():
        out += re.findall(r"\bFace \d+\b", str(label))
    return out


def _opener(text: str) -> str:
    return " ".join(re.findall(r"[a-z']+", str(text or "").casefold())[:2])


def check(reply: str, f: Mapping, *, last_reply: str = "") -> str:
    """Why a model-written reply may not be said ("" when it may), against the facts `f`."""
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
        low = text.casefold()
        if not any(p.casefold() in low for p in _places(f)):
            return "no place named"
    if not applied and _CLAIM.search(text):
        return "claims something was applied when nothing was"
    if last_reply and _opener(text) and _opener(text) == _opener(last_reply):
        return "the same opener as the last reply"
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
    plain = PT.reply_for(note, view, note_id=note_id)
    if PT.fixed_reply(note, view):
        return plain, SOURCE_FIXED
    f = facts(note, view, note_id=note_id)
    try:
        said = await asyncio.wait_for(
            _ask(messages_for(note, view, note_id=note_id, last_reply=last_reply),
                 job_id=f"plan-reply-{session_id}", user_id=user_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain sentence (NOTE-19)
        logger.info("plan reply: the plain sentence (%s, %s) - session=%s", RULE, type(exc).__name__, session_id)
        return plain, SOURCE_FALLBACK
    said = " ".join(said.strip().strip('"“”').split())
    why = check(said, f, last_reply=last_reply)
    if why:
        logger.info("plan reply: the model's reply refused (%s: %s) - session=%s", RULE, why, session_id)
        return plain, SOURCE_FALLBACK
    logger.info("plan reply: written by the model (%s) - session=%s", RULE, session_id)
    return said, SOURCE_MODEL


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
