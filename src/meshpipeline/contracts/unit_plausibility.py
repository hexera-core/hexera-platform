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


# ------------------------------------------------------------------ what the shape says ----
# A file with no unit and no name still has a SHAPE and a FLOW. A closed body in an external
# flow, sitting on the ground and two to five times as long as it is wide, is a car or something
# like one; a thin flat body out in the free stream is a wing or an aircraft; a passage the
# fluid flows through is a pipe, a duct or a vessel. Each has a believable size, and the unit is
# the reading that makes the part that size. The DrivAer car (4.7 units long) and the BlendedNet
# blended wing (1.0 unit) were meshed as a 4.7 mm car and a 1 mm aircraft because nothing asked
# what a part of that shape is usually like.

#: A body in an external flow that the shape reads as nothing more particular.
EXTERNAL_BODY = ExpectedSize("a body in an external flow", 0.005, 5000.0)
#: A passage the fluid flows through: a microfluidic chip (a few mm) up to plant piping.
INTERNAL_PASSAGE = ExpectedSize("a flow passage (pipe, duct or vessel)", 0.01, 100.0)
#: A closed body on the ground, longer than it is wide and not taller than wide: a car, a van, a
#: truck, an Ahmed or Windsor body - from a 1:10 model to an articulated lorry.
GROUND_VEHICLE = ExpectedSize("a car-like body on the ground", 0.1, 30.0, typical_m=4.5)
#: A thin, flat body out in the free stream: a wing, a blended wing, a small aircraft.
FLAT_LIFTING = ExpectedSize("a thin flat body like a wing or aircraft", 0.03, 100.0)

#: How the part's proportions are read: car-like is 1.5-6 times as long as wide, at most 1.6
#: times as tall as wide; flat is no thicker than 0.35 of its length, with a middle side at least
#: 0.2 of it (so a slender rod is not "flat").
CAR_LENGTH_TO_WIDTH = (1.5, 6.0)
CAR_HEIGHT_TO_WIDTH_MAX = 1.6
FLAT_THICKNESS_MAX = 0.35
FLAT_MIDDLE_MIN = 0.2


def _up_index(up_axis: str | None) -> int:
    a = str(up_axis or "+z").strip().lower()
    return "xyz".index(a[-1]) if a and a[-1] in "xyz" else 2


def shape_kind(flow: str, size, *, grounded: bool = False, up_axis: str | None = None) -> ExpectedSize | None:
    """What the measured shape and the flow say the part is, with no name for it: a car-like body
    on the ground, a thin flat body in the free stream, a flow passage, or just a body in an
    external flow. `size` is the part's three sides in any one unit (only their ratios are read).
    None when the flow is not known."""
    try:
        sides = [abs(float(v)) for v in size]
    except (TypeError, ValueError):
        return None
    if len(sides) != 3 or max(sides) <= 0 or not all(math.isfinite(v) for v in sides):
        return None
    flow = str(flow or "").lower()
    if flow == "internal":
        return INTERNAL_PASSAGE
    if flow != "external":
        return None
    up = _up_index(up_axis)
    height = sides[up]
    flat = sorted((sides[i] for i in range(3) if i != up), reverse=True)
    length, width = flat[0], flat[1]
    if grounded and width > 0 and length >= height:
        ratio = length / width
        if CAR_LENGTH_TO_WIDTH[0] <= ratio <= CAR_LENGTH_TO_WIDTH[1] and height <= CAR_HEIGHT_TO_WIDTH_MAX * width:
            return GROUND_VEHICLE
    a, b, c = sorted(sides, reverse=True)
    if not grounded and c <= FLAT_THICKNESS_MAX * a and b >= FLAT_MIDDLE_MIN * a:
        return FLAT_LIFTING
    return EXTERNAL_BODY


#: The unit a file format states by its own specification, when the format has one. glTF 2.0:
#: "the units for all linear distances are meters" - a strong first reading, still only a reading
#: (an artist's model is often drawn at any size), so the shape can overrule it.
FORMAT_UNIT = {"glb": LengthUnit.metre, "gltf": LengthUnit.metre}

CONFIDENCE = ("low", "medium", "high")
_PREFERENCE = (LengthUnit.metre, LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.inch)
_PASSAGE_PREFERENCE = (LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.metre, LengthUnit.inch)


@dataclass(frozen=True)
class UnitInference:
    """The unit a file that states none most likely is in, how sure that is, and the other reading
    worth naming beside it - what the stage and the unit question propose. Never applied on its
    own: the user confirms it, or names another."""

    unit: LengthUnit
    confidence: str                     # "high" | "medium" | "low"
    longest_m: float                    # the part's longest side under `unit`
    what: str                           # what the part reads as: "a car-like body on the ground"
    why: str
    alternative: LengthUnit | None = None
    alternative_longest_m: float | None = None

    @property
    def words(self) -> str:
        """One plain sentence: "This reads as a car-like body on the ground, 4.7 m long - metres?"."""
        said = f"This reads as {self.what}, {length_words(self.longest_m)} long - {UNIT_WORDS[self.unit]}?"
        if self.alternative is not None and self.alternative_longest_m is not None:
            said += (f" (In {UNIT_WORDS[self.alternative]} it would be "
                     f"{length_in(self.alternative_longest_m, self.alternative)} long.)")
        return said

    def as_dict(self) -> dict:
        return {"unit": self.unit.value, "confidence": self.confidence, "longest_m": self.longest_m,
                "what": self.what, "why": self.why,
                "alternative": self.alternative.value if self.alternative else None,
                "alternative_longest_m": self.alternative_longest_m, "words": self.words}


def _log_gap(metres: float, target: float) -> float:
    return abs(math.log10(metres / target))


def infer_unit(longest_file_units: float, *, expected: ExpectedSize | None = None,
               shape: ExpectedSize | None = None, format_unit: LengthUnit | None = None) -> UnitInference | None:
    """The unit a file that declares none is most likely in, from what the part is.

    What the part is comes, best first, from `expected` (the user's words or the naming model's
    estimate), else from `shape` (shape_kind: the measured proportions and the flow). The first
    reading is the format's own unit when it has one (glTF), else millimetres - what nearly every
    CAD file is drawn in. It stands while the part is believable in it; otherwise the reading
    that makes the part believable for what it is - nearest its usual size - is proposed instead.

    Confidence: high when one reading alone fits (or one fits a decade closer to the usual size
    than any other); medium when several fit; low when nothing says what the part is, or no
    reading fits. None only for a size that is not a size."""
    try:
        longest = float(longest_file_units)
    except (TypeError, ValueError):
        return None
    if not (longest > 0 and math.isfinite(longest)):
        return None
    kind = expected or shape
    # a body or a passage the shape reads as nothing more particular spans four to six decades:
    # it rules out the absurd, never makes a reading sure
    known = kind is not None and kind not in (EXTERNAL_BODY, INTERNAL_PASSAGE)
    kind = kind or ExpectedSize("a part", COMFORTABLE_M[0], COMFORTABLE_M[1])
    # the format's own unit leads only when the part is recognised: a glTF copy of a CAD part is
    # often still in the CAD's millimetres (a 1,054-unit Windsor body is not a 1 km body)
    first = format_unit if (format_unit is not None and known) else LengthUnit.millimetre
    lengths = {u: longest * scale_to_metres(u) for u in CANDIDATES}
    fits = [u for u in CANDIDATES if kind.holds(lengths[u])]

    def ranked(units):
        if kind.typical_m:
            return sorted(units, key=lambda u: _log_gap(lengths[u], kind.typical_m))
        # no usual size to go by: metres and millimetres first (the commonest slip), then the
        # other metric unit before inches. A PASSAGE too small in millimetres reads in
        # centimetres next: small flow devices (vessels, chips) are drawn in cm - the VMR
        # arteries, 1.6-5.5 units long, all are - far more often than a duct under ten units
        # is drawn in metres
        order = _PASSAGE_PREFERENCE if kind is INTERNAL_PASSAGE else _PREFERENCE
        return sorted(units, key=order.index)

    if not fits:
        # nothing makes it believable as what it reads as: the size alone decides, and says so
        unit = proposed_unit(longest)
        alt = first if unit is not first else None
        return UnitInference(unit, "low", lengths[unit], kind.what,
                             f"no unit makes it a believable size for {kind.what}",
                             alt, lengths[alt] if alt else None)
    if first in fits:
        others = ranked([u for u in fits if u is not first])
        alt = others[0] if others else None
        if not others:
            conf = "high" if known else "medium"
        else:
            conf = "medium" if known else "low"
        why = (f"{length_in(lengths[first], first)} is a believable size for {kind.what}"
               + (f"; so is {length_in(lengths[alt], alt)}" if alt else ""))
        return UnitInference(first, conf, lengths[first], kind.what, why,
                             alt, lengths[alt] if alt else None)
    best = ranked(fits)
    if first is not LengthUnit.millimetre and LengthUnit.millimetre in best:
        # the format's unit does not fit: the CAD convention is the next reading, before any
        # other unit that happens to land nearer the usual size
        best.remove(LengthUnit.millimetre)
        best.insert(0, LengthUnit.millimetre)
    unit = best[0]
    if len(best) == 1:
        conf = "high" if known else "medium"
    elif kind.typical_m and (_log_gap(lengths[best[1]], kind.typical_m)
                             - _log_gap(lengths[unit], kind.typical_m)) >= 1.0:
        conf = "high" if known else "medium"
    else:
        conf = "medium" if known else "low"
    small = lengths[first] < kind.lo_m
    why = (f"{length_in(lengths[first], first)} is too {'small' if small else 'large'} for {kind.what}; "
           f"{length_in(lengths[unit], unit)} is believable")
    # the other reading named beside it: on a sure reading, the first one and why it was set
    # aside ("in millimetres it would be 4.7 mm"); on an unsure one, the next believable reading
    alt = best[1] if (conf == "low" and len(best) > 1) else first
    return UnitInference(unit, conf, lengths[unit], kind.what, why, alt, lengths[alt])


# ------------------------------------------------------------------ the delivered mesh ------
@dataclass(frozen=True)
class ScaleConcern:
    """A delivered mesh whose physical size is not believable for what it is meant to be: a car
    4.7 mm long. Said beside the mesh - the unit is the likely cause, and only the user can say."""

    longest_m: float
    what: str
    too: str                            # "small" | "large"
    other_unit: LengthUnit | None = None
    other_longest_m: float | None = None

    @property
    def words(self) -> str:
        said = (f"This mesh is {length_words(self.longest_m)} long, which is far too {self.too} for "
                f"{self.what}. Check the unit of the file")
        if self.other_unit is not None and self.other_longest_m is not None:
            said += (f": in {UNIT_WORDS[self.other_unit]} it would be "
                     f"{length_words(self.other_longest_m)} long")
        return said + "."

    def as_dict(self) -> dict:
        return {"longest_m": self.longest_m, "what": self.what, "too": self.too,
                "other_unit": self.other_unit.value if self.other_unit else None,
                "other_longest_m": self.other_longest_m, "words": self.words}


#: How far outside the believable span a delivered mesh must be before it is told: a factor of
#: three either way, so a large model or a small one is never doubted for being unusual.
SCALE_MARGIN = 3.0


def scale_concern(longest_m: float, *, kind: ExpectedSize | None,
                  unit_in_effect: LengthUnit | None = None) -> ScaleConcern | None:
    """The concern a delivered mesh carries when it is not a believable size for `kind` (from the
    request's words, else the shape and the flow), or None. `unit_in_effect`, when known, names
    the other reading that would make it believable."""
    try:
        L = float(longest_m)
    except (TypeError, ValueError):
        return None
    if kind is None or not (L > 0 and math.isfinite(L)):
        return None
    if kind.lo_m / SCALE_MARGIN <= L <= kind.hi_m * SCALE_MARGIN:
        return None
    too = "small" if L < kind.lo_m else "large"
    other = other_m = None
    if unit_in_effect is not None:
        file_units = L / scale_to_metres(unit_in_effect)
        fits = [u for u in CANDIDATES if u is not unit_in_effect
                and kind.holds(file_units * scale_to_metres(u))]
        typical = kind.typical_m
        if fits:
            other = (min(fits, key=lambda u: _log_gap(file_units * scale_to_metres(u), typical))
                     if typical else fits[0])
            other_m = file_units * scale_to_metres(other)
    return ScaleConcern(L, kind.what, too, other, other_m)


__all__ = ["CANDIDATES", "COMFORTABLE_M", "CONFIDENCE", "DECLARED_BELIEVABLE_M", "EXTERNAL_BODY",
           "FLAT_LIFTING", "FORMAT_UNIT", "GROUND_VEHICLE", "INTERNAL_PASSAGE", "SCALE_MARGIN",
           "UNDECLARED_FLOOR_M", "UNIT_WORDS", "ExpectedSize", "ScaleConcern", "UnitInference",
           "UnitSuggestion", "expected_from_estimate", "expected_from_words", "infer_unit",
           "length_in", "length_words", "proposed_unit", "scale_concern", "shape_kind", "suggest"]
