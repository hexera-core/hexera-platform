# Responsibility: THE MESH CONTROLS REGISTRY (docs/RULES.md NOTE-13): every control a user's words may set is declared ONCE here - its key, kind, unit, sane range, where it may apply, the words the notes' reader is shown, the routes and stage, what every mesher does with it (an applier, the code that already reads it, or "not here" with why), how it is verified, and its rule - and the reader, the plan seams and the reports all work from this one table.
# Boundaries: pure data and small pure helpers; it imports no engine. Appliers are named by dotted path and resolved where a plan is made (engines/control_appliers.py), so this contract stays below the engines.
# Collaborates with: engines/mesh_requirements.py (the reader's `controls` output, its prompt lines and its checks are generated from CONTROLS), engines/control_appliers.py (the appliers and the one loop each plan seam calls), engines/fea_requirements.py and engines/cfd_requirements.py (the report rows), tests/unit/hygiene/test_mesh_controls_declared.py (every control says what every mesher does with it).
"""THE MESH CONTROLS, DECLARED ONCE.

To add a control:
  1. add a Control to CONTROLS below (key, kind, unit, sane range, prompt line, examples, routes, stage, rule, and
     what EVERY lane in LANES does with it: an applier's dotted name, Legacy(where) when existing code already reads
     it, or NotHere(why));
  2. write each applier in engines/control_appliers.py: fn(target: dict, value) -> (status, built words), status
     "applied" or "nearest"; it changes `target` (the plan seam's values) in place;
  3. if a plan seam does not run the loop yet, call control_appliers.apply(lane, requirements, target) there;
  4. add a test, and the rule in docs/RULES.md.
The reader's tool schema, its prompt lines, the checks, the "not on this mesher" lines and the report rows follow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RULE = "NOTE-13"

#: THE LANES a control must speak for: every mesher of user_notes.ENGINE_CONSUMERS, gmsh once per route it meshes
LANES: dict[str, tuple[str, str]] = {
    "fea_solid": ("gmsh", "fea"), "shell": ("gmsh", "fea"), "gmsh_flow": ("gmsh", "flow"),
    "snappy": ("snappy", "flow"), "cfmesh": ("cfmesh", "flow"), "snappy_multiregion": ("snappy_multiregion", "flow"),
    "vmtk": ("vmtk", "flow"), "snapgrid": ("snapgrid", "electronics"), "netgen": ("netgen", "flow"),
}
#: what a Control's `where` may name: the whole mesh, or a feature kind (the selector the shell audit asks for)
WHERE = ("mesh", "hole", "bolt_hole", "fillet", "bend", "flange", "edge", "face", "part", "gap", "wall")
KINDS = ("number", "length", "count", "choice", "flag")
STAGES = ("geometry", "mesh", "delivery")


@dataclass(frozen=True)
class NotHere:
    """This lane has no lever for the control: the row says so, with why."""
    why: str


@dataclass(frozen=True)
class Legacy:
    """Code that predates the registry already reads and applies this control on this lane (named, for the reader)."""
    where: str


@dataclass(frozen=True)
class Control:
    key: str
    kind: str
    unit: str                                   # "length", "count", "ratio", "none"
    sane: tuple[float, float] | None
    prompt: str
    examples: tuple[str, ...]
    routes: tuple[str, ...]
    stage: str
    lanes: dict[str, str | NotHere | Legacy]
    verify: str                                 # a dotted function name, or "setting"
    rule: str
    asked: str                                  # the row's words, "{value}" or "{choice}" filled in
    choices: tuple[str, ...] = ()
    where: tuple[str, ...] = ("mesh",)
    field: str = ""                             # an existing reader field that already reads it ("" = `controls`)
    integer: bool = False
    #: THE CONTROL'S OWN SUBJECT, in the words people use for it: a `where` made only of these (and words that mean
    #: "all of it": whole_mesh) restates the subject and is the whole-mesh ask ("4 elements across the thickness of
    #: each wall", "30 cells across the pipe"); anything else names one place (place_of)
    subject: tuple[str, ...] = ()


_NO_FLOW_WALLS = NotHere("a flow mesh has cells, not elements through a wall")
_NO_SHELL_WALL = NotHere("a shell is one element through its thickness by design")
_FLOW_LANES = ("gmsh_flow", "snappy", "cfmesh", "snappy_multiregion", "vmtk", "snapgrid", "netgen")
_NOT_SNAPPY = NotHere("this mesher sizes cells across a passage by its own rule; a number of yours is not a setting "
                      "it has yet")

CONTROLS: dict[str, Control] = {c.key: c for c in (
    Control(key="growth", kind="number", unit="ratio", sane=(1.0, 3.0), field="growth",
            prompt="(read as sizes.growth)", examples=("growth rate 1.2",), routes=("fea", "flow"), stage="mesh",
            lanes={"fea_solid": "meshpipeline.engines.control_appliers.fea_growth",
                   "shell": Legacy("engines/shell_requirements.size_plan"),
                   **{lane: Legacy("engines/cfd_requirements (each engine's own growth lever)") for lane in _FLOW_LANES}},
            verify="setting", rule=RULE, asked="sizes grow by at most {value} from one element to the next"),
    Control(key="wall_elements", kind="count", unit="count", sane=(1.0, 20.0), integer=True,
            prompt="elements THROUGH the thickness of every wall of a solid",
            examples=("at least 3 elements through the thickness", "4 elements through every wall"),
            routes=("fea",), stage="mesh",
            lanes={"fea_solid": "meshpipeline.engines.control_appliers.fea_wall_elements", "shell": _NO_SHELL_WALL,
                   **dict.fromkeys(_FLOW_LANES, _NO_FLOW_WALLS)},
            verify="setting", rule=RULE, asked="{value} elements through the thickness of every wall",
            subject=("wall", "walls", "thickness", "thicknesses", "thick", "part", "solid", "body", "plate", "plates",
                     "sheet", "section", "sections", "material")),
    Control(key="file_units", kind="choice", unit="none", sane=None, choices=("mm", "m"),
            prompt="the length unit of the delivered SOLVER FILES (not the unit of the numbers in the notes)",
            examples=("export in mm", "write the deck in millimetres", "Nastran deck in metres"),
            routes=("fea", "flow"), stage="delivery",
            lanes={lane: NotHere("every file is written in metres and says so; files in millimetres are not offered "
                                 "yet") for lane in LANES},
            verify="setting", rule=RULE, asked="solver files in {choice}",
            subject=("file", "files", "deck", "decks", "export", "exports", "output", "outputs", "solver", "mesh")),
    Control(key="cells_across", kind="count", unit="count", sane=(4.0, 200.0), integer=True,
            prompt="CFD: cells ACROSS a passage, bore or gap",
            examples=("30 cells across the pipe", "at least 10 cells across the gap"), routes=("flow",), stage="mesh",
            lanes={"snappy": "meshpipeline.engines.control_appliers.snappy_cells_across",
                   "vmtk": "meshpipeline.engines.control_appliers.vmtk_cells_across",
                   "cfmesh": "meshpipeline.engines.control_appliers.cfmesh_cells_across",
                   **dict.fromkeys(("gmsh_flow", "snappy_multiregion", "snapgrid", "netgen"), _NOT_SNAPPY),
                   "fea_solid": NotHere("a solid has no flow passage"), "shell": NotHere("a shell has no flow passage")},
            verify="setting", rule=RULE, asked="{value} cells across the passage",
            subject=("pipe", "pipes", "bore", "bores", "passage", "passages", "duct", "ducts", "channel", "channels",
                     "tube", "tubes", "inlet", "outlet", "opening", "openings", "port", "ports", "flow", "path",
                     "diameter", "cross", "section", "gap", "gaps", "interior", "inside", "lumen", "vessel",
                     "nozzle", "line", "manifold", "fluid", "domain")),
    Control(key="surface_level", kind="count", unit="count", sane=(0.0, 8.0), integer=True,
            prompt="CFD: the refinement LEVEL at the walls (snappyHexMesh's surface level: each level halves the cell)",
            examples=("surface refinement level 3", "4 levels at the surface"), routes=("flow",), stage="mesh",
            lanes={"snappy": "meshpipeline.engines.control_appliers.snappy_surface_level",
                   **{lane: NotHere("this mesher has no refinement levels") for lane in
                      ("gmsh_flow", "vmtk", "snapgrid", "netgen")},
                   "cfmesh": NotHere("this mesher sizes the wall by a cell size; give a size at the walls instead"),
                   "snappy_multiregion": NotHere("this mesher sets its levels per region from the parts"),
                   "fea_solid": NotHere("a solid mesh has no refinement levels"),
                   "shell": NotHere("a shell mesh has no refinement levels")},
            verify="setting", rule=RULE, asked="refinement level {value} at the walls",
            subject=("wall", "walls", "surface", "surfaces", "body", "bodies", "boundary", "boundaries", "geometry",
                     "part", "skin", "stl")),
    # SHMESH-52: a size (a number, "finer", "coarser") in ONE named place - the reader's own `local` field (NOTE-10);
    # the solid and every flow engine already decide each such row in their own code, the shell through this lane
    Control(key="place_size", kind="length", unit="length", sane=(1e-4, 1e5), field="local",
            prompt="(read as local: a size, finer or coarser in ONE named place)",
            examples=("5 mm elements on the base plate", "finer on the bracket", "3 mm elements around the flange"),
            routes=("fea", "flow"), stage="mesh", where=("part", "face", "hole", "fillet", "flange", "wall", "bend"),
            lanes={"fea_solid": Legacy("engines/fea_part_sizes and engines/fea_plan.user_sized (PLAN-fea-29, 32, 36)"),
                   "shell": "meshpipeline.engines.control_appliers.shell_place_sizes",
                   "snappy": Legacy("engines/cfd_requirements.snappy_levels, snappy_wake, snappy_port_levels"),
                   "cfmesh": Legacy("engines/cfd_requirements.cfmesh_local"),
                   "snappy_multiregion": Legacy("engines/cfd_requirements.multiregion_sizes"),
                   "gmsh_flow": Legacy("engines/cfd_requirements.gmsh_sizes (tells it has no such setting)"),
                   "vmtk": Legacy("engines/cfd_requirements.vmtk_spec (tells it has no such setting)"),
                   "snapgrid": Legacy("engines/cfd_requirements.snapgrid_plan (tells it has no such setting)"),
                   "netgen": Legacy("engines/cfd_requirements.netgen_inputs (tells it has no such setting)")},
            verify="meshpipeline.engines.shell_places.told", rule="SHMESH-52", asked="a size in one named place"),
)}


def read_by_reader() -> list[Control]:
    """The controls the reader's generic `controls` output carries (the rest have a field of their own)."""
    return [c for c in CONTROLS.values() if not c.field]


def by_field(name: str) -> Control | None:
    """The control an existing reader field is (growth, ...), or None."""
    return next((c for c in CONTROLS.values() if c.field == name), None)


def prompt_lines() -> str:
    """The reader's instructions for `controls`, generated from the table."""
    lines = []
    for c in read_by_reader():
        what = f"one of {', '.join(c.choices)} in `choice`" if c.kind == "choice" else "the number in `value`"
        own = (f' `where` stays "" when the words only name its own subject ({", ".join(c.subject[:6])}, '
               '"each", "every", "everywhere"): that is the whole mesh.' if c.subject else "")
        lines.append(f'  key "{c.key}": {c.prompt}; {what} ("' + '", "'.join(c.examples) + '").' + own)
    return "\n".join(lines)


def asked_words(c: Control, value=None, choice: str = "") -> str:
    v = (f"{int(value)}" if c.integer else f"{float(value):g}") if value is not None else ""
    return c.asked.format(value=v, choice=choice)


#: words that mean "all of it" or only join the subject's words ("the thickness of each wall", "everywhere")
WHOLE_MESH = frozenset("the a an each every all any its their of in on at for to across through throughout around along "
                       "within whole entire everywhere everything overall mesh model full complete same both".split())


def place_of(c: Control, where: str) -> str:
    """NOTE-13: the ONE place a control's `where` names, or "" when it only restates the control's own subject (its
    `subject` words and WHOLE_MESH: "each wall", "the wall thickness", "everywhere", "the inlet passage", "the
    bore") - which is the whole-mesh ask. A control that may apply in one place (`where` beyond "mesh") keeps its
    place as written."""
    said = " ".join(str(where or "").split())
    if not said:
        return ""
    words = re.findall(r"[a-z]+", said.lower())
    own = set(c.subject)
    if all(w in WHOLE_MESH or w in own or (w.endswith("s") and w[:-1] in own) for w in words):
        return ""
    return said


def lane_of(lane: str, key: str) -> str | NotHere | Legacy | None:
    c = CONTROLS.get(key)
    return c.lanes.get(lane) if c else None


__all__ = ["CONTROLS", "KINDS", "LANES", "RULE", "STAGES", "WHERE", "WHOLE_MESH", "Control", "Legacy", "NotHere",
           "asked_words", "by_field", "lane_of", "place_of", "prompt_lines", "read_by_reader"]
