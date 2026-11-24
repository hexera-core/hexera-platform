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
- NOTE: anything else, and always a message that carries pictures, files or picked faces: kept as a note, read for the
  checklist, and answered with what the checklist took (`reply_for`), never with free model text.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

RULE = "NOTE-18"

GO, ASK, NOTE, INTAKE = "go", "ask", "note", "intake"

#: the one question when a "go" is not plain
ASK_LINE = "Start the mesh now?"

#: words that carry nothing in a go ("all right, I'm good to mesh", "ok great, let's go")
_FILLER = frozenset({
    "ok", "okay", "alright", "all", "right", "so", "then", "now", "just", "please", "pls", "great", "cool", "perfect",
    "thanks", "thank", "you", "well", "yes", "yeah", "yep", "yup", "sure", "good", "fine", "awesome", "nice", "hey",
    "oh", "ah", "excellent", "brilliant", "lovely", "fab", "fantastic", "super", "and", "really", "very", "much",
})

_MESH = r"(?:mesh(?:ing)?|mesh generation|meshing generation|the mesh(?:ing)?|the mesh generation|generation|it|this|the run)"
#: one clause that says go, after fillers are taken out
_GO_CLAUSES = [re.compile(rf"^{p}$") for p in (
    r"(?:go|go ahead|go for it|go on|proceed|start|begin|run|run it|mesh it|do it|send it|ship it|carry on|continue)",
    rf"(?:go ahead and |please )?(?:proceed|go ahead|go on|move on|carry on|continue)(?: (?:to|with) {_MESH})?",
    rf"(?:start|begin|run|generate|build|make|create|kick off) {_MESH}",
    r"(?:start|begin) (?:meshing|generating|the meshing)(?: it)?",
    rf"(?:lets|let us) (?:go|mesh(?: it)?|proceed|start|begin|run(?: it)?|do it|do this|go ahead|move on|get meshing|"
    rf"get going)(?: (?:to|with|on) {_MESH})?",
    rf"(?:lets|let us) (?:start|begin|run|generate|build|make) {_MESH}",
    r"(?:i m|im|i am|we re|were|we are)? ?(?:good|ready|done|happy|set|finished)(?: to (?:go|mesh|proceed|start|run(?: it)?|"
    r"go ahead)| for (?:the )?mesh(?:ing)?| with (?:it|this|that|the plan))?",
    r"(?:good|ready) to (?:go|mesh|proceed|start|run(?: it)?)",
    r"(?:thats|that is|that s) (?:all|it|everything|enough)(?: for now)?",
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


def _clauses(text: str) -> list[str]:
    parts = re.split(r"[,.;:!?\n]+|\s[-–—]+\s|\s(?:and|then|so)\s", str(text or ""))
    return [w for w in (_words(p) for p in parts) if w]


def _go_clause(clause: str) -> bool:
    core = " ".join(w for w in clause.split() if w not in _FILLER)
    if not core:
        return False
    # the clause as typed ("i m good to mesh", "thats all") or with its fillers out ("ok great lets go")
    return any(p.match(clause) or p.match(core) for p in _GO_CLAUSES)


def _filler_only(clause: str) -> bool:
    return all(w in _FILLER for w in clause.split())


def _plain_go(text: str) -> bool:
    """Every clause is a go clause or filler, and at least one is a go."""
    clauses = _clauses(text)
    if not clauses or _NEVER_GO.search(_words(text)):
        return False
    gos = [c for c in clauses if _go_clause(c)]
    return bool(gos) and all(c in gos or _filler_only(c) for c in clauses)


def _asked_go(text: str) -> bool:
    """A go asked back: "ready to mesh?", "can we proceed now?", "shall we start the mesh?"."""
    if "?" not in str(text or ""):
        return False
    bare = _ASKED_LEAD.sub("", _words(text)).strip()
    return bool(bare) and _plain_go(bare)


def reading(text: str, *, carries: bool = False, asked: bool = False, consent: str = "") -> str:
    """What one message at the plan step means: GO, ASK, NOTE or INTAKE (the module's docstring). `carries`: it
    carries pictures, files or picked faces. `asked`: "Start the mesh now?" was the last thing we said. `consent`:
    the intake's own consent reading of the whole message (agents/intake/consent: "approve", "ambiguous", "other")."""
    raw = str(text or "")
    words = _words(raw)
    if carries:
        return NOTE
    if not words:
        # "👍" is the consent reader's yes; anything else with no words is asked about once
        return GO if consent == "approve" and "?" not in raw else (NOTE if asked else ASK)
    if _REQUIREMENT.search(words):
        # "can you do 1 mm here?" asks for it; "what will the cell count be?" asks about it
        asks_about = "?" in raw and _ASKS_ABOUT.match(words + " ") and not re.search(r"\d", words)
        return INTAKE if asks_about else NOTE
    if "?" not in raw and (consent == "approve" or _plain_go(raw)):
        return GO
    if _asked_go(raw) or consent in ("approve", "ambiguous"):
        return NOTE if asked else ASK
    if _PLAN_WORDS.search(words) or "?" in raw or _names_an_engine(raw):
        return INTAKE
    return NOTE


def _names_an_engine(text: str) -> bool:
    """"use cfMesh instead": the engine is the intake's to change (agents/intake/engine_selection)."""
    from meshpipeline.agents.intake import vocabulary as _vocab
    try:
        return bool(_vocab.engines_named_in(text, ""))
    except Exception:  # noqa: BLE001 - unread: kept as a requirement, whose reply says what it took
        return False


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
NOT_READ = ("I couldn't read that as a mesh requirement. I can take a size (\"2 mm\"), finer or coarser somewhere, "
            "a quality limit, or a face you click on the model.")
#: a picture with no words: what the product does with it (NOTE-16)
PICTURE_ONLY = ("I've kept the picture with your notes. It only helps show where your words point; it sets nothing "
                "by itself. Type what you want there, for example \"1 mm here\".")
#: a number seen only in a picture is not applied (NOTE-16)
PICTURE_NUMBERS = "A number that is only in the picture is not used: type it if it matters."
#: no checklist on this model before the run: kept, read when the mesh is built
NO_CHECKLIST = "Kept. I'll read it when the mesh is built, and the result will say what was applied."
#: a guessed place
CLICK_IF_ELSEWHERE = "Click the model if you meant somewhere else."
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


def _row_line(row: Mapping) -> tuple[str, bool]:
    """One row in words, and whether its place was the reader's guess."""
    faces = row.get("faces") or []
    if faces:
        return f"{_what(row)} on {face_words(faces)}.", False
    where = str(row.get("parts_text") or "").strip()
    if not where:
        return f"{_what(row)}.", False
    if where.startswith(("the whole", "the rest")):
        return f"{_what(row)} on {where}.", False
    return f"{_what(row)} at {where}.", not row.get("picked")


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


def reply_for(note: Mapping, view: Mapping | None) -> str:
    """WHAT WAS UNDERSTOOD, said back: built from the checklist the system produced for this message (`view`, the
    card's part_matches view: rows, unsure, lines) - never from free text, so it never says it took in what it did
    not. `note`: the message as kept (text, attachments, faces)."""
    text = str(note.get("text") or "")
    said = _norm(text)
    faces = [f for f in note.get("faces") or () if isinstance(f, Mapping)]
    pictures = [a for a in note.get("attachments") or ()
                if isinstance(a, Mapping) and (str(a.get("kind") or "") == "image"
                                               or str(a.get("mime") or "").startswith("image/"))]
    if not said:
        if faces:
            return f"Picked {face_words(faces)}. Type what you want there, for example \"1 mm here\"."
        return PICTURE_ONLY
    if view is None:
        where = f" for {face_words(faces)}" if faces else ""
        return NO_CHECKLIST.replace("Kept.", f"Kept{where}.")
    if view.get("status") == "reading":
        return STILL_READING
    if view.get("status") in ("waiting", "ready"):
        return READ_BEFORE_RUN
    rows = [r for r in view.get("rows") or () if isinstance(r, Mapping) and _mine(r, said)]
    unsure = [u for u in view.get("unsure") or () if isinstance(u, Mapping) and _mine(u, said)]
    out: list[str] = []
    guessed = False
    for r in rows[:MAX_SAID]:
        line, guess = _row_line(r)
        out.append(line)
        guessed = guessed or guess
    if len(rows) > MAX_SAID:
        out.append(f"And {len(rows) - MAX_SAID} more, in the list on the card.")
    for u in unsure[:MAX_SAID]:
        what = str(u.get("words") or u.get("quote") or "").strip()
        out.append(f"I couldn't tell where “{what}” is: click it on the model and send it again.")
    out += [_cap(x) for x in _lines_mine(view.get("lines") or (), said)][:MAX_SAID]
    if not out:
        out.append(NOT_READ)
    elif guessed:
        out.append(CLICK_IF_ELSEWHERE)
    if pictures:
        out.append(PICTURE_NUMBERS)
    return " ".join(out)


__all__ = ["ASK", "ASK_LINE", "GO", "INTAKE", "NOTE", "RULE", "asked_last", "face_words", "reading", "reply_for"]
