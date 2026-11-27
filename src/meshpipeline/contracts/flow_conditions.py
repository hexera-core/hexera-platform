# Responsibility: THE CFD FLOW CONDITIONS THE USER CONFIRMED (docs/RULES.md CFDREQ-8): the one sentence the plan carries for the fluid, its speed and the wall treatment, written and read back here, and what it asks of the first boundary layer - so what the card says and what the mesh uses are one thing.
# Boundaries: pure over plain strings and dicts; no model, no I/O. It never invents a number: a speed it cannot turn into metres per second, or a fluid with no viscosity here, is said, and the first layer keeps the mesher's own sizing.
# Collaborates with: agents/intake/flow.py (asks the question, writes the sentence into request_txt), engines/cfd_requirements.py (reads it back at the run and applies it through the same y+ seam a typed y+ uses).
"""THE FLOW CONDITIONS, WRITTEN ONCE AND READ BACK.

The intake's conditions step asks what flows, how fast, and how the wall is treated. The plan's summary
(request_txt) carries the answer as one fixed sentence:

    Fluid: <fluid> at <speed>[ at the inlet]. Near the walls: <wall>; <what the first layer does>.

`sentence` writes it; `read` reads it back from the summary the run carries. The wall treatment sets the
y+ the first layer is sized for (WALL_Y_PLUS): a resolved wall y+ 1, wall functions y+ 50 (inside their
30-300 band, with room below for slower regions). The fluid gives the viscosity (NU) and the speed is
turned into metres per second (SPEEDS; a Mach number for air; a volume or mass flow through the one inlet
whose size is known). What cannot be turned into a number is named, never guessed.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

RULE = "CFDREQ-8"

#: the two wall treatments' words, exactly as the question and the plan say them
WALL_FUNCTIONS = "wall functions (y⁺ 30–300)"
WALL_RESOLVED = "a resolved wall (y⁺ ≈ 1)"
WALL_WORDS = {"functions": WALL_FUNCTIONS, "resolved": WALL_RESOLVED}
#: the y+ each treatment sizes the first layer for
WALL_Y_PLUS = {"resolved": 1.0, "functions": 50.0}
#: kinematic viscosity at 20 °C, m^2/s, and density, kg/m^3, of the fluids we know
NU = {"air": 1.516e-5, "water": 1.004e-6}
RHO = {"air": 1.204, "water": 998.2}
#: the speed of sound in air at 20 °C, m/s (a Mach number)
SOUND_AIR = 343.2

FLUIDS = re.compile(r"\b(air|water|sea ?water|oil|nitrogen|helium|hydrogen|steam|blood|fuel|gasoline|petrol|"
                    r"diesel|kerosene|glycol|co2|exhaust gas|natural gas|methane|argon|refrigerant)\b", re.I)
SPEED = re.compile(r"(?:\bmach\s*\d+(?:\.\d+)?)|(?:\d+(?:[.,]\d+)?\s*(?:m/s|km/h|kph|mph|knots?|kts?|l/min|lpm|"
                   r"l/s|m3/s|m³/s|m3/h|m³/h|kg/s|kg/h|g/s|cfm|gpm)\b)", re.I)
#: metres per second per speed unit
SPEEDS = {"m/s": 1.0, "km/h": 1 / 3.6, "kph": 1 / 3.6, "mph": 0.44704, "knot": 0.514444, "knots": 0.514444,
          "kt": 0.514444, "kts": 0.514444}
#: cubic metres per second per volume-flow unit, and kilograms per second per mass-flow unit
FLOWS = {"l/min": 1 / 60000.0, "lpm": 1 / 60000.0, "l/s": 1e-3, "m3/s": 1.0, "m³/s": 1.0, "m3/h": 1 / 3600.0,
         "m³/h": 1 / 3600.0, "cfm": 0.000471947, "gpm": 6.30902e-5}
MASS_FLOWS = {"kg/s": 1.0, "kg/h": 1 / 3600.0, "g/s": 1e-3}

_SENTENCE = re.compile(r"Fluid: (?P<fluid>.+?) at (?P<speed>" + SPEED.pattern + r")(?: at the inlet)?\. "
                       r"Near the walls: (?P<wall>" + re.escape(WALL_FUNCTIONS) + "|" + re.escape(WALL_RESOLVED)
                       + r")[;.]", re.I)


def fluid_key(fluid_words: str) -> str:
    """"air" or "water" when the words name one we have the viscosity of, else the fluid's own name ("oil")."""
    m = FLUIDS.search(str(fluid_words or ""))
    return m.group(1).lower() if m else str(fluid_words or "").strip().lower()


def _inlet_area_m2(patches: Iterable[Mapping] | None) -> tuple[float | None, str]:
    """(the one inlet's area in m^2, why not): its stated area, else its diameter, else width x height."""
    inlets = [p for p in patches or () if isinstance(p, Mapping) and p.get("type") == "inlet"]
    if len(inlets) != 1:
        return None, ("there is more than one inlet to share it between" if inlets else "no inlet size is known")
    p = inlets[0]
    try:
        if p.get("area_mm2"):
            return float(p["area_mm2"]) * 1e-6, ""
        if p.get("diameter_mm"):
            d = float(p["diameter_mm"]) * 1e-3
            inner = float(p.get("inner_diameter_mm") or 0.0) * 1e-3
            return math.pi * (d * d - inner * inner) / 4.0, ""
        if p.get("width_mm") and p.get("height_mm"):
            return float(p["width_mm"]) * float(p["height_mm"]) * 1e-6, ""
    except (TypeError, ValueError):
        pass
    return None, "the inlet's size is not known"


def speed_ms(speed_words: str, fluid: str, patches: Iterable[Mapping] | None = None) -> tuple[float | None, str]:
    """(the speed in m/s, why not) for the words the user gave: a speed, a Mach number (air), or a volume or mass
    flow through the one inlet whose size is known."""
    t = " ".join(str(speed_words or "").split()).lower()
    m = re.match(r"mach\s*(\d+(?:\.\d+)?)", t)
    if m:
        if fluid != "air":
            return None, "a Mach number needs the speed of sound, which we only have for air"
        return float(m.group(1)) * SOUND_AIR, ""
    m = re.match(r"(\d+(?:[.,]\d+)?)\s*(\S+)", t)
    if not m:
        return None, f"{speed_words} is not a speed we can read"
    value, unit = float(m.group(1).replace(",", ".")), m.group(2)
    if unit in SPEEDS:
        return value * SPEEDS[unit], ""
    if unit in FLOWS or unit in MASS_FLOWS:
        area, why = _inlet_area_m2(patches)
        if area is None or area <= 0:
            return None, f"a flow rate needs the inlet's size to give a speed, and {why}"
        if unit in FLOWS:
            return value * FLOWS[unit] / area, ""
        if fluid not in RHO:
            return None, f"a mass flow needs the fluid's density, which we do not have for {fluid}"
        return value * MASS_FLOWS[unit] / RHO[fluid] / area, ""
    return None, f"{speed_words} is not a speed we can read"


def conditions(fluid_words: str, speed_words: str, wall: str,
               patches: Iterable[Mapping] | None = None) -> dict:
    """What the conditions ask of the first layer: {fluid, fluid_words, speed_words, velocity_ms, nu_m2s, wall,
    y_plus, why} - `why` names what is missing when the first layer cannot be sized from them ("" when it can)."""
    fluid = fluid_key(fluid_words)
    v, why_v = speed_ms(speed_words, fluid, patches)
    nu = NU.get(fluid)
    why = why_v or ("" if nu is not None else f"we have no viscosity for {fluid}")
    wall = wall if wall in WALL_Y_PLUS else "functions"
    return {"fluid": fluid, "fluid_words": str(fluid_words), "speed_words": str(speed_words),
            "velocity_ms": float(f"{v:.6g}") if v is not None else None, "nu_m2s": nu, "wall": wall,
            "y_plus": WALL_Y_PLUS[wall], "why": why}


def sizing_words(c: Mapping) -> str:
    """What the first layer does, in the plan's words."""
    if c.get("why"):
        return f"the first layer keeps the mesher's own sizing, because {c['why']}"
    if c.get("wall") == "resolved":
        return "the first layer is sized for y⁺ ≈ 1 at that speed"
    return f"the first layer is sized for y⁺ ≈ {c['y_plus']:g}, inside 30–300, at that speed"


#: what the plan says of a wall treatment the user only accepted (the proposal's wall functions): CFDREQ-13 sizes the
#: first layer for them as for a chosen one, fitted to the wall cells (engines/cfd_requirements.wall_fit)
PROPOSED_WORDS = ("the first layer is sized for wall functions (its cell centre at y⁺ about 50) at that speed; where "
                  "the cells a passage needs at the wall are too fine for that, the wall is resolved instead (y⁺ about "
                  "1), and the result says which")
#: how the plan says what the user left to the proposal (agents/intake/flow), read back by `accepted`
ACCEPTED_LEAD = "Left to the application's proposal, which the user accepted:"
_ACCEPTED = re.compile(re.escape(ACCEPTED_LEAD) + r"([^.]*)\.")


def sentence(fluid_words: str, speed_words: str, wall: str, *, internal: bool,
             patches: Iterable[Mapping] | None = None, wall_said: bool = True) -> str:
    """THE ONE SENTENCE the plan carries (read back by `read`). `wall_said` False: the user only accepted the proposed
    wall treatment, and the plan says the layers keep the mesher's own sizing."""
    c = conditions(fluid_words, speed_words, wall, patches)
    how = PROPOSED_WORDS if (not wall_said and c["wall"] == "functions" and not c["why"]) else sizing_words(c)
    return (f"Fluid: {fluid_words} at {speed_words}{' at the inlet' if internal else ''}. Near the walls: "
            f"{WALL_WORDS[c['wall']]}; {how}.")


def accepted(request_txt: str) -> set[str]:
    """What the plan says the user left to the proposal ("the fluid", "its speed", "the wall treatment")."""
    m = _ACCEPTED.search(str(request_txt or ""))
    return {w.strip() for w in m.group(1).split(",") if w.strip()} if m else set()


#: the rest of the conditions sentence after its wall words: what the first layer does, to the sentence's full stop
#: (a decimal point is no full stop)
_SENTENCE_REST = re.compile(r"(?:[^.]|\.(?=\S))*\.?")


def without(request_txt: str) -> str:
    """The summary with its conditions sentence and its what-was-left-to-the-proposal clause taken out (a summary
    that carries none: as it is). Read back by `read` as None."""
    text = str(request_txt or "")
    m = _SENTENCE.search(text)
    if m:
        end = m.end()
        rest = _SENTENCE_REST.match(text, end) if text[end - 1] == ";" else None
        if rest is not None:
            end = rest.end()
        text = text[:m.start()] + text[end:]
    text = _ACCEPTED.sub("", text)
    return " ".join(text.split())


def read(request_txt: str, patches: Iterable[Mapping] | None = None) -> dict | None:
    """The conditions the plan carries (`conditions`, with `wall_said`: whether the user chose the wall treatment
    rather than accepting the proposal), or None when the summary has no such sentence (a plan the model wrote in its
    own words, an FEA plan): nothing is then asked of the first layer."""
    m = _SENTENCE.search(str(request_txt or ""))
    if not m:
        return None
    wall = "resolved" if m.group("wall") == WALL_RESOLVED else "functions"
    out = conditions(m.group("fluid").strip(), " ".join(m.group("speed").split()), wall, patches)
    out["wall_said"] = "the wall treatment" not in accepted(request_txt)
    return out


__all__ = ["ACCEPTED_LEAD", "FLOWS", "FLUIDS", "MASS_FLOWS", "NU", "PROPOSED_WORDS", "RHO", "RULE", "SPEED", "SPEEDS",
           "WALL_FUNCTIONS", "WALL_RESOLVED", "WALL_WORDS", "WALL_Y_PLUS", "accepted", "conditions", "fluid_key", "read",
           "sentence", "sizing_words", "speed_ms", "without"]
