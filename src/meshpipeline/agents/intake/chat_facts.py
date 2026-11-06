# Responsibility: Give the chat the facts the application measured, and hold its replies to them.
# Owns: the facts block in the intake's instructions (the user's notes, the unit the delivered files are in,
#       the mesh's size and the last run's result) and the reply guards for a size the application did not
#       compute and a file unit that is not the delivered one.
# Boundaries: words over the turn's state and the reply; it decides nothing about a run and stores nothing.
# Collaborates with: agents/intake/agent.py (adds the block, applies the guards), contracts/user_notes.py
#                    (the notes, word for word), contracts/mesh_units.py (the one units sentence),
#                    application/run_facts.py (the delivered mesh's size), agents/intake/approval.py
#                    (what a question is).
"""THE CHAT SAYS WHAT WE KNOW, NOT WHAT A MODEL GUESSES (demo dry run, 2 Nov 2026).

The intake model answered the user's questions from its own general knowledge, and four demo parts
showed what that costs: a size "estimate" 3 to 30 times off the mesh the run built (the Camaro "10-30
M" for 3.7 M, the exhaust header "50k-200k" for 5.6 M), "the mesh has not been generated yet" after
the run had delivered it, "the requirements do not specify" bolt holes the user's own note asked for,
and "the coordinates will remain in millimetres" for files written in metres.

So every chat turn is given the facts the application holds - the user's notes, the delivered files'
unit, the size once a run has measured it (and, before that, that it is not computed yet), and the
last run's result - and a reply that states a cell count or a file unit those facts do not back has
that sentence replaced by the fact. The size of a mesh is the run's planner's to compute (it shows in
the run box: "I expect about 6M cells"); before a run it is not known, and the chat says so.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

#: What the chat says of the size before any run has measured it.
SIZE_NOT_COMPUTED = (
    "The mesh's size is not computed yet: the run works it out from your measured part when it starts, "
    "and its estimate then shows in the run box (\"I expect about ... {what}\"). I do not guess it "
    "beforehand.")

# #
# the facts
# #


def _purpose(state: Mapping) -> str:
    prev = state.get("previous_run") or {}
    return str(prev.get("purpose") or state.get("purpose") or "")


def counted_as(state: Mapping) -> str:
    """What the mesh is counted in: an FEA mesh in elements, a flow mesh in cells."""
    purpose = _purpose(state)
    fea = purpose == "structural" if purpose else _fea_declared(state)
    return "elements" if fea else "cells"


def _fea_declared(state: Mapping) -> bool:
    from meshpipeline.application.geometry_confirmation import fea_declared
    return fea_declared(list(state.get("messages") or []))


def delivered_count(state: Mapping) -> int | None:
    """The last run's delivered mesh size, as the run measured it; None when no run delivered one."""
    prev = state.get("previous_run") or {}
    try:
        n = int(prev.get("cells") or 0)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def size_fact(state: Mapping) -> str:
    n = delivered_count(state)
    if n:
        return f"The mesh the last run delivered has {n:,} {counted_as(state)}."
    return SIZE_NOT_COMPUTED.format(what=counted_as(state))


def units_fact(state: Mapping) -> str:
    from meshpipeline.application.geometry_confirmation import declared_unit
    from meshpipeline.contracts.mesh_units import export_units_words
    return export_units_words(declared_unit(list(state.get("messages") or [])) or None)


def facts_block(state: Mapping) -> str:
    """The section every chat turn's instructions end with: what the application knows, and the
    rule that the reply never states a size or a file unit these facts do not give."""
    from meshpipeline.contracts import user_notes as un

    lines = [
        "\n\n## FACTS THE APPLICATION HOLDS - answer from these, never from general knowledge",
        f"- MESH SIZE: {size_fact(state)}",
        f"- FILE UNITS: {units_fact(state)}",
        "Never state a cell or element count, or a range of them, that is not the one above - not as "
        "an estimate, a typical figure or an example. Asked how big the mesh will be before a run, say "
        "the size line above. Asked what unit the files are in, say the units line above.",
    ]
    notes = un.chat_block(state.get("user_notes") or [], consumer="chat")
    if notes:
        lines.append(notes)
    return "\n".join(lines)


def asks_about_last_run(state: Mapping, latest_user_msg: str) -> bool:
    """The turn after a run has ended, when the user only ASKS (how did it go, does anything need
    attention, how big is it): the answer comes from the run's result, and no new run is set up."""
    if not state.get("previous_run"):
        return False
    from meshpipeline.agents.intake import approval as ap
    return ap.is_question(latest_user_msg)


# #
# the guards
# #

_NUM = r"\d+(?:[, ]\d{3})*(?:\.\d+)?"
_MULT = r"(?:\s*(?:million|thousand|mil|[kKM])\b)?"
_COUNT_NOUN = r"(?:cells?|elements?|tetrahedra|tets?|hexahedra|hexes|polyhedra)"
#: "10-30 million cells", "100,000 to 1,000,000 elements", "**200,000-800,000** quadratic tetrahedra"
_CLAIM = re.compile(
    rf"(?P<a>{_NUM})(?P<am>{_MULT})(?:\s*(?:–|—|-|to)\s*(?P<b>{_NUM})(?P<bm>{_MULT}))?"
    rf"(?:\*\*)?\s*(?:\(?[A-Za-z][A-Za-z\-]*\)?\s+){{0,3}}?(?:\*\*)?{_COUNT_NOUN}\b")
#: a magnitude with a word for it ("1-3 million or more"): a size whatever it counts, once a reply
#: has made one count claim
_BARE_MAGNITUDE = re.compile(rf"\b{_NUM}\s*(?:million|thousand)\b|\b{_NUM}\s*[kM]\b(?!\s*/)")
_FACTOR = {"": 1.0, "k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mil": 1e6}


def _value(num: str, mult: str) -> float:
    return float(num.replace(",", "").replace(" ", "")) * _FACTOR.get(mult.strip().lower(), 1.0)


def _claims(text: str) -> list[tuple[float, float]]:
    """Every count claim in `text` as (low, high); a lone count is (n, n). Counts under a thousand
    with no magnitude word ("2 elements through the wall", "22 elements at a cusp") are not sizes."""
    out = []
    for m in _CLAIM.finditer(text):
        am, bm = m.group("am") or "", m.group("bm") or ""
        if m.group("b") is not None and not am.strip():
            am = bm                      # "10-30 million": the magnitude is said once, for both
        lo = _value(m.group("a"), am)
        hi = _value(m.group("b"), bm) if m.group("b") is not None else lo
        if max(lo, hi) < 1000 and not (am.strip() or bm.strip()):
            continue
        out.append((min(lo, hi), max(lo, hi)))
    return out


#: How far a stated size may sit from the measured one and still be that size ("about 3.7M" for 3,660,268)
_CLOSE = 1.35


def _backed(claim: tuple[float, float], known: int | None) -> bool:
    if not known:
        return False
    lo, hi = claim
    return known / _CLOSE <= lo and hi <= known * _CLOSE


_FILE_NOUN = re.compile(r"\b(?:files?|coordinates|mesh|deck|exports?|exported|delivered|solver files?|"
                        r"\.inp|\.bdf|\.cdb|openfoam|case)\b", re.IGNORECASE)
_OTHER_UNIT = re.compile(r"\b(?:in|remains? in|stays? in|written in|kept in|use|uses|using)\s+(?:\*\*)?"
                         r"(?:millimet(?:re|er)s?|mm|centimet(?:re|er)s?|cm|inch(?:es)?)\b(?:\*\*)?",
                         re.IGNORECASE)
_METRES = re.compile(r"\bmet(?:re|er)s\b|\bin (?:\*\*)?m(?:\*\*)?\b", re.IGNORECASE)


def _wrong_unit(sentence: str) -> bool:
    """A sentence that says the delivered files are in a unit other than metres."""
    return bool(_FILE_NOUN.search(sentence) and _OTHER_UNIT.search(sentence)
                and not _METRES.search(sentence))


@dataclass
class Held:
    """A reply after the guards: its text, and what was taken out of it (for the log)."""

    text: str
    dropped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped)


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*(\"'])")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def hold_to_facts(reply: str, state: Mapping) -> Held:
    """THE REPLY HELD TO THE FACTS: every sentence (or list item) stating a mesh size the facts do
    not back, or a file unit other than the delivered one, is taken out, and the fact is said once
    in its place. A reply that states neither is returned as it is."""
    text = str(reply or "")
    if not text.strip():
        return Held(text)
    known = delivered_count(state)
    any_claim = any(not _backed(c, known) for c in _claims(text))
    dropped: list[str] = []
    size_dropped = unit_dropped = False
    kept_lines: list[str] = []
    for line in text.split("\n"):
        if _LIST_ITEM.match(line) or not line.strip():
            parts = [line]
        else:
            parts = _SENTENCE.split(line)
        kept_parts = []
        for part in parts:
            claims = _claims(part)
            if any(not _backed(c, known) for c in claims) or (
                    any_claim and not claims and _BARE_MAGNITUDE.search(part)):
                dropped.append(part.strip())
                size_dropped = True
                continue
            if _wrong_unit(part):
                dropped.append(part.strip())
                unit_dropped = True
                continue
            kept_parts.append(part)
        if parts and not kept_parts:
            continue                     # the whole line was a claim: no empty line left behind
        kept_lines.append(" ".join(p.strip() if i else p.rstrip() for i, p in enumerate(kept_parts)))
    if not dropped:
        return Held(text)
    # a lead-in left with nothing to lead into ("A reasonable rough estimate is:")
    cleaned: list[str] = []
    for i, line in enumerate(kept_lines):
        nxt = next((x for x in kept_lines[i + 1:] if x.strip()), "")
        if line.rstrip().endswith(":") and not _LIST_ITEM.match(nxt):
            dropped.append(line.strip())
            continue
        cleaned.append(line)
    body = "\n".join(cleaned).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    said = [s for s, on in ((size_fact(state), size_dropped), (units_fact(state), unit_dropped)) if on]
    return Held("\n\n".join([*([body] if body else []), *said]), dropped)


__all__ = ["SIZE_NOT_COMPUTED", "Held", "asks_about_last_run", "counted_as", "delivered_count",
           "facts_block", "hold_to_facts", "size_fact", "units_fact"]
