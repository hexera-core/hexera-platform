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
- QUESTION: a question about the mesh itself - its size, element count, how fine, how long, what was set ("so what
  would the elemnt size be roughly", typos and no question mark included): nothing is kept; it is answered from the
  numbers the product already holds before the run (agents/intake/plan_reply, NOTE-20). Never "I couldn't find a
  mesh setting in that".
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from meshpipeline.contracts import user_notes as UN

RULE = "NOTE-18"
#: a question about the mesh at the plan step is answered from the numbers the product holds before the run
QUESTION_RULE = "NOTE-20"

GO, ASK, NOTE, INTAKE = "go", "ask", "note", "intake"
#: "wait", "not yet", "don't proceed yet": nothing starts, nothing is kept, and we say so
HOLD = "hold"
#: a plan change with a requirement in the same breath ("use cfMesh at 2 mm"): the words are kept as a requirement
#: AND the intake changes the plan, as it did before the plan step was a conversation
BOTH = "both"
#: a question about the mesh (its sizes, count, time, what was set): answered from the plan's own numbers (NOTE-20)
QUESTION = "question"

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
#: NOTE-20: A QUESTION IN ITS WORDS, question mark or not: a question word with a word after it anywhere ("so for this
#: mesh, what would the size be"), never the suggestion forms ("how about finer at the bore", "what if"); a question
#: lead-in ("tell me", "do you know", "any idea"); or a clause that opens on an auxiliary not addressed to us ("will
#: it be fine", "is the hole finer" - never "will you make it finer", which asks for it)
_Q_WORD = re.compile(r"\b(?:what|whats|how|hows|which|why)\s+(?!about\b|if\b|not\b)[a-z]")
_Q_LEAD_IN = re.compile(r"\b(?:tell me|do you know|any idea|i wonder|wondering|curious)\b")
_Q_AUX = re.compile(r"^(?:is|are|does|do|did|will|would|was|were|should)\s+(?!you\b|u\b)[a-z]")
#: NOTE-20: words that make a question one about the mesh beyond the requirement words: its count, its time, what is set
_MESH_ASK = re.compile(r"\b(?:count|how many|how long|how fine|how coarse|how big|how small|how dense|time|take|"
                       r"takes|set|requirement|requirements)\b")
#: ... but a question about the plan's KIND of mesh (element type, order, engine), which no number answers: the intake's
_NOT_OUR_NUMBERS = re.compile(r"\b(?:type|types|kind|kinds|order|shape|family|engine|mesher|why)\b")
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


#: a go that does something - the last word of "nope, nothing else. go" (never "nothing else", which only ends)
_ACTION_GO = [re.compile(rf"^{p}$") for p in (
    r"(?:go|go ahead|go for it|go on|proceed|start|begin|run|run it|mesh|mesh it|do it|send it|ship it|carry on|"
    r"continue)",
    rf"(?:go ahead and |please )?(?:proceed|go ahead|go on|move on|carry on|continue)(?: (?:to|with) {_MESH})?",
    rf"(?:start|begin|run|generate|build|make|create|kick off) {_MESH}",
    r"(?:lets|let us) (?:go|mesh(?: it)?|proceed|start|begin|run(?: it)?|do it|go ahead|get going)",
)]
#: "nope, nothing else": a no to "anything else?" - a no that says nothing more is wanted, never a no to the run
_NOTHING_MORE = re.compile(
    r"^(?:(?:no|nope|nah)(?: thanks| thank you)? )?(?:nothing (?:else|more)(?: to add)?(?: from me)?|"
    r"no (?:more|other|further) (?:changes|requirements|edits|notes)(?: from me)?|"
    r"(?:thats|that is|that s) (?:all|it|everything)(?: from me)?|(?:i have|ive) nothing (?:else|more)(?: to add)?)$")
_NO_WORD = re.compile(r"^(?:no|nope|nah)(?: thanks| thank you)?$")


def _done_then_go(text: str) -> bool:
    """NOTE-18: says nothing more is wanted, then ends on a clear go ("nope, nothing else. go", "no, that's all, mesh
    it"): every clause a no to more, a nothing-more, or filler, and the last a go that acts. Never "no go", "don't go
    yet", "nothing else to go on" (no clause of theirs is a go)."""
    if "?" in str(text or ""):
        return False
    # each clause as typed and with fillers off its ends ("ok go" -> "go"; "that's all" keeps its "all")
    clauses = [(c, _trimmed(c)) for c in _clauses(text) if not _filler_only(c)]
    if len(clauses) < 2 or not any(p.match(f) for p in _ACTION_GO for f in clauses[-1]):
        return False

    def nothing_more(forms: tuple[str, str]) -> bool:
        return any(_NOTHING_MORE.match(f) for f in forms)
    head = clauses[:-1]
    return any(nothing_more(c) for c in head) and all(
        nothing_more(c) or any(_NO_WORD.match(f) for f in c) for c in head)


def _plain_go(text: str) -> bool:
    """Every clause is a go clause or filler, and at least one is a go."""
    clauses = _clauses(text)
    if _done_then_go(text):
        return True
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
    if mesh_question(raw):
        # "can you do 1 mm here?" asks for it; "what will the cell count be?" and "so what would the elemnt size be
        # roughly" ask about it (NOTE-20)
        return QUESTION
    if _REQUIREMENT.search(words):
        if asks(raw) and not re.search(r"\d", words):
            # a question about the plan's kind of mesh ("what element type will it use?"): the intake answers
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


def asks(text: str) -> bool:
    """NOTE-20: the words are a question - a question word anywhere ("..., what would the size be roughly"), a question
    lead-in ("tell me", "do you know"), a clause opening on an auxiliary not addressed to us ("will it be fine"), or
    a question mark after a question-word opening. Typos elsewhere in the words do not matter."""
    raw = str(text or "")
    words = _words(raw)
    if not words:
        return False
    if _Q_WORD.search(words) or _Q_LEAD_IN.search(words):
        return True
    return any(_Q_AUX.match(c) or (c.startswith(("who ", "why ", "when ", "where ")) and "?" in raw)
               for c in (_trimmed(c, ends=False) for c in _clauses(raw)))


def mesh_question(text: str) -> bool:
    """NOTE-20, IN CODE: a question about the mesh itself - asked in question words, about a size, the element count,
    how fine, how long, what was set - and no number in it (a number states something to set: "would 2 mm do?" is kept
    as the requirement it names). Never a go, never kept as a requirement."""
    words = _words(text)
    return bool(words) and not re.search(r"\d", words) and not _NOT_OUR_NUMBERS.search(words) and asks(text) \
        and bool(_REQUIREMENT.search(words) or _MESH_ASK.search(words))


def about_the_mesh(text: str) -> bool:
    """The words hold a mesh word or a word about the mesh's count, time or settings (the model's "question" is ours
    to answer from the plan's numbers, NOTE-20; any other question is the intake's)."""
    words = _words(text)
    return bool(words) and bool(_REQUIREMENT.search(words) or _MESH_ASK.search(words))


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
    """A negation or a condition in the words ("not", "don't", "but", "instead", "first"): never a go - except a no
    that only says nothing more is wanted before a clear go ("nope, nothing else. go")."""
    if _done_then_go(text):
        return False
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
#: NOTE-25: words the checklist takes nothing from are kept for the run and said to be kept, never turned away
KEPT_RULE = "NOTE-25"
#: NOTE-25: the fallback when the checklist took nothing from a statement: the words are kept (NOTE-18) and every
#: route's planning step reads them as guidance (NOTE-2) - said the way a person says it, no list of what can be set
NOT_READ = "Noted. I'll keep that in mind when the mesh is planned."
#: ... when the words asked something of the mesh ("make it a good quality mesh please"): the result tells each ask
#: back (NOTE-8, NOTE-12), and that is said too
NOT_READ_MESH = "Noted. I'll keep that in mind when the mesh is planned, and the result will say what came of it."
#: words that ask something of the mesh itself, beside the requirement words ("a nice mesh", "make the mesh good")
_OF_THE_MESH = re.compile(r"\bmesh\w*\b", re.I)


def asks_of_the_mesh(text: str) -> bool:
    """NOTE-25: words that set nothing still ask something of the mesh ("make it a good quality mesh please") - the
    result will say what came of them - rather than tell plain context ("the part is cast aluminium")."""
    return requirement_content(text) or bool(_OF_THE_MESH.search(str(text or "")))


def kept_reply(text: str) -> str:
    """NOTE-25: the line said in code for words the checklist took nothing from."""
    if geometry_edit(text):
        return NOT_BY_MESHING
    return NOT_READ_MESH if asks_of_the_mesh(text) else NOT_READ
#: NOTE-25: a change to the CAD itself ("remove the small holes"): no mesh makes it, and that is said
NOT_BY_MESHING = ("Removing or filling features changes the CAD itself, which meshing can't do, so the mesh follows "
                  "the part as it is. I've kept your words, and the result will say that wasn't applied.")
#: "remove the small holes", "fill the slots", "suppress the fillets": a change to the part, not to its mesh
_GEOMETRY_EDIT = re.compile(
    r"\b(?:remove|removing|delete|deleting|fill|filling|suppress|suppressing|defeature|defeaturing|get rid of|"
    r"take out|plug|erase)\b[^.?!]*\b(?:holes?|bores?|fillets?|chamfers?|features?|details?|bosses|ribs?|"
    r"notch(?:es)?|slots?|threads?|logos?|text|engravings?|rounds?|pockets?|grooves?)\b", re.I)


def geometry_edit(text: str) -> bool:
    """NOTE-25: the words ask for a change to the part itself (removing or filling its features), which no mesh makes."""
    return bool(_GEOMETRY_EDIT.search(str(text or "")))
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


# #
# NOTE-24: an element or cell count typed at the plan step is a checklist row
# #
#: a count typed at the plan step ("keep it under 1.5 million elements") is a row of the checklist, read in code
COUNT_RULE = "NOTE-24"
#: the number of a count and its multiplier as typed ("1.5 million", "800k", "500,000", "2M")
_C_NUM = r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<mult>k|thousand|m|mil|mill?ion|millions|mio|b|bn|billion)?"
_C_NOUN = r"(?P<noun>elements?|cells?|tets?|tetrahedra|hexes|hexahedra)"
_C_BOUNDS = {
    "ceiling": r"under|below|less than|fewer than|no more than|not more than|at most|max(?:imum)?|up to|within|"
               r"cap(?:ped)? at|limit(?:ed)? to|not exceed(?:ing)?|no greater than|lower than",
    "floor": r"over|above|more than|at least|min(?:imum)?|no fewer than|no less than|not less than|"
             r"not fewer than",
    "target": r"about|around|roughly|approximately|approx|circa|near|aim for|aiming for|target(?:ing)?|of|~",
}
_C_BOUND = "|".join(f"(?P<{k}>{v})" for k, v in _C_BOUNDS.items())
#: "under 1.5 million elements", "about 800k elements", "max 500,000 elements" (the noun after the number)
_COUNT_AFTER = re.compile(rf"(?:\b(?:{_C_BOUND})\s+)?(?<![\w.,]){_C_NUM}\s+{_C_NOUN}\b", re.I)
#: "keep the element count under 1.5 million" (the noun before the number)
_COUNT_BEFORE = re.compile(rf"\b(?:element|cell|mesh)\s+count\b(?:\s+(?:of|to|at|is|should|must|be|stays?|"
                           rf"stay|kept|keep|it))*\s+(?:(?:{_C_BOUND})\s+)?(?<![\w.,]){_C_NUM}(?![\w.]|\s*(?:mm|cm|"
                           rf"um|µm|inch|in\b|%))", re.I)
_C_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mil": 1e6, "milion": 1e6, "million": 1e6, "millions": 1e6,
           "mio": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
#: a range or a number being replaced is not one count (the run's reader reads a range; "instead of 8 million")
_C_RANGE_BEFORE = re.compile(r"\d\s*(?:k|m|million|thousand)?\s*(?:-|–|to|and)\s*$", re.I)
_C_REPLACED = re.compile(r"\b(?:instead of|rather than|from|not)\s+$", re.I)
#: a bound written after the noun: "2M cells max", "500k elements or fewer"
_C_AFTER_BOUND = {"ceiling": re.compile(r"^\s*(?:max(?:imum)?|at most|tops|or (?:less|fewer)|at the most)\b", re.I),
                  "floor": re.compile(r"^\s*(?:min(?:imum)?|at least|or more)\b", re.I)}
_C_RANGE_AFTER = re.compile(r"^\s*(?:-|–|to)\s*\d", re.I)
#: the bound in the reply's words: "under", "at least", "about"
_C_SAID = {"ceiling": "under", "floor": "at least", "target": "about"}
_C_CARD = {"ceiling": "at most", "floor": "at least", "target": "about"}


def count_asks(text: str) -> list[dict]:
    """NOTE-24, IN CODE: the whole-mesh element or cell counts the words state - {"what": "ceiling" | "floor" |
    "target", "value", "typed": the number as typed ("1.5 million"), "noun"} - none for a number that is not a count
    ("3 mm elements", "2 elements through the wall"), a range ("700k-1.5M elements", "between 1 and 2 million") or a
    number being replaced ("instead of 8 million"): those are the run's reader's alone. Pure."""
    raw = " ".join(str(text or "").replace("’", "'").split())
    if re.search(r"\bbetween\b", raw, re.I):
        return []
    out: list[dict] = []
    seen: set[tuple] = set()
    for pat in (_COUNT_AFTER, _COUNT_BEFORE):
        for m in pat.finditer(raw):
            num, mult = m.group("num"), (m.group("mult") or "")
            start = m.start("num")
            if _C_RANGE_BEFORE.search(raw[:start]) or _C_RANGE_AFTER.match(raw[m.end("mult" if mult else "num"):]):
                return []
            if _C_REPLACED.search(raw[:start]):
                continue
            try:
                value = float(num.replace(",", "")) * _C_MULT.get(mult.casefold(), 1.0)
            except ValueError:
                continue
            if value < 1000:
                continue
            what = next((k for k in _C_BOUNDS if m.group(k)), "") or next(
                (k for k, p in _C_AFTER_BOUND.items() if p.match(raw[m.end():])), "target")
            noun = str(m.groupdict().get("noun") or "elements").casefold()
            if (what, int(value)) in seen:
                continue
            seen.add((what, int(value)))
            typed = num + ((" " if len(mult) > 1 else "") + mult if mult else "")
            out.append({"what": what, "value": int(value), "typed": typed,
                        "noun": "cells" if noun.startswith("cell") else "elements"})
    return out


def count_rows(note: Mapping, *, flow: bool, note_id: str = "") -> list[dict]:
    """NOTE-24: the checklist's rows for the counts one note states - the whole mesh, the bound and the number as
    typed ("at most 1.5 million elements"; "cells" on a flow mesh) - stamped with the note's id. Pure."""
    text = str(note.get("text") or "")
    noun = "cells" if flow else "elements"
    rows = []
    for c in count_asks(text):
        rows.append({"count": {**c, "noun": noun}, "quote": text, "words": " ".join(text.split())[:60],
                     "size": f"{_C_CARD[c['what']]} {c['typed']} {noun}", "parts": [],
                     "parts_text": "the whole mesh", **({"note": note_id} if note_id else {})})
    return rows


def count_line(row: Mapping) -> str:
    """NOTE-24: the plain sentence for a count row: "I'll keep it under 1.5 million elements."."""
    c = row.get("count") or {}
    said = f"{c.get('typed')} {c.get('noun') or 'elements'}"
    return {"ceiling": f"I'll keep it under {said}.", "floor": f"I'll make it at least {said}.",
            "target": f"I'll aim for about {said}."}.get(str(c.get("what")), f"I'll aim for about {said}.")


def count_clash(view: Mapping | None, rows: Iterable[Mapping]) -> str:
    """NOTE-24: when this message's count meets a size the user typed (on the checklist), which one wins on this route,
    in one plain sentence - the run decides and the result tells it (PLAN-fea-29 / SHMESH-24: the sizes typed are
    built and the count moves only the rest; CFDREQ-3: on a flow mesh the count comes first). "" for no clash."""
    counts = [r for r in rows if r.get("count")]
    sized = [r for r in (view or {}).get("rows") or () if isinstance(r, Mapping) and not r.get("count")
             and str(r.get("size") or "")[:1].isdigit()]
    if not counts or not sized:
        return ""
    if (view or {}).get("flow"):
        return ("If that can't hold with the sizes you typed, the count comes first and the result will say where the "
                "sizes gave way.")
    return "If that can't hold with the sizes you typed, your sizes win and the result will say so."


#: a guessed place
CLICK_IF_ELSEWHERE ="That's my guess from your words; click it on the model if you meant another spot."
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
    if row.get("count"):
        return _cap(size)                 # NOTE-24: "At most 1.5 million elements"
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
    """What a row sets, said in the first person: "set 1 mm elements", "set a finer mesh", "noted “...”"."""
    size = str(row.get("size") or "")
    if size == "finer":
        return "set a finer mesh"
    if size == "coarser":
        return "set a coarser mesh"
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


def plural(noun: str) -> str:
    """"hole" -> "holes", "notch" -> "notches", "boss" -> "bosses"."""
    noun = str(noun or "place")
    return noun + ("es" if noun.endswith(("ch", "sh", "s", "x")) else "s")


def which_one_line(u: Mapping) -> str:
    """NOTE-21: a phrase that names ONE place where the part has several the words do not tell apart (the reader's
    `which_one`: how many, and the noun): nothing is set, and one click is asked for. "" for any other phrase."""
    n = u.get("which_one")
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        return ""
    noun = str(u.get("noun") or "place")
    return (f"There are {n} {plural(noun)} on this part and I can't tell which one you mean, so I haven't set it "
            f"anywhere yet. Click it on the model and send that again.")


def none_line(u: Mapping) -> str:
    """NOTE-23: a phrase that names a kind of place this part has none of (the reader's `none_of`, and `found`: what
    the part does have): nothing is set, and a click on the face meant is asked for. "" for any other phrase."""
    noun = str(u.get("none_of") or "").strip()
    if not noun:
        return ""
    found = str(u.get("found") or "").strip()
    return (f"I couldn't find any {plural(noun)} on this part" + (f" (it has {found})" if found else "")
            + ", so I haven't set that anywhere. Click the face you mean and send that again.")


def none_found(note: Mapping, view: Mapping | None, *, note_id: str = "") -> bool:
    """NOTE-23: this message has a phrase naming a kind of place the part has none of - `none_line` is its fallback."""
    return view is not None and any(none_line(u) for u in mine(note, view, note_id=note_id)[1])


# #
# NOTE-22: a click sent alone answers the one-place ask that waits for it
# #
#: a click sent alone after a "which one?" (NOTE-21) applies the words that wait to exactly the faces clicked
CLICK_RULE = "NOTE-22"
#: where the waiting ask is kept: the session's plan-step state (its intake gate, stored with the session)
WAITING = "one_place_ask"
#: words that only point at what was clicked ("this one", "here", "that hole", "these") - nothing to set in them
_POINTING = frozenset((
    "this", "that", "these", "those", "here", "there", "it", "its", "thats", "is", "the", "one", "ones", "a",
    "an", "please", "pls", "plz", "ok", "okay", "i", "mean", "meant", "yes", "yeah", "yep", "right", "exactly", "just",
    "on", "in", "at", "me", "my", "hole", "holes", "bore", "bores", "fillet", "fillets", "notch", "notches",
    "slot", "slots", "groove", "boss", "rib", "flange", "pocket", "chamfer", "cutout", "opening", "lip", "tab", "round",
    "corner", "edge", "edges", "face", "faces", "wall", "walls", "thin", "bend", "bends", "shaft", "shafts", "spot", "place", "area", "surface", "bit"))
#: the most words a pointing message has
_POINTING_MAX = 8


def points_only(text: str) -> bool:
    """NOTE-22: the words, if any, only point at what was clicked - no words, or "this one", "here", "that hole",
    "these" - with no number and no mesh word of their own."""
    words = _words(text).split()
    return len(words) <= _POINTING_MAX and not _REQUIREMENT.search(" ".join(words)) and all(
        w in _POINTING for w in words)


def waiting_ask(note: Mapping, view: Mapping | None, *, note_id: str = "") -> dict | None:
    """NOTE-22: the one-place ask this message leaves waiting for a click - its phrase that names ONE place where the
    part has several (NOTE-21's `which_one`): its words, the kind of place, how many there are - kept one more turn
    when the next message is not a click. None when it left none, or more than one (one click answers one)."""
    if view is None:
        return None
    # (NOTE-23: a kind of place the part has none of waits for its click the same way, with n = 0)
    asks = [u for u in mine(note, view, note_id=note_id)[1] if which_one_line(u) or none_line(u)]
    if len(asks) != 1:
        return None
    u = asks[0]
    words = " ".join(str(u.get("quote") or u.get("words") or "").split())
    n = u.get("which_one")
    return {"words": words, "noun": str(u.get("noun") or u.get("none_of") or ""),
            "n": int(n) if isinstance(n, int) else 0, "turns_left": 1} if words else None


def after_turn(waiting: Mapping | None, *, text: str, faces: bool) -> tuple[str, dict | None]:
    """NOTE-22, decided in code for each message at the plan step while an ask waits: (the earlier words this
    message's click answers - "" for none; the ask still waiting after it - None when it is dropped).
    Faces picked with no words or only pointing words answer it (applied to exactly those faces, whatever kind they
    are); faces with words of their own, or mesh-setting words, win and it is dropped unsaid; any other message keeps
    it one more turn at most - it is never applied to a place the user did not click."""
    if not isinstance(waiting, Mapping) or not str(waiting.get("words") or "").strip():
        return "", None
    if faces:
        return (str(waiting["words"]) if points_only(text) else ""), None
    if requirement_content(text) and not mesh_question(text):
        return "", None                 # mesh-setting words of their own ("make the fillet finer")
    left = waiting.get("turns_left")
    left = left if isinstance(left, int) and not isinstance(left, bool) else 0
    return "", ({**waiting, "turns_left": left - 1} if left > 0 else None)


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
    said = _norm(UN.said(note))       # its words, and (NOTE-22) the earlier words its click answers
    rows = [r for r in (view or {}).get("rows") or () if isinstance(r, Mapping) and _row_is_mine(r, note_id, said)]
    unsure = [u for u in (view or {}).get("unsure") or ()
              if isinstance(u, Mapping) and _row_is_mine(u, note_id, said)]
    return rows, unsure


def fixed_reply(note: Mapping, view: Mapping | None) -> str:
    """The reply no model writes: the reading is not there to say anything about (still going, not started, failed).
    Empty when there are facts to answer from."""
    if not _norm(UN.said(note)):
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
    text = UN.said(note)               # (NOTE-22) with the earlier words a click sent alone answers
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
    # NOTE-24: a count is said on its own ("I'll keep it under 1.5 million elements."), with a clash told plainly
    counts = [r for r in rows if r.get("count")]
    rows = [r for r in rows if not r.get("count")]
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
    out += [count_line(r) for r in counts[:MAX_SAID]]
    clash = count_clash(view, counts)
    if clash:
        out.append(clash)
    for u in unsure[:MAX_SAID]:
        what = str(u.get("words") or u.get("quote") or "").strip()
        out.append(which_one_line(u) or none_line(u) or f"I couldn't tell where “{what}” is. Click it on the model and send it again.")
    flow = [_cap(x) for x in _lines_mine(view.get("lines") or (), said)][:MAX_SAID]
    if flow:
        # the flow reader's own lines: what a flow mesher applies depends on the mesher
        out += [*flow, CONFIRM_ON_RESULT]
    if not out:
        # NO CHECKLIST BEFORE THE RUN on this route (a flow model with no parts): the words are kept and the run
        # reads them - "couldn't read that" would be false; elsewhere the checklist really took nothing
        kept = (view.get("flow") or view.get("notes_only")) and (faces or requirement_content(text))
        # NOTE-25: nothing on the checklist from it - kept for the run all the same, and said so
        out.append(kept_line(text, faces) if kept else kept_reply(text))
    if pictures:
        out.append(PICTURE_NOTE)
    return " ".join(out)


def took_nothing(note: Mapping, view: Mapping | None, *, note_id: str = "") -> bool:
    """NOTE-25: the checklist, read and done, took nothing from these words (no row, no phrase waiting, no flow line,
    not a requirement kept for a flow run's reader) - the case `reply_for` answers with `kept_reply`."""
    text = UN.said(note)
    if not _norm(text) or view is None or fixed_reply(note, view):
        return False
    rows, unsure = mine(note, view, note_id=note_id)
    if rows or unsure or _lines_mine(view.get("lines") or (), _norm(text)):
        return False
    faces = [f for f in note.get("faces") or () if isinstance(f, Mapping)]
    return not ((view.get("flow") or view.get("notes_only")) and (faces or requirement_content(text)))


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


__all__ = ["ASK", "ASK_LINE", "BOTH", "CLICK_RULE", "GO", "HOLD", "HOLD_LINE", "INTAKE", "NOTE", "QUESTION",
           "QUESTION_RULE", "RULE", "WAITING", "about_the_mesh", "after_turn", "points_only", "waiting_ask",
           "answers_another_question", "asked_last", "asks", "COUNT_RULE", "count_asks", "count_clash",
           "count_line", "count_rows", "exact_go", "KEPT_RULE", "asks_of_the_mesh", "geometry_edit", "kept_reply", "took_nothing", "face_words", "fixed_reply", "lines_mine",
           "mesh_question", "mine", "never_go", "none_found", "none_line", "pictures_of", "plain_face_rows", "plan_change", "plural", "reading",
           "reply_for", "requirement_content", "row_place", "row_what", "which_one_line"]
