# Responsibility: Say which unit makes a measured part a believable size, and when the unit in
# effect does not - a "117 mm" wind turbine blade, a "3 km" pipe fitting, a triangle file whose
# numbers read as a 1 mm car.
# Owns: the believable-size bounds, the table of part kinds and their sizes, and the suggestion
# the stage and the chat show: the part's length in the unit in effect and in the one proposed.
# Boundaries: pure. It measures nothing and records nothing. A suggestion is never applied on its
# own: the user sees both lengths in plain words and picks, and what they pick is recorded.
# Collaborates with: contracts/geometry_units.py (the unit vocabulary), api/v1/geometry.py (the
# stage), agents/intake/unit_clarification.py (the chat's unit question).
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from meshpipeline.contracts.geometry_units import LengthUnit, scale_to_metres

#: The units a suggestion chooses between, in the order it prefers them when more than one would
#: make the part believable: the metre and the millimetre first, because a slip between the two
#: is by far the commonest (a CAD export in metres read as millimetres, or the other way round).
CANDIDATES = (LengthUnit.metre, LengthUnit.millimetre, LengthUnit.inch, LengthUnit.centimetre)
UNIT_WORDS = {LengthUnit.millimetre: "millimetres", LengthUnit.centimetre: "centimetres",
              LengthUnit.metre: "metres", LengthUnit.inch: "inches"}

#: Longest side, in metres, that a unit a file DECLARES is believed at on size alone. Outside it
#: - a part under a millimetre, or over two kilometres - the other reading is proposed.
DECLARED_BELIEVABLE_M = (0.001, 2000.0)
#: A file that declares nothing (STL, OBJ, VTP; a STEP with no unit) is read as millimetres unless
#: that makes the part shorter than this; a 1.04-unit Ahmed body is a 1.04 m car, not a 1 mm one.
UNDECLARED_FLOOR_M = 0.01
#: What a proposed unit must make the part, when size alone is the reason: a believable object.
COMFORTABLE_M = (0.005, 500.0)


def length_words(metres: float) -> str:
    """A length a person can picture: 2.66 cm, 1.05 m, 117 m, 1.05 km."""
    if metres >= 1000.0:
        value, unit = metres / 1000.0, "km"
    elif metres >= 1.0:
        value, unit = metres, "m"
    elif metres >= 0.01:
        value, unit = metres * 100.0, "cm"
    else:
        value, unit = metres * 1000.0, "mm"
    return (f"{value:,.0f}" if value >= 100 else f"{value:.3g}") + f" {unit}"


def length_in(metres: float, unit: LengthUnit) -> str:
    """A length as it reads under one unit: `length_words`, except that a millimetre reading
    under a metre stays in millimetres - "117 mm", the way the file's own number reads - so the
    two readings of one file sit side by side as the same number in two units."""
    if unit is LengthUnit.millimetre and 0.001 <= metres < 1.0:
        mm = metres * 1000.0
        return (f"{mm:,.0f}" if mm >= 100 else f"{mm:.3g}") + " mm"
    return length_words(metres)


@dataclass(frozen=True)
class ExpectedSize:
    """How long a real one of what the user says the part is tends to be, in metres."""

    what: str
    lo_m: float
    hi_m: float
    #: a single best estimate (the naming model's), when there is one
    typical_m: float | None = None

    def holds(self, metres: float) -> bool:
        return self.lo_m <= metres <= self.hi_m


#: (pattern, what it is, shortest, longest) in metres - wide on purpose: a wind-tunnel model and
#: the full-size article both belong in a row, so a row only speaks when a unit is out by a
#: factor no model scale explains.
_KINDS: tuple[tuple[str, str, float, float], ...] = (
    (r"wind[\s-]*turbine|\biea[\s-]*\d|\bnrel\b|\b\d+(?:\.\d+)?\s*mw\b", "a wind turbine blade", 0.3, 150.0),
    (r"air(?:craft|liner|plane)|aeroplane|\bjet\b|fuselage|nacelle|glider|\bdrone\b|\buav\b", "an aircraft", 0.1, 100.0),
    # a file that carries its wind tunnel (the ANSA DrivAer export is a 172 m box around a 4.6 m car)
    (r"wind[\s-]*tunnel", "a wind tunnel", 1.0, 500.0),
    (r"\bwings?\b|airfoil|aerofoil", "a wing", 0.05, 100.0),
    (r"\bcars?\b|vehicle|automobile|\btrucks?\b|\blorry\b|\bbus\b|\bahmed\b|drivaer|windsor|notchback|"
     r"fastback|squareback", "a car", 0.1, 25.0),
    (r"buildings?|skyscraper|\bcity\b|urban|\bstreets?\b|\btown\b|stadium|\bbridge\b", "a building", 2.0, 5000.0),
    (r"\bships?\b|\bhull\b|\bboat\b|\byacht\b|submarine|\btanker\b|\bferry\b", "a ship", 0.3, 500.0),
    (r"motor ?bike|motorcycle|bicycle|\bcyclist\b", "a bike", 0.2, 4.0),
    (r"\bpipes?\b|\belbow|\btee\b|\bwye\b|reducer|fitting|\bvalves?\b|\bducts?\b|manifold|nozzle|venturi|"
     r"orifice|diffuser|plenum", "a pipe or duct part", 0.002, 30.0),
    (r"\bpumps?\b|impeller|volute|compressor|\bfan\b|blower", "a pump or fan part", 0.005, 10.0),
    (r"propell?er", "a propeller", 0.01, 10.0),
    (r"heat ?sink|\bpcb\b|circuit board|connector", "an electronics part", 0.001, 1.0),
    (r"artery|aorta|aneurysm|\bstent|carotid|coronary|airway|trachea|bronch", "a blood vessel or airway", 0.001, 0.5),
    (r"cyclone", "a cyclone separator", 0.05, 20.0),
    (r"rocket|missile", "a rocket", 0.02, 120.0),
)
_COMPILED = tuple((re.compile(p, re.IGNORECASE), what, lo, hi) for p, what, lo, hi in _KINDS)


def expected_from_words(text: str) -> ExpectedSize | None:
    """What the user's words (or the naming's name for the part) say about its real size: the
    span of every kind they mention, so two mentions never make it narrower than either."""
    hits = [(what, lo, hi) for rx, what, lo, hi in _COMPILED if re.search(rx, text or "")]
    if not hits:
        return None
    return ExpectedSize(what=hits[0][0], lo_m=min(h[1] for h in hits), hi_m=max(h[2] for h in hits))


def expected_from_estimate(what: str, typical_m) -> ExpectedSize | None:
    """The naming model's estimate of how long a real one is, as a span a factor of ten each way."""
    try:
        x = float(typical_m)
    except (TypeError, ValueError):
        return None
    if not (x > 0 and math.isfinite(x)):
        return None
    name = " ".join(str(what or "").split())[:60]
    if not name:
        name = "this part"
    elif not name.lower().startswith(("a ", "an ", "the ", "this ")):
        name = ("an " if name[0].lower() in "aeiou" else "a ") + name
    return ExpectedSize(what=name, lo_m=x / 10.0, hi_m=x * 10.0, typical_m=x)


@dataclass(frozen=True)
class UnitSuggestion:
    """The other reading, proposed: the unit, the one it would replace, and both lengths."""

    unit: LengthUnit
    instead_of: LengthUnit
    longest_m: float                  # the part's length under the proposed unit
    longest_in_effect_m: float        # ... and under the unit in effect
    why: str

    @property
    def words(self) -> str:
        """What the stage prints beside the unit box."""
        return (f"{length_in(self.longest_in_effect_m, self.instead_of)} long - or "
                f"{length_in(self.longest_m, self.unit)} if the file is in {UNIT_WORDS[self.unit]}")

    def as_dict(self) -> dict:
        return {"unit": self.unit.value, "instead_of": self.instead_of.value,
                "longest_m": self.longest_m, "longest_in_effect_m": self.longest_in_effect_m,
                "sizes": {self.instead_of.value: length_in(self.longest_in_effect_m, self.instead_of),
                          self.unit.value: length_in(self.longest_m, self.unit)},
                "why": self.why, "words": self.words}


def _closest(fits, lengths: dict, target_m: float) -> LengthUnit:
    return min(fits, key=lambda u: abs(math.log10(lengths[u] / target_m)))


def suggest(longest_file_units: float, in_effect: LengthUnit, *, declared: bool,
            expected: ExpectedSize | None = None) -> UnitSuggestion | None:
    """The unit to propose instead of `in_effect`, or None when the part is believable as it is.

    `longest_file_units` is the part's longest side in the file's own numbers. `declared` says
    the unit in effect is the file's own declaration (believed unless the size is wild); without
    one it is only the default reading. `expected` is what the part is known to be - from the
    user's words or the naming model - and speaks first: a wind turbine blade 117 mm long is
    wrong however sure the file header is."""
    try:
        longest = float(longest_file_units)
    except (TypeError, ValueError):
        return None
    if not (longest > 0 and math.isfinite(longest)):
        return None
    lengths = {u: longest * scale_to_metres(u) for u in CANDIDATES}
    here = lengths[in_effect]

    if expected is not None and not expected.holds(here):
        fits = [u for u in CANDIDATES if u is not in_effect and expected.holds(lengths[u])]
        if fits:
            best = _closest(fits, lengths, expected.typical_m) if expected.typical_m else fits[0]
            size = "small" if here < expected.lo_m else "large"
            return UnitSuggestion(best, in_effect, lengths[best], here,
                                  f"{length_in(here, in_effect)} is {size} for {expected.what}")
    elif expected is not None:
        return None                     # what the part is says the unit in effect is right

    lo, hi = DECLARED_BELIEVABLE_M if declared else (UNDECLARED_FLOOR_M, DECLARED_BELIEVABLE_M[1])
    if lo <= here <= hi:
        return None
    fits = [u for u in CANDIDATES if u is not in_effect
            and COMFORTABLE_M[0] <= lengths[u] <= COMFORTABLE_M[1]]
    if not fits:
        return None
    best = _closest(fits, lengths, 1.0)
    size = "small" if here < lo else "large"
    return UnitSuggestion(best, in_effect, lengths[best], here,
                          f"{length_in(here, in_effect)} is very {size} for a part")


def proposed_unit(longest_file_units: float, expected: ExpectedSize | None = None) -> LengthUnit:
    """The unit to propose for a file that declares none: millimetres - what nearly every CAD
    file is drawn in - unless the part would then be implausible and another unit is not."""
    s = suggest(longest_file_units, LengthUnit.millimetre, declared=False, expected=expected)
    return s.unit if s is not None else LengthUnit.millimetre


__all__ = ["CANDIDATES", "COMFORTABLE_M", "DECLARED_BELIEVABLE_M", "UNDECLARED_FLOOR_M", "UNIT_WORDS",
           "ExpectedSize", "UnitSuggestion", "expected_from_estimate", "expected_from_words",
           "length_in", "length_words", "proposed_unit", "suggest"]
