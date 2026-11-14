# Responsibility: Read the MESH REQUIREMENTS a user wrote in their notes into one closed, typed set - sizes (a global size, the shortest and longest edge, growth, a wall thickness), the element kind, and quality limits (a measure, a shape, a direction, a target, the share of elements allowed past it and the outer limit none may pass) - checked by rules, with everything else the user wrote about the mesh kept as "not applied" and told. One reading serves every route; the flow fields (a cell count, boundary layers, a size at the walls or in a named place, the flow numbers a y+ needs) are read the same way (NOTE-10).
# Boundaries: the model only fills declared fields from a quote of the user's own words; every number must be in that quote, every unit is the user's, every direction and range is checked here. It writes no mesher setting. A model that is off, slow, wrong or down never fails a run: its saved reading for these same words, else nothing applied, and said. With no words to read no model is asked and the set is empty. What a route does with the set is the route's (the shell mesh: engines/shell_requirements.py).
# Collaborates with: contracts/model_inference (the builder route; contracts/decision_cache keys and replays the round), contracts/user_notes (the notes, word for word), cad/part_words.notes_text (the words read), engines/fea_driver.py (asks once per run), engines/shell_requirements.py (limits in force, asked against delivered).
"""THE MESH REQUIREMENTS IN THE USER'S NOTES (docs/RULES.md NOTE-7..9).

The model answers with ONE `mesh_requirements` call:

    {"stated_unit": "mm" | "cm" | "m" | "in" | "um" | "none",
     "sizes": [{"what": "global" | "min_edge" | "max_edge" | "growth" | "thickness",
                "value": 10, "unit": "mm" | ... | "x_global" | "none", "quote": "Global Mesh Size = 10 mm"}],
     "element_kind": [{"what": "quads_only" | "triangles_only" | "max_triangle_percent" | "linear" | "quadratic"
                             | "free_mesh" | "no_washers" | "structured",
                       "value": 0, "quote": "no triangles"}],
     "limits": [{"measure": "warpage", "shape": "quad" | "tria" | "tet" | "cell" | "any", "direction": "max" | "min",
                 "target": 15, "allowed_percent": 1, "outer": 30, "has_outer": true,
                 "quote": "Warpage < 15° (1% between 15° and 30° is acceptable)"}],
     "other": [{"quote": "...", "kind": "mesh" | "elsewhere" | "not_mesh"}],
     # NOTE-10, the flow fields (read on every route):
     "counts": [{"what": "target" | "ceiling" | "floor", "value": 8000000, "quote": "max 8 million cells"}],
     "layers": [{"what": "count" | "first_height" | "total_thickness" | "growth" | "y_plus", "value", "unit", "quote"}],
     "local": [{"where": "the wing", "what": "size" | "finer" | "coarser", "value", "unit", "quote"}],
     "physics": [{"what": "velocity" | "length" | "kinematic_viscosity" | "reynolds" | "air" | "water", "value",
                  "unit", "quote"}]}

and the rules turn it into the set every route reads (`requirements`):

    {"sizes": {"global_mm", "min_edge_mm", "max_edge_mm", "growth", "thickness_mm", "near_wall_mm"},  # None: not asked
     "kind": {"shell": "quads" | "triangles" | None, "tria_percent_max": None | p, "order": None | "linear" | "quadratic",
              "structure": "free" | "no_washers" | "structured" (only when said)},
     "cells": {"target", "ceiling", "floor"},                                              # a cell or element count
     "layers": {"count", "first_mm", "total_mm", "growth", "y_plus"},                      # the boundary layers
     "local": [{"id", "where", "what", "mm", "quote"}],                                    # sizes in one named place
     "physics": {"velocity_ms", "length_m", "nu_m2s", "reynolds", "fluid"},                # context for a y+, never asked
     "limits": [{"id", "measure", "shape", "op": "<=" | ">=", "target", "share", "outer": None | x, "quote"}],
     "rows": [{"id", "field", "quote", "asked"}]}                                          # one per requirement, in plain words

NOTE-7  the reading: one strict-schema call, every entry checked - the quote is the user's own words, every number
        is in the quote, the unit is the one the user states (mm, cm, m, inch, micron; a multiple of the global
        size; none stated: the unit the notes give once for all, else millimetres, said), the direction is the
        measure's own, the outer limit lies past the target, ranges are sane, two entries for one thing keep the
        LATEST written (the earlier one is not told back as a fault), a number the user names as the one being
        replaced ("3 layers instead of 8") is no request, the shortest edge is not over the longest. What fails is
        dropped and told in plain words.
NOTE-8  what fits no field is "not applied" and told; never silently dropped. A clause that asks nothing (no
        quantity, no mesh term, nothing named: "the rest looks good") is not a requirement and is not told.
NOTE-9  no words: no model call and an empty set; no answer and none saved: an empty set, and said.
NOTE-10 the flow fields of the same reading (a count, layers, a size at the walls, sizes in a place, three more flow
        measures, the flow's numbers for a y+), checked the same way; every flow engine applies them
        (engines/cfd_requirements.py), the solid and shell routes say what each is there.
NOTE-11 a shell element kind written in a few fixed phrases ("triangles only", "tri mesh", "Shell elements: triangles",
        "use triangles for shells", "no triangles", "all quads") that the model put in no field is read by the rules
        from the user's own line - never against a kind the model did read. A share of triangles carries a number:
        the model's alone.
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping

from meshpipeline.contracts.event_stream import StaleExecutionPublish

logger = logging.getLogger(__name__)

RULE_READ = "NOTE-7"
RULE_NOT_APPLIED = "NOTE-8"
RULE_NOTHING = "NOTE-9"
RULE_FLOW = "NOTE-10"          # the flow fields of the same reading (a count, layers, a size at the walls or in a place)
RULE_KIND_WORDS = "NOTE-11"
TOOL = "mesh_requirements"
CONSUMER = "mesh_requirements"
STEP = "mesh_requirements"
#: the reading as the workspace keeps it, for the steps that mesh in a process of their own (engines/shell_run,
#: engines/mixed_run): written when the notes ask for something, removed when they do not
FILE = "mesh_requirements.json"
#: how long the one reading may take before nothing is applied (the same as the other FEA decision)
TIMEOUT_S = 180.0
MIN_WAIT_S = 20.0
#: the most of the user's words one reading is shown (a pasted criteria sheet fits many times over)
MAX_CHARS = 8000

#: millimetres per unit the user may state
UNITS_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "um": 1e-3}
SIZE_UNITS = (*UNITS_MM, "x_global", "none")
SIZES = ("global", "min_edge", "max_edge", "growth", "thickness", "near_wall")
KINDS = ("quads_only", "triangles_only", "max_triangle_percent", "linear", "quadratic", "free_mesh", "no_washers",
         "structured")
#: SHMESH-43: the mesh's PATTERN (a shell's): "free_mesh" (no washers, no mapped faces), "no_washers", "structured"
#: (washers and mapped quads: what the shell mesh does anyway) - one slot, kind["structure"]
PATTERNS = {"free_mesh": "free", "no_washers": "no_washers", "structured": "structured"}
SHAPES = ("quad", "tria", "tet", "cell", "any")
OTHER_KINDS = ("mesh", "elsewhere", "not_mesh")
#: NOTE-10: the flow (CFD) fields - a cell count, the boundary layers, and sizes in a named place
COUNTS = ("target", "ceiling", "floor")
LAYERS = ("count", "first_height", "total_thickness", "growth", "y_plus")
LOCAL = ("size", "finer", "coarser")
#: the flow's own numbers a y+ needs (context, never a requirement): metres per second per speed unit
PHYSICS = ("velocity", "length", "kinematic_viscosity", "reynolds", "air", "water")
SPEEDS = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444, "ft/s": 0.3048}
#: the route whose meshes are cells of a flow: its words say "cell", and it alone reads the flow fields (the
#: solid and shell routes have their own steps for a count and for sizes in a place, and no boundary layers)
FLOW = "flow"
#: a cell or element count anyone means, a layer count, a y+ and a layer growth
COUNT_RANGE = (1_000.0, 1e11)
LAYER_COUNT_MAX = 60
Y_PLUS_RANGE = (0.01, 1000.0)
LAYER_GROWTH_RANGE = (1.0, 3.0)

#: THE DECLARED MEASURES: name -> (the directions it can be limited in, (lowest, highest) sane target, unit shown,
#: plain name). A direction outside these is a contradiction (an aspect ratio "over 5" is no quality limit).
MEASURES: dict[str, tuple[tuple[str, ...], tuple[float, float], str, str]] = {
    "aspect_ratio": (("max",), (1.0, 1000.0), "", "aspect ratio"),
    "warpage": (("max",), (0.0, 180.0), "°", "warpage"),
    "jacobian": (("min",), (0.0, 1.0), "", "Jacobian"),
    "skew": (("max", "min"), (0.0, 90.0), "°", "skew"),
    "taper": (("max",), (0.0, 1.0), "", "taper"),
    "min_angle": (("min",), (0.0, 180.0), "°", "smallest corner angle"),
    "max_angle": (("max",), (0.0, 180.0), "°", "largest corner angle"),
    "non_orthogonality": (("max",), (0.0, 90.0), "°", "non-orthogonality"),
    "skewness": (("max",), (0.0, 100.0), "", "skewness"),
    # NOTE-10: the flow measures checkMesh and the CFD meshers hold
    "determinant": (("min",), (0.0, 1.0), "", "determinant"),
    "min_volume": (("min",), (0.0, 1.0), " m³", "cell volume"),
    "orthogonal_quality": (("min",), (0.0, 1.0), "", "orthogonal quality"),
}
#: the measures whose sane range includes its ends (a Jacobian of 1, a taper of 0, a volume over 0)
_ENDS_SANE = ("jacobian", "taper", "min_volume", "determinant")
#: the share of elements a limit may allow past its target, in percent, at most
MAX_ALLOWED_PERCENT = 50.0
#: sizes: an element size anyone means (mm), a growth ratio, a multiple of the global size
SIZE_RANGE_MM = (1e-4, 1e5)
GROWTH_RANGE = (1.0, 3.0)
MULTIPLE_RANGE = (0.01, 100.0)


def _f(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _g(v: float) -> str:
    """A number as the user reads it: at most 4 significant digits, no trailing zeros."""
    return f"{float(f'{float(v):.4g}'):g}"


# #
# what the model is shown
# #

SYSTEM = """You read an engineer's notes about a mesh they are ordering and pick out the MESH REQUIREMENTS in them, by calling
mesh_requirements once. You only copy what they wrote into the declared fields: never invent, guess, convert or
round a number, and never add a requirement they did not write. Every entry carries a `quote`: the user's own
words for it, copied exactly (one line or phrase, as short as holds the whole requirement and all its numbers).

- stated_unit: the length unit the notes give ONCE FOR ALL their numbers ("all dimensions in inches", "units: mm"),
  else "none".
- sizes: element (or CFD cell) sizes for the WHOLE mesh.
  global = the element or cell size they want everywhere ("Global Mesh Size = 10 mm", "keep elements around 5 mm",
  "mesh at 0.2 in", "element size 3", "base cell size 20 mm", "background cells 50 mm", "cells around 5 mm").
  min_edge = the shortest element edge / smallest element or cell size allowed ("min cell size 0.5 mm"); max_edge =
  the longest / largest allowed ("max cell size 100 mm", "no cell bigger than 0.1 m", "far-field cells max 0.5 m":
  the cells far from the body are the largest).
  near_wall = the cell or element size AT THE WALLS / on the body's surface, for the whole mesh ("surface cells 2 mm",
  "wall cell size 1 mm", "2 mm cells on the surface"). A first LAYER height is a layer, not this.
  growth = how fast cell or element sizes may grow from one to the next through the volume ("growth rate 1.2", "cell
  size ratio at most 1.3"): unit "none". A growth or expansion ratio of the prism LAYERS is a layer, not this.
  thickness = a wall or shell thickness they state for the part ("2 mm sheet", "thickness = 1.5 mm").
  value and unit exactly as written: the unit beside the number (mm, cm, m, in for inches or ", um for microns);
  "x_global" when the number is a multiple of the global size ("0.5 x Global Size", "half the global size" -> 0.5,
  "1.5 times the element size"); "none" when no unit is written beside it. A fraction is its value ("3/8 inch" ->
  0.375, in).
  A size for ONE part, surface or region ("3 mm on the bracket", "2 mm at the holes", "finer near the weld", "refine
  around the wing to 2 mm") is NOT a size here: it is a local entry. A total element or cell COUNT is a count.
- counts: a total cell or element count. target ("about 5 million cells", "around 500k elements"), ceiling ("max 8
  million cells", "under 2M cells", "no more than 300k elements"), floor ("at least 1 million cells"). value is the
  whole number as written: "8 million" -> 8000000, "500k" -> 500000, "2.5M" -> 2500000, "1e6" -> 1000000.
- layers: the boundary layers at the walls (prism, inflation or wall layers; CFD). count ("5 prism layers" -> 5, "no
  boundary layers" -> 0); first_height ("first layer 0.02 mm", "first cell height 1e-5 m", "wall spacing 10 um"),
  value and unit as written; total_thickness (the whole stack: "layers 3 mm thick in total"); growth (how much each
  layer grows over the one under it: "layer growth 1.2", "expansion ratio 1.15", "inflation rate 1.2" - unit
  "none"); y_plus (the y+ they aim at: "y+ around 1" -> 1, "y+ < 5" -> 5, "y+ 30-100" -> 30, the low end of a
  range) - unit "none".
- local: a size or a refinement in ONE named place, each with `where` = the place in the user's words ("the wing",
  "the inlet", "the bracket", "the holes", "the wake", "the gap between the wheels and the floor"). what: size (a
  size given: "refine around the wing to 2 mm" -> where "the wing", value 2, mm; "wake cells 10 mm" -> where "the
  wake"), finer ("finer near the inlet", "refine the wake"; value 0, unit "none"), coarser ("coarser at the outlet";
  value 0, unit "none"). The walls, the surface or the body as a whole are not one place: a size there is near_wall;
  the far field is max_edge. A number of refinement LEVELS ("4 levels at the surface") is no size: other, "mesh".
- physics: the flow's numbers, ONLY as written, so a y+ can be turned into a first layer height. velocity ("air at
  30 m/s", "60 mph", "120 km/h"; unit m/s, km/h, mph, kn, ft/s), length (the reference length: "chord 0.5 m", "car
  length 4.2 m"; a length unit), kinematic_viscosity ("nu = 1.5e-5 m2/s"; unit m2/s), reynolds ("Re = 3e6"; unit
  none), air / water (the fluid is named; value 0, unit none). The physics lines are "not_mesh" too: list them under
  other as well.
- element_kind: quads_only ("no triangles", "quad mesh only", "all quads"), triangles_only ("tria mesh", "triangles
  only"), max_triangle_percent ("at most 5% trias" -> value 5), linear ("first order", "linear elements", "tet4"),
  quadratic ("second order", "tet10", "mid-side nodes"), free_mesh ("free mesh", "paved mesh", "no mapped mesh",
  "unstructured quads"), no_washers ("no washers", "no rings round the holes"), structured ("washers round holes",
  "mapped mesh", "structured quads", "rings of quads round bolt holes"). value 0 unless it is max_triangle_percent.
  "shell", "solid", "mid-surface" (what to mesh the part AS) is "elsewhere". A solver's element name at the head of
  a quality line ("QUAD4: skew angle > 30", "CTRIA3 min angle 20") only names the elements the limit is for (its
  shape): it asks for no element kind or order.
- limits: element QUALITY limits. measure: aspect_ratio; warpage (warp angle, degrees); jacobian (0 to 1); skew
  (degrees); taper; min_angle and max_angle (corner angles, degrees); non_orthogonality (degrees, CFD cells:
  "non-ortho under 65", "maxNonOrtho 70"); skewness (CFD cells: "max skewness 4"); determinant (CFD cell
  determinant, 0 to 1: "min determinant 0.001"); min_volume (the smallest CFD cell volume, in cubic metres: "min
  volume 1e-15"); orthogonal_quality (Fluent's orthogonal quality, 0 to 1: "orthogonal quality above 0.1").
  shape: quad, tria, tet or cell when the line names one ("Quad Max. Angle", "tria min angle", "tets", "cells"),
  else any. direction: "max" when the value must stay UNDER the target (<, at most, below, no more than),
  "min" when it must stay OVER it (>, at least, above, no less than) - as the user wrote it, even if it looks wrong.
  target: the limit. Many criteria sheets allow a few elements past the target up to an outer limit: "Warpage < 15°
  (1% between 15° and 30° is acceptable)" -> target 15, allowed_percent 1, outer 30, has_outer true. "5% of elements
  may exceed it" with no outer limit -> allowed_percent 5, has_outer false, outer 0. No allowance written ->
  allowed_percent 0, has_outer false, outer 0.
  A limit on element LENGTH ("Min. Element Length > 0.5 x Global Size") is a size (min_edge / max_edge), not a limit.
- other: every other thing they wrote, one entry per line, sentence or clause, never left out. kind "mesh": about
  the mesh but fitting no field above (chordal deviation, elements through the thickness or across a flange,
  cells across a gap, a named solver's own check, a quality measure not in the list, a
  preference like "quads preferred" or "hex-dominant", a vague wish like "good quality mesh" or "resolve the
  boundary layer well").
  "elsewhere": ONLY shell or solid (what to mesh the part as). "not_mesh": loads, supports, materials, the
  analysis, the flow's physics (velocity, fluid, Reynolds number, turbulence model, wall functions, inlet and outlet
  conditions), the solver or file format the mesh is for ("mesh for Fluent", "OpenFOAM format": the export is chosen
  at download), greetings, thanks, "use your defaults", headings, a yes or a verdict on what was proposed ("looks
  good", "the rest is right", "yes that's right"), anything that asks nothing of the mesh.
When the notes give the SAME thing twice ("5 layers. Actually 10 layers."), list BOTH entries, in the order written:
the LATEST one is kept (the user changed their mind).
A number the user names as the one being REPLACED ("3 prism layers instead of 8", "3 layers, not 8", "2 mm rather
than 5 mm") is not a request: list only the value they ask for (3, 3, 2).
An empty note, or one with no mesh requirement: empty lists (other still lists what was written)."""


def tool() -> dict:
    q = {"type": "string", "description": "the user's own words for this entry, copied exactly"}
    return {"type": "function", "function": {
        "name": TOOL,
        "description": "The mesh requirements written in the user's notes, each with the quote it was read from.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["stated_unit", "sizes", "counts", "layers", "local", "physics", "element_kind", "limits",
                         "other"],
            "properties": {
                "stated_unit": {"type": "string", "enum": [*UNITS_MM, "none"]},
                "sizes": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["what", "value", "unit", "quote"],
                    "properties": {"what": {"type": "string", "enum": list(SIZES)}, "value": {"type": "number"},
                                   "unit": {"type": "string", "enum": list(SIZE_UNITS)}, "quote": q}}},
                "counts": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["what", "value", "quote"],
                    "properties": {"what": {"type": "string", "enum": list(COUNTS)}, "value": {"type": "number"},
                                   "quote": q}}},
                "layers": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["what", "value", "unit", "quote"],
                    "properties": {"what": {"type": "string", "enum": list(LAYERS)}, "value": {"type": "number"},
                                   "unit": {"type": "string", "enum": [*UNITS_MM, "none"]}, "quote": q}}},
                "physics": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["what", "value", "unit", "quote"],
                    "properties": {"what": {"type": "string", "enum": list(PHYSICS)}, "value": {"type": "number"},
                                   "unit": {"type": "string", "enum": [*UNITS_MM, *SPEEDS, "m2/s", "none"]},
                                   "quote": q}}},
                "local": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["where", "what", "value", "unit", "quote"],
                    "properties": {"where": {"type": "string"}, "what": {"type": "string", "enum": list(LOCAL)},
                                   "value": {"type": "number"},
                                   "unit": {"type": "string", "enum": [*UNITS_MM, "none"]}, "quote": q}}},
                "element_kind": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["what", "value", "quote"],
                    "properties": {"what": {"type": "string", "enum": list(KINDS)}, "value": {"type": "number"},
                                   "quote": q}}},
                "limits": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["measure", "shape", "direction", "target", "allowed_percent", "outer", "has_outer",
                                 "quote"],
                    "properties": {"measure": {"type": "string", "enum": list(MEASURES)},
                                   "shape": {"type": "string", "enum": list(SHAPES)},
                                   "direction": {"type": "string", "enum": ["max", "min"]},
                                   "target": {"type": "number"}, "allowed_percent": {"type": "number"},
                                   "outer": {"type": "number"}, "has_outer": {"type": "boolean"}, "quote": q}}},
                "other": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["quote", "kind"],
                    "properties": {"quote": q, "kind": {"type": "string", "enum": list(OTHER_KINDS)}}}},
            }}}}


def notes_text(state: Mapping) -> str:
    """THE USER'S OWN WORDS, and only those (NOTE-7): what they typed in the notes box and their own chat messages
    as kept, word for word (cad/part_words.notes_text, contracts/user_notes.own_notes). Never the intake's summary
    of the request (request_txt, a model's rewrite: on a shell run it read "Generate a quadratic tetrahedral
    structural mesh ...", and was told back to the user as a note "not applied"), the geometry declaration, the
    confirmation card or any text the application wrote - so a "Not applied" line can only ever quote the user."""
    from meshpipeline.cad.part_words import notes_text as _words

    return _words(state)


def wanted(state: Mapping) -> bool:
    """NOTE-9: asked only when the user left words to read."""
    return bool(notes_text(state).strip())


def messages_for(state: Mapping, *, route: str = "") -> list[dict]:
    """What the model is shown, as plain data in a fixed order (the decision cache keys the round by it)."""
    from meshpipeline.contracts.user_notes import own_notes, prompt_block

    s = state or {}
    shown = {"mesh_for": route,
             "user_notes": prompt_block(own_notes(s.get("user_notes") or []), consumer="mesh_requirements",
                                        limit=MAX_CHARS)}
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(shown, sort_keys=True, separators=(",", ":"))}]


# #
# the answer, checked (NOTE-7)
# #

_DASHES = re.compile(r"[‐-―−]")
_NUM = re.compile(r"(?<![\w.])(?:\d+(?:[.,]\d+)?|\.\d+)")
#: numbers people write as words, for the "every number is in the quote" check
_WORDS = {"zero": 0.0, "no": 0.0, "half": 0.5, "quarter": 0.25, "one": 1.0, "two": 2.0, "twice": 2.0, "double": 2.0,
          "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
          "twelve": 12.0, "fifteen": 15.0, "twenty": 20.0, "thirty": 30.0, "forty": 40.0, "fifty": 50.0,
          "sixty": 60.0, "ninety": 90.0, "hundred": 100.0}


def _norm(text: str) -> str:
    t = _DASHES.sub("-", str(text or "")).replace("×", "x").replace("’", "'")
    return " ".join(t.casefold().split())


def _squash(text: str) -> str:
    return re.sub(r"[^\w.]+", "", _norm(text)).replace("_", "")


_FRACTION = re.compile(r"(?<![\w./])(\d+)\s*/\s*(\d+)(?![\w./])")
#: a length unit written right after a number, as people write it -> the unit this step converts
_UNIT_AFTER = {"mm": "mm", "millimetres": "mm", "millimeters": "mm", "millimetre": "mm", "millimeter": "mm",
               "cm": "cm", "um": "um", "µm": "um", "μm": "um", "microns": "um", "micron": "um",
               "inches": "in", "inch": "in", '"': "in", "”": "in", "metres": "m", "meters": "m"}
#: ("in" and "m" are words too - "10 in the flange" - so those two are the model's to read, never read off here)


def _unit_beside(value: float, quote: str) -> str | None:
    """The length unit the user wrote right after `value` in their own words ("0.4 in", "3/8 inch", "10mm"), or
    None when none is written there. The user's unit, never the model's guess."""
    t = _DASHES.sub("-", str(quote or "")).casefold()
    for m in re.finditer(r"(?<![\w.])(\d+\s*/\s*\d+|\d+(?:[.,]\d+)?|\.\d+)[\s-]*([a-zµμ]+|[\"”])?", t):
        raw = m.group(1)
        if "/" in raw:
            a, b = raw.split("/")
            num = float(a) / float(b) if float(b) else None
        else:
            num = float(raw.replace(",", "."))
        if num is None or abs(num - value) > 1e-9 * max(1.0, abs(value)):
            continue
        unit = _UNIT_AFTER.get(m.group(2) or "")
        if unit:
            return unit
    return None


#: a number in scientific notation as people write it: 1e-5, 2.5E6, 1x10^-5, 1×10-5, 1*10^6
_SCI = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*(?:[eE]|[x*]\s*10\s*\^?|\*\*)\s*([-+]?\d+)(?![\w.])")
#: a count's multiplier written after its number ("8 million", "500k", "2.5M cells", "3 mio")
_COUNT = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?|\.\d+)\s*(k|thousand|m|mio|mn|mln|million|millions|millionen|millones|"
                    r"millón|millon|milioni|milione|milhões|milhoes|b|bn|billion|万|億)?(?![a-zà-ÿ])", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mio": 1e6, "mn": 1e6, "mln": 1e6, "million": 1e6, "millions": 1e6,
         "millionen": 1e6, "millones": 1e6, "millón": 1e6, "millon": 1e6, "milioni": 1e6, "milione": 1e6,
         "milhões": 1e6, "milhoes": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "万": 1e4, "億": 1e8}


def _sci(text: str) -> list[float]:
    t = _DASHES.sub("-", str(text or "")).replace("×", "x")
    out = []
    for a, e in _SCI.findall(t):
        try:
            out.append(float(a) * 10.0 ** int(e))
        except (ValueError, OverflowError):
            continue
    return out


def _counts(text: str) -> list[float]:
    """The counts written in the user's words, with their multipliers and thousands marks ("8 million", "500k",
    "8,000,000", "2.5M", "1e6")."""
    t = re.sub(r"(?<=\d)[,' _](?=\d{3}(?!\d))", "", _DASHES.sub("-", str(text or "")))
    out = [float(n.replace(",", ".")) * _MULT.get(m.casefold(), 1.0) for n, m in _COUNT.findall(t)]
    return out + _sci(t) + _numbers(t)


def _numbers(text: str) -> list[float]:
    t = _DASHES.sub("-", str(text or ""))
    # a fraction is its value ("3/8 inch" is 0.375, and neither 3 nor 8)
    out = [float(a) / float(b) for a, b in _FRACTION.findall(t) if float(b)]
    out += _sci(t)
    t = _FRACTION.sub(" ", t)
    out += [float(m.replace(",", ".")) for m in _NUM.findall(t)]
    # "4,5" may be the European 4.5 or a list "4, 5": both readings count as written
    for m in re.findall(r"(?<![\w.])(\d+),(\d+)(?![\w.])", t):
        out += [float(m[0]), float(m[1])]
    out += [v for w, v in _WORDS.items() if re.search(rf"\b{w}\b", t.casefold())]
    return out


def _in_quote(value: float, numbers: list[float]) -> bool:
    return any(abs(value - n) <= 1e-9 * max(1.0, abs(n)) for n in numbers)


def _quoted(quote: str, said: str, said_squashed: str) -> bool:
    """Whether the quote is the user's own words: found in the notes as written (spacing and case aside), or
    letter for letter once punctuation is set aside."""
    q = _norm(quote)
    return bool(q) and (q in said or (len(_squash(quote)) >= 4 and _squash(quote) in said_squashed))


#: a quote this close to a line or sentence of the notes is read as that line (a model that copies a long line
#: can slip a character); the entry is then checked against the USER's line, never the model's copy
QUOTE_ALIKE = 0.9


def _locate(quote: str, notes: str) -> str | None:
    """The user's own line, sentence or clause nearest the quote, when it is at least QUOTE_ALIKE like it."""
    from difflib import SequenceMatcher

    q = _norm(quote)
    if len(q) < 6:
        return None
    best, score = None, QUOTE_ALIKE
    for piece in re.split(r"\n+|(?<=[.!?;。；])\s*", str(notes or "")):
        piece = " ".join(piece.split())
        if not piece:
            continue
        r = SequenceMatcher(None, q, _norm(piece), autojunk=False).ratio()
        if r >= score:
            best, score = piece, r
    return best


#: words that make a left-over clause one about the mesh (NOTE-8's floor)
_MESH_WORDS = re.compile(r"\b(?:elements?|nodes?|quads?|trias?|triangles?|tets?|tetrahedr\w+|hex\w*|"
                         r"layers?|refine\w*|coarse\w*|jacobian|warp\w*|aspect|skew\w*|taper|washers?)\b", re.I)
#: ... but a clause about WHAT to mesh the part as (shell or solid, FEA or CFD, the analysis) is another reader's
#: (SHSTAGE, the purpose map), never a setting left out: "FEA shell mesh of this part" is not told as not applied
_MESH_AS = re.compile(r"\b(?:shell|mid-?surface|solid|fea|cfd|structural|static|modal|buckling|analysis)\b", re.I)
_MARKER = re.compile(r"^(?:\[n\d+\]\s*\(.*\)|<<<|>>>|\.\.\. \(cut here.*)$")


def _left_over(notes: str, quotes: list[str]) -> list[str]:
    """The clauses of the user's words that no quote of the reading covers and that speak of the mesh - at least
    four words, as written. The model is told to list everything; this holds it to that."""
    text = "\n".join(line for line in str(notes or "").splitlines() if not _MARKER.match(line.strip()))
    mask = bytearray(len(text))
    for q in sorted({" ".join(str(q).split()) for q in quotes if str(q).strip()}, key=len, reverse=True):
        pattern = r"\s+".join(re.escape(w) for w in q.split())
        for m in re.finditer(pattern, text, re.I):
            mask[m.start():m.end()] = b"\x01" * (m.end() - m.start())
    out, start = [], None
    runs = []
    for i in range(len(text) + 1):
        free = i < len(text) and not mask[i] and text[i] != "\n"
        if free and start is None:
            start = i
        elif not free and start is not None:
            runs.append(text[start:i])
            start = None
    for run in runs:
        for piece in re.split(r"[.;!?。；]+\s+|[.;!?。；]+$", run):
            piece = re.sub(r"^(?:[\s,:\-–]+|(?:but|and|also|then)\b)+", "", piece.strip(), flags=re.I).strip(" ,:-–")
            if (len(piece.split()) >= 4 and _MESH_WORDS.search(piece) and not _MESH_AS.search(piece)
                    and piece not in out):
                out.append(piece[:400])
    return out


#: THE VALUE BEING REPLACED (NOTE-7): "3 prism layers instead of 8", "3 layers, not 8", "2 mm rather than 5 mm", "in
#: place of 8", "replacing the 8", "from 8 to 3" - the number after these words (before "to", for "from N to M") is the
#: one the user is changing, never a second request. Founder's run, 14 Nov: "3 prism layers instead of 8" was read as
#: two layer counts and the 8 told back as "a second layer count".
_REPLACED = re.compile(
    r"\b(?:instead\s+of|rather\s+than|in\s+place\s+of|replacing|not)\s+(?:the\s+|a\s+|an\s+|your\s+|our\s+)?"
    r"(?:(?:current|planned|proposed|suggested|default|usual|old|original|earlier)\s+)?"
    r"(\d+(?:[.,]\d+)?\s*(?:k|thousand|m|mio|mn|million|millions)?)(?![\w.])"
    r"|\bfrom\s+(\d+(?:[.,]\d+)?\s*(?:k|thousand|m|mio|mn|million|millions)?)\s+(?:[a-z]+\s+){0,2}?to\s+\d", re.I)


def replaced_numbers(quote: str) -> list[float]:
    """The numbers the user's words name as the value being replaced (_REPLACED), as written and with a count's
    multiplier ("instead of 8 million" is both 8 and 8,000,000): an entry of one of these is not a request."""
    out: list[float] = []
    for m in _REPLACED.finditer(_DASHES.sub("-", str(quote or ""))):
        out += _counts(m.group(1) or m.group(2) or "")
    return out


#: THE WORDS A CLAUSE THAT ASKS NOTHING IS MADE OF (NOTE-8's floor): a yes, a verdict on what was proposed, and the
#: small words that hold them together. Founder's run, 14 Nov: "the rest looks good" was told back as "Not applied:
#: ... no mesh setting of ours matches it yet". A clause is a requirement only when it carries a quantity, a mesh term
#: or a word outside this closed vocabulary (a part, a place, a feature) - so this is the vocabulary of a clause, not
#: a list of phrases.
_FILLER_WORDS = frozenset("""
    a about all also am and any anything are as at be been but by can could did do does doing done else even every
    everything for from go going good great had has have here i i'd i'm id im in is it it's its just keep let let's
    lets like look looking looks me more my nice no nothing now of ok okay on only or other otherwise our perfect
    please really rest right same seem seems so sound sounds still sure than that that's thats the them then there
    these thing things this those to too us very was we well were what whatever which with yes yeah yep you your
    agree agreed approve approved fine correct cool lovely excellent awesome happy thanks thank cheers proceed
    continue carry ahead stay stays remain remains want wanted should would will can't cannot don't dont
    """.split())
#: ...and the words that make a clause about the mesh, whatever else it holds (wider than _MESH_WORDS, which only
#: decides which left-over words are told)
_MESH_TERMS = re.compile(r"\b(?:mesh\w*|cells?|elements?|nodes?|sizes?|sized|layers?|prism\w*|inflation|y\+|yplus|"
                         r"wall|walls|boundary|grid|resolution|resolve\w*|refine\w*|finer|coarser|coarse|detail\w*|"
                         r"quality|skew\w*|orthogonal\w*|aspect|jacobian|warp\w*|quads?|trias?|triangles?|tets?|hex\w*|"
                         r"gaps?|thickness|edges?|growth|count|budget)\b", re.I)
_NUMBER_WORDS = tuple(w for w in _WORDS if w != "no")


def asks_nothing(text: str) -> bool:
    """Whether a clause of the user's words asks nothing of the mesh: no quantity (a digit or a number written as a
    word), no mesh term, and every word of it from the vocabulary a yes or a verdict is made of (_FILLER_WORDS) -
    "the rest looks good", "yes that's right", "looks good". A whole quote asks nothing when each of its clauses does."""
    t = _norm(text).replace("’", "'")
    clauses = [c for c in re.split(r"[,.;:!?()]+|\s+-\s+|\b(?:and|but)\b", t) if c.strip()]
    if not clauses:
        return False
    for c in clauses:
        if re.search(r"\d", c) or _MESH_TERMS.search(c) or any(re.search(rf"\b{w}\b", c) for w in _NUMBER_WORDS):
            return False
        words = re.findall(r"[a-z][a-z']*", c)
        if not words or any(w not in _FILLER_WORDS for w in words):
            return False
    return True


#: "no triangles", "without quads", "avoid trias": the user's words say which kind is NOT wanted
_NO_KIND = re.compile(r"\b(?:no|without|avoid|zero|never|not)\s+(?:\w+\s+){0,2}?(tri\w*|quad\w*)", re.I)


def _kind_said(what: str, quote: str) -> str:
    """The element kind the user's words say, when they name one they do NOT want: "no triangles" is quads only
    and "no quads" triangles only, whatever the model read (it read "no triangles" as triangles only on 3 of 10
    tries). Any other entry is left as read."""
    m = _NO_KIND.search(str(quote or ""))
    if m is None or what not in ("quads_only", "triangles_only"):
        return what
    return "quads_only" if m.group(1).lower().startswith("tri") else "triangles_only"


#: NOTE-11: THE FEW FIXED PHRASES THAT NAME A SHELL'S ELEMENT KIND in anyone's words - "triangles only", "tria mesh",
#: "tri mesh", "all quads", "no triangles", "Shell elements: triangles", "use triangles for shells". The stage's
#: control starts on what they say (application/shell_elements), and the reading takes one the model left in no field
#: (_kind_by_words). A share of triangles ("at most 5% trias") is the model's to read: it carries a number.
_TRIA = r"(?:triangles?|triangular|trias?|tris?)"
_QUAD = r"(?:quads?|quadrilaterals?)"
_SAID_TRIANGLES = re.compile(
    rf"\b(?:{_TRIA}\s+(?:elements?\s+|mesh\s+)?only|only\s+{_TRIA}|all[- ]{_TRIA}|(?:tria|tri|triangle|triangular)\s+"
    rf"(?:shell\s+)?mesh(?:es|ing)?|no\s+{_QUAD}|without\s+{_QUAD}|avoid\s+{_QUAD}|"
    rf"(?:shell\s+)?elements?\s*(?:[:=]|are|as)\s*{_TRIA}|use\s+{_TRIA})\b", re.I)
_SAID_QUADS = re.compile(
    rf"\b(?:{_QUAD}\s+(?:elements?\s+|mesh\s+)?only|only\s+{_QUAD}|all[- ]{_QUAD}|pure\s+{_QUAD}|no\s+{_TRIA}|"
    rf"without\s+{_TRIA}|avoid\s+{_TRIA}|(?:shell\s+)?elements?\s*(?:[:=]|are|as)\s*{_QUAD}|use\s+(?:only\s+)?{_QUAD})\b",
    re.I)


def kind_in_words(text: str) -> str:
    """NOTE-11: the shell element kind a piece of words names - "quads" (quads only), "triangles" or "" - the FIRST one
    written when it names both."""
    t = str(text or "")
    q, r = _SAID_QUADS.search(t), _SAID_TRIANGLES.search(t)
    if q and r:
        return "quads" if q.start() < r.start() else "triangles"
    return "quads" if q else "triangles" if r else ""


def _kind_by_words(notes: str) -> tuple[str, str] | None:
    """NOTE-11: (kind, the user's line) for the first line or sentence of the notes that names a shell element kind
    by the fixed phrases, a share of triangles aside; None with none."""
    for piece in re.split(r"\n+|(?<=[.!?;])\s+", str(notes or "")):
        piece = " ".join(piece.split())
        if not piece or "%" in piece or _MARKER.match(piece):
            continue
        got = kind_in_words(piece)
        if got:
            return got, piece[:400]
    return None


#: the words a shell element-kind row says (the settings page's "What we understood" shows them too)
KIND_WORDS = {"quads": "shell elements: quads only, no triangles", "triangles": "shell elements: triangles only"}


def _short(quote: str) -> str:
    q = " ".join(str(quote or "").split())
    return q if len(q) <= 90 else q[:87] + "..."


def _own_unit(unit: str, value: float) -> str:
    return "" if unit in ("mm", "x_global", "none") else f" ({_g(value)} {'inch' if unit == 'in' else unit})"


def _size_words(what: str, mm: float, multiple: float | None, unit: str, value: float, *, cells: bool = False) -> str:
    own = _own_unit(unit, value)
    tail = f" ({_g(multiple)} x the global size)" if multiple is not None else own
    if cells:
        # the flow route's words: a mesh of cells, sized by a cell's edge
        if what == "global":
            return f"cell size {_g(mm)} mm{tail}"
        if what == "min_edge":
            return f"no cell smaller than {_g(mm)} mm{tail}"
        if what == "max_edge":
            return f"no cell larger than {_g(mm)} mm{tail}"
        if what == "near_wall":
            return f"cells of {_g(mm)} mm at the walls{tail}"
        if what == "thickness":
            return f"wall thickness {_g(mm)} mm{tail}"
        return f"cell sizes grow by at most {_g(value)} from one cell to the next"
    if what == "global":
        return f"global element size {_g(mm)} mm{tail}"
    if what == "min_edge":
        return f"shortest element edge over {_g(mm)} mm{tail}"
    if what == "max_edge":
        return f"longest element edge under {_g(mm)} mm{tail}"
    if what == "thickness":
        return f"wall thickness {_g(mm)} mm{tail}"
    if what == "near_wall":
        return f"elements of {_g(mm)} mm at the walls{tail}"
    return f"sizes grow by at most {_g(value)} from one element to the next"


def _count_words(what: str, n: float, *, cells: bool = True) -> str:
    said, noun = _count(n), "cells" if cells else "elements"
    return {"target": f"about {said} {noun}", "ceiling": f"at most {said} {noun}",
            "floor": f"at least {said} {noun}"}[what]


def _count(n: float) -> str:
    """A cell count as people say it: 8 million, 2.5 million, 500,000."""
    n = float(n)
    if n >= 1e6:
        return f"{_g(n / 1e6)} million"
    return f"{int(round(n)):,}"


def _layer_words(what: str, value: float, mm: float | None = None, unit: str = "none") -> str:
    if what == "count":
        return "no boundary layers" if int(value) == 0 else (
            f"{int(value)} boundary layer{'' if int(value) == 1 else 's'}")
    if what == "first_height":
        return f"first boundary layer {_g(mm or 0.0)} mm thick{_own_unit(unit, value)}"
    if what == "total_thickness":
        return f"boundary layers {_g(mm or 0.0)} mm thick in all{_own_unit(unit, value)}"
    if what == "growth":
        return f"each boundary layer at most {_g(value)} times the one under it"
    return f"y+ of about {_g(value)} at the walls"


def _local_words(where: str, what: str, mm: float | None, unit: str = "none", value: float = 0.0, *,
                 cells: bool = True) -> str:
    noun = "cells" if cells else "elements"
    at = "in" if re.search(r"\b(wake|gap|region|zone|box|volume)\b", where, re.I) else "at"
    if what == "size":
        return f"{noun} of {_g(mm or 0.0)} mm {at} {where}{_own_unit(unit, value)}"
    return f"{'finer' if what == 'finer' else 'coarser'} {noun} {at} {where}"


def limit_words(lim: Mapping, *, cells: bool = False) -> str:
    """One limit in plain words: "warpage under 15° (up to 1% of elements between 15° and 30°)" ("of cells" on the
    flow route)."""
    _d, _r, unit, name = MEASURES[str(lim["measure"])]
    shape = {"quad": "quad ", "tria": "triangle ", "tet": "tetrahedron ", "cell": "cell "}.get(str(lim.get("shape")), "")
    if name.startswith("cell ") and shape == "cell ":
        shape = ""
    of = "cells" if cells else "elements"
    side = "under" if lim["op"] == "<=" else "over"
    said = f"{shape}{name} {side} {_g(lim['target'])}{unit}"
    share, outer = float(lim.get("share") or 0.0), lim.get("outer")
    if share > 0 and outer is not None:
        lo, hi = sorted((float(lim["target"]), float(outer)))
        said += f" (up to {_g(100 * share)}% of {of} between {_g(lo)}{unit} and {_g(hi)}{unit})"
    elif share > 0:
        said += f" (up to {_g(100 * share)}% of {of} may be past it)"
    return said


def empty() -> dict:
    """The set with nothing asked (NOTE-9): every route runs exactly as it does with no notes. The flow fields
    (NOTE-10: `cells`, `layers`, `local`, the size at the walls) are filled on the flow route only."""
    return {"sizes": {"global_mm": None, "min_edge_mm": None, "max_edge_mm": None, "growth": None,
                      "thickness_mm": None, "near_wall_mm": None},
            "kind": {"shell": None, "tria_percent_max": None, "order": None},
            "cells": {"target": None, "ceiling": None, "floor": None},
            "layers": {"count": None, "first_mm": None, "total_mm": None, "growth": None, "y_plus": None},
            "local": [],
            # context for a y+, never a requirement (is_empty does not read it)
            "physics": {"velocity_ms": None, "length_m": None, "nu_m2s": None, "reynolds": None, "fluid": None},
            "limits": [], "rows": []}


def is_empty(req: Mapping | None) -> bool:
    """Whether a requirement set asks for nothing a route applies (None, {} or empty())."""
    r = req or {}
    return (not any(v is not None for v in (r.get("sizes") or {}).values())
            and not any(v is not None for v in (r.get("kind") or {}).values()) and not r.get("limits")
            and not any(v is not None for v in (r.get("cells") or {}).values())
            and not any(v is not None for v in (r.get("layers") or {}).values()) and not r.get("local"))


def check(arguments: str, notes: str, route: str = "") -> tuple[dict, list[str], list[str]]:
    """(the requirement set, what was dropped and why in plain words, what the user wrote about the mesh that
    fits no field). Never raises: an answer of any other shape gives an empty set, said. `route` "flow" fills the
    flow fields and says "cells" (NOTE-10); any other route keeps its own steps for a count and for sizes in a
    place, and a boundary layer or a size at the walls is told as not applied there."""
    try:
        return _check(arguments, notes, route=str(route or ""))
    except Exception as exc:  # noqa: BLE001 - a malformed answer is the model's, never the run's
        return empty(), [f"the reading did not fit the schema ({type(exc).__name__}): nothing applied"], []


#: a count of elements through or across something ("3 elements through the thickness", "two rows across the flange")
_COUNTED = re.compile(r"\b(?:\d+|one|two|three|four|five|six)\s+(?:\w+\s+)?(?:elements?|rows?|layers)\b[^.;\n]{0,24}?"
                      r"\b(?:through|across)\b", re.I)


def _length_mm(value: float, unit: str, quote: str, stated: str) -> tuple[float, str]:
    """(millimetres, the unit used) for a length the user wrote: the unit beside the number in their words, else
    the model's, else the notes' once for all, else millimetres ("none": said)."""
    used = _unit_beside(value, quote) or (unit if unit in UNITS_MM else stated)
    return value * UNITS_MM[used if used in UNITS_MM else "mm"], used if used in UNITS_MM else "none"


def _replaced_only(swapped: Mapping[str, tuple[str, str]], kept, dropped: list[str]) -> None:
    """An entry read only as the value the user is replacing ("instead of 8"), with nothing else read for its field:
    told, so the user's line is never silently left out (NOTE-8). `swapped`: field -> (what it was, the quote)."""
    for field, (said, quote) in swapped.items():
        if field not in kept:
            dropped.append(f'"{_short(quote)}": {said} is the value your words replace, and nothing else was read '
                           "from them: not applied")


def _seq(e: Mapping) -> int:
    """The rank of an entry in the order the user wrote it (set by _check's `order`)."""
    try:
        return int(e.get("_seq") or 0)
    except (TypeError, ValueError):
        return 0


def _later(earlier: str, quote: str) -> None:
    """THE LATEST ONE WINS (NOTE-7): the user changed their mind, so the later value is the one in force and the
    earlier one is not told back as a fault - only kept in the log."""
    logger.info("mesh requirements: %r replaced by the later %r", _short(earlier), _short(quote))


def _flow_fields(raw: Mapping, out: dict, quoted, row, dropped: list[str], stated: str, *, cells: bool,
                 order=None) -> None:
    """NOTE-10: the flow fields of the reading, checked like the sizes - the quote is the user's, every number is in
    it, the unit is theirs, ranges are sane, the LATEST of two is kept (`order` puts the entries in the order the
    user wrote them), a number named as the one being replaced is no request, contradictions are told."""
    order = order or (lambda rows: rows)
    # a cell count: a target, a ceiling, a floor
    counts: dict[str, tuple[float, str]] = {}
    swapped: dict[str, tuple[str, str]] = {}
    for e in order(_rows_of(raw, "counts")):
        got = quoted(e)
        if got is None:
            continue
        quote = got[0]
        what, value, tag = str(e.get("what") or ""), _f(e.get("value")), f'"{_short(quote)}"'
        if what not in COUNTS:
            dropped.append(f"{tag}: not a cell count this step reads: not applied")
        elif value is None or not _in_quote(value, _counts(quote)):
            dropped.append(f"{tag}: the count {e.get('value')!r} is not in those words: not applied")
        elif _in_quote(value, replaced_numbers(quote)):
            swapped.setdefault(what, (_count(value), quote))
        elif not (COUNT_RANGE[0] <= value <= COUNT_RANGE[1]):
            dropped.append(f"{tag}: {_count(value)} cells is not a mesh size anyone means: not applied")
        else:
            if what in counts:
                _later(counts[what][1], quote)
            counts[what] = (float(round(value)), quote, _seq(e), e.get("_note", 0))
    _replaced_only(swapped, counts, dropped)

    def later_of(a: str, b: str) -> str | None:
        """Of two count fields that disagree, written in DIFFERENT notes, the one written later (the user changed
        their mind); None when one note says both (a contradiction in one breath, told)."""
        if counts[a][1] == counts[b][1] or counts[a][3] == counts[b][3]:
            return None
        return a if counts[a][2] > counts[b][2] else b

    lo, hi, mid = (counts.get(k, (None, "", 0, 0))[0] for k in ("floor", "ceiling", "target"))
    if lo is not None and hi is not None and lo >= hi:
        keep = later_of("floor", "ceiling")
        if keep is None:
            dropped.append(f"at least {_count(lo)} and at most {_count(hi)} cells cannot both hold: neither is applied")
            counts.pop("floor"), counts.pop("ceiling")
        else:
            gone = "ceiling" if keep == "floor" else "floor"
            _later(counts[gone][1], counts[keep][1])
            counts.pop(gone)
        lo, hi = (counts.get(k, (None, "", 0, 0))[0] for k in ("floor", "ceiling"))
    for bound, off in (("ceiling", mid is not None and hi is not None and mid > hi),
                       ("floor", mid is not None and lo is not None and mid < lo)):
        if not off or "target" not in counts or bound not in counts:
            continue
        if later_of("target", bound) == bound or later_of("target", bound) is None:
            if later_of("target", bound) is None:
                dropped.append(f"about {_count(mid)} cells is outside the range the notes give: the range is applied")
            else:
                _later(counts["target"][1], counts[bound][1])
            counts.pop("target")
        else:
            _later(counts[bound][1], counts["target"][1])
            counts.pop(bound)
    for what in COUNTS:
        if what in counts:
            out["cells"][what] = counts[what][0]
            row(f"cells:{what}", counts[what][1], _count_words(what, counts[what][0], cells=cells))

    # the boundary layers
    lay: dict[str, tuple[float, float | None, str, str]] = {}
    swapped = {}
    for e in order(_rows_of(raw, "layers")):
        got = quoted(e)
        if got is None:
            continue
        quote, numbers = got
        what, value, tag = str(e.get("what") or ""), _f(e.get("value")), f'"{_short(quote)}"'
        unit = str(e.get("unit") or "none").strip().lower()
        if what not in LAYERS:
            dropped.append(f"{tag}: not a boundary-layer setting this step reads: not applied")
            continue
        if value is None or value < 0 or not (_in_quote(value, numbers) or (what == "count" and value == 0
                                                                          and re.search(r"\b(no|without|zero)\b",
                                                                                        quote, re.I))):
            dropped.append(f"{tag}: the number {e.get('value')!r} is not in those words: not applied")
            continue
        if value and _in_quote(value, replaced_numbers(quote)):
            swapped.setdefault(what, (_g(value), quote))
            continue
        mm = None
        if what == "count":
            if value != int(value) or value > LAYER_COUNT_MAX:
                dropped.append(f"{tag}: {_g(value)} is not a layer count anyone means: not applied")
                continue
        elif what in ("first_height", "total_thickness"):
            mm, unit = _length_mm(value, unit, quote, stated)
            if not (SIZE_RANGE_MM[0] <= mm <= SIZE_RANGE_MM[1]) or value <= 0:
                dropped.append(f"{tag}: {_g(mm)} mm is not a layer thickness anyone means: not applied")
                continue
        elif what == "growth":
            if not (LAYER_GROWTH_RANGE[0] < value <= LAYER_GROWTH_RANGE[1]):
                dropped.append(f"{tag}: a layer growth of {_g(value)} is not between {_g(LAYER_GROWTH_RANGE[0])} and "
                               f"{_g(LAYER_GROWTH_RANGE[1])}: not applied")
                continue
        elif not (Y_PLUS_RANGE[0] <= value <= Y_PLUS_RANGE[1]):
            dropped.append(f"{tag}: a y+ of {_g(value)} is not one anyone aims at: not applied")
            continue
        if what in lay:
            _later(lay[what][3], quote)
        lay[what] = (float(value), mm, unit, quote)
    _replaced_only(swapped, lay, dropped)
    if "count" in lay and int(lay["count"][0]) == 0 and len(lay) > 1:
        dropped.append("the notes ask for no boundary layers and also set them: no layers is kept, the rest is not "
                       "applied")
        lay = {"count": lay["count"]}
    first_mm = lay["first_height"][1] if "first_height" in lay else None
    total_mm = lay["total_thickness"][1] if "total_thickness" in lay else None
    if first_mm is not None and total_mm is not None and first_mm >= total_mm:
        dropped.append(f"the first layer ({_g(first_mm)} mm) is not thinner than the whole stack ({_g(total_mm)} mm): "
                       "the stack's thickness is not applied")
        lay.pop("total_thickness")
    keys = {"count": "count", "first_height": "first_mm", "total_thickness": "total_mm", "growth": "growth",
            "y_plus": "y_plus"}
    for what in LAYERS:
        if what not in lay:
            continue
        value, mm, unit, quote = lay[what]
        out["layers"][keys[what]] = int(value) if what == "count" else float(mm if mm is not None else value)
        words = _layer_words(what, value, mm, unit)
        if what in ("first_height", "total_thickness") and unit == "none":
            words += " (no unit was written: read as millimetres)"
        row(f"layers:{what}", quote, words)

    # a size or a refinement in one named place
    for e in _rows_of(raw, "local"):
        got = quoted(e)
        if got is None:
            continue
        quote, numbers = got
        where = " ".join(str(e.get("where") or "").split())[:120]
        what, value, tag = str(e.get("what") or ""), _f(e.get("value")), f'"{_short(quote)}"'
        unit = str(e.get("unit") or "none").strip().lower()
        if not where or what not in LOCAL:
            dropped.append(f"{tag}: no place named for it: not applied")
            continue
        mm = None
        if what == "size":
            if value is None or value <= 0 or not _in_quote(value, numbers):
                dropped.append(f"{tag}: the number {e.get('value')!r} is not in those words: not applied")
                continue
            mm, unit = _length_mm(value, unit, quote, stated)
            if not (SIZE_RANGE_MM[0] <= mm <= SIZE_RANGE_MM[1]):
                dropped.append(f"{tag}: {_g(mm)} mm is not a cell size anyone means: not applied")
                continue
        words = _local_words(where, what, mm, unit, value or 0.0, cells=cells)
        if what == "size" and unit == "none":
            words += " (no unit was written: read as millimetres)"
        rid = row("local", quote, words)
        out["local"].append({"id": rid, "where": where, "what": what, "mm": mm, "quote": quote})

    # the flow's own numbers (a y+ needs them): kept only as written, never a row
    ph = out["physics"]
    for e in _rows_of(raw, "physics"):
        quote = " ".join(str(e.get("quote") or "").split())[:400]
        what, value = str(e.get("what") or ""), _f(e.get("value"))
        unit = str(e.get("unit") or "none").strip().lower()
        got = quoted(e) if quote else None
        if got is None:
            continue
        if what in ("air", "water"):
            ph["fluid"] = ph["fluid"] or what
            continue
        if value is None or value <= 0 or not _in_quote(value, got[1] + (_counts(got[0]) if what == "reynolds" else [])):
            continue
        if what == "velocity" and unit in SPEEDS and ph["velocity_ms"] is None:
            ph["velocity_ms"] = float(value) * SPEEDS[unit]
        elif what == "length" and ph["length_m"] is None:
            ph["length_m"] = _length_mm(value, unit, got[0], stated)[0] / 1000.0
        elif what == "kinematic_viscosity" and unit in ("m2/s", "none") and 1e-8 < value < 1e-2 \
                and ph["nu_m2s"] is None:
            ph["nu_m2s"] = float(value)
        elif what == "reynolds" and ph["reynolds"] is None and value >= 1.0:
            ph["reynolds"] = float(value)


_LAYER_NAMES = {"count": "layer count", "first_height": "first layer height", "total_thickness": "layer stack "
                "thickness", "growth": "layer growth", "y_plus": "y+"}


def _rows_of(raw: Mapping, key: str) -> list[dict]:
    v = raw.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise TypeError(f"{key} is not a list")
    return [x for x in v if isinstance(x, dict)]


def _check(arguments: str, notes: str, route: str = "") -> tuple[dict, list[str], list[str]]:
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return empty(), ["the reading was not JSON: nothing applied"], []
    if not isinstance(raw, dict):
        return empty(), ["the reading was not an object: nothing applied"], []
    flow = route == FLOW
    said, squashed = _norm(notes), _squash(notes)
    out = empty()
    dropped: list[str] = []
    covered: list[str] = []          # every quote that was read: what is left of the notes is looked at below
    stated = str(raw.get("stated_unit") or "none").strip().lower()
    stated = stated if stated in UNITS_MM else "none"

    def quoted(e: Mapping) -> tuple[str, list[float]] | None:
        quote = " ".join(str(e.get("quote") or "").split())[:400]
        if not _quoted(quote, said, squashed):
            near = _locate(quote, notes) if quote else None
            if near is None:
                dropped.append(f'"{_short(quote)}" is not in your words as written: not applied' if quote
                               else "an entry with no quote: not applied")
                return None
            quote = near[:400]
        covered.append(quote)
        return quote, _numbers(quote)

    made = [0]

    def row(field: str, quote: str, asked: str) -> str:
        made[0] += 1
        rid = f"r{made[0]}"
        out["rows"].append({"id": rid, "field": field, "quote": quote, "asked": asked})
        return rid

    def unrow(*fields: str, rid: str = "") -> None:
        """A value replaced by a later one (NOTE-7, the latest wins): its row goes (ids are never reused)."""
        out["rows"] = [r for r in out["rows"] if r["field"] not in fields and r["id"] != rid]

    def at(e: Mapping) -> int:
        """Where the user wrote an entry's quote in the notes (their notes in the order they were written), so the
        latest of two is the one kept; an entry whose quote is not found keeps the model's order after the rest."""
        q = _squash(" ".join(str(e.get("quote") or "").split()))
        i = squashed.find(q) if len(q) >= 2 else -1
        if i < 0:
            near = _locate(str(e.get("quote") or ""), notes)
            i = squashed.find(_squash(near)) if near else -1
        return i if i >= 0 else len(squashed) + 1

    # where each of the user's notes starts, in the same squashed text (a note is one message or one box entry)
    starts, run = [], 0
    for block in re.split(r"(?=\[n\d+\] \()", str(notes or "")):
        starts.append(run)
        run += len(_squash(block))

    def order(rows: list[dict]) -> list[dict]:
        """The entries in the order the user wrote them (ties: the model's order), each marked with that rank and
        the note it was written in."""
        ranked = sorted(enumerate(rows), key=lambda p: (at(p[1]), p[0]))
        for n, (_i, e) in enumerate(ranked):
            e["_seq"] = n
            e["_note"] = bisect.bisect_right(starts, at(e)) - 1
        return [e for _i, e in ranked]

    # sizes: the global size first (the others may be multiples of it), each kind in the order written
    sizes = order(_rows_of(raw, "sizes"))
    sizes_swapped: dict[str, tuple[str, str]] = {}
    pending: list[tuple[str, float, float | None, str, float, str]] = []
    for e in sorted(sizes, key=lambda e: 0 if str(e.get("what")) == "global" else 1):
        got = quoted(e)
        if got is None:
            continue
        quote, numbers = got
        what, value, unit = str(e.get("what") or ""), _f(e.get("value")), str(e.get("unit") or "none").strip().lower()
        tag = f'"{_short(quote)}"'
        if what not in SIZES or unit not in SIZE_UNITS:
            dropped.append(f"{tag}: not a size this step reads: not applied")
            continue
        if _COUNTED.search(quote):
            # "3 elements through the thickness" counts elements; it is no length (read once as a 3 inch wall)
            dropped.append(f"{tag}: a count of elements through or across a wall is not a size: not applied")
            continue
        if value is None or value <= 0 or not _in_quote(value, numbers):
            dropped.append(f"{tag}: the number {e.get('value')!r} is not in those words: not applied")
            continue
        if _in_quote(value, replaced_numbers(quote)):
            sizes_swapped.setdefault(what, (_g(value), quote))
            continue
        key = {"global": "global_mm", "min_edge": "min_edge_mm", "max_edge": "max_edge_mm", "growth": "growth",
               "thickness": "thickness_mm", "near_wall": "near_wall_mm"}[what]
        if what == "growth":
            if not (GROWTH_RANGE[0] < value <= GROWTH_RANGE[1]):
                dropped.append(f"{tag}: a growth of {_g(value)} is not between {_g(GROWTH_RANGE[0])} and "
                               f"{_g(GROWTH_RANGE[1])}: not applied")
                continue
            if out["sizes"]["growth"] is not None:
                _later(next((r["quote"] for r in out["rows"] if r["field"] == "growth"), ""), quote)
                unrow("growth")
            out["sizes"]["growth"] = float(value)
            row("growth", quote, _size_words("growth", 0.0, None, "none", value, cells=flow))
            continue
        multiple = None
        if unit == "x_global":
            if what in ("global", "thickness"):
                dropped.append(f"{tag}: a {what} size cannot be a multiple of the global size: not applied")
                continue
            if out["sizes"]["global_mm"] is None:
                dropped.append(f"{tag}: it is a multiple of the global size, and no global size was given: not applied")
                continue
            if not (MULTIPLE_RANGE[0] <= value <= MULTIPLE_RANGE[1]):
                dropped.append(f"{tag}: {_g(value)} times the global size is not a size anyone means: not applied")
                continue
            multiple, mm = value, value * float(out["sizes"]["global_mm"])
        else:
            # THE UNIT IS THE USER'S: the one written right beside the number in their words; else the one the
            # model read; else the one the notes give once for all; else millimetres, and said
            used = _unit_beside(value, quote) or (unit if unit != "none" else stated)
            mm = value * UNITS_MM[used if used != "none" else "mm"]
            if not (SIZE_RANGE_MM[0] <= mm <= SIZE_RANGE_MM[1]):
                dropped.append(f"{tag}: {_g(mm)} mm is not an element size anyone means: not applied")
                continue
            unit = used
        earlier = next((p for p in pending if p[0] == what), None)
        if earlier is not None:
            # THE LATEST ONE WINS (NOTE-7): "mesh at 5 mm. actually mesh at 8 mm" is 8 mm
            _later(earlier[5], quote)
            pending.remove(earlier)
        out["sizes"][key] = float(mm)
        pending.append((what, mm, multiple, unit, value, quote))
    _replaced_only(sizes_swapped, {p[0] for p in pending} | ({"growth"} if out["sizes"]["growth"] is not None
                                                              else set()), dropped)
    s = out["sizes"]
    # contradictions: the shortest edge over the longest, or the global size outside them
    lo, hi, glob = s["min_edge_mm"], s["max_edge_mm"], s["global_mm"]
    bad: set[str] = set()
    if lo is not None and hi is not None and lo >= hi:
        dropped.append(f"the shortest edge ({_g(lo)} mm) is not under the longest ({_g(hi)} mm): neither is applied")
        bad |= {"min_edge", "max_edge"}
    if glob is not None and lo is not None and "min_edge" not in bad and lo > glob:
        dropped.append(f"the shortest edge ({_g(lo)} mm) is over the global size ({_g(glob)} mm): the shortest "
                       "edge is not applied")
        bad.add("min_edge")
    if glob is not None and hi is not None and "max_edge" not in bad and hi < glob:
        dropped.append(f"the longest edge ({_g(hi)} mm) is under the global size ({_g(glob)} mm): the longest "
                       "edge is not applied")
        bad.add("max_edge")
    for what in bad:
        s[f"{what}_mm"] = None
    for what, mm, multiple, unit, value, quote in pending:
        if what in bad:
            continue
        words = _size_words(what, mm, multiple, unit, value, cells=flow)
        if unit == "none":
            words += " (no unit was written: read as millimetres)"
        row(what, quote, words)
    # NOTE-10: the count, the boundary layers and the sizes in a place - read the same on every route (the route
    # that applies the set says what it does with each, and what it cannot)
    _flow_fields(raw, out, quoted, row, dropped, stated, cells=flow, order=order)

    # the element kind - of two, the one written later (NOTE-7)
    for e in order(_rows_of(raw, "element_kind")):
        got = quoted(e)
        if got is None:
            continue
        quote, numbers = got
        what, tag = _kind_said(str(e.get("what") or ""), quote), f'"{_short(quote)}"'
        k = out["kind"]
        if what not in KINDS:
            dropped.append(f"{tag}: not an element kind this step reads: not applied")
        elif what in ("quads_only", "triangles_only"):
            want = "quads" if what == "quads_only" else "triangles"
            if k["shell"] != want:
                if k["shell"] is not None:
                    _later(next((r["quote"] for r in out["rows"] if r["field"] in ("quads_only", "triangles_only")),
                                ""), quote)
                    unrow("quads_only", "triangles_only")
                k["shell"] = want
                row(what, quote, KIND_WORDS[want])
        elif what in PATTERNS:
            # SHMESH-43: the pattern - the one written later; "no washers" beside a free mesh is part of it
            if k.get("structure") == "free" and PATTERNS[what] == "no_washers":
                continue
            if k.get("structure") != PATTERNS[what]:
                if k.get("structure") is not None:
                    _later(next((r["quote"] for r in out["rows"] if r["field"] in PATTERNS), ""), quote)
                    unrow(*PATTERNS)
                k["structure"] = PATTERNS[what]
                row(what, quote, {"free": "a free mesh: no washers round holes, no mapped faces",
                                  "no_washers": "no washers round holes",
                                  "structured": "washers round holes and mapped quads where a face allows"}[PATTERNS[what]])
        elif what == "max_triangle_percent":
            p = _f(e.get("value"))
            if p is None or not (0.0 <= p <= 100.0) or not _in_quote(p, numbers):
                dropped.append(f"{tag}: the percent {e.get('value')!r} is not in those words: not applied")
            elif _in_quote(p, replaced_numbers(quote)):
                continue
            else:
                if k["tria_percent_max"] is not None:
                    _later(next((r["quote"] for r in out["rows"] if r["field"] == what), ""), quote)
                    unrow(what)
                k["tria_percent_max"] = float(p)
                row(what, quote, f"at most {_g(p)}% triangles")
        elif k["order"] != what:
            if k["order"] is not None:
                _later(next((r["quote"] for r in out["rows"] if r["field"] in ("linear", "quadratic")), ""), quote)
                unrow("linear", "quadratic")
            k["order"] = what
            row(what, quote, f"{what} elements")
    rescued = ""
    if out["kind"]["shell"] is None and out["kind"]["tria_percent_max"] is None:
        # NOTE-11: a shell element kind the user plainly wrote ("Shell elements: triangles", "tri mesh") that the
        # model put in no field is read by the fixed phrases - the same field, from the user's own line
        got_kind = _kind_by_words(notes)
        if got_kind is not None:
            want, rescued = got_kind
            out["kind"]["shell"] = want
            covered.append(rescued)
            row("quads_only" if want == "quads" else "triangles_only", rescued, KIND_WORDS[want])
    if out["kind"]["shell"] == "quads" and out["kind"]["tria_percent_max"] is not None:
        # "quads, triangles under 5%": the share is the ask, said once (quads only would read as none at all)
        out["rows"] = [r for r in out["rows"] if r["field"] != "quads_only"]
        out["kind"]["shell"] = None
    if out["kind"]["shell"] == "triangles" and out["kind"]["tria_percent_max"] is not None:
        dropped.append("triangles only and a most share of triangles were both asked: the share is not applied")
        out["rows"] = [r for r in out["rows"] if r["field"] != "max_triangle_percent"]
        out["kind"]["tria_percent_max"] = None

    # the quality limits - of two for the same elements, the one written later (NOTE-7)
    seen: dict[tuple[str, str, str], dict] = {}
    limits_swapped: dict = {}
    for e in order(_rows_of(raw, "limits")):
        got = quoted(e)
        if got is None:
            continue
        quote, numbers = got
        tag = f'"{_short(quote)}"'
        measure, shape, direction = str(e.get("measure") or ""), str(e.get("shape") or "any"), str(e.get("direction") or "")
        target = _f(e.get("target"))
        if measure not in MEASURES or shape not in SHAPES or direction not in ("max", "min"):
            dropped.append(f"{tag}: not a quality limit this step reads: not applied")
            continue
        directions, (low, high), unit, name = MEASURES[measure]
        if target is None or not _in_quote(target, numbers):
            dropped.append(f"{tag}: the limit {e.get('target')!r} is not in those words: not applied")
            continue
        if measure == "min_volume" and re.search(r"mm\s*(?:\^\s*3|³|3\b)", quote, re.I):
            target = target * 1e-9           # the user's cubic millimetres, in the cubic metres the meshers hold
        if direction not in directions:
            dropped.append(f"{tag}: {name} can only be held {'under' if directions[0] == 'max' else 'over'} a value, "
                           f"and this asks for it {'under' if direction == 'max' else 'over'} one: not applied")
            continue
        if not (low <= target <= high) or (target in (low, high) and measure not in _ENDS_SANE
                                           and not (measure == "aspect_ratio" and target > low)):
            dropped.append(f"{tag}: {_g(target)}{unit} is not a {name} limit anyone means: not applied")
            continue
        if _in_quote(float(_f(e.get("target")) or 0.0), replaced_numbers(quote)):
            # "warpage under 10 instead of 15": the 15 is the value being replaced
            limits_swapped.setdefault((measure, shape, direction), (f"{_g(target)}{unit}", quote))
            continue
        earlier = seen.get((measure, shape, direction))
        if earlier is not None:
            _later(str(earlier.get("quote") or ""), quote)
            unrow(rid=str(earlier.get("id") or ""))
            out["limits"].remove(earlier)
        share, outer = 0.0, None
        pct = _f(e.get("allowed_percent")) or 0.0
        has_outer = bool(e.get("has_outer")) and _f(e.get("outer")) is not None
        if pct:
            # the share is in the user's words, as written or as its complement ("for 99% of elements" allows 1%)
            if not (0.0 < pct <= MAX_ALLOWED_PERCENT) or not (_in_quote(pct, numbers)
                                                              or _in_quote(100.0 - pct, numbers)):
                dropped.append(f"{tag}: the allowed share {_g(pct)}% is not in those words (or not a share anyone "
                               "means): the limit is applied with no allowance")
                has_outer = False
            else:
                share = pct / 100.0
        if has_outer and share > 0:
            o = float(e["outer"])
            past = o > target if direction == "max" else o < target
            if not _in_quote(o, numbers) or not past or not (low <= o <= high):
                dropped.append(f"{tag}: the outer limit {_g(o)}{unit} is not past the target {_g(target)}{unit} (or "
                               "not in those words): the limit is applied with no allowance")
                share = 0.0
            else:
                outer = o
        lim = {"measure": measure, "shape": shape, "op": "<=" if direction == "max" else ">=", "target": float(target),
               "share": float(share), "outer": outer, "quote": quote}
        lim["id"] = row(f"limit:{measure}", quote, limit_words(lim, cells=flow))
        out["limits"].append(lim)
        seen[(measure, shape, direction)] = lim
    _replaced_only(limits_swapped, seen, dropped)

    # what fits no field (NOTE-8): told, never silently dropped
    not_applied: list[str] = []
    typed = list(covered)                # the quotes read into a field: a line told "not applied" is what is left
    for e in _rows_of(raw, "other"):
        quote = " ".join(str(e.get("quote") or "").split())[:400]
        covered.append(quote)
        if rescued and kind_in_words(quote):
            continue        # NOTE-11: read above as the element kind
        if asks_nothing(quote):
            continue        # NOTE-8: a yes or a verdict ("the rest looks good") asks nothing, so nothing is left out
        if str(e.get("kind")) == "mesh" and _quoted(quote, said, squashed) and quote not in not_applied:
            over = [t for t in typed if t and (_squash(t) in _squash(quote) or _squash(quote) in _squash(t))]
            if over and not _left_over(quote, over):
                continue                 # every mesh word of it was read into a field already
            not_applied.append(quote)
    # NOTE-8's floor: words about the mesh that the reading listed NOWHERE are told too, as the user wrote them
    for piece in _left_over(notes, covered):
        if piece not in not_applied and not (rescued and kind_in_words(piece)):
            not_applied.append(piece)
    return out, dropped, not_applied[:20]


# #
# the section, its words and its why-record entries
# #

UNREAD_WORDS = ("If your notes asked for element sizes or quality limits for the whole mesh, they were not applied "
                "this run: the AI step that reads them gave no answer. This is our rules' own mesh.")


def section(status: str, *, requirements: Mapping | None = None, dropped=None, not_applied=None, note: str = "",
            key: str = "", source: str = "") -> dict:
    """The reading as every route carries it: status "none" (no words, or the step off), "used" (the checked set,
    maybe empty), or why nothing was applied ("failed", "invalid", "no_time")."""
    return {"status": status, "requirements": dict(requirements) if requirements else empty(),
            "dropped": list(dropped or []), "not_applied": list(not_applied or []), "note": note, "key": key,
            "source": source}


def requirements_of(sec: Mapping | None) -> dict | None:
    """The set a route applies: None when nothing was read or nothing asked (the route runs as it does with no
    notes), else the checked set."""
    if not sec or sec.get("status") != "used":
        return None
    req = sec.get("requirements") or {}
    return None if is_empty(req) else dict(req)


def not_applied_words(sec: Mapping | None) -> list[str]:
    """The lines of words() that say what was NOT applied (NOTE-8): they stay with the result."""
    return [w for w in words(sec) if w.startswith("Not applied")] if sec and sec.get("status") == "used" else []


def write(workspace, sec: Mapping | None) -> bool:
    """Keep the reading in the workspace when it asks for something or has something to tell (what was not
    applied); remove a carried file when it has neither (a run with no notes leaves the workspace exactly as it
    always was). True when written. Never raises."""
    from pathlib import Path

    path = Path(workspace) / FILE
    try:
        # (a pick on the stage, STD-4, is kept too: the result says which elements were made and why)
        if requirements_of(sec) is None and not not_applied_words(sec) and not (sec or {}).get("stage"):
            path.unlink(missing_ok=True)
            return False
        path.write_text(json.dumps(sec, indent=1, sort_keys=True), encoding="utf-8")
        return True
    except OSError:
        logger.warning("mesh requirements: could not keep %s", path, exc_info=True)
        return False


def read(workspace) -> dict:
    """The reading kept in the workspace, {} when there is none."""
    from pathlib import Path

    try:
        got = json.loads((Path(workspace) / FILE).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def words(sec: Mapping | None) -> list[str]:
    """Before meshing, in plain words: what was read from the notes, one short line each; then what was not applied."""
    if not sec or sec.get("status") == "none":
        return []
    if isinstance(sec.get("lines"), list):
        # merged with the team's meshing standard (engines/meshing_standard): said by where each came from
        return [str(w) for w in sec["lines"]]
    if sec.get("status") != "used":
        return [str(sec.get("note") or UNREAD_WORDS)]
    out = [f"From your notes: {r['asked']}." for r in (sec.get("requirements") or {}).get("rows") or []]
    if sec.get("source") == "saved" and sec.get("note"):
        out.append(str(sec["note"]))
    out += [f"Not applied: {d}." for d in sec.get("dropped") or []]
    out += [f'Not applied: "{_short(q)}" - no mesh setting of ours matches it yet.' for q in sec.get("not_applied") or []]
    return out


def decisions(sec: Mapping | None, stage: str = "notes") -> list[dict]:
    """The why-record entries (contracts/decision_record.cite shape, source ai-lever)."""
    if not sec or sec.get("status") == "none":
        return []
    from meshpipeline.contracts.decision_record import SOURCE_AI_LEVER

    if sec.get("status") != "used":
        return [{"stage": stage, "decided": "mesh requirements not read", "value": sec.get("status"),
                 "rule": RULE_NOTHING, "inputs": {"note": sec.get("note"), "saved": sec.get("source") or ""},
                 "words": UNREAD_WORDS}]
    req = sec.get("requirements") or {}
    out = [{"stage": stage, "decided": "mesh requirements read", "value": len(req.get("rows") or []),
            "rule": RULE_READ, "source": SOURCE_AI_LEVER,
            "inputs": {"sizes": req.get("sizes"), "kind": req.get("kind"), "cells": req.get("cells"),
                       "layers": req.get("layers"), "local": req.get("local"),
                       "limits": [{k: lim.get(k) for k in ("measure", "shape", "op", "target", "share", "outer", "quote")}
                                  for lim in req.get("limits") or []],
                       "dropped": sec.get("dropped") or [], "key": sec.get("key"), "from": sec.get("source")},
            "words": " ".join(words(sec)[:4]) or "The notes ask for no mesh setting."}]
    if sec.get("not_applied") or sec.get("dropped"):
        out.append({"stage": stage, "decided": "notes not applied",
                    "value": len(sec.get("not_applied") or []) + len(sec.get("dropped") or []),
                    "rule": RULE_NOT_APPLIED, "source": SOURCE_AI_LEVER,
                    "inputs": {"not_applied": sec.get("not_applied") or [], "dropped": sec.get("dropped") or []},
                    "words": "What the notes say about the mesh that was not applied, told to the user."})
    return out


# #
# asked against delivered: what every route's report shares
# #

def pct(n: int, total: int) -> float:
    return float(f"{100.0 * n / total:.3g}") if total else 0.0


def share_words(n: int, total: int) -> str:
    return f"{pct(n, total):g}% of elements ({n:,})"


def edge_words(field: str, outside: int, n: int, limit: float, worst: float) -> str:
    """An edge-length target that some elements are outside of, in plain words: a target, with the share outside."""
    short = field == "min_edge"
    return (f"a target - {share_words(outside, n)} have an edge {'shorter' if short else 'longer'} than {_g(limit)} mm "
            f"(the {'shortest' if short else 'longest'} is {_g(worst)} mm)")


def limit_row(lim: Mapping, values, n: int) -> dict:
    """One quality limit against the built mesh: `values` the figure of every element the limit applies to, `n`
    all the elements of the mesh (the share a limit allows is of all of them). {"applied", "outside" (past the
    target), "outside_pct", "beyond" (past the outer limit), "share", "worst", "met", "delivered"}."""
    _d, _range, unit, _name = MEASURES[str(lim["measure"])]
    target, outer, share = float(lim["target"]), lim.get("outer"), float(lim.get("share") or 0.0)
    least = lim["op"] == ">="
    outside = int((values < target).sum() if least else (values > target).sum())
    beyond = int((values < outer).sum() if least else (values > outer).sum()) if outer is not None else 0
    worst = float(values.min() if least else values.max()) if len(values) else None
    out: dict = {"applied": True, "outside": outside, "outside_pct": pct(outside, n), "beyond": beyond,
                 "share": share, "worst": float(f"{worst:.6g}") if worst is not None else None,
                 "limit_target": target, "limit_outer": float(outer) if outer is not None else None,
                 "met": bool(outside <= share * n + 1e-9 and beyond == 0)}
    side = "under" if least else "over"
    if not outside:
        out["delivered"] = "every element is inside it"
    else:
        said = f"{share_words(outside, n)} are {side} {_g(target)}{unit}"
        if outer is not None:
            said += f", {beyond:,} of them {side} {_g(outer)}{unit}" if beyond else f", none {side} {_g(outer)}{unit}"
        out["delivered"] = said + (f"; the worst is {_g(worst)}{unit}" if worst is not None else "")
    return out


def finish_report(rows: list[dict], n: int, definitions: str) -> dict:
    """The report every route gives: its rows, each with the elements outside what was asked (`excess`: 0 when
    met - what the result's row and the verdict read), how many were met and missed, and how the figures are
    measured."""
    for r in rows:
        r["excess"] = 0 if r["met"] is not False else max(
            1, int(r.get("beyond") or 0),
            int(r.get("outside") or 0) - int(math.floor(float(r.get("share") or 0.0) * n + 1e-9)))
    return {"rows": rows, "met": sum(1 for r in rows if r["met"] is True),
            "missed": sum(1 for r in rows if r["met"] is False), "definitions": definitions}


#: NOTE-10: the fields the flow route reads that a solid or a shell mesh answers on its own terms
FLOW_FIELDS = ("near_wall", "local", "cells:target", "cells:ceiling", "cells:floor", *(f"layers:{w}" for w in LAYERS))


def elements_row(field: str, req: Mapping, n: int, kind: str) -> dict | None:
    """A flow field (FLOW_FIELDS) on a solid or a shell mesh of `n` elements: a count is measured against what
    was asked; a size in a place is the sizes-per-part step's; boundary layers and a size at the walls are not
    built on this kind of mesh - told. None for any other field."""
    if field.startswith("cells:"):
        asked = float(((req.get("cells") or {}).get(field.split(":", 1)[1])) or 0.0)
        what = field.split(":", 1)[1]
        met = (n <= asked) if what == "ceiling" else (n >= asked) if what == "floor" else None
        return {"applied": True, "measured": int(n), "met": met, "delivered": f"the mesh has {n:,} elements"}
    if field == "local":
        return {"applied": True, "met": None,
                "delivered": "sizes in a named place are set part by part (see the lines about your parts)"}
    if field == "near_wall" or field.startswith("layers:"):
        return {"applied": False, "met": None,
                "delivered": f"not applied - a {kind} mesh has no boundary layers or wall cells; they are a "
                             "CFD setting"}
    return None


def line(row: Mapping) -> str:
    """One requirement as the user reads it after the mesh: what was asked, then what was delivered. Never "met"
    unless it was measured and reached."""
    asked = str(row.get("asked") or "")
    asked = asked[:1].upper() + asked[1:]
    if row.get("from") == "stage":
        # the shell line's own choice (engines/meshing_standard STD-4)
        asked += " (chosen on the stage)"
    elif row.get("from"):
        # a run with a team meshing standard (engines/meshing_standard): each row says where it came from
        asked += f" (from your {row['from']})"
    if row.get("met") is True:
        tail = str(row.get("delivered") or "")
        return f"{asked}: met" + (f" - {tail}." if tail and tail != "every element is inside it" else ".")
    if row.get("met") is False:
        return f"{asked}: not fully met - {row.get('delivered')}."
    return f"{asked}: {row.get('delivered')}."


def report_words(rep: Mapping | None) -> list[str]:
    """A report in plain words, one line per requirement ([] with none)."""
    return [line(r) for r in (rep or {}).get("rows") or []]


def asked_rows(quality: Mapping | None, *, source: str = "your notes") -> list[dict]:
    """THE USER'S OWN REQUIREMENTS as rows of the result's and the verdict's one list of limits (quality.json's
    "requirements", written by the route that measured them): each measured as the elements outside what was
    asked (0 when met), with the plain line that says asked against delivered (`said`). One not reached is a
    note - the mesh is delivered - never a failure; one with nothing to measure it against (applied as a setting,
    or not applied) is no row: it is told in the plan's words."""
    rep = (quality or {}).get("requirements")
    out = []
    for r in (rep.get("rows") if isinstance(rep, Mapping) else None) or []:
        if not isinstance(r, Mapping) or r.get("met") not in (True, False):
            continue
        came = str(r.get("from") or "")
        label = (f"Chosen on the stage: {r.get('asked')}" if came == "stage"
                 else f"Your {came} ask: {r.get('asked')}" if came else f"You asked: {r.get('asked')}")
        out.append({"key": f"asked_{r.get('id')}",
                    "label": label,
                    "op": "==", "limit": 0, "measured": int(r.get("excess") or 0), "said": line(r),
                    "source": ("the stage" if came == "stage" else source.replace("your notes", f"your {came}", 1))
                    if came else source})
    return out


def summary(rep: Mapping | None, *, kind: str = "mesh", limit: int = 380, not_applied: int = 0) -> str:
    """The one line the closing message carries ("From your notes: ..."): how many of the things asked for are
    met, and the ones that are not with how far off each is - as many as fit `limit` characters (the result and
    the mesh report list every one) - and how many things the notes say about the mesh that no setting of ours
    matches (`not_applied`, NOTE-8). "" with nothing to say."""
    rows = list((rep or {}).get("rows") or [])
    extra = (f"{not_applied} other thing{'' if not_applied == 1 else 's'} your notes say about the mesh could not "
             "be applied (listed with the result)") if not_applied else ""
    if not rows:
        return (extra + ".").replace(" other thing", " thing") if extra else ""
    # EVERY LINE THE USER WROTE IS COUNTED, each in one group: met, not fully met, an edge-length target reported
    # with its share, applied as a setting, or not applied to this kind of mesh ("Of the 10 things you asked for:
    # 8 met; 2 edge-length targets: ...") - never "all 8 met" over a list of 10
    met = [r for r in rows if r.get("met") is True]
    missed = [r for r in rows if r.get("met") is False]
    targets = [r for r in rows if r.get("met") is None and r.get("target")]
    aside = [r for r in rows if r.get("met") is None and not r.get("target") and r.get("applied") is False]
    settings = [r for r in rows if r.get("met") is None and not r.get("target") and r.get("applied") is not False]
    n = len(rows)
    out = f"Of the {n} thing{'' if n == 1 else 's'} you asked for: {len(met)} met"
    if missed:
        out += f", {len(missed)} not fully met"
        shown = 0
        for r in missed:
            more = f"{': ' if not shown else '; '}{r['asked']} ({r['delivered']})"
            if len(out) + len(more) > limit - 90:
                break
            out, shown = out + more, shown + 1
        if shown < len(missed):
            out += f"{'; and ' if shown else ': '}{len(missed) - shown} more, listed on the result"
    if targets:
        shares = [f"{r.get('outside_pct'):g}% of elements have an edge "
                  f"{'shorter' if r.get('field') == 'min_edge' else 'longer'} than you asked"
                  for r in targets if r.get("outside")]
        out += (f"; {len(targets)} edge-length target{'' if len(targets) == 1 else 's'}"
                + (": " + ", ".join(shares) if shares else ""))
    if settings:
        out += f"; {len(settings)} applied as {'a setting' if len(settings) == 1 else 'settings'}"
    if aside:
        out += f"; {len(aside)} could not be applied to a {kind} mesh (listed on the result)"
    if extra:
        out += f". {extra}"
    return out + "."


#: the most lines said live before meshing (the rest are in the plan's words on the result)
MAX_LIVE = 14
_LIVE = (("=", " is "), ("<", " under "), (">", " over "), ("{", "("), ("}", ")"), ("`", "'"), ("_", " "))


def live_words(sec: Mapping | None) -> list[str]:
    """words(), fit for the live commentary (contracts/narration's guard takes no "=", "<", ">" or snake_case: a
    quote of the user's own line is said with those spelled out). At most MAX_LIVE lines."""
    out = []
    for w in words(sec):
        for a, b in _LIVE:
            w = w.replace(a, b)
        out.append(" ".join(w.split()))
    if len(out) > MAX_LIVE:
        out = [*out[:MAX_LIVE - 1], f"And {len(out) - MAX_LIVE + 1} more from your notes, listed with the result."]
    return out


# #
# asking
# #

def _args_of(rr) -> str | None:
    for c in getattr(rr, "tool_calls", ()) or ():
        if getattr(c, "name", "") == TOOL:
            return str(getattr(c, "arguments", "") or "")
    return None


def _choice(t: dict) -> dict:
    return {"type": "function", "function": {"name": t["function"]["name"]}}


def _key(messages: list[dict], t: dict) -> str:
    return hashlib.sha256(json.dumps({"m": messages, "t": t}, sort_keys=True).encode()).hexdigest()[:16]


async def decide(state: Mapping, *, route: str = "", job_id: str = "", run=None, enabled: bool = True,
                 wait_s: float = TIMEOUT_S) -> dict:
    """The reading for this run: section "none" (no words to read, or the step off - no model asked, NOTE-9),
    "used", or why nothing was applied. Never raises (but a fenced worker's stale publish)."""
    if not enabled or not wanted(state):
        return section("none")
    msgs = messages_for(state, route=route)
    t = tool()
    key = _key(msgs, t)
    notes = notes_text(state)
    user_id = str((state or {}).get("user_id") or "")

    def answered(rr, source: str) -> dict | None:
        args = _args_of(rr) if rr is not None else None
        if args is None:
            return None
        req, dropped, not_applied = check(args, notes, route)
        return section("used", requirements=req, dropped=dropped, not_applied=not_applied, key=key, source=source)

    def without(status: str, why: str) -> dict:
        """The AI gave no usable reading just now: its SAVED reading of these same words when there is one (the
        result never depends on the AI being available), else nothing applied, and said."""
        from meshpipeline.contracts.decision_cache import recorded_answer

        saved = answered(recorded_answer("builder", messages=msgs, tools=[t], tool_choice=_choice(t), job_id=job_id,
                                         user_id=user_id), "saved")
        if saved is not None:
            saved["note"] = f"The AI was not available just now ({why}); its saved reading of these same words was used."
            return saved
        logger.warning("mesh requirements: no reading (%s) and none saved - nothing applied - job_id=%s", why, job_id)
        return section(status, key=key, source="rules (nothing saved)", note=UNREAD_WORDS)

    if wait_s < MIN_WAIT_S:
        return without("no_time", "no time to spare")
    from meshpipeline.contracts import execution_guard as _fence
    from meshpipeline.contracts import model_inference as llm_router

    # FENCE: a fenced worker must not spend provider budget on a run a newer generation owns
    await _fence.assert_still_owner("mesh requirements")
    try:
        rr = await asyncio.wait_for(llm_router.call_builder_model(
            msgs, tools=[t], tool_choice=_choice(t), job_id=job_id, user_id=user_id),
            timeout=min(TIMEOUT_S, float(wait_s)))
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the AI never fails a run
        return without("failed", type(exc).__name__)
    note = getattr(run, "note_plan_round", None)
    if callable(note):
        note(rr)
    if getattr(rr, "failure_marker", ""):
        return without("failed", str(rr.failure_marker))
    got = answered(rr, "model")
    return got if got is not None else without("invalid", "it gave no reading")


__all__ = ["CONSUMER", "FILE", "KINDS", "KIND_WORDS", "kind_in_words", "MAX_LIVE", "asked_rows", "not_applied_words", "finish_report", "limit_row", "line", "live_words",
           "pct", "report_words", "share_words", "summary", "MEASURES", "RULE_NOTHING", "RULE_NOT_APPLIED", "RULE_READ", "SHAPES", "SIZES", "SYSTEM",
           "TOOL", "UNITS_MM", "UNREAD_WORDS", "check", "decide", "decisions", "empty", "is_empty", "limit_words",
           "messages_for", "notes_text", "read", "requirements_of", "section", "tool", "wanted", "words", "write"]
