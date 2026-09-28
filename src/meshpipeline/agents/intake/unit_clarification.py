# Responsibility: Ask, once, what physical unit an ambiguous geometry is in - and remember the answer.
# Boundaries: STL and VTK carry no unit, so the question is asked once per session rather than
# defaulted. When the part has been measured the question proposes millimetres and shows the
# part's size in every candidate unit, so "ok" is an answer the user can check against what they
# know of their part. A reply that names no unit is an ordinary turn, not a refusal: the run
# itself cannot start without the unit (application/dispatch_contract refuses the pair), and the
# approval asks once more before it.
from __future__ import annotations

import re

from meshpipeline.contracts.geometry_units import (
    LengthUnit,
    UnitResolutionError,
    parse_unit,
    scale_to_metres,
)
from meshpipeline.contracts.unit_plausibility import (
    UNIT_WORDS,
    expected_from_words,
    length_words,
    proposed_unit,
)

#: Asked once, when a session's geometry has no confirmed scale and the part was not measured.
QUESTION = (
    "Before meshing I need to know what one unit in your geometry means, because this file does "
    "not state it. Are the coordinates in millimetres, centimetres, metres, or inches?"
)

#: The unit the question proposes for a file that carries none, once the part is measured: what
#: nearly every CAD file is drawn in - unless that makes the part implausible (a 1.04-unit Ahmed
#: body is a 1.04 m car, not a 1 mm one; see `proposal_for`). A proposal the user confirms with
#: the sizes in front of them - never a default that is taken in silence.
PROPOSED = LengthUnit.millimetre

_UNIT_WORDS = UNIT_WORDS
_CANDIDATES = (LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.metre, LengthUnit.inch)

#: A unit named anywhere in a sentence: "the coordinates are in millimetres", "mm I think". The
#: bare "m" and "in" are too common as words to count here; as the whole answer parse_unit
#: reads them.
_UNIT_WORD = r"(?:millimet(?:er|re)s?|mm|centimet(?:er|re)s?|cm|met(?:er|re)s?|inch(?:es)?)"
_UNIT_ANYWHERE = re.compile(r"\b(" + _UNIT_WORD + r")\b")
#: A number right before the unit word makes it a size ("about 2 metres long"), not the file's
#: unit - the one thing this question must never take from a sentence about size.
_SIZE_BEFORE = re.compile(r"\d\s*" + _UNIT_WORD + r"\b")
#: A negation right before it names the unit the user rejects ("not millimetres", "it isn't in
#: inches"), never the one the file is in.
_NEGATED_BEFORE = re.compile(r"\b(?:not|no|isn'?t|aren'?t|never|nor|rather than|instead of)\s+(?:in\s+|the\s+)?"
                             + _UNIT_WORD + r"\b")

#: What confirms a proposed unit: the whole message, nothing else in it. A sentence that merely
#: contains "yes" is not an answer to a question about scale.
_ASSENT = frozenset({"ok", "okay", "yes", "yep", "yeah", "sure", "fine", "correct", "right",
                     "that's right", "thats right", "go on", "go ahead", "confirmed", "confirm",
                     "yes please", "ok go", "sounds right", "looks right", "agreed", "y"})


_length_words = length_words


def size_hint(size_mm) -> str:
    """The part's longest side in each candidate unit, for the user to tell which one their file
    is in: "Its longest side would be 1.05 m if millimetres, 10.5 m if centimetres, ...\"."""
    longest = max(float(v) for v in size_mm)
    return "Its longest side would be " + ", ".join(
        f"{_length_words(longest * scale_to_metres(u))} if {_UNIT_WORDS[u]}" for u in _CANDIDATES) + "."


def proposal_for(size_mm, words: str = "") -> LengthUnit:
    """The unit the question proposes for a measured part: millimetres, unless that makes it
    implausible - by size alone (a 1.04-unit car read as 1 mm) or by what the user said it is
    (a "city block" 229 mm long) - and another unit does not."""
    if not size_mm:
        return PROPOSED
    return proposed_unit(max(float(v) for v in size_mm), expected_from_words(words))


def question_for(size_mm, proposal: LengthUnit | None = None) -> str:
    """The unit question. With the part's measured size in hand it proposes a unit - millimetres
    unless the size says otherwise - and shows how long the part would be in each unit, so the
    user can tell at a glance which one their file is in and a plain "ok" confirms the proposal.
    Without a size it is the plain question: nothing is proposed that the user cannot check."""
    if not size_mm:
        return QUESTION
    take = proposal or PROPOSED
    return ("This file does not say what one unit of its coordinates means, and I need to know "
            f"before meshing. {size_hint(size_mm)} I'll take {_UNIT_WORDS[take]} - say ok, or "
            "name the unit: millimetres, centimetres, metres, or inches.")


def before_run(size_mm, proposal: LengthUnit | None) -> str:
    """The question once more, when the user approves a run while it is still open - with the
    proposal when the part was measured, and why it cannot wait."""
    body = question_for(size_mm, proposal)
    if not size_mm and proposal is not None:
        body += f" Or say ok to take {_UNIT_WORDS[proposal]}."
    return "One thing before I start the run. " + body


def noted(unit: LengthUnit) -> str:
    return f"Noted: the file is in {_UNIT_WORDS[unit]}."


def needs_confirmation(session) -> bool:
    return bool(getattr(session, "geometry_source_id", None)) and not getattr(
        session, "geometry_interpretation_id", None)


def already_asked(intake_gate) -> bool:
    return bool((intake_gate or {}).get("unit_question"))


def proposed(intake_gate) -> LengthUnit | None:
    """The unit the question proposed, when it did; a plain "ok" confirms only that."""
    value = ((intake_gate or {}).get("unit_question") or {}).get("proposed")
    try:
        return LengthUnit(value) if value else None
    except ValueError:
        return None


def classify(message: str, proposal: LengthUnit | None = None) -> LengthUnit | None:
    """The unit the message names, or the proposed one when the message is a plain assent to
    it. A bare unit word is the answer, as before; so is a unit named anywhere in the sentence
    ("the coordinates are in millimetres", "yes, mm", "mm I think") when it is the only one
    named, no number sits before it - "about 2 metres long" states a size, not the file's
    scale - and no negation does: "not millimetres" rejects one. Anything else names nothing."""
    text = str(message or "").strip()
    try:
        return parse_unit(text)
    except UnitResolutionError:
        pass
    low = text.lower()
    named = {parse_unit(word) for word in _UNIT_ANYWHERE.findall(low)}
    if len(named) == 1 and not _SIZE_BEFORE.search(low) and not _NEGATED_BEFORE.search(low):
        return named.pop()
    if proposal is not None and " ".join(low.replace(",", " ").split()).strip(" .!") in _ASSENT:
        return proposal
    return None


#: A sentence that says what unit the FILE is in, unasked: "the file is in metres", "it's in
#: inches", "the units are mm", "the model was drawn in cm". The subject has to be the file, its
#: numbers or "it"; a speed ("metres per second") and a size ("117 metres long") are not a unit.
_SUBJECT = (r"(?:file|geometry|model|cad|step|stl|obj|vtp|iges|part|drawing|coordinates?|numbers|"
            r"dimensions|sizes|units?|scale|it|everything|this|that|they)")
_FILLER = r"(?:all|actually|really|definitely|drawn|modell?ed|exported|saved|given|written|made)"
_STATES = re.compile(r"\b" + _SUBJECT + r"(?:'s|'re|\s+(?:is|are|was|were))?\s+(?:" + _FILLER + r"\s+)*"
                     r"(?:in\s+)?(?:the\s+)?(" + _UNIT_WORD + r")\b(?!\s*(?:per\b|/))")
_WORD = re.compile(r"[a-z0-9']+")
_UNIT_ONLY = re.compile(_UNIT_WORD)
#: The little words a reply that is only a unit may carry around it.
_REPLY_FILLER = frozenset({"no", "nope", "not", "sorry", "oh", "ah", "actually", "really", "it", "it's", "its",
                           "is", "in", "the", "all", "they're", "theyre", "are", "that's", "thats", "wait",
                           "i", "mean", "meant", "rather", "than", "instead", "of", "definitely", "yes", "ok"})


def stated_unit(message: str) -> LengthUnit | None:
    """The unit the user says the file is in, when nobody asked: after the unit was settled, a
    later "the file is in metres" is a change of unit, not a size. A bare unit or a short reply
    naming exactly one ("metres, not millimetres") counts too. A question ("is it in metres?"),
    a negation, a speed or a size names nothing."""
    text = str(message or "").strip()
    if not text or text.endswith("?"):
        return None
    try:
        return parse_unit(text)
    except UnitResolutionError:
        pass
    low = text.lower()
    rejected = {parse_unit(w) for w in re.findall(r"\b(?:not|no|isn'?t|aren'?t|never|rather than|instead of)\s+"
                                                  r"(?:in\s+|the\s+)?(" + _UNIT_WORD + r")\b", low)}
    stated = {parse_unit(m.group(1)) for m in _STATES.finditer(low)} - rejected
    if len(stated) == 1:
        return stated.pop()
    if stated:
        return None
    # A SHORT REPLY THAT IS ONLY A UNIT - "no, metres", "metres, not mm", "actually inches": every
    # word a unit or a little word around one. "use snappy with mm" is about something else.
    rest = [w for w in _WORD.findall(low) if w not in _REPLY_FILLER and not _UNIT_ONLY.fullmatch(w)]
    named = {parse_unit(w) for w in _UNIT_ANYWHERE.findall(low)} - rejected
    if not rest and len(named) == 1 and not re.search(r"\b" + _UNIT_WORD + r"\s*(?:per\b|/)", low):
        return named.pop()
    return None


def asked(intake_gate, proposal: LengthUnit | None = None) -> dict:
    gate = dict(intake_gate or {})
    gate["unit_question"] = {"asked": True, **({"proposed": proposal.value} if proposal else {})}
    return gate


def answered(intake_gate) -> dict:
    gate = dict(intake_gate or {})
    gate.pop("unit_question", None)
    return gate


async def record(db, *, owner_id: str, geometry_source_id, unit: LengthUnit,
                 organization_id: str = ""):
    from meshpipeline.contracts.geometry_units import ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    return await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=geometry_source_id, unit=unit,
        basis=ResolutionBasis.user_confirmed, evidence="confirmed in conversation",
        organization_id=organization_id)
