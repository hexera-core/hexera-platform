# Responsibility: Tell an intake reply that is not fit to send - the model's own planning notes
# leaked into it, or a hole where a value belongs - from the engineering prose a user should read.
# Boundaries: pure text in, a verdict out. It calls no model and changes no text; the loop policy
# (agents/intake/loop_policy.py) sends such a reply back once for a clean rewrite.
"""Garbled replies.

The intake model sometimes lets its working show. From the demo transcript gate (2026-09-30) and
the soak transcripts before it, all in replies proposing a near-wall setup:

* planning notes: "about Layer? Need avoid unclear. say 8 prism layers", and "_layers
  unspecified?** Wait. Need sensible specific layer count. Say 10 prism layers";
* holes: "about  layer inflation", "and approximately  prism layers" - the number left out, two
  spaces where it stood - and "approximately it 5 prism layers".

The holes are the model's own: the same ones are in replies captured before any text was
normalised on their way to the user, and the normaliser (contracts/engineering_text.py) never
drops a digit.

Each rule below is a shape no finished sentence to a user has, so ordinary engineering prose is
never caught: every rule was run over every reply in the soak and demo transcripts, and it finds
the garbled ones and nothing else. Code - fenced blocks, `inline code`, table rows - is not read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Flaw", "flaw"]


@dataclass(frozen=True)
class Flaw:
    kind: str          # "note" (planning text) or "hole" (a value missing)
    rule: str          # which shape was found, for the log - never the reply's words
    fragment: str      # the words around it, for a test to read


#: A sentence that starts with a bare "Need" and a word that is not "to", and ends with a full
#: stop: telegraphic planning ("Need avoid unclear.", "Need sensible specific layer count.").
#: A question ("Need a finer mesh near the wall?") ends with a question mark and is left alone.
_NEED = re.compile(r"(?:^|[.!?]\s+|\*\*\s*)(Need +(?!to\b)[a-z][^.?!\n]{0,80}\.)(?=\s|$)", re.M)
#: "Wait." as a sentence of its own: the model stopping itself mid-thought.
_WAIT = re.compile(r"(?:^|[.!?*]\s+)(Wait\.)(?=\s|$)", re.M)
#: A sentence that starts with a lower-case "say" and a number: an instruction to itself ("...
#: unclear. say 8 prism layers"). After an abbreviation ("e.g. say 5 layers") it is prose.
_SAY = re.compile(r"(\S*)[.!?] +(say +[0-9][^.?!\n]{0,40})")
_ABBREVIATIONS = frozenset(("e.g", "i.e", "etc", "approx", "vs", "cf", "ca", "incl", "resp", "no", "fig"))
#: Two spaces between words in the middle of a line: where a value stood before it went missing
#: ("about  layer inflation", "and approximately  prism layers", "30–300,  nSurfaceLayers").
_GAP = re.compile(r"[a-z,] {2,}[a-z]")
#: A hedge, then "it", then a number: "approximately it 5 prism layers". Only the hedges that
#: cannot take "it" as their object: "refine around it 5 mm beyond the edge" is a sentence.
_HEDGE_IT = re.compile(r"\b(?:approximately|roughly) +it +[0-9]")

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")


def _readable(text: str) -> list[str]:
    """The reply's prose lines: code blocks and inline code taken out, table rows skipped,
    indentation and line ends trimmed."""
    prose = _INLINE.sub("`code`", _FENCE.sub("\n", str(text or "")))
    return [line.strip() for line in prose.split("\n") if line.strip() and "|" not in line]


def _around(line: str, start: int, end: int) -> str:
    return line[max(0, start - 40):end + 40]


def flaw(text: str) -> Flaw | None:
    """The first thing that makes `text` unfit to send as it stands, or None when it reads as a
    finished reply to the user."""
    for line in _readable(text):
        for rule, pattern in (("need", _NEED), ("wait", _WAIT)):
            m = pattern.search(line)
            if m:
                return Flaw("note", rule, _around(line, m.start(1), m.end(1)))
        for m in _SAY.finditer(line):
            if m.group(1).lower().rstrip(".") not in _ABBREVIATIONS:
                return Flaw("note", "say", _around(line, m.start(2), m.end(2)))
        for rule, pattern in (("gap", _GAP), ("hedge_it", _HEDGE_IT)):
            m = pattern.search(line)
            if m:
                return Flaw("hole", rule, _around(line, m.start(), m.end()))
    return None
