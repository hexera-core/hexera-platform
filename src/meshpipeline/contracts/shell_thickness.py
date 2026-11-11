# Responsibility: Read a shell's WALL THICKNESS from the user's own words (SURF-4): "1.2 mm thick", "thickness 0.8",
# "t = 0.8", "gauge 2 mm", "2mm wall" - one fixed word table, no model. A surface file has no thickness of its own, so
# this is the only place one comes from.
# Boundaries: pure over text; it never guesses. A bare number is millimetres (said back to the user in the result's
# line). A bare gauge number ("16 gauge") is not a thickness - the tables differ by metal - and is handed back as
# unread, as is a range. It decides nothing about meshing.
# Collaborates with: engines/surface_run.py (the run's thickness), application/surface_check.py (the stage's line),
# contracts/user_notes.py (where the words are kept). Kept apart so a general reader of the notes' mesh
# requirements can take it over whole.
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: millimetres per unit, by the words a user writes it in
_UNITS: dict[str, float] = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0, "millimetres": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0, "centimetre": 10.0, "centimetres": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0, "metres": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
    "thou": 0.0254, "mil": 0.0254, "mils": 0.0254,
    "um": 0.001, "µm": 0.001, "μm": 0.001, "micron": 0.001, "microns": 0.001,
}
#: a thickness is believable from a tenth of a micron to a metre
MIN_MM, MAX_MM = 1e-4, 1000.0

_NUM = r"(?P<num>\d+(?:[.,]\d+)?|\.\d+)"
_UNIT = r"(?P<unit>" + "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True)) + r")"
#: after a unit (or a bare number) no further letter or digit may follow, nor a unit that is not a length: "0.8 s",
#: "300K", "5 mmHg", "2 kg" are not thicknesses
_END = (r"(?![A-Za-z0-9µμ])(?!\s*(?:s|sec|secs|seconds?|ms|min|k|c|f|deg|degrees?|hz|khz|n|kn|pa|kpa|mpa|gpa|bar|kg|g|"
        r"elements?|nodes?|cells?|layers?|parts?|x)\b)(?!\s*[%°])")
_JOIN = r"(?:\s*(?:of|is|was|at|to|about|around|approx\.?|approximately|roughly|~|=|:|-)\s*|\s+){1,3}"
_KEY = r"(?:wall\s+thickness|sheet\s+thickness|plate\s+thickness|shell\s+thickness|material\s+thickness|thickness|thk\.?)"
_GAUGE = r"(?:gauge|gage)"
_NOUN = r"(?:wall|sheet|plate|gauge|gage)"

#: THE WORD TABLE, in the order it is tried at each place in the text
_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "1.2 mm thick", "1.2mm-thick", "2 mm thk", "0.8 thick"
    ("thick", re.compile(_NUM + r"\s*" + _UNIT + r"?" + r"[\s-]*(?:thick(?:ness)?|thk)\b", re.I)),
    # "thickness 1.2 mm", "thickness of 1.2", "wall thickness: 0.8", "thk = 2mm"
    ("thickness", re.compile(r"\b" + _KEY + _JOIN + _NUM + r"\s*" + _UNIT + r"?" + _END, re.I)),
    # "gauge 2 mm" (a gauge needs its unit: a bare gauge number is a table look-up, not a length)
    ("gauge", re.compile(r"\b" + _GAUGE + _JOIN + _NUM + r"\s*" + _UNIT + _END, re.I)),
    # "2 mm gauge", "0.8 mm sheet", "1.5mm wall", "3 mm plate" - never "5 mm plate elements" (an element size)
    ("noun", re.compile(_NUM + r"\s*" + _UNIT + r"[\s-]+" + _NOUN + r"\b(?!\s+(?:elements?|mesh|size))", re.I)),
    # "t = 0.8", "t=0.8mm" (a lower-case t: "T = 300" is a temperature)
    ("t", re.compile(r"(?<![A-Za-z0-9_])t\s*=\s*" + _NUM + r"\s*" + _UNIT + r"?" + _END)),
)
#: words that look like a thickness and are not read as one
_BARE_GAUGE = re.compile(r"\b(?:\d+\s*-?\s*(?:gauge|gage|ga)\b|(?:gauge|gage)\s*(?:of|is|=|:)?\s*\d+(?![\d.,]*\s*(?:"
                         + "|".join(re.escape(u) for u in _UNITS) + r")))", re.I)
_RANGE_BEFORE = re.compile(r"(?:\d\s*(?:-|–|to)\s*)$", re.I)
#: a thickness of something else: a boundary layer, a coating
_NOT_A_WALL = re.compile(r"(?:layer|coating|film|paint|plating)\s*$", re.I)


@dataclass(frozen=True)
class ThicknessReading:
    """What the words say about the wall: `mm` (None when they give none), the `quote` it was read from, any OTHER
    different thicknesses the same words name (one is used for the whole file), and words that look like a thickness
    but were not read as one (`unread`: a bare gauge number, a range)."""
    mm: float | None = None
    quote: str = ""
    others_mm: tuple[float, ...] = ()
    unread: str = ""

    @property
    def given(self) -> bool:
        return self.mm is not None


def _mm(num: str, unit: str | None) -> float | None:
    try:
        value = float(num.replace(",", "."))
    except ValueError:
        return None
    mm = value * _UNITS.get((unit or "mm").lower(), 1.0)
    return float(f"{mm:.6g}") if MIN_MM <= mm <= MAX_MM else None


def read_text(text: Any) -> ThicknessReading:
    """SURF-4: the thickness one piece of text gives. The first one named stands; others that differ are listed."""
    said = str(text or "")
    found: list[tuple[int, float, str]] = []
    unread = ""
    taken: list[tuple[int, int]] = []
    for _name, pattern in _FORMS:
        for m in pattern.finditer(said):
            span = m.span("num")
            if any(a <= span[0] < b for a, b in taken):
                continue
            before = said[:m.start()]
            quote = " ".join(m.group(0).split())
            if _RANGE_BEFORE.search(said[:span[0]]):
                unread = unread or f"a range ({quote})"
                taken.append(m.span())
                continue
            if _NOT_A_WALL.search(before):
                taken.append(m.span())
                continue
            mm = _mm(m.group("num"), m.groupdict().get("unit"))
            taken.append(m.span())
            if mm is None:
                unread = unread or quote
                continue
            found.append((m.start(), mm, quote))
    if not unread:
        g = _BARE_GAUGE.search(said)
        if g and not any(a <= g.start() < b for a, b in taken):
            unread = f"a gauge number ({' '.join(g.group(0).split())})"
    if not found:
        return ThicknessReading(unread=unread)
    found.sort()
    first = found[0]
    others = tuple(dict.fromkeys(mm for _at, mm, _q in found[1:] if abs(mm - first[1]) > 1e-9 * max(mm, first[1])))
    return ThicknessReading(mm=first[1], quote=first[2], others_mm=others, unread=unread)


def read(texts: Iterable[Any]) -> ThicknessReading:
    """SURF-4: the thickness the user's words give, THE LATEST WORD STANDING: `texts` oldest first; the newest text
    that names a thickness is the one read. Words not read as a thickness are kept from the newest text that has any."""
    unread = ""
    for text in reversed([t for t in texts if str(t or "").strip()]):
        got = read_text(text)
        if got.given:
            return got if got.unread or not unread else ThicknessReading(got.mm, got.quote, got.others_mm, unread)
        unread = unread or got.unread
    return ThicknessReading(unread=unread)


def texts_of(state: Mapping | None) -> list[str]:
    """The user's words a run carries, oldest first: the intake's summary, then their notes word for word
    (contracts/user_notes) - so a note beats the summary."""
    s = state or {}
    out = [str(s.get("request_txt") or "")]
    out += [str(n.get("text") or "") for n in s.get("user_notes") or [] if isinstance(n, Mapping)]
    return [t for t in out if t.strip()]


def words(mm: float) -> str:
    """A thickness as it is said: "1.2 mm", "0.05 mm", "12 mm"."""
    return f"{float(mm):.4g} mm"


__all__ = ["MAX_MM", "MIN_MM", "ThicknessReading", "read", "read_text", "texts_of", "words"]
