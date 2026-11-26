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
  mesh" all count), that gives a guessed place as certain, that claims something was applied when nothing was (a
  condition - "the result will say if it was applied" - is no claim), that does not say none was found and ask for
  a click when the part has none of the kind asked for (NOTE-23), that cites a rule id, that runs past `MAX_CHARS`, or that exclaims; which check refused it is logged;
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
from meshpipeline.contracts import user_notes as UN

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
    counts = [r for r in rows if r.get("count")]
    for r in counts:
        # NOTE-24: a count is the whole mesh's; the run sizes the mesh to it (said as typed, with its bound)
        c = r.get("count") or {}
        applied.append({"what": f"an element count for the whole mesh: {r.get('size')}",
                        "count": {"bound": PT._C_CARD.get(str(c.get("what")), "about"),
                                  "number_as_typed": str(c.get("typed") or ""), "noun": str(c.get("noun") or "")},
                        "where": "the whole mesh", "place": "a count is always for the whole mesh"})
    for r in rows:
        if r.get("count"):
            continue
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
                    "faces_picked": [str(f.get("label") or f.get("id") or "") for f in faces],
                    # NOTE-22: a click sent alone answers their earlier words, which were for the place they clicked
                    **({"a_click_that_answers_their_earlier_words": str(note["answers"])}
                       if note.get("answers") else {})},
        "applied": applied,
        "flow_lines": PT.lines_mine(view, UN.said(note)),
        "could_not_place": [str(u.get("words") or u.get("quote") or "") for u in unsure],
        # NOTE-21: words that name ONE place where the part has several they do not tell apart: nothing set there,
        # one click asked for (how many there are, and what they are)
        "which_one": [{"words": str(u.get("words") or u.get("quote") or ""), "how_many_there_are": int(u["which_one"]),
                       "noun": str(u.get("noun") or "")} for u in unsure if PT.which_one_line(u)],
        # NOTE-23: words that name a kind of place the part has none of: nothing set anywhere, a click applies them
        "none_found": [{"words": str(u.get("words") or u.get("quote") or ""),
                        "kind_asked_for": PT.plural(str(u.get("none_of") or "")),
                        "the_part_has_none": True,
                        "what_the_part_has": str(u.get("found") or ""),
                        "nothing_was_set": True,
                        "what_applies_it": "a click on the face they meant, then sending it again"}
                       for u in unsure if PT.none_line(u)],
        "picture": picture,
        # NOTE-24: a count beside sizes the user typed - which one wins on this route; the result tells which held
        "count_may_not_hold": PT.count_clash(view, counts),
        # NOTE-26: what what they set does to the mesh, when it is worth saying (application/plan_cost)
        **_cost_facts(view, rows),
        # what the checklist already holds from their earlier messages (so the reply can speak like one who knows it)
        "already_on_the_checklist": [{"what": PT.row_what(r), "where": PT.row_place(r)[0]}
                                     for r in (view or {}).get("rows") or ()
                                     if isinstance(r, Mapping) and r not in rows][:MAX_LISTED],
        # NOTE-25: nothing on the checklist from it - what really happens to the words (kept: NOTE-18; read by every
        # route's planning step as guidance: NOTE-2; told back with the result: NOTE-8, NOTE-12)
        **_kept_facts(note, view, note_id=note_id),
        "no_checklist": view is None and bool(text.strip()),
        # nothing reads it before the run on this route: kept, read when the mesh is built - never "couldn't read"
        "kept_for_the_run": PT.NO_CHECKLIST in PT.reply_for(note, view, note_id=note_id),
        # the plain sentence that says exactly this (plan_talk.reply_for): the guard's source of truth
        "plain_version_that_is_exactly_right": PT.reply_for(note, view, note_id=note_id),
    }


def _cost_facts(view: Mapping | None, rows) -> dict[str, Any]:
    """NOTE-26: the plan's size and time before and after what the user set, as the model may say it; {} when
    nothing was set by this message or the change is too small to say (PT.cost_mine)."""
    cost = PT.cost_mine(view, rows)
    if not cost:
        return {}
    b, a = cost.get("before") or {}, cost.get("after") or {}
    out: dict[str, Any] = {"elements_before": PT.count_words(b.get("elements")),
                           "elements_after": PT.count_words(a.get("elements")),
                           "needs_a_bigger_machine": bool(cost.get("bigger_machine")),
                           "say_it_like": PT.cost_line(cost)}
    tb, ta = PT.usual_minutes(b), PT.usual_minutes(a)
    if tb and ta:
        out.update(minutes_before=tb, minutes_after=ta)
    return {"what_it_costs": out}


def _kept_facts(note: Mapping, view: Mapping | None, *, note_id: str = "") -> dict[str, Any]:
    """NOTE-25: for words the checklist took nothing from, what the run really does with them; {} otherwise."""
    if not PT.took_nothing(note, view, note_id=note_id):
        return {}
    if PT.geometry_edit(UN.said(note)):
        return {"not_by_meshing": "they ask for a change to the CAD itself (removing or filling features), which "
                                  "meshing cannot do: the mesh follows the part as it is. Their words are kept with "
                                  "the request, and the result will say it was not applied"}
    asked = PT.asks_of_the_mesh(UN.said(note))
    return {"kept_for_planning": {
        "what_happens": "nothing is set from these words now; they are kept, and you keep them in mind when the "
                        "mesh is planned (the planning step reads them as guidance)",
        "they_asked_something_of_the_mesh": asked,
        **({"the_result": "the result will say what came of it"} if asked else {})}}


# #
# the prompt: its own block registry, composed in order
# #

def _block_voice() -> str:
    return ("You are the meshing assistant, talking with an engineer in chat while they set up a mesh. Answer THE "
            "PERSON, the way a friendly colleague would: first person ('I've set', 'I'll go finer', 'I took that "
            "as'), contractions, everyday words. When you did what they asked, you may start with a short, natural "
            "acknowledgement that fits what they said, a different one each time: a question like 'can you do X?' "
            "gets 'Sure', 'Yep' or 'Done'; a statement gets 'Okay', 'All right', 'Sounds good', 'Will do' or "
            "'Done', or no opener at all - just say what you did. Use 'Got it' rarely, and never when your last "
            "reply (it is given) opened with it. When you could NOT do it, or could not tell where, give no "
            "acknowledgement at all (words you keep in mind for the planning are not that: see below): say it straight ('I couldn't tell where the lip is'). Then say what you set "
            "and where, in your own words, as short as it can be said. One or two short sentences. No exclamation "
            "marks, no emoji, no 'Certainly', no 'Great question', no 'As an AI'.\n")


def _block_facts_only() -> str:
    return ("Say ONLY what the FACTS say. Name the place naturally: 'Face 23', 'that face', 'the 3 faces you "
            "picked', 'the 22 mm hole', 'the whole mesh'. Never mention mesh quality, time, an element or cell "
            "count the facts do not hold, or anything that will happen beyond the facts. Never use a number that is not in the facts. If nothing "
            "was applied, do not say or suggest that anything was set: say plainly what you couldn't take and what "
            "they can do (kept_for_planning is different: see below). Nothing has been meshed yet at this point: speak of what you've SET or what you'll do "
            "('I've set 1 mm on Face 23', 'I'll go finer at that hole'), never of a mesh already made ('I've made the "
            "mesh finer' is wrong). If kept_for_the_run is true, nothing reads it before the run: say you've noted it (with "
            "the size and face from their message) and, in a few words of your own, that the result will show "
            "whether this mesher could apply it; never say it is applied or set, and never say you couldn't read "
            "it. Never cite rule ids.\n")


def _block_guess_and_picture() -> str:
    return ("If a place is your guess, say so as yours ('I took that as the 22 mm hole') and that they can click it "
            "on the model if they meant another spot. If some words could not be placed, say which and that they can "
            "click the spot on the model and send it again. If which_one lists words, they named ONE place and the "
            "part has that many: say how many there are, that you can't tell which one they mean (from the picture "
            "either, if one came), that you haven't set it anywhere yet, and ask them to click it on the model and send it "
            "again. If none_found lists words, they asked for a kind of place the part has none of: say you couldn't "
            "find any of that kind on this part and, briefly, what it does have, that you haven't set it anywhere, and "
            "ask them to click the face they mean and send it again. If a picture came, add ONE short clause, in your own "
            "words, that you can't read the picture itself so you went by their words (or by the face they "
            "clicked); nothing more about the picture.\n")


def _block_count() -> str:
    return ("If applied holds a count, they gave the whole mesh an element (or cell) count: say you'll size the mesh "
            "to it, with their number as they typed it and its bound in plain words ('under 1.5 million elements', "
            "'at least 2 million cells', 'about 800k elements'). A count needs no place. If count_may_not_hold says "
            "something, the count may not fit with sizes they typed: say in a few natural words which one wins and "
            "that the result will say if the count couldn't be held; never promise the count. You may mention what "
            "is already_on_the_checklist when it helps, never as something this message set.\n")


def _block_cost() -> str:
    return ("If what_it_costs is there, what they set changes how big the mesh will be: after saying what you set, add "
            "ONE short plain sentence with the element count before and after, as the facts word it ('about 0.7 "
            "million to about 2.9 million elements'), the minutes before and after when given ('roughly 25 minutes "
            "instead of 8'), and that it needs a bigger machine only if needs_a_bigger_machine is true. say_it_like "
            "is one way to say it. No warning tone, no 'are you sure', no advice to change it: just the cost.\n")


def _block_examples() -> str:
    return ("Examples of the voice (the facts differ each time; never copy their numbers, and do not reuse their "
            "openers in the same order):\n"
            "They: 'can you do a 0.5 mm mesh sizing in here?' (Face 23 picked; applied 0.5 mm on Face 23) -> "
            "'Sure, I've set 0.5 mm elements on Face 23.'\n"
            "They: '1 mm on these too' (3 faces picked; applied 1 mm on them) -> 'Done, those 3 faces get 1 mm "
            "elements as well.'\n"
            "They: 'in that big hole I want a refined mesh, see the picture' (picture; finer at the hole, 22 mm "
            "across, your guess) -> 'Okay, I'll go finer in that hole. I took it as the 22 mm one, so click it on "
            "the model if you meant another spot. I can't read the picture itself, so I went by your words.'\n"
            "They: '2 mm on the flange' (applied 2 mm on the flange, your guess) -> 'I've put 2 mm on the flange. "
            "That's my read of where you meant; click it on the model if not.'\n"
            "They: 'keep skewness under 0.8' (applied to the whole model) -> 'Will do, I'll hold skewness under 0.8 "
            "across the whole mesh.'\n"
            "They: 'use 3 mm everywhere' (applied 3 mm on the whole model) -> 'All right, 3 mm elements across the "
            "whole part.'\n"
            "They: '1 mm on the lip' (could not be placed) -> 'I couldn't tell where the lip is. Click it on the "
            "model and send that again.'\n"
            "They: 'make it finer at this fillet' (picture; which_one: 7 fillets) -> 'There are 7 fillets on this "
            "part and I can't tell which one you mean from the picture, so I haven't set it yet. Click it on the "
            "model and send that again.'\n"
            "They: 'keep it under 1.5 million elements' (applied: a count, at most 1.5 million elements) -> "
            "'Will do, I'll keep the mesh under 1.5 million elements.'\n"
            "They: 'about 800k elements' (applied: a count; count_may_not_hold: their 2 mm size wins) -> 'Okay, "
            "I'll aim for about 800k elements. Your 2 mm size comes first, so if both can't fit, the result will "
            "say so.'\n"
            "They: 'y+ of 1 on the walls please' (kept_for_the_run) -> 'Noted, y+ of 1 on the walls. The result "
            "will show whether this mesher could apply it.'\n"
            "They: 'the part is cast aluminium' (kept_for_planning) -> 'Got it, cast aluminium. I'll keep that in "
            "mind when the mesh is planned.'\n"
            "They: 'it's for a stress analysis' (kept_for_planning) -> 'Noted, it's for a stress analysis. I'll keep "
            "that in mind for the mesh planning.'\n"
            "They: 'make it a good quality mesh please' (kept_for_planning, they_asked_something_of_the_mesh) -> "
            "'Okay, a good quality mesh. I'll keep that in mind when the mesh is planned, and the result will say "
            "what came of it.'\n"
            "They: 'remove the small holes' (not_by_meshing) -> 'Removing holes is a change to the CAD itself, which "
            "meshing can't do, so the mesh keeps them. I've kept your note, and the result will say it wasn't "
            "applied.'\n")


def _block_kept() -> str:
    return ("If kept_for_planning is there, this message set nothing and that is fine: answer like a person who "
            "heard them, in ONE short sentence, two at most. A short opener fits here ('Got it', 'Noted', 'Okay'); "
            "say what they told you in a few words of your own (never their whole sentence, never in quotation "
            "marks) and that you'll keep it in mind when the mesh is planned. Only if "
            "they_asked_something_of_the_mesh is true may you add that the result will say what came of it. No "
            "list of what they can set, no 'checklist', no 'lock in', no 'mesh setting', and never say it is set, "
            "applied, done or guaranteed. If not_by_meshing is there, they asked for a "
            "change to the CAD itself: say meshing can't do that, so the mesh follows the part as it is, and that "
            "you've kept their words and the result will say it wasn't applied.\n")


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
    ("guess_and_picture", "a guess said as mine with how to correct it; words not placed said; a kind of place the "
                          "part has none of: none found, what it has, nothing set, a click asked; a picture: kept, "
                          "not read, once", _block_guess_and_picture),
    ("count", "a whole-mesh count said with its typed number and bound, no place needed; beside typed sizes, which "
              "wins and that the result tells it, never a promise", _block_count),
    ("cost", "what they set does to the mesh, when worth saying: elements and minutes before and after, a bigger "
             "machine if needed, one plain sentence, no warning", _block_cost),
    ("kept", "words that set nothing: one or two short natural sentences - what they said, in my words, kept in "
             "mind when the mesh is planned (the result told only when they asked something of the mesh); no menu, "
             "no jargon; a change to the CAD: meshing can't, kept, told; never 'set'", _block_kept),
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
#: NOTE-21: one place among several is asked for by a click
_CLICK = re.compile(r"\b(?:click|pick|select)\w*\b", re.I)
#: NOTE-23: a kind of place the part has none of, said as none found
_NONE_SAID = re.compile(r"\b(?:couldn'?t find|could not find|can'?t find|cannot find|didn'?t find|did not find|"
                        r"found no|find no|doesn'?t have|does not have|has no|have no|there (?:are|is) no|"
                        r"there aren'?t|there isn'?t|no \w+(?: \w+)? (?:on|in) (?:this|the) part|none)\b", re.I)
#: a requirement kept for the run is said as kept, never as unreadable
_UNREAD = re.compile(r"\b(?:couldn'?t|could not|can'?t|cannot|unable|didn'?t|did not)\b", re.I)
_KEPT = re.compile(r"\b(?:kept|keep(?:ing)?|noted|noting|saved|hold(?:ing)? on to|held|remember\w*|in mind|"
                   r"made a note|take note|taken note)\b", re.I)
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


#: NOTE-24: a count's bound in the reply's words
_BOUND_WORDS = {
    "at most": re.compile(r"\b(?:under|below|at most|no more than|max\w*|less than|fewer than|cap\w*|within|"
                          r"limit\w*|ceiling)\b", re.I),
    "at least": re.compile(r"\b(?:at least|over|above|more than|min\w*|no fewer|no less|floor)\b", re.I),
    "about": re.compile(r"\b(?:about|around|roughly|approximately|close to|near|aim\w*|target\w*)\b", re.I),
}
#: a count that may not hold, said as sure
_SURE = re.compile(r"\b(?:guarantee\w*|promise\w*|for sure|definitely|no matter what|whatever happens)\b", re.I)
#: ... and said as one that may give way: a condition, or who wins, or that the result tells it
_MAY = re.compile(r"\b(?:if|may|might|could|can'?t|cannot|won'?t|unless|depend\w*|result|wins?|first|"
                  r"gives? way|gave way|priority)\b", re.I)


#: NOTE-25: a claim that kept words were set (first person included), unless a negation stands just before it
_SET_CLAIM = re.compile(r"\b(?:applied|applying|in place|locked in|i(?:'| ha)ve (?:set|made|added|put|applied|gone)|"
                        r"i set|i(?:'| wi)ll (?:set|use|apply|make|put|add|go|hold)|is set|are set|now set|set to|"
                        r"set at|set on|sized|guarantee\w*|will be (?:applied|set|used)|done)\b", re.I)
_NEGATED = re.compile(r"(?:n't|n’t|\bnot|\bnever|\bno|\bcannot)\b[^.;]{0,25}$", re.I)
_JARGON = re.compile(r"\bmesh settings?\b", re.I)
#: NOTE-25: words kept for the planning are answered like a person: none of our form words, no menu of what can be set
_FORM_WORDS = re.compile(r"\b(?:mesh settings?|checklist|lock(?:ed)? (?:it |that |this )?in)\b", re.I)
_MENU = re.compile(r"\b(?:(?:i|you) can (?:set|give|type|click|pick|add)|click\w*|quality limit|element count|"
                   r"if you want something)\b", re.I)
_RESULT = re.compile(r"\bresults?\b", re.I)
_QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
#: the most sentences a kept reply has (one more when a picture came, for its one clause)
KEPT_SENTENCES = 2


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.?])\s+", text.strip()) if s.strip()])


def _quotes_them(text: str, f: Mapping) -> bool:
    """Their words quoted back: a quoted span of three words or more that is their own sentence (or most of it)."""
    said = " ".join(str((f.get("message") or {}).get("words") or "").casefold().split())
    for m in _QUOTED.finditer(text):
        span = " ".join(m.group(1).casefold().strip(" .,!?").split())
        if len(span.split()) >= 3 and span in said:
            return True
    return False


def _kept_check(text: str, f: Mapping) -> str:
    """NOTE-25: a reply to words kept for the planning reads like a person: short, their words not quoted back, no
    form words, no menu, and the result told only when they asked something of the mesh. "" when it may be said."""
    kept = f.get("kept_for_planning") or {}
    if _FORM_WORDS.search(text):
        return "our form words ('checklist', 'lock in', 'mesh setting')"
    if _MENU.search(text):
        return "a list of what can be set"
    if _sentences(text) > KEPT_SENTENCES + (1 if (f.get("message") or {}).get("pictures") else 0):
        return "a kept note said in more than two sentences"
    if _quotes_them(text, f):
        return "their sentence quoted back"
    if _RESULT.search(text) and not kept.get("they_asked_something_of_the_mesh"):
        return "the result promised for plain context"
    return ""


#: ... or said as a condition, not as done: "whether it could apply", "will say if it was applied", "may be applied"
_IF_BEFORE = re.compile(r"(?:\bwhether|\bif)\b[^.;,]{0,40}$|"
                        r"\b(?:may|might|could|would|can)(?:\s+not)?(?:\s+(?:be|have been|get|have))?\s*$", re.I)
_IF_AFTER = re.compile(r"^[^.;,]{0,30}\b(?:if|whether|unless)\b", re.I)


def _claims_set(text: str) -> bool:
    """A claim that it IS or WAS set, applied, done or guaranteed (or a firm promise that it will be) - not one that
    is negated ("it wasn't applied") or said as a condition ("the result will say if it was applied")."""
    return any(not (_NEGATED.search(text[:m.start()]) or _IF_BEFORE.search(text[:m.start()])
                    or _IF_AFTER.search(text[m.end():])) for m in _SET_CLAIM.finditer(text))


def _count_check(text: str, f: Mapping) -> str:
    """NOTE-24: a count is said with its own number and bound, and - beside sizes it may not fit with - never as
    sure. "" when the reply may be said."""
    for a in f.get("applied") or ():
        c = a.get("count")
        if not isinstance(c, Mapping):
            continue
        if not _numbers(str(c.get("number_as_typed") or "")) <= _numbers(text):
            return "the count not said as typed"
        bound = str(c.get("bound") or "")
        said = {k for k, pattern in _BOUND_WORDS.items() if re.search(pattern, text)}
        if bound in _BOUND_WORDS and bound not in said and said:
            return "a count said with another bound"
    if f.get("count_may_not_hold") and (_SURE.search(text) or not _MAY.search(text)):
        return "a count that may not hold said as sure"
    return ""


_BIGGER = re.compile(r"\b(?:bigger|larger|faster|more powerful)\s+machine\b", re.I)


def _cost_check(text: str, f: Mapping) -> str:
    """NOTE-26: a cost worth saying is said - the element count after, as the facts word it - and a bigger machine is
    said only when it is needed. "" when the reply may be said."""
    cost = f.get("what_it_costs")
    if not isinstance(cost, Mapping):
        return ""
    if not _numbers(str(cost.get("elements_after") or "")) <= _numbers(text):
        return "the cost not said"
    if _BIGGER.search(text) and not cost.get("needs_a_bigger_machine"):
        return "a bigger machine said when none is needed"
    return ""


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
        # (NOTE-24: a count is the whole mesh's by nature - it names no place)
        if any(not a.get("count") for a in f["applied"]) and not _place_named(text, f):
            return "no place named"
        why = _count_check(text, f) or _cost_check(text, f)
        if why:
            return why
        if any(str(a.get("place") or "").startswith("YOUR guess") for a in f["applied"]) \
                and not _GUESS_SAID.search(text):
            return "a guessed place not said as a guess"
    if f.get("kept_for_planning") or f.get("not_by_meshing"):
        # NOTE-25: kept words are said as kept - never as set, applied or guaranteed, never in our jargon
        if _claims_set(text):
            return "claims something was applied when nothing was (a kept note)"
        if not _KEPT.search(text):
            return "a kept note not said as kept"
        if _JARGON.search(text):
            return "our jargon ('mesh setting')"
        why = _kept_check(text, f) if f.get("kept_for_planning") else ""
        if why:
            return why
    elif f.get("kept_for_the_run") and not applied:
        # kept for a run that reads it when the mesh is built: "noted", "the result will show whether it could apply
        # it" are not claims - only that it is (or was) set, applied, done or guaranteed is
        if _claims_set(text):
            return "claims something was applied when nothing was (kept for the run)"
    elif not applied and _CLAIM.search(text):
        return "claims something was applied when nothing was"
    if f.get("kept_for_the_run") and (_UNREAD.search(text) or not _KEPT.search(text)):
        return "a kept requirement not said as kept"
    if f.get("which_one") and not _CLICK.search(text):
        return "one place among several, and no click asked for"
    if f.get("none_found"):
        # NOTE-23: a kind of place the part has none of - said as none found, a click asked for, nothing said as set
        if not _NONE_SAID.search(text):
            return "a kind the part has none of, not said as none found"
        if not _CLICK.search(text):
            return "a kind the part has none of, and no click asked for"
        if not f.get("applied") and _claims_set(text):
            return "claims something was applied when nothing was (none found)"
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
            if asked_not_set(note, view, note_id=nid):
                # NOTE-20: the checklist took nothing and the words ask a question: answered as the question it is,
                # from the numbers the product holds before the run - never "I couldn't find a mesh setting"
                from meshpipeline.application import fea_check
                plan = await asyncio.to_thread(fea_check.planned_sizes, sid)
                got = await answered(str(note.get("text") or ""), view, plan, faces=list(note.get("faces") or ()),
                                     pictures=len(PT.pictures_of(note)),
                                     last_reply=last_reply_of(getattr(session, "messages", None) or ()),
                                     session_id=sid, user_id=owner_id)
                reply, source = got["text"], got["source"]
            else:
                reply, source = await (write_fn or write)(
                    note, view, note_id=nid, last_reply=last_reply_of(getattr(session, "messages", None) or ()),
                    session_id=sid, user_id=owner_id)
            async with db_factory() as db:
                fresh = await session_repo.get_for_owner(db, session.id, owner_id,
                                                         organization_id=organization_id)
                if fresh is None or owed_note(fresh) != note:
                    return ""          # answered meanwhile, or the conversation moved on
                waiting = PT.waiting_ask(note, view, note_id=nid)
                if waiting:
                    # NOTE-22: the one-place ask waits with the plan-step state (it survives a reload): a click sent
                    # alone next applies it to exactly the faces clicked (agents/intake/message._settle)
                    from meshpipeline.agents.intake import message as M
                    await M.keep_one_place_ask(db, fresh, waiting, session_repo=session_repo)
                    logger.info("plan reply: a one-place ask waits for a click (%s) - session=%s", PT.CLICK_RULE, sid)
                await session_repo.append_message(db, session.id, "assistant", reply)
                await db.commit()
            logger.info("plan reply: said (%s, source=%s) - session=%s", RULE, source, sid)
            return reply
        except Exception as exc:  # noqa: BLE001 - the card still shows the list; the next poll tries again
            logger.warning("plan reply: not said (%s: %s) - session=%s", type(exc).__name__, str(exc)[:200], sid)
            return ""


# #
# NOTE-20: a question about the mesh, answered from the numbers the product holds before the run
# #

QUESTION_RULE = PT.QUESTION_RULE
#: an answer may say a little more than a requirement's reply (the sizes, the requirements so far, what is not known)
MAX_ANSWER_CHARS = 500
#: the requirements so far listed in the facts at most
MAX_LISTED = 8


def _size_there(row: Mapping, step: Mapping) -> str:
    """What one requirement does to the size, in words the answer may repeat: a typed size as typed; "finer" /
    "coarser" as the one step it takes (PLAN-fea-36) - the exact size there is the run's."""
    size = str(row.get("size") or "")
    if size in ("finer", "coarser"):
        k = step.get(size)
        if not k:
            return (f"{size} than the rest; the exact size there is settled when the mesh is built")
        return (f"about {'half' if size == 'finer' else 'twice'} the size the mesh would have had there "
                f"(x{float(k):g}); the exact size there is settled when the mesh is built")
    if size and size[0].isdigit():
        return f"{size}, as you typed it"
    return ""


def question_facts(text: str, view: Mapping | None, plan: Mapping | None, *, faces: Sequence[Any] = (),
                   pictures: int = 0) -> dict[str, Any]:
    """NOTE-20: what the product really holds for a question about the mesh, as the model is told it: the plan's own
    numbers (application/fea_check.planned_sizes - made before the user's requirements), each requirement so far from
    the checklist (what, where, and what it does to the size), the phrases still waiting for a click, and what is not
    known yet with when it will be. Pure."""
    plan = dict(plan or {})
    step = plan.get("step") or {}
    rows = [r for r in (view or {}).get("rows") or () if isinstance(r, Mapping)]
    reqs = []
    for r in rows[:MAX_LISTED]:
        where, _how = PT.row_place(r)
        item = {"what": PT.row_what(r), "where": where}
        there = _size_there(r, step)
        if there:
            item["size_there"] = there
        reqs.append(item)
    waiting = [{"words": str(u.get("words") or u.get("quote") or ""),
                **({"places_it_could_be": int(u["which_one"]), "noun": str(u.get("noun") or "")}
                   if PT.which_one_line(u) else {})}
               for u in (view or {}).get("unsure") or () if isinstance(u, Mapping)][:MAX_LISTED]
    numbers = {k: plan[k] for k in ("coarsest_mm", "parts_mm", "finest_mm", "finest_where", "elements", "time")
               if plan.get(k)}
    not_known: list[str] = []
    if not plan.get("known"):
        if plan.get("why"):
            not_known.append(str(plan["why"]))
    elif not plan.get("elements"):
        not_known.append("The element count is known once the run starts.")
    f: dict[str, Any] = {
        "question": {"words": str(text or ""), "pictures": int(pictures),
                     "faces_picked": [str(x.get("label") or x.get("id") or "") for x in faces or ()
                                      if isinstance(x, Mapping)]},
        "plan": ({**numbers, "planned": "by the geometry check, before your requirements"} if numbers else {}),
        "your_requirements": reqs,
        "more_requirements": max(0, len(rows) - MAX_LISTED),
        "waiting_for_a_click": waiting,
        "still_being_read": bool(view is not None and view.get("status") == "reading"),
        "not_known_yet": [s for s in not_known if s],
    }
    f["plain_version_that_is_exactly_right"] = answer_plain(f)
    return f


def _mm_range(v) -> str:
    return f"{v[0]:g} to {v[1]:g} mm" if isinstance(v, (list, tuple)) and len(v) == 2 else ""


def answer_plain(f: Mapping) -> str:
    """The plain answer that says exactly the facts (the model's fallback and the guard's source of truth)."""
    plan = f.get("plan") or {}
    out: list[str] = []
    if plan.get("coarsest_mm") or plan.get("parts_mm"):
        size = (f"about {_mm_range(plan['parts_mm'])} on the parts" if plan.get("parts_mm")
                else f"about {plan['coarsest_mm']:g} mm where nothing needs finer")
        fine = (f", down to about {plan['finest_mm']:g} mm at {plan['finest_where']}"
                if plan.get("finest_mm") and plan.get("finest_where") else "")
        out.append(f"Before your requirements, the plan sizes the mesh at {size}{fine}.")
    reqs = list(f.get("your_requirements") or ())
    if plan.get("elements"):
        out.append(f"The check planned {plan['elements']}"
                   + ("; the count with your requirements comes once the run starts." if reqs else "."))
    if reqs:
        bits = [f"{r['what'][:1].lower()}{r['what'][1:]} on {r['where']}" for r in reqs[:3]]
        more = len(reqs) - 3 + int(f.get("more_requirements") or 0)
        out.append("Your requirements: " + PT._and(bits) + "." + (f" There are {more} more in the list."
                                                                   if more > 0 else ""))
        steps = {"half" if "about half" in str(r.get("size_there")) else "twice" for r in reqs
                 if "settled when the mesh is built" in str(r.get("size_there") or "")}
        if steps:
            out.append(f"Where you asked {'finer' if steps == {'half'} else 'coarser' if steps == {'twice'} else 'finer or coarser'}"
                       f" it'll be about {' or '.join(sorted(steps))} the size around it; the exact size is set when "
                       "the mesh is built.")
    for w in list(f.get("waiting_for_a_click") or ())[:2]:
        out.append(f"“{w['words']}” is not set yet: click the place on the model and send it again.")
    if f.get("still_being_read"):
        out.append("Your latest requirement is still being read; the list will show it in a moment.")
    out += [str(s) for s in f.get("not_known_yet") or ()]
    return " ".join(out) or PT.ASK_LINE


def _block_answer_voice() -> str:
    return ("You are the meshing assistant, talking with an engineer in chat before their mesh is built. They asked "
            "a question about the mesh. Answer it the way a friendly colleague would: plainly, first person, "
            "contractions, everyday words, one to three short sentences. Answer what they asked first. No "
            "exclamation marks, no emoji, no 'Certainly', no 'Great question', no 'As an AI'.\n")


def _block_answer_facts() -> str:
    return ("Say ONLY what the FACTS say. Every number you write must be in the facts exactly: never work out a new "
            "number yourself (no multiplying, no adding, no rounding to another figure). For a place asked finer or "
            "coarser with no number, say it will be about half (or twice) the size around it and that the exact "
            "size is settled when the mesh is built. Give an element count or a time only if the facts have one, and "
            "say a planned count was planned before their requirements. If something is not known yet, say what and "
            "when, in one short sentence. Nothing has been set by this message and nothing has been meshed yet. "
            "Never cite rule ids.\n")


def _block_answer_examples() -> str:
    return ("Examples of the voice (the facts differ each time; never copy their numbers):\n"
            "They: 'so what would the element size be roughly' (plan: about 4 mm where nothing needs finer; finer on "
            "the 6 faces you picked; count not known) -> 'Roughly 4 mm over most of the part. On the 6 faces you "
            "picked it'll be about half that, and I'll have the element count once the run starts.'\n"
            "They: 'how many elements will it be' (plan: about 180,000 elements, planned before the requirements) "
            "-> 'About 180,000 elements as planned before your requirements; your finer places will add to that, "
            "and I'll have the real count once the run starts.'\n"
            "They: 'what size will it use' (no plan numbers: not known until the run) -> 'I don't have the size yet: "
            "it's worked out from the measured part when the mesh is built, and I'll have it once the run starts.'\n")


def _block_answer_language() -> str:
    return ("Answer in the language the user wrote in. Plain text only: no markdown, no lists, no quotes around the "
            "whole reply. At most 500 characters.\n")


#: THE QUESTION ANSWERER'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text it gets
PLAN_ANSWER_PROMPT_BLOCKS: tuple = (
    ("voice", "answer the question like a colleague: first person, plain, one to three sentences, no filler",
     _block_answer_voice),
    ("facts_only", "only the facts' own numbers, none worked out; finer/coarser as about half/twice with the exact "
                   "size at the build; a count or time only when given; what is not known said with when",
     _block_answer_facts),
    ("examples", "three example exchanges in that voice", _block_answer_examples),
    ("language", "the user's own language; plain text; at most 500 characters", _block_answer_language),
)


def compose_answer_system() -> str:
    return "".join(build() for _name, _purpose, build in PLAN_ANSWER_PROMPT_BLOCKS)


def answer_messages(f: Mapping, *, last_reply: str = "") -> list[dict]:
    body = {"facts": dict(f), "your_last_reply": last_reply}
    return [{"role": "system", "content": compose_answer_system()},
            {"role": "user", "content": "Answer this question.\n" + json.dumps(body, ensure_ascii=False)}]


#: a count said in words ("a million", "a few thousand"): only when the facts say one
_BIG_WORDS = re.compile(r"\b(?:hundreds?|thousands?|millions?|billions?|dozens?)\b", re.I)
_SMALL_WORDS = {w: str(n) for n, w in _COUNT_WORDS.items()}
#: "I've set..." in an answer: only when there is a requirement to speak of
_SET_NOW = re.compile(r"\bi(?:'| ha)ve (?:set|added|put|applied|made)\b|\bi(?:'| wi)ll (?:set|add|apply)\b", re.I)


def check_answer(reply: str, f: Mapping) -> str:
    """NOTE-20: why a model-written answer may not be said ("" when it may), against the facts `f`: empty; longer than
    MAX_ANSWER_CHARS; a rule id; an exclamation, filler or emoji; a number the facts do not hold, in figures or in
    words ("a million", "six"); a claim that something is set when no requirement is."""
    text = " ".join(str(reply or "").split())
    if not text:
        return "empty"
    if len(text) > MAX_ANSWER_CHARS:
        return "too long"
    if _RULE_ID.search(text):
        return "a rule id"
    if _BANNED.search(text):
        return "an exclamation, filler or emoji"
    held = json.dumps(f, ensure_ascii=False)
    stray = sorted(_numbers(text) - _numbers(held))
    if stray:
        return f"a number not in the facts ({', '.join(stray)})"
    low, held_low = text.casefold(), held.casefold()
    big = sorted({w.casefold().rstrip("s") for w in _BIG_WORDS.findall(text)} - {
        w.casefold().rstrip("s") for w in _BIG_WORDS.findall(held)})
    small = sorted(w for w, n in _SMALL_WORDS.items() if re.search(rf"\b{w}\b", low)
                   and n not in _numbers(held) and not re.search(rf"\b{w}\b", held_low))
    if big or small:
        return f"a number not in the facts ({', '.join(big + small)})"
    if not f.get("your_requirements") and _SET_NOW.search(text):
        return "claims something is set when nothing is"
    return ""


async def answered(text: str, view: Mapping | None, plan: Mapping | None, *, faces: Sequence[Any] = (),
                   pictures: int = 0, last_reply: str = "", session_id: str = "", user_id: str = "",
                   timeout_s: float = TIMEOUT_S, ask_fn=None) -> dict[str, str]:
    """NOTE-20: the answer to a question about the mesh: {"text", "source", "raw", "why"}, written by the intake's
    model from `question_facts` and checked by `check_answer`; refused, failed or slow, the plain answer. Never
    raises."""
    f = question_facts(text, view, plan, faces=faces, pictures=pictures)
    plain = str(f["plain_version_that_is_exactly_right"])
    try:
        said = await asyncio.wait_for((ask_fn or _ask)(answer_messages(f, last_reply=last_reply),
                                                       job_id=f"plan-answer-{session_id}", user_id=user_id),
                                      timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain answer (NOTE-20)
        logger.info("plan answer: the plain answer (%s, %s) - session=%s", QUESTION_RULE, type(exc).__name__,
                    session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "raw": "", "why": f"no answer ({type(exc).__name__})"}
    said = " ".join(str(said or "").strip().strip('"“”').split())
    why = check_answer(said, f)
    if why:
        logger.info("plan answer: the model's answer refused by the check '%s' (%s) - session=%s", why,
                    QUESTION_RULE, session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "raw": said, "why": why}
    return {"text": said, "source": SOURCE_MODEL, "raw": said, "why": ""}


def asked_not_set(note: Mapping, view: Mapping | None, *, note_id: str = "") -> bool:
    """NOTE-20, THE SAFE SIDE OF "I couldn't find a mesh setting in that": a message kept as a requirement from which
    the checklist took nothing, whose words ask a question - it is answered as the question it is."""
    text = str(note.get("text") or "")
    return bool(text.strip()) and view is not None and PT.asks(text) and PT.took_nothing(note, view, note_id=note_id)


async def answer_question(session: Any, view: Mapping | None, plan: Mapping | None, *, owner_id: str, session_repo,
                          db_factory, organization_id: str = "", text: str = "", faces: Sequence[Any] = (),
                          pictures: int = 0, ask_fn=None) -> str:
    """NOTE-20: the answer to the conversation's last message - a question about the mesh - written now and kept in
    the conversation, once (the conversation is read again first: an answer already given, or a conversation that
    moved on, keeps nothing). Never raises; "" when nothing was kept."""
    sid = str(getattr(session, "id", "") or "")
    messages = list(getattr(session, "messages", None) or [])
    if not sid or not messages or not isinstance(messages[-1], Mapping) or messages[-1].get("role") != "user":
        return ""
    asked = messages[-1]
    text = text or str(asked.get("content") or "")
    lock = _LOCKS.setdefault(sid, asyncio.Lock())
    async with lock:
        try:
            got = await answered(text, view, plan, faces=faces or list(asked.get("faces") or ()), pictures=pictures,
                                 last_reply=last_reply_of(messages), session_id=sid, user_id=owner_id, ask_fn=ask_fn)
            async with db_factory() as db:
                fresh = await session_repo.get_for_owner(db, session.id, owner_id, organization_id=organization_id)
                last = list(getattr(fresh, "messages", None) or [])[-1:] if fresh is not None else []
                if not last or not isinstance(last[0], Mapping) or last[0].get("role") != "user":
                    return ""          # answered meanwhile, or the conversation moved on
                await session_repo.append_message(db, session.id, "assistant", got["text"])
                await db.commit()
            logger.info("plan answer: said (%s, source=%s) - session=%s", QUESTION_RULE, got["source"], sid)
            return got["text"]
        except Exception as exc:  # noqa: BLE001 - the question stays; the user can ask again
            logger.warning("plan answer: not said (%s: %s) - session=%s", type(exc).__name__, str(exc)[:200], sid)
            return ""


def last_reply_of(messages: Sequence[Any]) -> str:
    """Our last line in the conversation (its opener is not used again)."""
    for m in reversed(list(messages or ())):
        if isinstance(m, Mapping) and m.get("role") == "assistant":
            return str(m.get("content") or "")
    return ""


__all__ = ["MAX_ANSWER_CHARS", "MAX_CHARS", "PLAN_ANSWER_PROMPT_BLOCKS", "PLAN_REPLY_PROMPT_BLOCKS", "QUESTION_RULE",
           "RULE", "SOURCE_FALLBACK", "SOURCE_FIXED", "SOURCE_MODEL", "TIMEOUT_S", "answer_messages", "answer_plain",
           "answer_question", "answered", "asked_not_set", "check", "check_answer", "compose_answer_system",
           "compose_system", "facts", "last_reply_of", "messages_for", "question_facts", "write"]
