# Responsibility: THE ONE AUTHORITY for whether a flow part is INTERNAL (the fluid flows through its
# inside: a pipe, a manifold, a valve, a duct, the fluid body itself) or EXTERNAL (the fluid flows
# around its outside: a car, a wing, a building) - with how sure that is, the measured reasons, and a
# clean "undecided" when nothing settles it, so the intake can ask one fixed question instead of a
# silent guess (rules FLOW-1..5, docs/RULES.md).
# Boundaries: pure - the check's facts dict (cad/scout, cad/scout_mesh, cad/inside_space,
# cad/flow_reading, cad/fluid_volume_check), the user's own words and the picture model's sanitized
# answer in; a plain dict out. No file, no model call, no storage. Never raises.
from __future__ import annotations

import re
from dataclasses import dataclass

#: The one fixed question for an undecided part (FLOW-4). The intake asks it, word for word.
QUESTION = "Is the flow through the inside or around the outside?"
#: The intake owns the buttons; whatever they send is read back here as the user's said words (FLOW-1).

INTERNAL, EXTERNAL, UNDECIDED = "internal", "external", "undecided"
#: FLOW-2: the geometry decides only at this sureness or more; below it the kind is undecided.
DECIDE_AT = 0.6
#: FLOW-3: the pictures may decide an undecided part (or overrule a measured kind) only when the model
#: is surer than the measured reading by this much and nothing measured contradicts it.
PICTURE_MARGIN = 0.15

RULE_WORDS, RULE_GEOMETRY, RULE_PICTURES, RULE_UNDECIDED, RULE_FILE = "FLOW-1", "FLOW-2", "FLOW-3", "FLOW-4", "FLOW-5"
#: FLOW-9: the undecided case whose best guess is flow through the part - its faces read a body, its inside-space map
#: finds a way through it with two mouths or more
RULE_THROUGH_GUESS, THROUGH_CASE = "FLOW-9", "map_disagrees"


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


# ------------------------------------------------------------------------------ the words ----
#: FLOW-1: WORDS THAT STATE WHERE THE FLUID GOES. Said, they win over everything measured.
_SAID_INTERNAL = _rx(
    r"\bflow(?:s|ing)?\s+(?:is\s+|goes\s+)?(?:through|inside|within|into and out)\b",
    r"\b(?:fluid|air|water|gas|oil|blood|coolant|liquid)\s+(?:\w+\s+){0,2}(?:through|inside|within)\s+(?:the|this|it|its)\b",
    r"\bthrough (?:the|its) inside\b", r"\binternal(?:[- ]flow|[- ]cfd|[- ]aero\w*)?\b(?![- ]combustion)",
    r"\binside (?:the|this) (?:part|pipe|duct|passage|channel|tube|body|manifold|valve|housing|tank|vessel)\b",
    r"\b(?:the|this|it is the|it's the|is the)\s+(?:fluid|air|water|gas)\s+(?:volume|domain|region|body)\b",
    r"\bfluid (?:volume|domain) (?:itself|of the)\b", r"\bpressure (?:drop|loss)\b", r"\bhead loss\b",
    r"\bgoes in\b.*\bout\b")
_SAID_EXTERNAL = _rx(
    r"\bflow(?:s|ing)?\s+(?:is\s+|goes\s+)?(?:around|round|over|past|outside)\b",
    r"\b(?:fluid|air|water|wind)\s+(?:\w+\s+){0,2}(?:around|round|over|past)\s+(?:the|this|it|its)\b",
    r"\baround (?:the|this|it|its)\b",r"\bexternal(?:[- ]flow|[- ]cfd|[- ]aero\w*)?\b",
    r"\baero(?:dynamic)?s?\b", r"\bdrag\b", r"\blift\b(?! station)", r"\bdownforce\b", r"\bwind[- ]tunnel\b",
    r"\bfar[- ]?field\b", r"\bfree[- ]?stream\b", r"\bwake\b")
#: Words that take a statement back ("not internal", "not through it"): the words then say nothing.
_TAKEN_BACK = _rx(r"\b(?:not|n't|never|no)\s+(?:an?\s+)?(?:\w+\s+)?(?:internal|external|through|around|inside|outside)\b")
#: THE OBJECT THE WORDS NAME, when they state no flow: what the part is says where the fluid goes.
_NAMED_INTERNAL = _rx(
    r"\bpipes?\b", r"\bpiping\b", r"\bducts?\b", r"\bducting\b", r"\bmanifolds?\b", r"\bvalves?\b", r"\bnozzles?\b",
    r"\bventuri\b", r"\bdiffusers?\b", r"\belbows?\b", r"\btee\b", r"\breducers?\b", r"\bvolutes?\b",
    r"\bplenums?\b", r"\barter(?:y|ies)\b", r"\baort\w*\b", r"\bveins?\b", r"\bairways?\b", r"\bfilter\b",
    r"\bcombustors?\b", r"\bmixing\b", r"\bheader\b", r"\bchannels?\b", r"\bpassages?\b", r"\bin the (?:room|tank|combustor)\b")
_NAMED_EXTERNAL = _rx(
    r"\bcars?\b", r"\bvehicles?\b", r"\btrucks?\b", r"\bbus\b", r"\btrains?\b", r"\bwings?\b", r"\bairfoils?\b",
    r"\baerofoils?\b", r"\baircraft\b", r"\bairplanes?\b", r"\baeroplanes?\b", r"\bplanes?\b", r"\bdrones?\b",
    r"\buavs?\b", r"\bfuselage\b", r"\brockets?\b", r"\bmissiles?\b", r"\bhull\b", r"\bships?\b", r"\bboats?\b",
    r"\bbuildings?\b", r"\bcity\b", r"\bbridges?\b", r"\bcyclists?\b", r"\bbikes?\b", r"\bhelmets?\b",
    r"\bturbine blades?\b", r"\bwind turbines?\b", r"\bpropellers?\b", r"\bbluff bod(?:y|ies)\b", r"\bspoilers?\b")


@dataclass(frozen=True)
class WordsReading:
    """What the user's own words say about the flow. `kind` is internal / external / "" ; `tier` is
    "said" (a flow stated outright: FLOW-1, it wins) or "named" (only the object named: a strong hint);
    `words` the cue words read, for the reason line."""

    kind: str = ""
    tier: str = ""
    words: tuple[str, ...] = ()


def _hits(rx: re.Pattern, text: str) -> list[str]:
    return [m.group(0).strip().lower() for m in rx.finditer(text)]


def words_reading(text: str) -> WordsReading:
    """FLOW-1: the flow the words state, else the flow the object they name implies, else nothing.
    Words that state both, or take a statement back, settle nothing at that tier."""
    said = " ".join(str(text or "").split())
    if not said:
        return WordsReading()
    if not _TAKEN_BACK.search(said):
        inside, outside = _hits(_SAID_INTERNAL, said), _hits(_SAID_EXTERNAL, said)
        if inside and not outside:
            return WordsReading(INTERNAL, "said", tuple(inside))
        if outside and not inside:
            return WordsReading(EXTERNAL, "said", tuple(outside))
        if inside and outside:
            return WordsReading("", "both", tuple(inside + outside))
    inside, outside = _hits(_NAMED_INTERNAL, said), _hits(_NAMED_EXTERNAL, said)
    if inside and not outside:
        return WordsReading(INTERNAL, "named", tuple(inside))
    if outside and not inside:
        return WordsReading(EXTERNAL, "named", tuple(outside))
    return WordsReading()


def topology_of_words(text: str) -> str:
    """The flow the words give (said, else named): "internal", "external" or "" - what the purpose
    routing reads, so the route and the stage read the words one way."""
    return words_reading(text).kind


# ---------------------------------------------------------------------------- the geometry ----
@dataclass(frozen=True)
class Reading:
    """One reading of the flow kind: the kind it points to, how sure, and why in plain words."""

    kind: str
    confidence: float
    reason: str
    #: the measured situation it is (for the score table and the tests), never shown to the user
    case: str = ""


def _ports(facts: dict) -> list[dict]:
    return [o for o in facts.get("openings") or [] if isinstance(o, dict) and o.get("role") in ("inlet", "outlet")]


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


#: FLOW-2's measured thresholds (general, never tuned to a file):
#: A SHEET encloses under this share of its box - no material and no closed fluid: a skin or a patch.
SHEET_SHARE = 0.005
#: Two mouths face opposite ways on one line when their normals' dot is under -STRAIGHT and their
#: centres lie off each other's axis by under OFF_LINE of the wider mouth.
STRAIGHT, OFF_LINE = 0.95, 0.25
#: How sure each measured situation is: its share right on the labelled development corpus (see
#: docs/RULES.md FLOW-2), rounded down; under DECIDE_AT the kind is undecided.
SURE = {"cavity": 0.85, "passage_wall": 0.9, "fluid_body": 0.8, "straight_passage": 0.7, "body": 0.8,
        "body_with_ends": 0.7, "solids_with_ends": 0.65, "rotor": 0.75,
        "scene": 0.7, "closed_surfaces": 0.65, "lumen": 0.65, "perforated": 0.7}
#: A straight solid's widest flat end SPANS its cross-section when it is this share of the cross-section's
#: wider side or more: a passage's end is its whole section; a body on a support bulges far past it.
SPANS = 0.8


def _ends_span(ports: list[dict], facts: dict) -> float:
    """How much of a straight solid's cross-section its widest end covers: the widest end's size over
    the wider of the two box sides across the axis its ends face along."""
    size = [_num(v) for v in (facts.get("size_mm") or [0.0, 0.0, 0.0])]
    normal = [abs(_num(v)) for v in ports[0].get("normal") or [0, 0, 0]]
    axis = max(range(3), key=lambda k: normal[k])
    across = max(size[k] for k in range(3) if k != axis)

    def width(o: dict) -> float:
        d = _num(o.get("diameter_mm"))
        return d if d > 0 else max(_num(o.get("width_mm")), _num(o.get("height_mm")))
    return max(width(o) for o in ports) / across if across > 0 else 0.0


def _size(facts: dict) -> list[float]:
    return sorted(_num(v) for v in (facts.get("size_mm") or [0.0, 0.0, 0.0]))


def _straight_pair(ports: list[dict]) -> bool:
    """Two mouths that face opposite ways on one line: the two ends of a straight body."""
    import math

    if len(ports) != 2:
        return False
    try:
        n0, n1 = ([_num(v) for v in o.get("normal") or [0, 0, 0]] for o in ports)
        c0, c1 = ([_num(v) for v in o.get("centroid_mm") or [0, 0, 0]] for o in ports)
    except Exception:  # noqa: BLE001
        return False
    dot = sum(a * b for a, b in zip(n0, n1))
    d = [b - a for a, b in zip(c0, c1)]
    along = sum(a * b for a, b in zip(d, n1))
    lateral = math.sqrt(max(sum(v * v for v in d) - along * along, 0.0))
    wide = max(_num(o.get("diameter_mm")) or 2.0 * math.sqrt(max(_num(o.get("area_mm2")), 0.0) / math.pi)
               for o in ports)
    return dot < -STRAIGHT and lateral <= OFF_LINE * max(wide, 1e-9)


def geometry_reading(facts: dict) -> Reading:
    """FLOW-2: what the measured shape alone says, from the measuring step's reading and the
    inside-space map (INS-1) - each situation with the sureness it has on the labelled corpus, and
    UNDECIDED where the shape cannot tell (a straight solid with two flat ends, an open skin, a sheet,
    a face reading and a map that disagree)."""
    try:
        return _geometry(facts)
    except Exception:  # noqa: BLE001 - a reading that breaks says nothing
        return Reading(UNDECIDED, 0.0, "the shape could not be read", "unread")


def _geometry(facts: dict) -> Reading:
    scout = str(facts.get("flow_scout") or facts.get("flow") or "")
    kind = str(facts.get("input_kind_scout") or facts.get("input_kind") or "")
    body = str(facts.get("body_kind") or "")
    conf = _num((facts.get("confidence") or {}).get("input_kind"), 0.5)
    ports = _ports(facts)
    n = len(ports)
    ins = facts["inside"] if isinstance(facts.get("inside"), dict) else {}
    mapped = bool(ins) and ins.get("mapped") is not False and "has_inside" in ins
    mouths = max([int(_num(m)) for m in ins.get("mouths_per_passage") or []] or [0])
    through = mapped and bool(ins.get("has_inside")) and mouths >= 2
    size = _size(facts)
    box = size[0] * size[1] * size[2]
    enclosed = _num(ins.get("enclosed_mm3"), -1.0) if "enclosed_mm3" in ins else -1.0
    surface = facts["surface"] if isinstance(facts.get("surface"), dict) else {}
    open_edges = int(_num(surface.get("open_edges_not_openings")))
    reading = facts["reading"] if isinstance(facts.get("reading"), dict) else {}
    overruled = str((reading.get("kind") or {}).get("body_kind") or "")
    solids = int(_num(facts.get("solids")))

    if scout == INTERNAL and (facts.get("openings_from") == "fluid_volume" or body == "assembly_cavity"):
        return Reading(INTERNAL, SURE["cavity"], f"the parts close off a space inside them at {n} openings", "cavity")
    if body == "rotor" or overruled == "rotor":
        return Reading(EXTERNAL, SURE["rotor"], "it is blades round a hub", "rotor")
    # INS-23: MANY SHORT HOLES THROUGH A THIN PLATE - a sieve, a tube sheet, a grille in a duct's face - are
    # there for the fluid to pass; the stage asks the user to confirm it (application/geometry_check)
    perf: dict = facts["perforated"] if isinstance(facts.get("perforated"), dict) else {}
    if perf.get("proposed"):
        what = "a grille" if perf.get("kind") == "grille" else "a perforated plate"
        return Reading(INTERNAL, SURE["perforated"], f"it has {int(_num(perf.get('holes')))} short holes side by side "
                                                     f"through a thin plate: {what} the fluid flows through",
                       "perforated")
    # A SHEET: nothing inside it, neither metal nor fluid - a duct's wall, a body's skin and a single
    # boundary patch all look alike. A flat patch has no thickness at all.
    flat = size[2] > 0.0 and size[0] <= 1e-6 * size[2]
    if flat or (mapped and enclosed >= 0.0 and box > 0.0 and enclosed < SHEET_SHARE * box
                and (open_edges > 0 or body == "surface")):
        return Reading(UNDECIDED, 0.4, "it is a surface with nothing inside it: a duct's wall, a body's skin or a "
                                       "single boundary patch look the same", "sheet")
    if through and scout == INTERNAL and body == "surface":
        return Reading(UNDECIDED, 0.5, "it is an open skin with open ends: a duct's wall and a body's skin open at "
                                       "its ends look the same", "open_skin")
    if through and scout == INTERNAL:
        return Reading(INTERNAL, SURE["passage_wall"], f"its open ends lead into a way through it ({mouths} mouths "
                                                       "on the inside-space map)", "passage_wall")
    if through:
        return Reading(UNDECIDED, 0.5, "its faces read as a body, but the inside-space map finds a way through it",
                       "map_disagrees")
    if body == "scene":
        return Reading(EXTERNAL, SURE["scene"], f"it is {int(_num(facts.get('components')))} separate pieces, a scene "
                                                "the fluid flows around", "scene")
    if mapped and scout == INTERNAL and body in ("pipe_wall", "hollow_wall"):
        return Reading(UNDECIDED, 0.45, "it has ring-shaped ends, but the inside-space map finds no way through it",
                       "rings_no_way")
    if overruled == "bar" or body == "bar":
        return Reading(UNDECIDED, 0.5, "a straight convex bar with two flat ends: the fluid inside a straight pipe, or "
                                       "a bar in a cross-flow", "bar")
    if scout == INTERNAL and kind == "fluid-domain" and body == "single_solid":
        if solids > 1:
            return Reading(INTERNAL, SURE["solids_with_ends"], f"{solids} closed solids whose flat ends read as the "
                                                               "mouths of passages: the fluid's own volume",
                           "solids_with_ends")
        if _straight_pair(ports):
            if _ends_span(ports, facts) >= SPANS:
                return Reading(INTERNAL, SURE["straight_passage"], "a straight closed solid whose flat ends are its "
                                                                   "whole cross-section: the fluid inside a straight "
                                                                   "pipe (a stepped rod looks the same)", "straight_passage")
            return Reading(UNDECIDED, 0.5, "a straight closed solid that bulges well past its two flat ends: the fluid "
                                           "of a chamber between two pipes, or a body on a support", "straight_bulge")
        return Reading(INTERNAL, SURE["fluid_body"], f"a closed solid shaped like a passage, bent or branched, its {n} "
                                                     "flat ends the mouths: the fluid itself", "fluid_body")
    if scout == INTERNAL and body in ("annular_fluid", "single_solid") and kind == "fluid-domain":
        return Reading(INTERNAL, SURE["lumen"], "a closed solid read as the fluid itself (an annulus or a tube "
                                               "network)", "lumen")
    if scout == EXTERNAL and body == "single_solid" and n >= 2:
        return Reading(EXTERNAL, SURE["body_with_ends"], f"a closed solid whose {n} flat ends do not read as the "
                                                         "mouths of a passage (too small, or too many odd faces): a "
                                                         "body in a flow", "body_with_ends")
    if scout == EXTERNAL and body == "single_solid":
        return Reading(EXTERNAL, SURE["body"], "a closed solid with no mouths of a passage: a body in a flow", "body")
    if scout == EXTERNAL and body == "surface" and (open_edges == 0 or (mapped and n == 0)):
        return Reading(EXTERNAL, SURE["closed_surfaces"], "its surfaces have no openings a fluid could pass and no "
                                                          "inside", "closed_surfaces")
    if scout in (INTERNAL, EXTERNAL):
        return Reading(scout, conf, "the measuring step's reading of its faces", "scout")
    return Reading(UNDECIDED, 0.0, "nothing measured says", "nothing")


# --------------------------------------------------------------------- what is measured ----
def contradiction(facts: dict, flow: str, kind: str = "") -> str:
    """Why a flow kind goes against what was measured - the openings and the inside-space map
    (INS-1) - in plain words, else "". Fluid flowing through needs two openings at least and, for a
    wall round a passage, a way through; fluid flowing around never fits a part the map shows a
    passage through with two mouths."""
    inside: dict = facts["inside"] if isinstance(facts.get("inside"), dict) else {}
    mapped = bool(inside) and inside.get("mapped") is not False
    if flow == INTERNAL:
        if len([o for o in facts.get("openings") or [] if isinstance(o, dict)]) < 2:
            return "the measuring step found fewer than two openings for the fluid to pass"
        if mapped and not inside.get("has_inside"):
            if kind == "body-surface":
                return "the part's inside-space map shows no way through it"
            if "enclosed_mm3" in inside and _num(inside.get("enclosed_mm3")) <= 0.0:
                return "the part's inside-space map shows no volume for the fluid"
    if flow == EXTERNAL and mapped and inside.get("has_inside"):
        return "the part's inside-space map shows a passage through it"
    return ""


def input_kind_for(flow: str, facts: dict, said_kind: str = "") -> str:
    """The input kind that goes with a flow kind: the scout's when it already fits, a kind the model
    or the user gave when it fits, else what the measured shape is - a wall round a passage the map
    found (body-surface), a closed volume that is the fluid itself (fluid-domain), a body (solid-body)."""
    of = {"body-surface": INTERNAL, "fluid-domain": INTERNAL, "solid-body": EXTERNAL, "solid-assembly": INTERNAL}
    scout = str(facts.get("input_kind_scout") or facts.get("input_kind") or "")
    if said_kind and of.get(said_kind) == flow:
        return said_kind
    if of.get(scout) == flow:
        return scout
    if flow == EXTERNAL:
        return "solid-body"
    inside: dict = facts["inside"] if isinstance(facts.get("inside"), dict) else {}
    return "body-surface" if inside.get("has_inside") else "fluid-domain"


# ----------------------------------------------------------------------------- the record ----
def _record(kind: str, best: str, confidence: float, by: str, rule: str, reasons: list[str],
            geometry: Reading | None, facts: dict, said_kind: str = "") -> dict:
    flow = kind if kind in (INTERNAL, EXTERNAL) else best
    out = {"kind": kind, "flow": flow, "best_guess": best, "confidence": round(float(confidence), 2), "by": by,
           "rule": rule, "reasons": [r for r in reasons if r], "question": QUESTION if kind == UNDECIDED else "",
           "input_kind": input_kind_for(flow, facts, said_kind)}
    if geometry is not None:
        out["geometry"] = {"kind": geometry.kind, "confidence": round(geometry.confidence, 2), "case": geometry.case}
    return out


def _picture(vision: dict | None) -> tuple[str, float, str, str]:
    """The model's flow, sureness, kind and part name from a SANITIZED naming answer, or ("", 0, "", "")."""
    if not isinstance(vision, dict) or "error" in vision:
        return "", 0.0, "", ""
    of = {"body-surface": INTERNAL, "fluid-domain": INTERNAL, "solid-body": EXTERNAL}
    kind = str(vision.get("input_kind") or "")
    flow = str(vision.get("flow") or of.get(kind, ""))
    if flow not in (INTERNAL, EXTERNAL) or (kind and of.get(kind) != flow):
        return "", 0.0, "", ""
    return flow, _num(vision.get("confidence")), kind, str(vision.get("part") or "")


def _quote(words: tuple[str, ...]) -> str:
    return ", ".join(f'"{w}"' for w in dict.fromkeys(words))


def decide(facts: dict, *, words: str = "", vision: dict | None = None) -> dict:
    """THE FLOW KIND: internal, external or undecided, with the flow to draw meanwhile (the best
    guess), how sure, who decided and the reasons. In order: what the file declares (FLOW-5), the
    user's stated words (FLOW-1), the measured shape (FLOW-2) with the object the words name and the
    pictures weighed against it (FLOW-3), else undecided with the one question (FLOW-4). Never raises."""
    try:
        return _decide(facts if isinstance(facts, dict) else {}, words, vision)
    except Exception:  # noqa: BLE001 - a reading that breaks is no reading: ask
        return _record(UNDECIDED, str((facts or {}).get("flow") or INTERNAL), 0.0, "guess", RULE_UNDECIDED,
                       ["the part could not be read"], None, facts if isinstance(facts, dict) else {})


def _decide(facts: dict, words: str, vision: dict | None) -> dict:
    # FLOW-5: A FILE THAT DECLARES ITS REGIONS (a thermal model names its air): nothing to weigh
    if facts.get("check_mode") == "thermal" or facts.get("input_kind") == "solid-assembly":
        return _record(INTERNAL, INTERNAL, 0.95, "file", RULE_FILE,
                       ["the file itself declares its parts and the air among them"], None, facts)
    geo = geometry_reading(facts)
    said = words_reading(words)
    geo_line = (f"the shape reads as {'flow through it' if geo.kind == INTERNAL else 'flow around it'}: {geo.reason}"
                if geo.kind in (INTERNAL, EXTERNAL) else f"the shape alone cannot tell: {geo.reason}")
    best = geo.kind if geo.kind in (INTERNAL, EXTERNAL) else (str(facts.get("flow_scout") or facts.get("flow") or "")
                                                              or INTERNAL)
    if geo.case == THROUGH_CASE:
        # FLOW-9: faces that read a body while the map finds a way through it with two mouths or more - drawn as flow
        # through it until the user says (still asked, FLOW-4): the way through is what the fluid has to pass
        best = INTERNAL
    # FLOW-1: THE USER'S STATED WORDS WIN, whatever the shape says (the shape's reading is kept as a reason)
    if said.tier == "said":
        reasons = [f"you said {_quote(said.words)}"]
        if geo.kind in (INTERNAL, EXTERNAL) and geo.kind != said.kind:
            reasons.append(geo_line + "; your words win")
        return _record(said.kind, said.kind, 0.95, "words", RULE_WORDS, reasons, geo, facts)
    kind, conf, by, rule = geo.kind, geo.confidence, "geometry", RULE_GEOMETRY
    reasons = [geo_line]
    if kind not in (INTERNAL, EXTERNAL) or conf < DECIDE_AT:
        kind = UNDECIDED
    # THE OBJECT THE USER NAMED ("an exhaust manifold", "a car"): the user's own word for what the part
    # is, so it wins over the shape too (FLOW-1) - a little less sure than a stated flow, and with the
    # shape's disagreement said, so the panel shows both
    if said.tier == "named":
        line = f"you named {_quote(said.words)}"
        if kind == said.kind:
            return _record(kind, kind, max(conf, 0.85), "words", RULE_WORDS, [line, geo_line], geo, facts)
        reasons = [line] + ([geo_line + "; your words win"] if geo.kind in (INTERNAL, EXTERNAL) else [])
        return _record(said.kind, said.kind, 0.8, "words", RULE_WORDS, reasons, geo, facts)
    # FLOW-3: THE PICTURES, only where they are clearly surer than the shape and nothing measured says no
    p_flow, p_conf, p_kind, p_part = _picture(vision)
    said_kind = ""
    if p_flow and by != "words":
        why = contradiction(facts, p_flow, p_kind)
        name = f" ({p_part})" if p_part else ""
        if p_flow == kind:
            conf = max(conf, min(p_conf, 0.9))
            reasons.append(f"the pictures agree{name}")
        elif why:
            reasons.append(f"The pictures suggested the fluid flows {'through' if p_flow == INTERNAL else 'around'} "
                           f"it{name}, but {why}, so the measured reading stands.")
        elif p_conf >= max(conf, DECIDE_AT - PICTURE_MARGIN) + PICTURE_MARGIN:
            kind, conf, by, rule, best, said_kind = p_flow, p_conf, "pictures", RULE_PICTURES, p_flow, p_kind
            reasons.insert(0, f"the pictures show flow {'through' if p_flow == INTERNAL else 'around'} it{name}")
        else:
            reasons.append(f"the pictures suggested flow {'through' if p_flow == INTERNAL else 'around'} it{name}, "
                           "not surely enough to decide")
    if kind == UNDECIDED:
        rule = RULE_UNDECIDED
        by = "guess"
    out = _record(kind, best if kind == UNDECIDED else kind, conf, by, rule, reasons, geo, facts, said_kind)
    if kind == UNDECIDED and geo.case == THROUGH_CASE and best == INTERNAL:
        out["guess_rule"] = RULE_THROUGH_GUESS
    return out


def answer(record: dict, facts: dict, text: str) -> dict:
    """The record once the user answers the one question (or says the flow in any words later): their
    words decide (FLOW-1); words that still say nothing leave the record as it was."""
    if words_reading(text).tier != "said":
        return record
    return decide(facts, words=text)
