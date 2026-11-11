# Responsibility: Read from the user's own words HOW FAR A SURFACE FILE'S GAPS MAY BE STITCHED (SURF-8: "stitch gaps
# up to 0.5 mm", "sewing tolerance 0.2 mm", "close gaps under 0.3 mm", "do not stitch") and WHICH SMALL HOLES TO FILL
# (SURF-10: "ignore holes under 5 mm", "fill holes smaller than 3 mm") - one fixed word table, no model.
# Boundaries: pure over text; it never guesses. A bare number is millimetres. It decides nothing about the geometry:
# cad/surface_clean applies what is read here, and engines/surface_run says it back.
# Collaborates with: engines/surface_run.py (the run's tolerance), cad/surface_clean.py, contracts/shell_thickness.py
# (the same units table and the same "latest word stands" reading).
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from meshpipeline.contracts.shell_thickness import _UNITS

#: a stitch tolerance or a hole size is believable from a hundredth of a micron to 100 mm
MIN_MM, MAX_MM = 1e-5, 100.0

_NUM = r"(?P<num>\d+(?:[.,]\d+)?|\.\d+)"
_UNIT = r"(?P<unit>" + "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True)) + r")"
_END = r"(?![A-Za-z0-9µμ])(?!\s*[%°])"
_UPTO = (r"(?:\s*(?:up\s+to|of\s+up\s+to|under|below|smaller\s+than|less\s+than|narrower\s+than|within|at|to|of|is|"
         r"about|around|<=|≤|<|=|:|-)\s*|\s+){1,3}")
_VERB = r"(?:stitch(?:ing|ed)?|sew(?:ing|n)?|knit(?:ting)?|close|closing|merge|merging|heal(?:ing)?|zip|join|weld)"
_GAPS = r"(?:(?:all|any|the|small|tiny)\s+)*(?:gaps?|cracks?|open\s+edges|free\s+edges|edges|surfaces|faces|seams?)"
_TOL_NOUN = (r"(?:stitch(?:ing)?|sew(?:ing)?|knit(?:ting)?|heal(?:ing)?|gap|topology|topo|clean[\s-]?up|"
             r"geometry\s+clean[\s-]?up)\s+tol(?:erance)?\.?")

#: THE WORD TABLE for the stitch tolerance, in the order it is tried
_STITCH: tuple[re.Pattern[str], ...] = (
    # "stitch gaps up to 0.5 mm", "close all gaps under 0.3mm", "sew the surfaces within 0.2", "merge edges < 0.1 mm"
    re.compile(r"\b" + _VERB + r"\s+" + _GAPS + _UPTO + _NUM + r"\s*" + _UNIT + r"?" + _END, re.I),
    # "stitch up to 0.5 mm", "sew at 0.2 mm", "stitch within 0.1"
    re.compile(r"\b(?:stitch|sew|knit)\s+(?:up\s+to|at|within|to)\s+" + _NUM + r"\s*" + _UNIT + r"?" + _END, re.I),
    # "stitch tolerance 0.5 mm", "sewing tolerance = 0.1", "geometry clean-up tolerance: 0.2 mm", "gap tol 0.05"
    re.compile(r"\b" + _TOL_NOUN + _UPTO + _NUM + r"\s*" + _UNIT + r"?" + _END, re.I),
    # "tolerance for stitching 0.5 mm", "tolerance for sewing of 0.2"
    re.compile(r"\btol(?:erance)?\.?\s+for\s+(?:stitching|sewing|knitting|healing|closing\s+gaps)" + _UPTO + _NUM
               + r"\s*" + _UNIT + r"?" + _END, re.I),
    # "gaps up to 0.5 mm can be closed", "gaps under 0.2 mm should be stitched"
    re.compile(r"\bgaps?" + _UPTO + _NUM + r"\s*" + _UNIT + r"?" + _END
               + r"\s+(?:can|may|should|must|are\s+to|to)\s+be\s+(?:closed|stitched|sewn|merged|healed)", re.I),
)
#: "do not stitch", "no stitching", "don't close gaps", "leave the gaps", "without sewing"
_NO_STITCH = re.compile(
    r"\b(?:(?:do\s+not|don'?t|never|no|without)\s+(?:stitch(?:ing)?|sew(?:ing)?|knit(?:ting)?|clos(?:e|ing)\s+"
    r"(?:the\s+|any\s+)?gaps?|heal(?:ing)?|geometry\s+clean[\s-]?up)|"
    r"(?:leave|keep)\s+(?:the\s+|all\s+)?gaps(?:\s+(?:open|as\s+(?:they\s+are|is|drawn)))?)\b", re.I)

_HOLE_VERB = (r"(?:ignor(?:e|ing)|remov(?:e|ing)|fill(?:ing)?|suppress(?:ing)?|delet(?:e|ing)|clos(?:e|ing)|"
              r"defeatur(?:e|ing)|drop(?:ping)?|skip(?:ping)?|leave\s+out)")
_HOLES = r"(?:(?:all|any|the|small|tiny|round|circular)\s+)*(?:pin\s*)?holes?"
_HOLE_UPTO = (r"(?:\s*(?:that\s+are\s+|with\s+(?:a\s+)?(?:diameters?|dia\.?|d)\s+)?(?:up\s+to|under|below|smaller\s+than|"
              r"less\s+than|<=|≤|<)\s*|\s+(?:of|with)\s+(?:diameters?|dia\.?|d)\s*(?:<=|≤|<|under|below|up\s+to)\s*)")
#: THE WORD TABLE for the holes to fill
_HOLE: tuple[re.Pattern[str], ...] = (
    # "ignore holes under 5 mm", "fill all holes smaller than 3mm", "remove small holes with diameter < 2 mm"
    re.compile(r"\b" + _HOLE_VERB + r"\s+" + _HOLES + _HOLE_UPTO + _NUM + r"\s*" + _UNIT + r"?" + _END, re.I),
    # "holes under 5 mm can be ignored", "holes smaller than 3 mm should be filled"
    re.compile(r"\b" + _HOLES + _HOLE_UPTO + _NUM + r"\s*" + _UNIT + r"?" + _END
               + r"(?:\s+(?:in\s+)?(?:diameter|dia\.?|across))?\s+(?:can|may|should|must|are\s+to|to)\s+be\s+"
               r"(?:ignored|removed|filled|suppressed|deleted|closed|defeatured|dropped|skipped|left\s+out)", re.I),
)


@dataclass(frozen=True)
class StitchReading:
    """What the words say about the clean-up: `mm` the gap the user lets us stitch (None: nothing said), `off` when
    they said not to stitch at all, `holes_mm` the size under which holes are to be filled (None: nothing said),
    and the quotes each was read from."""
    mm: float | None = None
    off: bool = False
    quote: str = ""
    holes_mm: float | None = None
    holes_quote: str = ""

    @property
    def given(self) -> bool:
        return self.mm is not None or self.off or self.holes_mm is not None


def _mm(num: str, unit: str | None) -> float | None:
    try:
        value = float(num.replace(",", "."))
    except ValueError:
        return None
    mm = value * _UNITS.get((unit or "mm").lower(), 1.0)
    return float(f"{mm:.6g}") if MIN_MM <= mm <= MAX_MM else None


def _first(patterns: tuple[re.Pattern[str], ...], said: str) -> tuple[float, str, int] | None:
    best: tuple[float, str, int] | None = None
    for pattern in patterns:
        for m in pattern.finditer(said):
            mm = _mm(m.group("num"), m.groupdict().get("unit"))
            if mm is not None and (best is None or m.start() < best[2]):
                best = (mm, " ".join(m.group(0).split()), m.start())
    return best


def read_text(text: Any) -> StitchReading:
    """What one piece of text says: the first stitch tolerance named, "do not stitch", and the first hole size."""
    said = str(text or "")
    hole = _first(_HOLE, said)
    # a hole's size is no stitch tolerance: its words are blanked before the stitch table is tried
    rest = said
    if hole is not None:
        for pattern in _HOLE:
            rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
    stitch = _first(_STITCH, rest)
    no = _NO_STITCH.search(rest)
    out = {"holes_mm": hole[0], "holes_quote": hole[1]} if hole is not None else {}
    if stitch is not None and (no is None or stitch[2] > no.start()):
        return StitchReading(mm=stitch[0], quote=stitch[1], **out)
    if no is not None:
        return StitchReading(off=True, quote=" ".join(no.group(0).split()), **out)
    return StitchReading(**out)


def read(texts: Iterable[Any]) -> StitchReading:
    """THE LATEST WORD STANDS, for the stitching and for the holes each on its own: `texts` oldest first."""
    mm, off, quote, holes, hq = None, False, "", None, ""
    got_stitch = got_holes = False
    for text in reversed([t for t in texts if str(t or "").strip()]):
        r = read_text(text)
        if not got_stitch and (r.mm is not None or r.off):
            mm, off, quote, got_stitch = r.mm, r.off, r.quote, True
        if not got_holes and r.holes_mm is not None:
            holes, hq, got_holes = r.holes_mm, r.holes_quote, True
        if got_stitch and got_holes:
            break
    return StitchReading(mm=mm, off=off, quote=quote, holes_mm=holes, holes_quote=hq)


__all__ = ["MAX_MM", "MIN_MM", "StitchReading", "read", "read_text"]
