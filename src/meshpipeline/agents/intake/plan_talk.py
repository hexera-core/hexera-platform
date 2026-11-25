# Responsibility: The plan step as a conversation (NOTE-18): read what one message at the plan step means - start the
#       mesh, ask once, a mesh requirement, or a turn for the intake - and say back, in plain words, what the checklist
#       really took from a requirement.
# Owns: the "go" reading of the plan step, the one "Start the mesh now?" line, the reply built from checklist rows.
# Boundaries: pure text in, a reading or a reply out. What a "go" starts is the approval authority's
#       (agents/intake/message, approval); the rows are application/part_matches' own; nothing here calls a model.
"""THE PLAN STEP IS A CONVERSATION (NOTE-18).

Once the plan card stands (the approval is live), the chat box is where requirements are typed. Each message is read
here, in code, as one of:

- GO: it plainly says to start the mesh ("proceed", "I'm good to mesh", "let's proceed to mesh generation", "that's
  all, run it"). Whole message only: every clause is a go clause or filler. A number, a mesh word ("finer", "size",
  "layers"), a place, a negation or anything unknown makes it no go - "go finer on the bore" is a requirement.
- ASK: a go asked back ("ready to mesh?") or a hedge ("maybe"): "Start the mesh now?" is asked, once. Asked already,
  the message is kept as a requirement.
- INTAKE: a question with no requirement in it, or a change to the plan itself (the engine, the kind of flow, the
  domain): the intake answers it, as before.
- HOLD: "wait", "not yet", "don't proceed yet": nothing starts and nothing is kept; `HOLD_LINE` says so.
- NOTE: anything else, and always a message that carries pictures, files or picked faces: kept as a note, read for the
  checklist, and answered with what the checklist took. The model writes that answer from the checklist's facts
  under a guard (agents/intake/plan_reply, NOTE-19); `reply_for` is its plain fallback and the guard's source of truth.
- BOTH: a requirement that also names another engine ("use cfMesh at 2 mm"): kept as a NOTE and handed to the intake,
  which switches the plan.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

RULE = "NOTE-18"

GO, ASK, NOTE, INTAKE = "go", "ask", "note", "intake"
#: "wait", "not yet", "don't proceed yet": nothing starts, nothing is kept, and we say so
HOLD = "hold"
#: a plan change with a requirement in the same breath ("use cfMesh at 2 mm"): the words are kept as a requirement
#: AND the intake changes the plan, as it did before the plan step was a conversation
BOTH = "both"

#: the one question when a "go" is not plain
ASK_LINE = "Start the mesh now?"
#: the answer to a "wait"
HOLD_LINE = "OK, I won't start yet. Tell me when you're ready, or add anything else you want first."

#: words that carry nothing in a go ("all right, I'm good to mesh", "ok great, let's go")
_FILLER = frozenset({
    "ok", "okay", "alright", "all", "right", "so", "then", "now", "just", "please", "pls", "great", "cool", "perfect",
    "thanks", "thank", "you", "well", "yes", "yeah", "yep", "yup", "sure", "good", "fine", "awesome", "nice", "hey",
    "oh", "ah", "excellent", "brilliant", "lovely", "fab", "fantastic", "super", "and", "really", "very", "much",
    "k", "kk", "ty", "thx",
})
#: "no more changes", "nothing else": a no that ends the step, not a no to it
_BENIGN_NO = re.compile(r"\bno (?:more|other|further) (?:changes|requirements|edits|notes)\b")

_MESH = r"(?:mesh(?:ing)?|mesh generation|meshing generation|the mesh(?:ing)?|the mesh generation|generation|it|this|the run)"
#: one clause that says go, after fillers are taken out
_GO_CLAUSES = [re.compile(rf"^{p}$") for p in (
    r"(?:go|go ahead|go for it|go on|proceed|start|begin|run|run it|mesh|mesh it|do it|send it|ship it|carry on|"
    r"continue)",
    r"(?:it |that |this |all )?(?:looks|sounds|seems) (?:good|fine|great|ok|okay|right|perfect)(?: to me)?",
    r"no (?:more|other|further) (?:changes|requirements|edits|notes)(?: from me)?",
    rf"(?:go ahead and |please )?(?:proceed|go ahead|go on|move on|carry on|continue)(?: (?:to|with) {_MESH})?",
    rf"(?:start|begin|run|generate|build|make|create|kick off) {_MESH}",
    r"(?:start|begin) (?:meshing|generating|the meshing)(?: it)?",
    rf"(?:lets|let us) (?:go|mesh(?: it)?|proceed|start|begin|run(?: it)?|do it|do this|go ahead|move on|get meshing|"
    rf"get going)(?: (?:to|with|on) {_MESH})?",
    rf"(?:lets|let us) (?:start|begin|run|generate|build|make) {_MESH}",
    r"(?:i m|im|i am|we re|were|we are)? ?(?:good|ready|done|happy|set|finished)(?: to (?:go|mesh|proceed|start|run(?: it)?|"
    r"go ahead)| for (?:the )?mesh(?:ing)?| with (?:it|this|that|the plan)| here)?",
    r"(?:good|ready) to (?:go|mesh|proceed|start|run(?: it)?)",
    r"(?:thats|that is|that s) (?:all|it|everything|enough)(?: from me)?(?: for now)?",
    r"(?:nothing (?:else|more))(?: to add)?",
    r"(?:thats|that is|that s) (?:all|it) (?:i need|i want)",
    r"(?:i have|ive) (?:nothing (?:else|more))(?: to add)?",
    r"(?:mesh|meshing) (?:time|away)",
)]
#: a go asked back: "ready to mesh?", "can we proceed?", "shall we start the mesh?"
_ASKED_LEAD = re.compile(r"^(?:can|could|shall|should|may|do|are|is) (?:we|i|you) (?:now )?|^(?:are we|am i) ")
#: any of these makes a message no go however it is worded
_NEVER_GO = re.compile(r"\b(?:not|no|nope|don|dont|wait|hold|stop|cancel|later|yet|before|but|except|instead|without|"
                       r"unless|first|again|change|redo)\b")
#: a message that only says "not now": "wait", "hold on", "not yet", "don't proceed yet", "go later"
_HOLD = re.compile(r"\b(?:wait|hold|hang on|stop|not yet|later|not ready|one (?:sec|second|moment|minute)|"
                   r"(?:dont|do not|don t) (?:proceed|start|go|mesh|run|begin|continue))\b|^(?:no|nope|not now)$")

#: a mesh requirement in the words: a number, or a word about sizes, density, quality or element kind
_REQUIREMENT = re.compile(
    r"\d|\b(?:mm|cm|um|µm|micron|microns|finer|coarser|coarse|refine|refined|refinement|refining|dense|denser|"
    r"density|resolution|resolve|size|sizes|sizing|sized|element|elements|cell|cells|layer|layers|inflation|quality|"
    r"skew|skewness|aspect|jacobian|warp|warpage|growth|spacing|curvature|tet|tets|tetra|hex|"
    r"hexes|hexa|quad|quads|triangles|tris|quadratic|linear|yplus|smooth|smoother|gradual|transition)\b")
#: a question that asks about something rather than asking for it ("what will the cell count be?"): the intake answers
_ASKS_ABOUT = re.compile(r"^(?:what|whats|how|hows|why|which|when|where|who|is|are|does|do|will|would|did|was|were) ")
#: a change to the plan itself, not a mesh requirement: the intake re-plans it
_PLAN_WORDS = re.compile(
    r"\b(?:engine|mesher|purpose|internal|external|2d|3d|far ?field|domain|reference length|flow direction|"
    r"detail level|fidelity|cfd|fea|structural|thermal|modal|velocity|reynolds|mach|inlet|outlet|unit|units|inches|"
    r"metres|meters|millimetres|millimeters|analysis|static|stress|buckling|vibration|fatigue|harmonic|frequency|"
    r"frequencies|shell|solid|mid ?surface)\b")


def _words(text: str) -> str:
    t = str(text or "").casefold().replace("’", "'")
    t = re.sub(r"'", "", t)                       # "i'm" -> "im", "let's" -> "lets", "that's" -> "thats"
    return " ".join(re.findall(r"[a-z0-9+]+", t))


def words_of(text: str) -> str:
    """The message's words, lower case, apostrophes out ("I'm good" -> "im good")."""
    return _words(text)


def _clauses(text: str) -> list[str]:
    parts = re.split(r"[,.;:!?\n]+|\s[-–—]+\s|\s(?:and|then|so)\s", str(text or ""))
    return [w for w in (_words(p) for p in parts) if w]


def _trimmed(clause: str, *, ends: bool = True) -> str:
    """The clause with fillers taken off its start (and its end, `ends`) only ("ok im good to mesh" -> "im good to
    mesh", "ok im good" -> "im good"): a filler inside ("good" in "good to mesh") is part of the go."""
    ws = clause.split()
    while ws and ws[0] in _FILLER:
        ws.pop(0)
    while ends and ws and ws[-1] in _FILLER:
        ws.pop()
    return " ".join(ws)


def _go_clause(clause: str) -> bool:
    core = " ".join(w for w in clause.split() if w not in _FILLER)
    if not core:
        return False
    # the clause as typed ("i m good to mesh", "thats all"), with fillers off its ends ("ok im good to mesh"), or
    # with every filler out ("ok great lets go")
    forms = (clause, _trimmed(clause), _trimmed(clause, ends=False), core)
    return any(p.match(f) for p in _GO_CLAUSES for f in forms)


def _filler_only(clause: str) -> bool:
    return all(w in _FILLER for w in clause.split())


def _plain_go(text: str) -> bool:
    """Every clause is a go clause or filler, and at least one is a go."""
    clauses = _clauses(text)
    if not clauses or never_go(text):
        return False
    gos = [c for c in clauses if _go_clause(c)]
    return bool(gos) and all(c in gos or _filler_only(c) for c in clauses)


def _asked_go(text: str) -> bool:
    """A go asked back: "ready to mesh?", "can we proceed now?", "shall we start the mesh?"."""
    if "?" not in str(text or ""):
        return False
    bare = _ASKED_LEAD.sub("", _words(text)).strip()
    return bool(bare) and _plain_go(bare)


def reading(text: str, *, carries: bool = False, asked: bool = False, consent: str = "", engine: str = "") -> str:
    """What one message at the plan step means: GO, ASK, HOLD, NOTE, BOTH or INTAKE (the module's docstring).
    `carries`: it carries pictures, files or picked faces. `asked`: "Start the mesh now?" was the last thing we said.
    `consent`: the intake's own consent reading of the whole message (agents/intake/consent: "approve", "ambiguous",
    "other"). `engine`: the plan's engine (naming it again is no change)."""
    raw = str(text or "")
    words = _words(raw)
    if carries:
        return BOTH if words and _REQUIREMENT.search(words) and _names_another_engine(raw, engine) else NOTE
    if not words:
        # "👍" is the consent reader's yes; anything else with no words is asked about once
        return GO if consent == "approve" and "?" not in raw else (NOTE if asked else ASK)
    if _REQUIREMENT.search(words):
        # "can you do 1 mm here?" asks for it; "what will the cell count be?" asks about it
        asks_about = "?" in raw and _ASKS_ABOUT.match(words + " ") and not re.search(r"\d", words)
        if asks_about:
            return INTAKE
        # "use cfMesh at 2 mm": the engine is switched now, and the size kept
        return BOTH if _names_another_engine(raw, engine) else NOTE
    if "?" not in raw and (consent == "approve" or _plain_go(raw)):
        return GO
    if "?" not in raw and _HOLD.search(words) and not _PLAN_WORDS.search(words) and not _names_an_engine(raw):
        return HOLD
    if _asked_go(raw) or consent in ("approve", "ambiguous"):
        return NOTE if asked else ASK
    if _PLAN_WORDS.search(words) or "?" in raw or _names_an_engine(raw):
        return INTAKE
    return NOTE


def requirement_content(text: str, *, carries: bool = False) -> bool:
    """THE SAFE SIDE OF A GO, decided in code (NOTE-18): a message that carries a face, a picture or a file, or whose
    words hold a number, a unit, a size or a mesh requirement word ("finer", "layers", "skewness"), is a requirement
    or a question about one - never a go, whatever a model would say."""
    return bool(carries) or bool(_REQUIREMENT.search(_words(text)))


#: the button's own words, typed or clicked: a go with no model asked
_EXACT_GO = frozenset({"proceed"})


def exact_go(text: str) -> bool:
    """The Proceed button's text, as it is (not asked back: "proceed?")."""
    return "?" not in str(text or "") and _words(text) in _EXACT_GO


def plan_change(text: str) -> bool:
    """A word about the plan itself (the engine, the kind of flow, the domain, the unit): the intake's, in code."""
    words = _words(text)
    return bool(words and _PLAN_WORDS.search(words)) or _names_an_engine(text)


def never_go(text: str) -> bool:
    """A negation or a condition in the words ("not", "don't", "but", "instead", "first"): never a go."""
    return bool(_NEVER_GO.search(_BENIGN_NO.sub(" ", _words(text))))


def answers_another_question(text: str, last_line: str) -> bool:
    """"ok" / "yes" / "sure" right after we asked something that is not whether to start: an answer to THAT
    question, never a go."""
    words = _words(text)
    last = str(last_line or "").strip()
    return bool(words) and _filler_only(words) and last.endswith("?") and last != ASK_LINE


def _engines_named(text: str, selected: str = "") -> list[str]:
    from meshpipeline.agents.intake import vocabulary as _vocab
    try:
        return [str(e) for e in _vocab.engines_named_in(text, selected) or ()]
    except Exception:  # noqa: BLE001 - unread: kept as a requirement, whose reply says what it took
        return []


def _names_an_engine(text: str) -> bool:
    """"use cfMesh instead": the engine is the intake's to change (agents/intake/engine_selection)."""
    return bool(_engines_named(text))


def _names_another_engine(text: str, engine: str) -> bool:
    """An engine other than the plan's is named: a plan change (the plan's own engine named again is none)."""
    return bool(_engines_named(text, engine))


def asked_last(messages: Iterable[Mapping]) -> bool:
    """Whether our last line in the conversation was the one go question."""
    for m in reversed(list(messages or ())):
        if isinstance(m, Mapping) and m.get("role") == "assistant":
            return str(m.get("content") or "").strip() == ASK_LINE
    return False


# #
# the reply: what the checklist took from one message
# #

#: the reading did not land in time: the card follows it
STILL_READING = "Still reading that one. The list on the card will show it in a moment."
#: not read now (the part still measured, or nothing to read it): the yes reads it first (NOTE-5)
READ_BEFORE_RUN = "Kept. I'll read it before the mesh starts."
#: nothing in the words is a mesh requirement we can apply
NOT_READ = ("I couldn't find a mesh setting in that. You can give me a size (\"2 mm\"), finer or coarser somewhere, "
            "a quality limit, or click a face on the model.")
#: a picture with no words: what the product does with it (NOTE-16)
PICTURE_ONLY = ("I've kept the picture. I don't read pictures myself, so tell me what you want there, for example "
                "\"1 mm here\", or click the spot on the model.")
#: a picture with words: kept and shown, never read itself - the place comes from the words or a click (NOTE-16)
PICTURE_NOTE = ("I've kept the picture with your note, but I don't read pictures myself, so the place comes from "
                "your words or a face you click.")
#: no checklist on this model before the run: kept, read when the mesh is built (said by `kept_line`)
NO_CHECKLIST = "I'll read it when the mesh is built and confirm on the result whether this mesher could apply it."
#: after the flow reader's own lines: on a flow mesh what is applied depends on the mesher
CONFIRM_ON_RESULT = "I'll confirm on the result whether this mesher could apply it."
#: what a requirement plainly holds, said back when it is kept for the run: "0.5 mm", "y+ of 1", "3 layers"
_PLAIN_BITS = re.compile(
    r"\by\s*\+\s*(?:of|=|:|at|around|about|below|under)?\s*(?:about\s*|around\s*|~\s*)?\d+(?:[.,]\d+)?|"
    r"\b\d+(?:[.,]\d+)?\s*(?:mm|cm|µm|um|microns?|m|in|inch(?:es)?)\b|\b\d+\s+(?:prism\s+|inflation\s+|boundary\s+)?"
    r"layers?\b", re.I)


def kept_line(text: str, faces: Iterable[Mapping] = ()) -> str:
    """A requirement kept for the run where nothing reads it before (no checklist on this route), said in the first
    person with the size and the face plainly in the message - never as applied, never as unreadable."""
    bits: list[str] = []
    for m in _PLAIN_BITS.finditer(str(text or "")):
        bit = " ".join(m.group(0).split())
        if bit.casefold() not in (b.casefold() for b in bits):
            bits.append(bit)
    picked = [f for f in faces or () if isinstance(f, Mapping)]
    what = _and(bits) if bits else "that"
    where = f" on {face_words(picked)}" if picked else ""
    return f"Got it, I've kept {what}{where} for the mesh. {NO_CHECKLIST}"


#: a guessed place
CLICK_IF_ELSEWHERE = "That's my guess from your words; click it on the model if you meant another spot."
#: the most rows said in one reply
MAX_SAID = 3


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def face_words(faces: Iterable[Mapping]) -> str:
    """"Face 32 (round Ø6 mm)" for one face, "the 4 faces you picked" for several."""
    faces = [f for f in faces or () if isinstance(f, Mapping)]
    if len(faces) == 1:
        label = str(faces[0].get("label") or faces[0].get("id") or "the face you picked")
        name, _, what = label.partition(" · ")
        return f"{name} ({what})" if what else name
    return f"the {len(faces)} faces you picked" if faces else "the face you picked"


def _what(row: Mapping) -> str:
    size = str(row.get("size") or "")
    if size == "finer":
        return "Finer mesh"
    if size == "coarser":
        return "Coarser mesh"
    if size and size[0].isdigit():
        return f"{size} elements"
    words = str(row.get("words") or row.get("quote") or "").strip()
    return f"“{words}”" if words else "Your request"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _done_what(row: Mapping) -> str:
    """What a row sets, said in the first person: "set 1 mm elements", "made the mesh finer", "noted “...”"."""
    size = str(row.get("size") or "")
    if size == "finer":
        return "made the mesh finer"
    if size == "coarser":
        return "made the mesh coarser"
    if size and size[0].isdigit():
        return f"set {size} elements"
    words = str(row.get("words") or row.get("quote") or "").strip()
    return f"noted “{words}”" if words else "noted your request"


def _row_line(row: Mapping) -> tuple[str, bool]:
    """One row in words ("set 1 mm elements on Face 3"), and whether its place was the reader's guess."""
    faces = row.get("faces") or []
    if faces:
        return f"{_done_what(row)} on {face_words(faces)}", False
    where = str(row.get("parts_text") or "").strip()
    if not where:
        return _done_what(row), False
    if where.startswith(("the whole", "the rest")):
        return f"{_done_what(row)} on {where}", False
    return f"{_done_what(row)} at {where}", not row.get("picked")


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _mine(row: Mapping, said: str) -> bool:
    quote = _norm(str(row.get("quote") or "").rstrip("…"))
    return bool(quote) and quote in said


def _lines_mine(lines: Iterable[str], said: str) -> list[str]:
    """The reading's own lines (a flow mesh, NOTES-CFD-1) that quote these words."""
    out = []
    for line in lines or ():
        quotes = re.findall(r"[\"“]([^\"”]+)[\"”]", str(line))
        if any(_norm(q.rstrip("…")) and _norm(q.rstrip("…")) in said for q in quotes):
            out.append(str(line).removeprefix("From your notes: ").strip())
    return out


#: the reading failed (the model step did not answer): the note stays, and the run reads it again
READ_FAILED = "I couldn't read that one just now. It's kept, and it will be read again when the mesh starts."


def pictures_of(note: Mapping) -> list[Mapping]:
    return [a for a in note.get("attachments") or ()
            if isinstance(a, Mapping) and (str(a.get("kind") or "") == "image"
                                           or str(a.get("mime") or "").startswith("image/"))]


def _row_is_mine(row: Mapping, note_id: str, said: str) -> bool:
    """A row is this message's when the reading tied it to this message's note (its `note`, application/
    part_matches.with_notes); a row from a reading made before rows carried their note falls back to its quote."""
    if note_id and row.get("note"):
        return str(row.get("note")) == note_id
    return _mine(row, said)


def mine(note: Mapping, view: Mapping | None, *, note_id: str = "") -> tuple[list[Mapping], list[Mapping]]:
    """(this message's rows, this message's phrases no place was found for) in a done view."""
    said = _norm(note.get("text"))
    rows = [r for r in (view or {}).get("rows") or () if isinstance(r, Mapping) and _row_is_mine(r, note_id, said)]
    unsure = [u for u in (view or {}).get("unsure") or ()
              if isinstance(u, Mapping) and _row_is_mine(u, note_id, said)]
    return rows, unsure


def fixed_reply(note: Mapping, view: Mapping | None) -> str:
    """The reply no model writes: the reading is not there to say anything about (still going, not started, failed).
    Empty when there are facts to answer from."""
    if not _norm(note.get("text")):
        return ""
    if view is not None and view.get("failed"):
        return READ_FAILED
    if view is not None and view.get("status") == "reading":
        return STILL_READING
    if view is not None and view.get("status") in ("waiting", "ready"):
        return READ_BEFORE_RUN
    return ""


def reply_for(note: Mapping, view: Mapping | None, *, note_id: str = "") -> str:
    """WHAT WAS UNDERSTOOD, said back: built from the checklist the system produced for this message (`view`, the
    card's part_matches view: rows, unsure, lines) - never from free text, so it never says it took in what it did
    not. `note`: the message as kept (text, attachments, faces); `note_id`: its id among the user's notes, which the
    reading stamps on the rows it read from it. The plain sentence the model-written reply falls back to
    (agents/intake/plan_reply, NOTE-19) and the guard's source of truth."""
    text = str(note.get("text") or "")
    said = _norm(text)
    faces = [f for f in note.get("faces") or () if isinstance(f, Mapping)]
    pictures = pictures_of(note)
    if not said:
        if faces:
            return f"Got {face_words(faces)}. What do you want there? For example \"1 mm here\"."
        return PICTURE_ONLY
    if view is None:
        return kept_line(text, faces) + (f" {PICTURE_NOTE}" if pictures else "")
    fixed = fixed_reply(note, view)
    if fixed:
        return fixed
    rows, unsure = mine(note, view, note_id=note_id)
    out: list[str] = []
    guessed = False
    done: list[str] = []
    for r in rows[:MAX_SAID]:
        line, guess = _row_line(r)
        done.append(line)
        guessed = guessed or guess
    if done:
        out.append(f"I've {_and(done)}.")
    if len(rows) > MAX_SAID:
        out.append(f"There are {len(rows) - MAX_SAID} more in the list.")
    if guessed:
        out.append(CLICK_IF_ELSEWHERE)
    for u in unsure[:MAX_SAID]:
        what = str(u.get("words") or u.get("quote") or "").strip()
        out.append(f"I couldn't tell where “{what}” is. Click it on the model and send it again.")
    flow = [_cap(x) for x in _lines_mine(view.get("lines") or (), said)][:MAX_SAID]
    if flow:
        # the flow reader's own lines: what a flow mesher applies depends on the mesher
        out += [*flow, CONFIRM_ON_RESULT]
    if not out:
        # NO CHECKLIST BEFORE THE RUN on this route (a flow model with no parts): the words are kept and the run
        # reads them - "couldn't read that" would be false; elsewhere the checklist really took nothing
        kept = (view.get("flow") or view.get("notes_only")) and (faces or requirement_content(text))
        out.append(kept_line(text, faces) if kept else NOT_READ)
    if pictures:
        out.append(PICTURE_NOTE)
    return " ".join(out)


#: a plain size in millimetres, the only number in the words ("1 mm on these too", "can you do 0.5mm here?")
_PLAIN_MM = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*mm\b", re.I)
#: words that make a face message more than a plain size (a limit, layers, growth, a count, another unit)
_MORE_THAN_A_SIZE = re.compile(
    r"\b(?:finer|coarser|coarse|refine\w*|dense\w*|layer\w*|inflation|quality|skew\w*|aspect|jacobian|warp\w*|"
    r"growth|ratio|curvature|tets?|tetra|hex\w*|quads?|triangles|tris|quadratic|linear|yplus|smooth\w*|gradual|"
    r"transition|cells? across|percent|cm|um|µm|micron\w*|inch\w*|max\w*|min\w*|under|below|above|over|"
    r"between|from|to|except|but|not|no|instead|whole|everywhere|rest|all over|and|or)\b|%|\bx\b|[<>=]", re.I)


def plain_face_rows(note: Mapping) -> list[dict] | None:
    """THE SHORT WAY (speed, NOTE-18): a message sent with faces picked whose words hold one size in millimetres and
    nothing else a reader could take ("1 mm on these too", "can you do a 0.5 mm mesh sizing in here?") needs no model
    to know where (the faces are given: the run binds them in code, engines/mesh_requirements.bind_picks) or what
    (the one size). Its row, as the checklist will show it, is known now - so its reply need not wait for the
    reading. None for anything else (the reading's rows answer it)."""
    faces = [f for f in note.get("faces") or () if isinstance(f, Mapping)]
    text = str(note.get("text") or "")
    if not faces or not text.strip() or note.get("attachments"):
        return None
    sizes = _PLAIN_MM.findall(text)
    if len(sizes) != 1 or len(re.findall(r"\d+(?:[.,]\d+)?", text)) != 1 or _MORE_THAN_A_SIZE.search(
            _PLAIN_MM.sub(" ", text)):
        return None
    mm = sizes[0].replace(",", ".")
    return [{"quote": text, "words": text, "size": f"{mm} mm", "picked": True, "parts": [],
             "faces": [{"id": str(f.get("id") or ""), "label": str(f.get("label") or f.get("id") or "")}
                       for f in faces]}]


def row_place(row: Mapping) -> tuple[str, str]:
    """(where a row applies, in words; how the place was found: "picked" - the user's click, "guessed" - the reader's
    choice, "whole" - the words said the whole model)."""
    faces = row.get("faces") or []
    if faces:
        return face_words(faces), "picked"
    where = str(row.get("parts_text") or "").strip()
    if not where or where.startswith(("the whole", "the rest")):
        return where or "the whole model", "whole"
    return where, ("picked" if row.get("picked") else "guessed")


def row_what(row: Mapping) -> str:
    """What a row sets, in words: "1 mm elements", "finer mesh", "coarser mesh", or its own words."""
    return _what(row).strip("“”")


def lines_mine(view: Mapping | None, text: str) -> list[str]:
    """The flow reader's own lines (NOTES-CFD-1) for these words."""
    return [_cap(x) for x in _lines_mine((view or {}).get("lines") or (), _norm(text))][:MAX_SAID]


__all__ = ["ASK", "ASK_LINE", "BOTH", "GO", "HOLD", "HOLD_LINE", "INTAKE", "NOTE", "RULE", "answers_another_question",
           "asked_last", "exact_go", "face_words", "fixed_reply", "lines_mine", "mine", "never_go", "pictures_of",
           "plain_face_rows", "plan_change", "reading", "reply_for", "requirement_content", "row_place", "row_what"]
