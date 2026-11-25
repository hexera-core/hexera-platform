# Responsibility: THE USER'S MESH REQUIREMENTS ON A FLOW (CFD) MESH (docs/RULES.md CFDREQ-1..7): read the notes once per attempt into the one typed set (engines/mesh_requirements, route "flow"), give every CFD engine the same set through one seam, turn each field into that engine's own lever - snapped to what the engine can build, the nearest that works when it cannot, "not applied on this mesher" when it has no lever - and, on the built mesh, say for every requirement what was asked and what was delivered.
# Boundaries: pure over plain data, the workspace's own files and numpy, but read_for_attempt (one model call through engines/mesh_requirements). It never fails a run and never loosens a validity check: a requirement that cannot be met is the nearest that builds, delivered with notes. With nothing asked every function returns its input unchanged - the plan, the dictionaries and the mesh are byte-identical to a run with no notes.
# Collaborates with: agents/builder/invoke.py (reads once per attempt), the CFD engines (engines/snappy, cfmesh, snappy_multiregion, gmsh flow, vmtk, snapgrid: each calls requirements_for / Ask.of where its plan is made), engines/manifest.py (the report into the quality record), engines/verdict_limits.py (the user's rows), engines/cfd_cell_measures.py (per-cell shares).
"""THE USER'S REQUIREMENTS ON A CFD MESH.

    CFDREQ-1  ONE SEAM. The notes are read once per attempt (route "flow", NOTE-10) and kept in the workspace;
              every CFD engine reads the same set through requirements_for(workspace) - nothing else.
    CFDREQ-2  SIZES are snapped to what the engine builds (an octree halves: base / 2^n) and the size built is
              said. A size that would leave the body unsealed, or a mesh no machine we have can hold, is the
              nearest that works, said. Our accuracy minimums give way to the user's size (warned), never our
              validity checks.
    CFDREQ-3  A CELL COUNT the user sets wins over ours, up to the largest machine (settings/policy
              CELL_HARD_LIMIT); a budget of ours gives way to the user's size, with a note.
    CFDREQ-4  BOUNDARY LAYERS: the count, the first layer's height, the stack's thickness, the growth, and a y+
              turned into a first layer height when the flow's speed (or Reynolds number), fluid and length are
              known - else the missing number is named and the layers keep our sizing.
    CFDREQ-5  QUALITY LIMITS tighter than ours go to the mesher where it has the setting (snappyHexMesh's
              meshQualityControls); ours stay where they are tighter. Every limit is measured on the mesh.
    CFDREQ-6  ASKED AGAINST DELIVERED: one row per requirement on the result and in the verdict's limits; a
              missed limit is "delivered with notes", never a failed run.
    CFDREQ-7  NOTHING SAID = IDENTICAL: no notes, or notes that ask nothing, change no byte of any plan,
              dictionary or mesh.
    CFDREQ-8  THE FLOW CONDITIONS the user confirmed (fluid, speed, wall treatment) size the first layer as a y+
              in the notes would; the notes' own layer values win (with_conditions).
    CFDREQ-13 THE WALL TREATMENT accepted ("OK": wall functions) or chosen (resolved) is a y+ at the first cell's
              centre, from the flow's own length (a passage's hydraulic diameter, a body's length), fitted to the wall
              cells: inside the wall-function band, else the nearest in it, else a resolved wall - said (wall_fit).
"""
from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from meshpipeline.contracts import flow_conditions as _FC
from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.engines import mesh_requirements as MR

logger = logging.getLogger(__name__)

RULE_SEAM = "CFDREQ-1"
RULE_SIZES = "CFDREQ-2"
RULE_CELLS = "CFDREQ-3"
RULE_LAYERS = "CFDREQ-4"
RULE_LIMITS = "CFDREQ-5"
RULE_REPORT = "CFDREQ-6"
RULE_SAME = "CFDREQ-7"
RULE_WALL = "CFDREQ-13"
#: what the user asked steers the engine, among the engines valid for the part (steer)
RULE_ASKS_ENGINE = "ENG-10"
ROUTE = MR.FLOW
CONSUMER = "mesh_requirements"
#: each CFD engine's lane in the controls registry (contracts/mesh_controls.LANES): gmsh's flow lane is its own
LANES = {"gmsh": "gmsh_flow"}
#: what each engine built for each requirement (beside the case; the report reads it after the mesh)
PLAN_FILE = "cfd_requirements_plan.json"
#: the CFD engines (engines/registry names)
ENGINES = ("snappy", "cfmesh", "snappy_multiregion", "gmsh", "vmtk", "snapgrid", "netgen")
#: kinematic viscosity at 20 °C, m^2/s, of the fluids a note may name (one table with the flow conditions')
NU = _FC.NU
DEFINITIONS = ("Non-orthogonality: on each internal face, the angle between the face normal and the line joining "
               "the two cell centres (a cell's worst face). Skewness: the OpenFOAM measure, the distance from the "
               "face centre to where that line crosses the face, over the face's size. Aspect ratio: OpenFOAM's "
               "(1 for a cube). Determinant: of the cell's face-area tensor, 1 for a cube. Orthogonal quality: "
               "Fluent's, the smallest cosine between a face normal and the lines to the face and to the next "
               "cell (1 is perfect). Cell size: the cube root of the cell's volume. Percentages are of all the "
               "cells of the mesh. A first layer height from y+ uses the flat-plate skin friction Cf = 0.026 "
               "Re^(-1/7) and is the height of the first cell at the wall.")


# #
# never a failed mesh: every engine-facing step is guarded
# #

def guarded(fallback: Callable):
    """A step an engine calls with an Ask (its first argument): any error of ours is logged, every requirement the
    Ask has not decided yet is told "not applied - an error of ours", and the engine gets `fallback(*args, **kw)` -
    its own value, unchanged. The user's notes never fail a mesh."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kw):
            try:
                return fn(*args, **kw)
            except Exception:  # noqa: BLE001 - the notes never fail a mesh
                logger.warning("cfd requirements: %s failed - the engine's own value stands", fn.__name__,
                               exc_info=True)
                ask = args[0] if args and isinstance(args[0], Ask) else None
                if ask is not None:
                    for r in ask.req.get("rows") or []:
                        ask.rows.setdefault(r["id"], {"status": "not_applied",
                                                      "built": "it could not be applied here (an error of ours); "
                                                               "the mesh was made with our own setting"})
                return fallback(*args, **kw)
        return run
    return wrap


# #
# CFDREQ-1: the reading, once per attempt, and the one seam
# #

def requirements_for(workspace) -> dict | None:
    """THE SEAM every CFD engine calls where its plan is made: the set this attempt applies - None when the notes
    ask nothing (the engine then runs exactly as it always has, CFDREQ-7)."""
    if not workspace:
        return None
    return MR.requirements_of(MR.read(workspace))


def flow_run(workspace, state=None) -> bool:
    """Whether this attempt meshes a flow (every purpose but structural FEA, which reads its own)."""
    from meshpipeline.engines.fea_review import is_fea
    from meshpipeline.engines.workspace_facts import read_purpose

    purpose = read_purpose(workspace) or str((state or {}).get("purpose") or "")
    return not is_fea(purpose)


async def read_for_attempt(workspace, state, *, job_id: str = "", publish=None, run=None,
                           wait_s: float | None = None, engine: str = "") -> dict:
    """NOTE-10 / CFDREQ-1: the notes read once for this attempt on the flow route, kept in the workspace for every
    engine (and every process) that builds it, cited, and told before any mesh is made - one short line each, and
    what was not applied. No words: nothing asked, nothing written, nothing said. Never raises (but a fenced
    worker's stale publish)."""
    from meshpipeline.contracts import narration as _N
    from meshpipeline.engines import meshing_standard as MS

    ws = Path(workspace)
    try:
        # STD-2: the notes merged with the team's meshing standard (settings < notes, field by field); with no
        # standard this is the notes' own reading, exactly as before (STD-3)
        if wait_s is None:
            sec = await MS.decide(state or {}, route=ROUTE, job_id=job_id, run=run)
        else:
            sec = await MS.decide(state or {}, route=ROUTE, job_id=job_id, run=run, wait_s=float(wait_s))
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the notes never fail a run
        logger.warning("cfd requirements: not read (%s) - job_id=%s", type(exc).__name__, job_id)
        sec = MR.section("failed", note=MR.UNREAD_WORDS)
    # CFDREQ-8: the flow conditions the user confirmed size the first layer, as a y+ in their notes would
    sec = with_conditions(sec, state)
    # NOTE-15: what the notes say that the product acted on outside the reading - the engine they name (this
    # attempt's engine says whether it was used), the flow conditions confirmed, the kind of mesh, the unit - is
    # never told or counted as not applied, whatever the reading made of it
    from meshpipeline.engines import notes_acted as NA
    sec = NA.apply(sec, MR.notes_text(state or {}), NA.facts_of(state, engine))
    # CFDREQ-34: a size typed with faces picked on the model is for those faces, resolved to their own triangles
    _with_picks(ws, sec, state)
    MR.write(ws, sec)
    (ws / PLAN_FILE).unlink(missing_ok=True)          # a carried plan never speaks for this attempt
    _say_engine_for_asks(ws, state, engine)
    if sec.get("status") == "none":
        return sec
    try:
        from meshpipeline.contracts import decision_record as _dr
        for d in [*MR.decisions(sec, stage="plan"), *MS.decisions(sec, stage="plan")]:
            _dr.cite(str(job_id or ""), stage=str(d["stage"]), decided=str(d["decided"]), value=d.get("value"),
                     rule=str(d["rule"]), inputs=d.get("inputs") or {},
                     source=str(d.get("source") or _dr.SOURCE_RULE), words=str(d.get("words") or ""))
    except Exception:  # noqa: BLE001 - the why record never stops a mesh
        logger.debug("NOTE-10 not cited", exc_info=True)
    if publish is not None:
        for i, line in enumerate(MR.live_words(sec)):
            await _N.asay(publish, line, op_id=f"cfd:notes:{i}")
    return sec


def _with_picks(ws: Path, sec: Mapping, state) -> None:
    """CFDREQ-34: the sizes typed in a note sent with faces picked bound to those faces (engines/cfd_picked.bind), and
    each picked face resolved from the uploaded CAD to its own triangles (cfd_picked.resolve). Never raises."""
    from meshpipeline.engines import cfd_picked as CP

    req = sec.get("requirements") if isinstance(sec, Mapping) and sec.get("status") == "used" else None
    if not isinstance(req, dict):
        (ws / CP.FILE).unlink(missing_ok=True)
        return
    try:
        CP.bind(req, (state or {}).get("user_notes") or [])
        if CP.picked_entries(req.get("local")):
            from meshpipeline.pipeline.geometry_state import materialized
            g = materialized(state or {})
            CP.resolve(ws, req, str(g.path) if g else "")
        else:
            (ws / CP.FILE).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - the picks never fail a run; an unresolved pick is told by the engine
        logger.warning("cfd requirements: the picked faces were not resolved", exc_info=True)


def _say_engine_for_asks(ws: Path, state, engine: str) -> None:
    """ENG-10: an attempt on the engine the user's asks moved the run to keeps that line with its result (one remark:
    "Meshed with snappyHexMesh, because it can set ..."). Nothing moved, or another engine now: nothing."""
    try:
        rec = (state or {}).get("engine_ladder")
        asks = rec.get("asks") if isinstance(rec, Mapping) else None
        if not isinstance(asks, Mapping) or not asks.get("words") or str(asks.get("to") or "") != str(engine or ""):
            return
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(ws, str(asks["words"]), by="engine_for_asks")
    except Exception:  # noqa: BLE001 - a line that cannot be kept never stops a mesh
        logger.debug("ENG-10 line not kept", exc_info=True)


def live(lines) -> list[str]:
    """Lines fit for the live commentary (its guard takes no "=", "<", ">" or snake_case), at most MR.MAX_LIVE."""
    out = []
    for w in lines or []:
        w = str(w)
        for a, b in MR._LIVE:
            w = w.replace(a, b)
        w = " ".join(w.split())
        out.append(w if len(w) <= 236 else w[:233].rsplit(" ", 1)[0] + "...")   # the live line's limit is 240
    if len(out) > MR.MAX_LIVE:
        out = [*out[:MR.MAX_LIVE - 1], f"And {len(out) - MR.MAX_LIVE + 1} more, listed with the result."]
    return out


async def say(publish, lines, *, op: str = "cfd:notes-plan") -> None:
    """What the mesher does with the notes, said once before it meshes (CFDREQ-2..5): "On this mesher, ..."."""
    if publish is None or not lines:
        return
    from meshpipeline.contracts import narration as _N

    for i, line in enumerate(live([f"On this mesher, {str(x)[:1].lower()}{str(x)[1:]}" for x in lines])):
        await _N.asay(publish, line, op_id=f"{op}:{i}")


def say_sync(workspace, lines) -> None:
    """For an engine with no publisher at hand (its configure step runs as a tool): the lines kept with the result
    (one remark), so what it did with the notes is still told."""
    if not lines:
        return
    try:
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(Path(workspace), " ".join(f"On this mesher, {x[:1].lower()}{x[1:]}" for x in lines)[:400],
                      by="cfd_requirements_plan")
    except Exception:  # noqa: BLE001
        logger.debug("cfd plan lines not kept", exc_info=True)


# #
# the flow's numbers: a y+ as a first layer height (CFDREQ-4)
# #

def skin_friction(re_number: float) -> float:
    """Flat-plate skin friction, Cf = 0.026 Re^(-1/7) (the usual y+ calculators')."""
    return 0.026 * float(re_number) ** (-1.0 / 7.0)


#: CFDREQ-13: below these Reynolds numbers the flow is laminar (or transitional): no wall-function target exists
RE_LAMINAR_PIPE = 2300.0          # on the passage's hydraulic diameter
RE_LAMINAR_PLATE = 5.0e5          # on the body's length (flat-plate transition)


def friction(re_number: float, *, internal: bool) -> tuple[float, bool, str]:
    """CFDREQ-13: (the turbulent skin-friction coefficient the first layer is sized with, laminar?, the estimate's
    name). A passage: the smooth-pipe friction factor on its hydraulic diameter, as a Fanning coefficient (Blasius
    below Re 1e5, Haaland above); a body: the flat plate on its length (Cf = 0.026 Re^-1/7). The height is always
    sized with the turbulent estimate - thinner than a laminar one, so a y+ asked is never overshot near transition;
    laminar (Re under 2300 in a passage, 5e5 on a body) says that the flow has no wall-function target at all."""
    re_n = max(float(re_number), 1.0)
    if internal:
        laminar = re_n < RE_LAMINAR_PIPE
        if re_n < 1.0e5:
            return 0.316 * re_n ** -0.25 / 4.0, laminar, "smooth-pipe friction (Blasius)"
        return (1.0 / (-1.8 * math.log10(6.9 / re_n))) ** 2 / 4.0, laminar, "smooth-pipe friction (Haaland)"
    return skin_friction(re_n), re_n < RE_LAMINAR_PLATE, "flat-plate skin friction"


#: CFDREQ-13: from this y+ up the target is the first cell's CENTRE (the y+ the solver reports): wall functions'
#: 50, a typed 30 - their height at the target would put the centre at half of it, in the buffer layer. Under it (a
#: resolved wall's y+ 1, typed or chosen) the target is the cell's full height, so its centre sits at about half -
#: the safe side for a resolved wall, on an estimate good to a factor of two
Y_PLUS_AT_CENTRE_FROM = 5.0
#: the buffer layer: neither wall functions nor a resolved wall is accurate with the first cell centre in it
BUFFER_LAYER = (5.0, 30.0)


def y_plus_height(y_plus: float, physics: Mapping | None, *, length_m: float | None = None,
                  length_from: str = "") -> tuple[float | None, list[str], dict]:
    """(the first layer height in metres for this y+, the numbers missing for it, how it was worked out). CFDREQ-13:
    a y+ of 5 or more is the one the solver reports, at the first cell's CENTRE (the height is twice y+ nu / u_tau);
    under 5 it is the cell's full height (y+ nu / u_tau), as it always was. A flow
    through a part takes its Reynolds number on the passage's hydraulic diameter (the conditions' inlet, in
    `physics`) and the smooth-pipe friction; a flow round a body on the reference length - the notes' own, else
    `length_m` (the case's: the user's ruler, else the body's length along the flow, said by `length_from`) - and the
    flat plate."""
    ph = dict(physics or {})
    U, nu, Re = ph.get("velocity_ms"), ph.get("nu_m2s"), ph.get("reynolds")
    if nu is None and ph.get("fluid") in NU:
        nu = NU[str(ph["fluid"])]
    internal = ph.get("flow") == "internal"
    L, said_L = ph.get("length_m"), "the length in your notes"
    if L is None and internal and ph.get("hydraulic_diameter_m"):
        L, said_L = float(ph["hydraulic_diameter_m"]), "the inlet's hydraulic diameter"
    if L is None and length_m:
        L, said_L = float(length_m), length_from or "the body's length"
    missing = []
    if Re is None:
        if U is None:
            missing.append("the flow speed")
        if nu is None:
            missing.append("the fluid (air or water) or its viscosity")
    if L is None:
        missing.append("a reference length")
    if missing:
        return None, missing, {}
    length = float(L or 0.0)
    re_n = float(Re) if Re is not None else float(U or 0.0) * length / float(nu or 1.0)
    cf, laminar, basis = friction(re_n, internal=internal)
    if U is not None and nu is not None:
        visc = float(nu) / (float(U) * math.sqrt(cf / 2.0))          # nu / u_tau, the wall's viscous length
    else:
        visc = length / (re_n * math.sqrt(cf / 2.0))
    centre = float(y_plus) >= Y_PLUS_AT_CENTRE_FROM
    h = (2.0 if centre else 1.0) * float(y_plus) * visc
    return h, [], {"reynolds": float(f"{re_n:.4g}"), "cf": float(f"{cf:.4g}"), "length_m": length,
                   "length_from": said_L, "first_m": float(f"{h:.4g}"), "laminar": laminar, "friction": basis,
                   "first_exact_m": h, "visc_m": visc, "at_centre": centre}


def y_plus_of(height_m: float, how: Mapping, y_plus: float | None = None) -> float:
    """The y+ at the CENTRE of a first cell `height_m` high, by the same estimate (y_plus_height's `how`). `y_plus` is
    the target the height was sized for - used only when `how` predates the viscous length."""
    visc = how.get("visc_m")
    if visc:
        return 0.5 * float(height_m) / float(visc)
    ref = how.get("first_exact_m") or how.get("first_m") or 0.0
    return float(y_plus or 0.0) * float(height_m) / max(float(ref), 1e-30)


# #
# CFDREQ-8: the flow conditions the user confirmed (the intake's conditions step)
# #

#: where a row the conditions set comes from, as the user reads it ("(from your flow conditions)")
CONDITIONS_FROM = "flow conditions"


def with_conditions(sec: dict, state) -> dict:
    """CFDREQ-8: THE CONDITIONS STEP'S ANSWER REACHES THE MESH. The plan's conditions sentence (contracts/flow_conditions,
    in request_txt) gives the y+ of the wall treatment and the flow's speed and fluid; they go into the set exactly as
    the same words typed in the notes would - the first layer is then sized by every engine that sizes one, and told
    "not applied on this mesher" by every other. What the user's own notes (or settings) set wins: the conditions only
    fill what they left. No such sentence (a plan in the model's own words, no conditions step): `sec` unchanged."""
    import copy

    from meshpipeline.contracts import flow_conditions as FC

    s = state or {}
    c = FC.read(str(s.get("request_txt") or ""), s.get("intake_patches") or [])
    if c is None:
        return sec
    # CFDREQ-13: THE PROPOSAL ACCEPTED SIZES THE WALL TOO. Pressing OK on the proposed wall functions was no ask before
    # (round 4, F: an absolute y+ 50 first layer as thick as the wall cells grew on 4-6% of the wall), and the mesher's
    # own relative layers put the first cell at y+ 3-14 - the buffer layer, wrong for wall functions and for a resolved
    # wall alike (the CFD board, 20 Nov: torus pipe 3.2, blood-pump chassis 4.6, cyclone 14). Now the accepted wall
    # functions are a y+ target like a chosen one, and every engine fits the first layer to its wall cells
    # (wall_fit): where a wall-function first cell does not fit the cells across the passage, the wall is resolved.
    accepted = not c.get("wall_said", True) and c["wall"] == "functions"
    before = MR.words(sec)
    used = sec.get("status") == "used"
    req = copy.deepcopy(dict(sec.get("requirements") or MR.empty())) if used else MR.empty()
    lay = req.setdefault("layers", {})
    ph = req.setdefault("physics", {})
    # CFDREQ-13: the flow's own length scale - a passage's hydraulic diameter (its inlet, as the user confirmed it) on
    # a flow through a part; the engines give the body's length on a flow round one
    req_txt = str(s.get("request_txt") or "")
    internal = ("internal" in str(s.get("purpose") or "") or str(s.get("flow_topology") or "") == "internal"
                or req_txt.lstrip().startswith("Internal flow") or " at the inlet. Near the walls:" in req_txt)
    ph.setdefault("flow", "internal" if internal else "external")
    if internal and ph.get("hydraulic_diameter_m") is None:
        d_in = [float(p.get("diameter_mm") or 0.0) for p in s.get("intake_patches") or ()
                if isinstance(p, Mapping) and p.get("type") == "inlet" and p.get("diameter_mm")]
        if d_in:
            ph["hydraulic_diameter_m"] = max(d_in) / 1000.0
    treatment = "a resolved wall" if c["wall"] == "resolved" else "wall functions, y+ 30-300"
    lead = "From your flow conditions:"
    if lay.get("count") == 0:
        line = f"{lead} no boundary layers are built, as asked, so the wall treatment sizes none."
    elif lay.get("first_mm") is not None:
        line = f"{lead} the first layer is the height you gave, not the one the wall treatment's y+ would give."
    elif lay.get("y_plus") is not None:
        line = f"{lead} the y+ you gave ({MR._g(lay['y_plus'])}) is used, not the wall treatment's."
        # ... at the fluid and speed of the conditions, where the notes give none: a y+ typed as the answer to the
        # conditions question ("y+ of 1 at the wall") is read as a note too, and with no speed or fluid of its own it
        # was never turned into a height - the layers kept our relative sizing, about y+ 50 (job 63b0fd25)
        if ph.get("velocity_ms") is None and ph.get("reynolds") is None and c["velocity_ms"] is not None:
            ph["velocity_ms"] = float(c["velocity_ms"])
        if ph.get("fluid") is None and ph.get("nu_m2s") is None and c["fluid"] in FC.NU:
            ph["fluid"] = c["fluid"]
    else:
        lay["y_plus"] = float(c["y_plus"])
        # CFDREQ-13: which wall treatment this y+ stands for - the engines fit the first layer to it (wall_fit)
        lay["treatment"] = c["wall"]
        # CFDREQ-17: whether the user said it (a button or their words) or only accepted the proposal
        lay["treatment_said"] = bool(c.get("wall_said"))
        if ph.get("velocity_ms") is None and c["velocity_ms"] is not None:
            ph["velocity_ms"] = float(c["velocity_ms"])
        if ph.get("fluid") is None and c["fluid"] in FC.NU:
            ph["fluid"] = c["fluid"]
        asked = (f"y+ of about {MR._g(c['y_plus'])} at the walls ({treatment}"
                 + (", the proposal you accepted" if accepted else "") + f"), for {c['fluid_words']} at {c['speed_words']}")
        req.setdefault("rows", []).append({"id": "c1", "field": "layers:y_plus", "asked": asked,
                                           "quote": f"Fluid: {c['fluid_words']} at {c['speed_words']}; "
                                                    f"{FC.WALL_WORDS[c['wall']]}",
                                           "from": CONDITIONS_FROM})
        line = f"{lead} {asked}" + (f" - the first layer keeps our sizing, because {c['why']}." if c["why"] else ".")
    out = dict(sec) if used else MR.section("used", note=str(sec.get("note") or ""), source=str(sec.get("source") or ""))
    out["status"] = "used"
    out["requirements"] = req
    out["lines"] = [line, *before]
    out["conditions"] = {k: c[k] for k in ("fluid", "velocity_ms", "wall", "y_plus", "why")}
    return out


# #
# what an engine does with the set: one Ask per engine and attempt
# #

def _mm(m: float) -> str:
    return f"{MR._g(float(m) * 1000.0)} mm"


def _cells(n: float) -> str:
    return MR._count(n)


class Ask:
    """One attempt's requirements on one engine: the values its levers read, and for every requirement what the
    engine did with it (`done`, `nearest`, `not_here`) - kept beside the case (write) for the report."""

    def __init__(self, req: Mapping, engine: str):
        self.req = dict(req)
        self.engine = str(engine)
        self.rows: dict[str, dict] = {}
        self.picked_cells: float = 0.0       # CFDREQ-36: cells cfmesh_picked's refinements add to the plan
        s = self.req.get("sizes") or {}
        self.global_m = _m(s.get("global_mm"))
        self.min_m = _m(s.get("min_edge_mm"))
        self.max_m = _m(s.get("max_edge_mm"))
        self.wall_m = _m(s.get("near_wall_mm"))
        self.growth = s.get("growth")
        c = self.req.get("cells") or {}
        self.target, self.ceiling, self.floor = c.get("target"), c.get("ceiling"), c.get("floor")
        lay = self.req.get("layers") or {}
        self.n_layers = lay.get("count")
        self.first_m = _m(lay.get("first_mm"))
        self.total_m = _m(lay.get("total_mm"))
        self.layer_growth = lay.get("growth")
        self.y_plus = lay.get("y_plus")
        #: CFDREQ-13: "functions" / "resolved" when the y+ is the wall treatment's (the conditions), else None
        self.treatment = lay.get("treatment")
        #: CFDREQ-17: the user said the wall treatment themselves (not only accepted the proposal)
        self.treatment_said = bool(lay.get("treatment_said"))
        self.local = [dict(e) for e in self.req.get("local") or []]
        self.limits = [dict(lim) for lim in self.req.get("limits") or []]
        #: the case's reference length for a y+ (the engine sets it: the user's ruler, else the body's length)
        self.length_m: float | None = None
        self.length_from = ""
        #: snappy: levels per named region the user sized; whether a budget of ours was raised for their sizes
        self.region_levels: dict = {}
        self.raised = False
        #: why the layers may cover less of the wall (a quality limit of the user's held while they are added)
        self.layer_note = ""
        #: CFDREQ-13: how the y+ was turned into a height (y_plus_height's record), and a laminar flow seen
        self.how: dict = {}
        self.laminar = False
        #: CFDREQ-17: a body that wall functions do not suit (too slow or too small) is resolved instead - the why,
        #: the Reynolds number and the boundary layer's estimated thickness; None when no such fallback was taken
        self.fallback: dict | None = None
        #: CFDREQ-17: the wall cells coarsened so a wall-function first layer fits under them, said ("" when not)
        self.wall_level_note = ""
        #: CFDREQ-31: cfMesh's one wall-function layer's wall cell and y+ record, when that rule chose the wall cell
        self.cfmesh_one_layer: dict | None = None

    @classmethod
    def of(cls, workspace, engine: str) -> Ask | None:
        """The Ask for this engine, or None when nothing is asked (the engine's own plan, unchanged)."""
        req = requirements_for(workspace)
        return cls(req, engine) if req else None

    # -- rows ---------------------------------------------------------------------------------------------
    def rid(self, field: str) -> str | None:
        return next((r["id"] for r in self.req.get("rows") or [] if r["field"] == field), None)

    def _set(self, rid: str | None, status: str, built: str, **facts) -> None:
        if rid:
            self.rows[rid] = {"status": status, "built": built, **{k: v for k, v in facts.items() if v is not None}}

    def done(self, field_or_id: str, built: str, **facts) -> None:
        self._set(self._id(field_or_id), "applied", built, **facts)

    def nearest(self, field_or_id: str, built: str, **facts) -> None:
        self._set(self._id(field_or_id), "nearest", built, **facts)

    def not_here(self, field_or_id: str, why: str, **facts) -> None:
        self._set(self._id(field_or_id), "not_applied", why, **facts)

    def _id(self, field_or_id: str) -> str | None:
        if any(r["id"] == field_or_id for r in self.req.get("rows") or []):
            return field_or_id
        return self.rid(field_or_id)

    def controls(self, target: dict) -> dict:
        """NOTE-13: the one loop at this engine's plan seam - each control the set asks for applied on `target` by the
        registry's applier, or not applied with its reason; the rows say what was done. Returns `target`."""
        from meshpipeline.engines import control_appliers as CA

        for got in CA.apply(LANES.get(self.engine, self.engine), self.req, target).values():
            self._set(got.get("id"), got["status"], got["built"])
        return target

    def limit(self, measure: str) -> dict | None:
        return next((lim for lim in self.limits if lim["measure"] == measure), None)

    def asks_layers(self) -> bool:
        return any(v is not None for v in (self.n_layers, self.first_m, self.total_m, self.layer_growth, self.y_plus))

    def write(self, workspace) -> None:
        """The record beside the case: what this engine did with each requirement. A row it never decided is
        "not applied on this mesher" - told, never silently dropped. On the CFD wrap rung (LAD-cfd-5) every row
        also says it was applied to the wrapped surface (wrap_rows)."""
        wrap_rows(self, workspace)
        name = _ENGINE_WORDS.get(self.engine, self.engine)
        lid = self.rid("layers:count")
        if self.layer_note and lid in self.rows:
            self.rows[lid]["note"] = self.layer_note
        for r in self.req.get("rows") or []:
            if r["id"] in self.rows:
                continue
            if str(r["field"]).startswith("limit:"):
                self.rows[r["id"]] = {"status": "measured", "built": f"{name} has no setting for it while meshing; "
                                                                     "it is measured on the mesh"}
            elif str(r["field"]).startswith("control:"):
                # NOTE-13: "not on this mesher", with the registry's own reason
                from meshpipeline.contracts import mesh_controls as MC
                how = MC.lane_of(LANES.get(self.engine, self.engine), str(r["field"]).split(":", 1)[1])
                self.rows[r["id"]] = {"status": "not_applied", "built": how.why if isinstance(how, MC.NotHere)
                                      else f"{name} has no setting for it on this case"}
            else:
                self.rows[r["id"]] = {"status": "not_applied", "built": f"{name} has no setting for this"}
        try:
            (Path(workspace) / PLAN_FILE).write_text(json.dumps({"engine": self.engine, "rows": self.rows},
                                                                indent=1, sort_keys=True))
        except OSError:
            logger.warning("cfd requirements: the plan was not kept", exc_info=True)

    def lines(self) -> list[str]:
        """What this engine does with the notes, before it meshes, in plain words (one line each)."""
        asked = {r["id"]: r["asked"] for r in self.req.get("rows") or []}
        out = []
        for rid, row in self.rows.items():
            a = asked.get(rid, "")
            a = a[:1].upper() + a[1:]
            if row["status"] == "applied":
                out.append(f"{a}: {row['built']}.")
            elif row["status"] == "nearest":
                out.append(f"{a}: not possible as asked - {row['built']}.")
            elif row["status"] == "measured":
                out.append(f"{a}: {row['built']}.")
            else:
                out.append(f"{a}: not applied - {row['built']}.")
        return out

    # -- CFDREQ-3: the cell budget ---------------------------------------------------------------------------
    def budget(self, ours: int, *, hard: int | None = None) -> int:
        """The cell budget this engine plans to: the user's count when they set one (a ceiling, a target; a floor
        raises ours), held to `hard` - the most this engine can build: the standard machine's (CELL_HARD_LIMIT) by
        default, the largest machine's for an engine that moves a big plan to a bigger machine (RUN-9: snappy,
        cfMesh); ours otherwise, unchanged. A count over the standard machine is said to run on a bigger one."""
        if self.target is None and self.ceiling is None and self.floor is None:
            return int(ours)
        from meshpipeline.contracts import mesh_machine as MM
        normal = MM.normal_cells()
        if hard is None:
            hard = normal
        most = (f"the most the largest machine we run holds is {_cells(hard)} cells" if hard >= MM.ceiling_cells()
                else f"the most this mesher builds here is {_cells(hard)} cells")
        bigger = "; that is more than our standard machine is sized for, so it is meshed on a bigger one"
        out = int(ours)
        for field, want in (("cells:ceiling", self.ceiling), ("cells:target", self.target)):
            if want is None:
                continue
            if want > hard:
                self.nearest(field, f"{most}, so the mesh is planned to that", planned=hard)
                out = hard
            else:
                self.done(field, f"the cell budget is set to {_cells(want)}" + (bigger if want > normal else ""),
                          planned=int(want))
                out = int(want)
            break
        if self.floor is not None:
            want = min(int(self.floor), hard)
            if out < want:
                out = want
            if self.floor > hard:
                self.nearest("cells:floor", most, planned=hard)
            else:
                self.done("cells:floor", f"the cell budget is at least {_cells(want)}"
                          + (bigger if out > normal else ""), planned=out)
        return int(out)

    def user_budget(self) -> bool:
        return self.target is not None or self.ceiling is not None

    def cell_limit(self, normal: int) -> int:
        """CFDREQ-3: the most cells this attempt's plan may grow to - our sizing rules (the gap boxes) raise a budget
        of ours up to it, never past the user's own count: their ceiling, else their target, else (a floor alone, or
        nothing) the larger of the floor and `normal` (the standard machine's)."""
        want = self.ceiling if self.ceiling is not None else self.target
        if want is not None:
            return int(want)
        return max(int(normal), int(self.floor or 0))

    # -- CFDREQ-4: the first layer -------------------------------------------------------------------------
    def first_layer(self, *, length_m: float | None = None, length_from: str = "") -> float | None:
        """The first layer height the user asked for, metres: their own, else the one their y+ gives (said), else
        None (the missing numbers named on the y+ row)."""
        if self.first_m is not None:
            if self.y_plus is not None:
                self.done("layers:y_plus", f"your first layer height ({_mm(self.first_m)}) is used as given; the y+ "
                                           "it gives depends on the flow")
            return self.first_m
        if self.y_plus is None:
            return None
        h, missing, how = y_plus_height(float(self.y_plus), self.req.get("physics"), length_m=length_m,
                                        length_from=length_from)
        if h is None:
            self.not_here("layers:y_plus", f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not "
                                           "known, so the first layer height cannot be worked out from it; "
                                           "the layers keep our sizing")
            return None
        self.how = how
        body = (self.req.get("physics") or {}).get("flow") != "internal"
        typed_wf = self.treatment is None and float(self.y_plus) >= WALL_FUNCTIONS_BAND[0]
        if (self.treatment == "functions" or typed_wf) and body:
            # CFDREQ-17: A BODY WALL FUNCTIONS DO NOT SUIT IS RESOLVED, NEVER LEFT IN THE BUFFER LAYER. A laminar body
            # (too slow or too small: Re under 5e5 on its length) has no log layer for a wall function to sit in, and
            # a y+ 30 cell would be about as thick as its whole boundary layer (abc 00050662: 60 mm long at 10 m/s,
            # Re 4e4: 1.36 mm against a 1.5 mm layer). Before, the mesher's relative layers were kept - 0.169 of the
            # local cell, y+ 2-5 on 107 of 120 external bodies (the CFD board's external sweep, 22 Nov): too coarse to
            # resolve, too fine for wall functions. Now such a wall is resolved (a y+ 1 first layer), said. Accepted
            # (the proposal promised it): applied; asked in so many words (chosen, or a y+ of 30 or more typed):
            # the nearest there is, with the reason
            fb = resolved_fallback(self, h, how, said=typed_wf or self.treatment_said)
            if fb is not None:
                return fb
        if self.treatment == "functions" and how.get("laminar"):
            # CFDREQ-13: a laminar (or transitional) flow has no wall-function band and no buffer layer to avoid: the
            # wall is resolved by the cells across the passage, with our own layers. A resolved wall the user CHOSE is
            # still built at y+ 1 (the turbulent estimate: thinner than a laminar one, so never too thick) - a bluff body
            # at Re 1e5 lost the resolved first layer it had before (the CFD board, 20 Nov)
            self.done("layers:y_plus", f"the flow is laminar at these conditions (Re {MR._g(how['reynolds'])} on "
                                       f"{how['length_from']}), so there is no wall-function target: the wall is "
                                       "resolved by the cells across the passage, with our own layers",
                      laminar=True, reynolds=how["reynolds"])
            self.laminar = True
            return None
        yc = y_plus_of(h, how)
        said = (f"first layer {_mm(h)} high, its centre at y+ about {MR._g(round(yc, 2))} (worked out at Re "
                f"{MR._g(how['reynolds'])}, with {how['length_from']} as the reference length)")
        if BUFFER_LAYER[0] <= float(self.y_plus) < BUFFER_LAYER[1]:
            said += (" - that is the buffer layer (y+ 5-30), where neither wall functions nor a resolved wall is "
                     "accurate")
        self.done("layers:y_plus", said, **{k: v for k, v in how.items() if k not in ("visc_m", "first_exact_m")},
                  y_plus_centre=float(f"{yc:.3g}"))
        return h


# #
# CFDREQ-13: the wall treatment's own first layer, fitted to the wall cells
# #

#: the y+ band a wall-function first cell centre must sit in (the target is the conditions' 50); under 30 is the
#: buffer layer, which neither wall functions nor a resolved wall model (Pope, Turbulent Flows; the solvers' guides)
WALL_FUNCTIONS_BAND = (30.0, 100.0)
#: the resolved wall's y+ at the first cell centre
RESOLVED_Y_PLUS = 1.0
#: the thickest first layer a wall cell takes, as a share of it (one layer; the next cell keeps 0.4 of the wall cell,
#: inside the board's last-layer-to-core band 0.3-1)
FIRST_CAP_REL = 0.6
#: a wall-function stack's last layer aims at this share of the wall cell (a smooth step into the core cells)
STACK_TOP_REL = 0.4
#: the most layers a wall-function stack gets
MAX_WALL_FUNCTION_LAYERS = 10


#: CFDREQ-17: a wall-function first cell may reach at most this share of a turbulent boundary layer's thickness (the
#: log layer ends at about 0.1-0.2 of it; past it the cell is in the outer, wake part of the layer)
WALL_FUNCTION_BL_SHARE = 0.2
#: CFDREQ-17: the cells a resolved body wall puts across its boundary layer (the layers and the core cells under the
#: layer's edge): a laminar layer is resolved with about 10 or more (Blasius profile; the usual CFD guides' 10-15)
BL_CELLS = 10
#: CFDREQ-22: where along a laminar body its resolved wall is sized - the boundary layer a fifth of the way from the
#: front (5 x / sqrt(Re_x) at x = 0.2 L: 0.45 of its thickness at the end). It only thickens behind that point, so
#: the cells across it there are across it, or more, on the 80% of the length behind
BL_SIZE_AT = 0.2
#: CFDREQ-22: a laminar resolved stack stops at this many layers when the core cells inside the boundary layer make up
#: the rest of BL_CELLS (snappyHexMesh kept its layers on 71 of 77 bodies planned with 8 or fewer, on 14 of 46 with
#: 9-16 - the CFD board, batch 6); past it, layers alone make the cells across, up to BL_CELLS of them
LAMINAR_LAYERS_KEPT = 8
#: CFDREQ-22: the growth of a laminar resolved stack when the user gave none (never more than the plan's own)
LAMINAR_GROWTH = 1.2
#: CFDREQ-22: the built mesh holds BL_CELLS across where it holds this many (to the nearest cell), on this share of
#: the wall
BL_CELLS_MET = BL_CELLS - 0.5
BL_WALL_SHARE = 80.0
#: CFDREQ-22: under a tight wall's cap a laminar stack's first layer may be this share of its floor (the first layer of
#: BL_CELLS layers filling the layer), no thinner (abc 00021335 held 10 cells across on all its wall from 0.48 of it)
LAMINAR_CAP_THIN = 0.25


def laminar_bl_m(how: Mapping) -> float | None:
    """CFDREQ-22: the laminar boundary layer's thickness the resolved wall is sized on, metres: 5 x / sqrt(Re_x) at
    x = BL_SIZE_AT of the body's length (sqrt(BL_SIZE_AT) of the thickness at its end). None without the length or
    the Reynolds number."""
    end = boundary_layer_m({**how, "laminar": True})
    return end * math.sqrt(BL_SIZE_AT) if end else None


def stack_height(first_m: float, growth: float, n: int) -> float:
    """A layer stack's depth: `n` layers from `first_m`, each `growth` times the one under it."""
    g = float(growth)
    return float(first_m) * n if g <= 1.0001 else float(first_m) * (g ** n - 1.0) / (g - 1.0)


def laminar_stack(delta_m: float, wall_cell_m: float, growth: float, *, n_min: int = 1,
                  n_max: int = BL_CELLS, n_fixed: int | None = None,
                  stack_max_m: float | None = None, floor_n: int | None = None) -> tuple[int, float, float]:
    """CFDREQ-22: THE LAMINAR RESOLVED WALL'S STACK, FROM ITS BOUNDARY LAYER: (layers, first layer m, cells across
    `delta_m`). The fewest layers from `n_min` (a count of the user's: `n_fixed`) that, with the thickest first layer
    that still does it, put BL_CELLS across the layer - the layers under its edge and the core cells (`wall_cell_m`)
    between the stack and the edge. Up to LAMINAR_LAYERS_KEPT layers while the core cells can make up the rest; past
    that the stack alone fills the layer, at most `n_max` (BL_CELLS) layers. A first layer is never past
    FIRST_CAP_REL of the wall cell at the stack's top, nor the stack past `stack_max_m` (a tight wall's cap). When
    BL_CELLS cannot be reached, the most cells across these bounds allow (the stack filling the layer), said by the
    caller."""
    g, wc, delta = max(float(growth), 1.0), max(float(wall_cell_m), 1e-30), float(delta_m)
    counts = [int(n_fixed)] if n_fixed else list(range(max(1, int(n_min)), max(int(n_min), int(n_max)) + 1))

    def top(n: int) -> float:
        # the thickest first layer n layers may start from: the stack inside the layer and the cap, its top layer
        # under FIRST_CAP_REL of the wall cell
        hi = delta / stack_height(1.0, g, n)
        if stack_max_m:
            hi = min(hi, float(stack_max_m) / stack_height(1.0, g, n))
        return min(hi, FIRST_CAP_REL * wc / g ** max(0, n - 1))

    # never a first layer thinner than the one a full stack (n_max layers filling the layer) starts from: a thinner
    # one is harder to build and resolves nothing more (`floor_n`: that full stack's count when it is not n_max - the
    # near-wall band's shorter stack keeps the floor of the plan without it)
    floor = delta / stack_height(1.0, g, max(int(floor_n or n_max), int(n_fixed or 0), 1))
    if stack_max_m and not n_fixed:
        # A TIGHT WALL'S CAP THINS THE FIRST LAYER DOWN TO LAMINAR_CAP_THIN OF THE FLOOR, THEN CUTS LAYERS: n layers
        # whose stack from that passes the cap are not built (the cloud sample, extlam_ef6c20fc: a cap thinned abc
        # 00067838's first layer to 1e-8 m and snappyHexMesh grew no layer at all; but cutting layers at the floor
        # itself, extlam2_r3c_6b8a01ca, left abc 00021335 6 layers and 10 cells across on 7% of its wall, where 9
        # layers from 0.48 of the floor had held 10 or more on all of it); at least one layer stays
        fits = [n for n in counts
                if stack_height(LAMINAR_CAP_THIN * floor, g, n) <= float(stack_max_m) * (1 + 1e-9)]
        if fits:
            counts = fits
        else:
            # a cap under even one such layer (abc 00067838: about 3e-7 m) is not a stack any wall can hold; it is set
            # aside, as CFDREQ-9's cap only ever shortened a bridged stack before
            stack_max_m = None
    best = None
    thinned = False
    for n in counts:
        if n > LAMINAR_LAYERS_KEPT and not n_fixed and not thinned:
            # CFDREQ-32: BEFORE A STACK GOES PAST THE COUNT SNAPPYHEXMESH KEEPS, ITS FIRST LAYER IS THINNED (to
            # LAMINAR_CAP_THIN of the floor, as a tight wall's cap does) so the core cells inside the layer make up
            # BL_CELLS on LAMINAR_LAYERS_KEPT layers - the thickest such first layer
            thinned = True
            got = _kept_thin(counts, top, floor, g, wc, delta)
            if got is not None:
                return got
        hi = top(n)
        if cells_across(hi, g, n, wc, delta) >= BL_CELLS - 1e-9:
            # at its thickest the stack already gives them: keep the thickest
            return n, hi, cells_across(hi, g, n, wc, delta)
        if n > LAMINAR_LAYERS_KEPT and not n_fixed:
            # past the kept count only a stack that fills the layer counts (see above)
            best = (n, hi, cells_across(hi, g, n, wc, delta))
            continue
        lo = min(floor, hi)
        if cells_across(lo, g, n, wc, delta) >= BL_CELLS:
            # the thickest first layer between the floor and the top that still puts BL_CELLS across
            for _ in range(60):
                mid = math.sqrt(lo * hi)
                lo, hi = (mid, hi) if cells_across(mid, g, n, wc, delta) >= BL_CELLS else (lo, mid)
            return n, lo, cells_across(lo, g, n, wc, delta)
        got = (n, hi, cells_across(hi, g, n, wc, delta))
        if best is None or got[2] > best[2] + 1e-9:
            best = got
    assert best is not None
    return best


def _kept_thin(counts: list[int], top, floor: float, g: float, wc: float,
               delta: float) -> tuple[int, float, float] | None:
    """CFDREQ-32: the stack of the most layers up to LAMINAR_LAYERS_KEPT whose first layer, between LAMINAR_CAP_THIN
    of the floor and its top, puts BL_CELLS across `delta` - the thickest first layer that does (the smoothest step
    into the wall cells). None when even the thinnest does not: the stack then fills the layer, as before."""
    kept = [c for c in counts if c <= LAMINAR_LAYERS_KEPT]
    if not kept:
        return None
    n = max(kept)
    hi = top(n)
    lo = min(LAMINAR_CAP_THIN * floor, hi)
    if cells_across(lo, g, n, wc, delta) < BL_CELLS:
        return None
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if cells_across(mid, g, n, wc, delta) >= BL_CELLS else (lo, mid)
    return n, lo, cells_across(lo, g, n, wc, delta)


#: CFDREQ-23: A LAMINAR WALL'S NEAR-WALL BAND. Where the wall cells are as thick as the boundary layer (abc 00000039:
#: 3.3 mm cells, 2.2 mm layer) only layers make the cells across it, and snappyHexMesh kept a 10-layer stack on 61-79%
#: of the wall (the cloud sample, extlam_ef6c20fc). A distance band of finer cells as deep as the layer lets core cells
#: count inside it, so a stack of at most LAMINAR_BAND_LAYERS layers reaches BL_CELLS. The band is at most this many
#: levels finer than the wall cells ...
LAMINAR_BAND_LEVELS = 2
#: ... its stack at most this many layers ...
LAMINAR_BAND_LAYERS = 6
#: ... and its cells are priced like the plan's own distance bands (snappy_runner.CAST_BAND_GRADING: about this many
#: times the cells of the band's own volume, the grading out of it included)
LAMINAR_BAND_GRADING = 2.0


def laminar_band(delta_m: float, wall_cell_m: float, growth: float, *, faces: float, room: float,
                 n_now: int, stack_max_m: float | None = None) -> dict:
    """CFDREQ-23: the near-wall band a laminar resolved wall can afford. For 1..LAMINAR_BAND_LEVELS levels finer than
    the wall cells (`wall_cell_m`, `faces` of them on the wall): the stack laminar_stack plans on the band's cells,
    at most LAMINAR_BAND_LAYERS layers, and what the band costs - its cells over the layer's depth (`delta_m`) less
    the cells there now, plus its stack's layer cells less the `n_now` layers' now. The coarsest band that puts
    BL_CELLS across is used when it costs no more than `room` (the cells the plan lets a resolved wall's layers add):
    {"used": True, "levels", "cell_m", "layers", "first_m", "across", "cells"}. Else {"used": False, "cells": what
    the coarsest band that reaches BL_CELLS would cost (None when none within LAMINAR_BAND_LEVELS does),
    "levels"} - the stack is then planned on the wall cells as before."""
    wc, delta, f = float(wall_cell_m), float(delta_m), float(faces)
    if not (wc > 0 and delta > 0 and f > 0):
        return {"used": False, "cells": None, "levels": 0}
    now = f * max(1.0, math.ceil(delta / wc)) + int(n_now) * f
    for k in range(1, LAMINAR_BAND_LEVELS + 1):
        h = wc / 2 ** k
        n, first, across = laminar_stack(delta, h, growth, n_max=LAMINAR_BAND_LAYERS, stack_max_m=stack_max_m,
                                         floor_n=BL_CELLS)
        if across < BL_CELLS - 1e-9:
            continue
        fk = f * 4 ** k
        cost = max(0.0, LAMINAR_BAND_GRADING * fk * math.ceil(delta / h) + n * fk - now)
        if cost <= float(room):
            return {"used": True, "levels": k, "cell_m": h, "layers": int(n), "first_m": float(first),
                    "across": float(across), "cells": int(cost)}
        return {"used": False, "cells": int(cost), "levels": k}
    return {"used": False, "cells": None, "levels": LAMINAR_BAND_LEVELS}


def band_regions(text: str, delta_m: float, levels: int) -> str | None:
    """CFDREQ-23: the snappyHexMeshDict with the near-wall band in the wall surface's own distance region: `levels`
    past the surface's finest level out to `delta_m`, the plan's bands kept past it (each coarser and further out,
    as snappyHexMesh reads them). None when the dict has no such region (the band is not added)."""
    ms = re.search(r"refinementSurfaces\s*\{\s*(\w+)\s*\{\s*level\s*\(\s*\d+\s+(\d+)\s*\)", text)
    if not ms:
        return None
    name, top = ms.group(1), int(ms.group(2)) + int(levels)
    mr = re.search(rf"(refinementRegions\s*\{{\s*{re.escape(name)}\s*\{{\s*mode\s+distance\s*;\s*levels\s*\()"
                   rf"((?:\s*\(\s*[-+0-9.eE]+\s+\d+\s*\))+)(\s*\)\s*;)", text)
    if not mr:
        return None
    old = [(float(d), int(lv)) for d, lv in re.findall(r"\(\s*([-+0-9.eE]+)\s+(\d+)\s*\)", mr.group(2))]
    keep = [(d, lv) for d, lv in old if d > float(delta_m) and lv < top]
    new = "".join(f"({d:.6g} {lv}) " for d, lv in [(float(delta_m), top), *keep]).strip()
    return text[:mr.start(2)] + new + text[mr.end(2):]


def boundary_layer_m(how: Mapping) -> float | None:
    """CFDREQ-17: the boundary layer's thickness at the body's end, metres, from the case's Reynolds number on its
    length: laminar 5.0 L / sqrt(Re) (Blasius), turbulent 0.37 L Re^-0.2 (the flat plate's 1/7 power law). None when
    the length or the Reynolds number is not in `how`."""
    L, re_n = how.get("length_m"), how.get("reynolds")
    if not L or not re_n:
        return None
    L, re_n = float(L), max(float(re_n), 1.0)
    return 5.0 * L / math.sqrt(re_n) if how.get("laminar") else 0.37 * L * re_n ** -0.2


def resolved_fallback(ask: Ask, wf_first_m: float, how: Mapping, *, said: bool = False) -> float | None:
    """CFDREQ-17: the resolved wall's first layer (y+ 1 at its top, the same estimate) for a body in wall-function
    mode that wall functions do not suit, with `ask.fallback` set and the y+ row said; None when they suit it.
    They do not when the flow is laminar on the body's length (no log layer at all) or when the wall-function first
    cell (`wf_first_m`) is thicker than WALL_FUNCTION_BL_SHARE of the turbulent layer."""
    delta = boundary_layer_m(how)
    if how.get("laminar"):
        why = "laminar"
    elif delta and float(wf_first_m) > WALL_FUNCTION_BL_SHARE * delta:
        why = "thick"
    else:
        return None
    visc = float(how.get("visc_m") or 0.0)
    if visc <= 0:
        return None
    h1 = RESOLVED_Y_PLUS * visc
    bl = laminar_bl_m(how) if why == "laminar" else None
    if bl:
        # CFDREQ-22: A LAMINAR WALL IS RESOLVED ACROSS ITS BOUNDARY LAYER, NOT FROM Y+ 1. It has no viscous sublayer
        # law to resolve; it needs BL_CELLS across the layer. Until the engine's wall cells are known: BL_CELLS layers
        # growing LAMINAR_GROWTH that fill the layer a fifth of the way along the body (laminar_stack refines it on
        # the wall cells). A y+ 1 first layer (0.026-0.034 mm) took 9-16 layers that snappyHexMesh cut short on 32 of
        # 46 bodies (the CFD board, batch 6)
        h1 = bl / stack_height(1.0, LAMINAR_GROWTH, BL_CELLS)
    ask.fallback = {"why": why, "reynolds": float(how.get("reynolds") or 0.0), "delta_m": delta,
                    "wf_first_m": float(wf_first_m), "first_m": h1, "length_from": how.get("length_from"),
                    "said": bool(said), **({"bl_delta_m": bl} if bl else {})}
    tell = ask.nearest if said else ask.done
    facts = {**{k: v for k, v in how.items() if k not in ("visc_m", "first_exact_m")}, "first_m": h1, "fallback": why}
    tell("layers:y_plus", fallback_words(ask, h1, None, None), **facts)
    return h1


def fallback_words(ask: Ask, first_m: float, n: int | None, across: float | None) -> str:
    """CFDREQ-17's plain line: why wall functions do not suit this body, and the resolved wall built instead."""
    fb = ask.fallback or {}
    re_s = MR._g(float(f"{float(fb.get('reynolds') or 0.0):.2g}"))
    if fb.get("why") == "laminar":
        lead = f"this flow is too slow, or the body too small, for wall functions (Re {re_s} on {fb.get('length_from')})"
    else:
        lead = (f"a wall-function first cell ({_mm(float(fb.get('wf_first_m') or 0.0))}) would be too thick for this "
                f"body's boundary layer (Re {re_s} on {fb.get('length_from')})")
    if fb.get("bl_delta_m"):
        # CFDREQ-22: the laminar wall in plain words - why not wall functions, and what was built instead
        out = (f"the flow over this body is laminar at this speed (Re {re_s} on {fb.get('length_from')}), and wall "
               f"functions need a turbulent one, so the wall is resolved instead: its boundary layer is about "
               f"{_mm(float(fb['bl_delta_m']))} thick a fifth of the way along the body (thicker behind)")
        if n:
            out += (f", and {int(n)} layer{'s' if int(n) != 1 else ''} from {_mm(first_m)} put about "
                    f"{int(round(across)) if across is not None else BL_CELLS} cells across it")
            if across is not None and across < BL_CELLS_MET:
                out += (f" (not the {BL_CELLS} a resolved layer needs: more would take layers thinner or more "
                        "numerous than the mesher keeps, or finer wall cells)")
        else:
            out += f", with about {BL_CELLS} cells across it, the first {_mm(first_m)}"
        return out
    out = f"{lead}: the wall is resolved instead: first cell {_mm(first_m)}"
    if n:
        out += f", {int(n)} layer{'s' if int(n) != 1 else ''}"
    if across is not None and fb.get("delta_m"):
        out += (f", about {int(round(across))} cells across its boundary layer "
                f"(about {_mm(float(fb['delta_m']))} thick)")
    return out


def cells_across(first_m: float, growth: float, n: int, wall_cell_m: float, delta_m: float) -> float:
    """CFDREQ-17: the cells across a boundary layer `delta_m` thick - the layers under its edge (a part of the one
    the edge cuts) and, past the stack, the wall cells up to it."""
    g, h, z, k = max(float(growth), 1.0), float(first_m), 0.0, 0.0
    for _ in range(max(0, int(n))):
        if z + h >= delta_m:
            return k + (delta_m - z) / h
        z += h
        k += 1.0
        h *= g
    return k + max(0.0, delta_m - z) / max(float(wall_cell_m), 1e-30)


def bl_layers(first_m: float, growth: float, n_plan: int, wall_cell_m: float, delta_m: float, *,
              stack_max_m: float, max_layers: int | None = None) -> int:
    """CFDREQ-17: the fewest layers, never fewer than the plan's, that put BL_CELLS across the boundary layer -
    at most `max_layers` (MAX_BRIDGE_LAYERS), and never a stack past `stack_max_m` (the stack the wall's cells hold:
    CFDREQ-9) or past the layer's own edge (a layer past it adds nothing across it)."""
    g, n = max(float(growth), 1.0), max(1, int(n_plan))
    max_layers = int(max_layers if max_layers is not None else MAX_BRIDGE_LAYERS)

    def height(k: int) -> float:
        return first_m * k if g <= 1.0001 else first_m * (g ** k - 1.0) / (g - 1.0)

    while (n < max_layers and cells_across(first_m, g, n, wall_cell_m, delta_m) < BL_CELLS
           and height(n) < delta_m and height(n + 1) <= stack_max_m * (1 + 1e-9)):
        n += 1
    return n


def wall_fit(ask: Ask, first_m: float, wall_cell_m: float, *, cap_rel: float = FIRST_CAP_REL) -> dict:
    """CFDREQ-13: what a wall-function first layer of `first_m` (sized for the conditions' y+) becomes under wall
    cells of `wall_cell_m`: {"treatment", "first_m", "y_plus", "why"}.
      - it fits under `cap_rel` of the wall cell: kept, y+ as targeted;
      - it does not, but the thickest that fits still puts the first cell centre at y+ 30 or more: that one;
      - else wall functions do not suit this wall (the cells the passage needs across are finer than a wall-function
        first cell): the wall is resolved - the first layer for y+ 1, by the same estimate.
    A laminar flow never reaches here (Ask.first_layer); a body wall functions do not suit (CFDREQ-17) has its
    resolved first layer already: kept, resolved."""
    if ask.fallback is not None:
        return {"treatment": "resolved", "first_m": float(first_m), "y_plus": y_plus_of(first_m, ask.how or {}),
                "why": fallback_words(ask, float(first_m), None, None), "fallback": ask.fallback.get("why"),
                "said": bool(ask.fallback.get("said"))}
    yp = float(ask.y_plus or 50.0)
    how = ask.how or {"visc_m": float(first_m) / (2.0 * yp)}
    visc = float(how.get("visc_m") or float(first_m) / (2.0 * yp))
    cap = float(cap_rel) * float(wall_cell_m)
    if first_m <= cap:
        return {"treatment": "functions", "first_m": float(first_m), "y_plus": y_plus_of(first_m, how, yp), "why": ""}
    y_cap = y_plus_of(cap, how, yp)
    if y_cap >= WALL_FUNCTIONS_BAND[0] * (1 - 1e-9):
        return {"treatment": "functions", "first_m": cap, "y_plus": y_cap,
                "why": f"the {_mm(wall_cell_m)} cells at the wall leave room for no thicker first layer"}
    h30 = 2.0 * WALL_FUNCTIONS_BAND[0] * visc
    h1 = RESOLVED_Y_PLUS * visc                     # a resolved wall's first layer: y+ 1 at its top, as typed
    out = {"treatment": "resolved", "first_m": h1, "y_plus": y_plus_of(h1, how, yp),
           "why": (f"wall functions do not suit this wall at these conditions: a first cell at y+ 30 would be "
                   f"{_mm(h30)} high, more than the {_mm(wall_cell_m)} cells the passage needs at the wall "
                   "allow, and a thinner one sits in the buffer layer (y+ 5-30), which no wall treatment models")}
    if not wall_functions_typed(ask):
        # CFDREQ-25: wall functions only ACCEPTED (the proposal, or nothing said): the proposal itself says the wall is
        # resolved where they do not fit, so keeping our cells across and resolving it is the ask applied, said with why
        # (judged on the resolved first layer built); typed, it stays the nearest there is, and the y+ row a miss
        out.update(fallback="cells", said=False)
    return out


def wall_words(ask: Ask, fit: Mapping, n: int, g: float, *, engine_words: str = "") -> str:
    """The plain line for the y+ row: which wall treatment was built, its first layer, layers, and the y+ it expects
    for the fluid and speed assumed."""
    ph = ask.req.get("physics") or {}
    fluid = str(ph.get("fluid") or "the fluid")
    u = ph.get("velocity_ms")
    at = f"{fluid} at {MR._g(float(u))} m/s" if u is not None else fluid
    re_s = f", Re {MR._g(ask.how['reynolds'])} on {ask.how.get('length_from')}" if ask.how.get("reynolds") else ""
    kind = "wall functions" if fit["treatment"] == "functions" else "a resolved wall"
    layers = f"{n} layer{'s' if n != 1 else ''}" + (f" growing {MR._g(g)}" if n > 1 else "")
    yc = float(fit["y_plus"])
    out = (f"{kind}: first layer {_mm(fit['first_m'])} high, {layers}, so the first cell's centre sits at y+ about "
           f"{MR._g(round(yc, 1) if yc >= 2 else round(yc, 2))} for {at}{re_s}")
    if fit.get("why"):
        out += f" ({fit['why']})"
    return out + engine_words


def _wrap_of(workspace) -> dict | None:
    """The CFD wrap rung's request and measured report when this attempt meshes a wrapped surface, else None."""
    try:
        from meshpipeline.pipeline import cfd_wrap
        req = cfd_wrap.read_request(Path(workspace))
        if not req:
            return None
        return {**req, "report": cfd_wrap.read_report(workspace) or {}}
    except Exception:  # noqa: BLE001 - a wrap we cannot read is no wrap for the notes
        return None


#: the wrap rung's degraded step: tetrahedra filled against the wrap's own triangles (pipeline/cfd_wrap)
_WRAP_AS_IS = "gmsh"
_SIZE_FIELDS = ("global", "near_wall", "min_edge", "max_edge")
_LAYER_FIELDS = ("layers:count", "layers:first_height", "layers:total_thickness", "layers:growth", "layers:y_plus")


def wrap_rows(ask: Ask, workspace) -> bool:
    """LAD-cfd-5 x CFDREQ-1: on the wrap rung the mesh is built on the wrapped surface, not the CAD. Each engine
    applies the notes there as on any surface; what that changes is said on its rows. On the degraded step (gmsh
    filling the wrap's own triangles) the wall keeps the wrap's triangles, so a size at the wall is the nearest,
    and no wall layers are built. The wrap's own distance is set by the geometry, not by the notes. True when the
    attempt is on the rung."""
    wrap = _wrap_of(workspace)
    if not wrap:
        return False
    rep = wrap.get("report") or {}
    far = rep.get("max_on_cad_m") if rep.get("max_on_cad_m") is not None else rep.get("max_m")
    within = f"within {_mm(float(far))} of your CAD" if isinstance(far, (int, float)) else "close to your CAD"
    clause = f"on the wrapped surface, which lies {within}; details narrower than the wrap are not in the mesh"
    as_is = ask.engine == _WRAP_AS_IS and bool(wrap.get("engine") == _WRAP_AS_IS or wrap.get("degraded"))
    for r in ask.req.get("rows") or []:
        rid, field = r["id"], str(r["field"])
        row = ask.rows.get(rid)
        if as_is and field in _LAYER_FIELDS:
            ask.rows[rid] = {"status": "not_applied", "built": "the last step of the wrap rung builds no wall layers "
                                                              "(tetrahedra filled against the wrapped surface)"}
            continue
        if as_is and field in ("near_wall", "min_edge") and row and row.get("status") == "applied":
            ask.rows[rid] = {**row, "status": "nearest",
                             "built": f"{row['built']} inside the volume; the wall keeps the wrapped surface's own "
                                      "triangles"}
            continue
        if row and row.get("status") in ("applied", "nearest") and field in _SIZE_FIELDS:
            row["note"] = clause if not row.get("note") else f"{row['note']}; {clause}"
    return True


#: how a line the user reads names the mesher: never by its name (contracts/narration's guard)
_ENGINE_WORDS = {"snappy": "this mesher", "cfmesh": "this mesher", "snappy_multiregion": "this mesher",
                 "gmsh": "this mesher", "vmtk": "this mesher", "snapgrid": "this mesher", "netgen": "this mesher"}


def _m(mm) -> float | None:
    return float(mm) * 1e-3 if mm is not None else None


def level_for(base_m: float, size_m: float, *, lo: int = 0, hi: int = 10) -> int:
    """The octree level whose cell (base / 2^level) is nearest `size_m`, held to [lo, hi]."""
    lv = int(round(math.log2(max(base_m, 1e-30) / max(size_m, 1e-30))))
    return int(min(hi, max(lo, lv)))


def cells_between_levels(growth: float) -> int:
    """snappyHexMesh's buffer for a growth rate: n cells per level grow sizes by about 2^(1/n) a cell."""
    return max(1, int(math.ceil(math.log(2.0) / math.log(float(growth)) - 1e-9)))


# #
# snappyHexMesh's sizes (external flow): the background cell, the wall's level, the finest level, the wake
# #

def _hard() -> int:
    from meshpipeline.settings import policy as pol
    return int(pol.CELL_HARD_LIMIT)


@guarded(lambda ask, ext, base, div, deficit: (list(div), int(deficit)))
def snappy_background(ask: Ask, ext, base: float, div, deficit: int) -> tuple[list[int], int]:
    """(divisions, levels to add) of the background box for the user's global size - or their largest cell when
    ours is larger. The box is cut in whole cubic cells; a size whose box alone would pass half the largest machine
    is doubled until it fits (the nearest, said). Levels are re-counted on the cell really used, so the wall keeps
    the absolute size the rules gave it."""
    from meshpipeline.engines.snappy.snappy_runner import BG_CELL_BUDGET, background_grid

    ours = max(float(ext[i]) / max(int(div[i]), 1) for i in range(3))
    want, field = ask.global_m, "global"
    if want is None:
        most = float(ask.max_m) if ask.max_m is not None else None
        if most is not None and ours > most * 1.0001:
            want, field = most, "max_edge"
        elif ask.wall_m is not None:
            # A SIZE AT THE WALLS ONLY: the background is nudged (under 1.42 times either way) to the wall size times
            # a whole power of two, so the wall's level lands on the size asked rather than the nearest halving
            if most is not None:
                ask.done("max_edge", f"the largest cells are {_mm(ours)}, already under it")
            nudged = float(ask.wall_m) * 2.0 ** round(math.log2(ours / float(ask.wall_m)))
            d2, _k = background_grid(ext, nudged, budget=BG_CELL_BUDGET)
            cell = max(float(ext[i]) / max(int(d2[i]), 1) for i in range(3))
            return list(d2), int(round(math.log2(cell / float(base))))
        else:
            ask.done("max_edge", f"the largest cells are {_mm(ours)}, already under it")
            return list(div), int(deficit)
    budget = max(BG_CELL_BUDGET, int(0.5 * _hard()))
    d2, k = background_grid(ext, float(want), budget=budget, max_div=4096)
    cell = max(float(ext[i]) / max(int(d2[i]), 1) for i in range(3))
    lv = int(round(math.log2(cell / float(base))))
    if k > 0:
        n = int(math.prod(math.ceil(float(e) / float(want)) for e in ext))
        ask.nearest(field, f"cells of {_mm(cell)} away from the body: {_mm(want)} cells across the whole box would be "
                           f"{_cells(n)} cells before any refinement, more than the largest machine we run holds",
                    built_m=cell)
    else:
        ask.done(field, f"cells of {_mm(cell)} away from the body (the box cut in whole cells), finer at the walls",
                 built_m=cell)
    if field == "global" and ask.max_m is not None:
        ask.done("max_edge", f"the largest cells are {_mm(cell)}")
    return list(d2), lv


def _region_of(where: str, regions) -> str | None:
    w = " ".join(re.sub(r"[_\-]+", " ", str(where)).casefold().replace("the ", " ").split())
    for r in regions:
        name = " ".join(re.sub(r"[_\-]+", " ", str(r)).casefold().split())
        if name and (name in w or (len(w) >= 3 and w in name)):
            return str(r)
    return None


@guarded(lambda ask, base, *, floor, level, flevel, regions, part_levels: (10, int(level), int(level), int(flevel)))
def snappy_levels(ask: Ask, base: float, *, floor: int, level: int, flevel: int, regions, part_levels: bool
                  ) -> tuple[int, int, int, int]:
    """(the finest level any cell may take, the wall's level twice, the feature edges' level) for the user's size
    at the walls, their sizes and refinements in named places, and their smallest cell. The body-sealing level
    `floor` is never undercut: coarser than it, the body would not be closed off from the flow (validity)."""
    hard_max = 10
    lv = int(level)
    ask.region_levels = {}
    if ask.wall_m is not None:
        want = level_for(base, ask.wall_m, lo=0, hi=hard_max)
        if want < floor:
            lv = floor
            ask.nearest("near_wall", f"cells of {_mm(base / 2 ** lv)} at the walls: cells larger than that would not "
                                     "hold the body's shape (the flow would leak into it)", built_m=base / 2 ** lv)
        else:
            lv = want
            ask.done("near_wall", f"cells of {_mm(base / 2 ** lv)} at the walls (the nearest size the background cell "
                                  "halves to)", built_m=base / 2 ** lv, level=lv)
    for e in ask.local:
        where = str(e.get("where") or "")
        if e.get("faces"):
            continue                                   # snappy_picked (CFDREQ-34)
        if "wake" in where.casefold():
            continue                                   # snappy_wake
        if part_levels:
            ask.done(e["id"], "set part by part (see the lines about your parts)")
            continue
        r = _region_of(where, regions)
        mine = r is not None or len(list(regions)) <= 1
        if not mine:
            ask.not_here(e["id"], f"no part named '{where}' in this model (its parts: {', '.join(map(str, regions))})")
            continue
        if e["what"] == "size":
            got = level_for(base, float(e["mm"]) * 1e-3, lo=floor, hi=hard_max)
        else:
            got = min(hard_max, lv + 1) if e["what"] == "finer" else max(floor, lv - 1)
        cell = base / 2 ** got
        # CFDREQ-35: a relative ask is judged against the cell the place had without it (`before_m`)
        rel = {"before_m": base / 2 ** lv} if e["what"] != "size" else {}
        if r is not None:
            ask.region_levels[r] = (got, got)
            ask.done(e["id"], f"cells of {_mm(cell)} on {r}", built_m=cell, patches=[r], **rel)
        else:
            if ask.wall_m is None:
                lv = got
            # CFDREQ-35: measured on every wall of the built mesh - never "applied" on the plan alone
            from meshpipeline.engines.cfd_picked import WALL_PATCHES
            ask.done(e["id"], f"read as the body's walls: cells of {_mm(cell)} there", built_m=cell,
                     patches=[WALL_PATCHES], **rel)
    cap = hard_max
    if ask.min_m is not None:
        most = int(math.floor(math.log2(base / ask.min_m) + 1e-9))
        if most < floor:
            cap = floor
            ask.nearest("min_edge", f"the finest cells are {_mm(base / 2 ** cap)}: larger ones would not hold the "
                                    "body's shape", built_m=base / 2 ** cap)
        else:
            cap = min(hard_max, most)
            ask.done("min_edge", f"the finest cells are {_mm(base / 2 ** cap)} (boundary layer cells are thinner, "
                                 "by design)", built_m=base / 2 ** cap)
        lv = min(lv, cap)
        ask.region_levels = {r: (min(a, cap), min(b, cap)) for r, (a, b) in ask.region_levels.items()}
    return cap, lv, lv, min(cap, max(lv, int(flevel)))


#: CFDREQ-17: the cells a body's wall keeps along its length however coarse a wall-function first layer would want them
#: (the CFD board's proof rule CB-E4: 100 wall faces along the body)
BODY_CELLS_ALONG = 100


def wall_function_level(ask: Ask | None, base: float, *, level: int, floor: int) -> int:
    """CFDREQ-17, THE WALL'S CELLS AND ITS FIRST LAYER CHOSEN TOGETHER (snappyHexMesh, a body in wall-function mode):
    the wall level, coarsened from `level` only as far as the wall-function first layer needs to fit under its wall
    cell (FIRST_CAP_REL) - never coarser than the body-sealing `floor` nor than BODY_CELLS_ALONG cells along the
    body. `level` unchanged when nothing of this applies: no wall-function y+, a height, stack, wall size or local
    size of the user's, a body wall functions do not suit (the resolved fallback), or a first layer that fits."""
    if (ask is None or ask.treatment != "functions" or ask.first_m is not None or ask.total_m is not None
            or ask.wall_m is not None or not ask.length_m or ask.y_plus is None
            or any("wake" not in str(e.get("where") or "").casefold() for e in ask.local)
            or (ask.req.get("physics") or {}).get("flow") == "internal"):
        return int(level)
    h, missing, how = y_plus_height(float(ask.y_plus), ask.req.get("physics"), length_m=ask.length_m,
                                    length_from=ask.length_from)
    if h is None or missing or how.get("laminar"):
        return int(level)
    delta = boundary_layer_m(how)
    if delta and h > WALL_FUNCTION_BL_SHARE * delta:
        return int(level)
    need = float(h) / FIRST_CAP_REL
    most = float(ask.length_m) / BODY_CELLS_ALONG
    lv = int(level)
    while base / 2 ** lv < need * (1 - 1e-9) and lv - 1 >= int(floor) and base / 2 ** (lv - 1) <= most * (1 + 1e-9):
        lv -= 1
    if lv < int(level):
        ask.wall_level_note = (f"the wall cells are {_mm(base / 2 ** lv)} (not {_mm(base / 2 ** int(level))}) so the "
                               f"wall-function first layer ({_mm(h)}) fits under them")
    return lv


#: CFDREQ-18: the fewest cells a passage keeps across its bore however coarse a wall-function first layer would want
#: the wall cells (engines/passage's resolution target, the CFD board's passage_cells_across limit)
WALL_FUNCTION_MIN_CELLS_ACROSS = 13


def wall_function_bore_cells(ask: Ask | None, *, bore_D: float, cells_across: int,
                             cap_rel: float | None = None) -> int:
    """CFDREQ-18, AN INTERNAL FLOW'S WALL CELLS AND ITS WALL-FUNCTION FIRST LAYER CHOSEN TOGETHER: the cells across the
    bore (`bore_D`, the wall cell is bore / cells across), lowered from the plan's `cells_across` only as far as the
    wall-function first layer the conditions' y+ gives needs to fit under the wall cell (FIRST_CAP_REL) - never under
    WALL_FUNCTION_MIN_CELLS_ACROSS. Before, the passage plan's fine wall cells (0.5-0.7 mm across a 44 mm venturi)
    left no room for a y+ 50 layer (1.9 mm in water at 1 m/s): wall_fit fell to a resolved wall, y+ 0.5 against the
    50 asked, on 92 of 96 internal parts (the CFD board, batch 6). Narrow passages keep their own refinement (the gap
    plan). `cap_rel`: the engine's thickest first layer as a share of the wall cell (cfMesh: CFMESH_STACK_REL).
    `cells_across` unchanged with no wall-function y+, a height, stack, wall or smallest size of the user's,
    a laminar flow, or a first layer that fits already; one too thick for the bore's log layer (past
    WALL_FUNCTION_BL_SHARE of its radius) is left to wall_fit, which says why."""
    typed = wall_functions_typed(ask)
    if (ask is None or (ask.treatment != "functions" and not typed) or ask.y_plus is None or ask.first_m is not None
            or ask.total_m is not None or ask.wall_m is not None or ask.min_m is not None or not bore_D
            or (ask.req.get("physics") or {}).get("flow") != "internal"):
        return int(cells_across)
    h, missing, how = y_plus_height(float(ask.y_plus), ask.req.get("physics"), length_m=float(bore_D),
                                    length_from="the bore")
    if h is None or missing or how.get("laminar"):
        return int(cells_across)
    cap = float(cap_rel or FIRST_CAP_REL)
    deepest = WALL_FUNCTION_BL_SHARE * 0.5 * float(bore_D)
    if h > deepest:
        # CFDREQ-25: typed, the thickest first layer the bore's log layer holds, if its centre is still at y+ 30+
        if not typed or y_plus_of(deepest, how, float(ask.y_plus)) < WALL_FUNCTIONS_BAND[0] * (1 - 1e-9):
            return int(cells_across)
        h = deepest
    want = int(math.floor(cap * float(bore_D) / float(h)))
    # CFDREQ-25: wall functions the user TYPED win over our minimum of cells across (warned); accepted ones do not
    least = wall_function_hard_cells(cap) if typed else WALL_FUNCTION_MIN_CELLS_ACROSS
    n = max(min(int(cells_across), want), min(int(cells_across), least))
    if n < int(cells_across):
        ask.wall_level_note = (f"the wall cells are {_mm(float(bore_D) / n)} ({n} across the bore, not "
                               f"{int(cells_across)}) so the wall-function first layer ({_mm(h)}) fits under them")
        if n < WALL_FUNCTION_MIN_CELLS_ACROSS:
            ask.wall_level_note += (f"; the passage now has {n} cells across its bore - we would normally keep at "
                                    f"least {WALL_FUNCTION_MIN_CELLS_ACROSS}, but you asked for wall functions, so "
                                    "the flow across the passage is coarser than we would choose")
    return n


#: CFDREQ-24: the most a wall-function first layer may stand over the cell above it, as a height ratio (the census's
#: GROWTH_MAX: the step a layer stack may take)
WALL_FUNCTION_JUMP = 2.0


def wall_function_keep_cell(ask: Ask | None, *, bore_D: float, wall_cell_m: float,
                            cap_rel: float = FIRST_CAP_REL) -> float | None:
    """CFDREQ-24, LOCAL REFINEMENT AT THE WALL SUITS THE WALL-FUNCTION LAYER: the smallest cell a local refinement (a
    thin feature's) may put at the wall of an internal flow in wall-function mode - the first layer that will be built
    there (the conditions' y+ on the bore, capped at `cap_rel` of the wall cell as wall_fit caps it) over `cap_rel`
    (CFDREQ-29: the cell the layer stands on keeps the share of it the wall cell was sized to give it). None when there is no such layer: no wall-function y+, a height or stack of the user's, a
    laminar flow, or a wall that will be resolved."""
    if (ask is None or (ask.treatment != "functions" and not wall_functions_typed(ask)) or ask.y_plus is None
            or ask.first_m is not None or ask.total_m is not None or not bore_D or not wall_cell_m
            or (ask.req.get("physics") or {}).get("flow") != "internal"):
        return None
    h, missing, how = y_plus_height(float(ask.y_plus), ask.req.get("physics"), length_m=float(bore_D),
                                    length_from="the bore")
    if h is None or missing or how.get("laminar"):
        return None
    built = float(h) if ask.treatment is None else min(float(h), float(cap_rel) * float(wall_cell_m))
    if y_plus_of(built, how, float(ask.y_plus)) < WALL_FUNCTIONS_BAND[0] * (1 - 1e-9):
        return None
    # CFDREQ-29: CELL HEIGHTS DO NOT SHRINK INTO A JUMP GOING AWAY FROM THE WALL. snappyHexMesh makes room for the
    # layer by pushing the snapped wall into the cell it stands on, so the cell above is about that cell less the
    # layer: the layer needs a cell at least built / cap_rel tall (the share wall_fit sizes the wall cell by; the cell
    # above is then 0.67 of the layer). The keep cell was the layer over WALL_FUNCTION_JUMP, which let a 1.57 mm layer
    # stand on 1.31 mm cells - the cell above read 0.33 of it on 61% of the wall (00157704, batch 7)
    return built / float(cap_rel)


def wall_functions_typed(ask: Ask | None) -> bool:
    """CFDREQ-25: the user asked for wall functions in so many words - the conditions step's choice (not only the
    proposal accepted), or a y+ of 30 or more typed in the notes (no wall treatment of the conditions behind it)."""
    if ask is None or ask.y_plus is None:
        return False
    if ask.treatment == "functions":
        return bool(ask.treatment_said)
    return ask.treatment is None and float(ask.y_plus) >= WALL_FUNCTIONS_BAND[0]


def wall_function_hard_cells(cap_rel: float) -> int:
    """CFDREQ-25: the fewest cells across a bore that a TYPED wall-function ask may lower it to - the count at which the
    engine's thickest first layer (`cap_rel` of the wall cell) is still inside the bore's log layer, a fifth of its
    radius (WALL_FUNCTION_BL_SHARE; past it the outer flow, not the log law, sets the velocity at the first cell's
    centre, so a wall function there is no model of the wall at all): snappyHexMesh 6, cfMesh 5."""
    return max(1, int(math.ceil(float(cap_rel) / (WALL_FUNCTION_BL_SHARE * 0.5) - 1e-9)))


#: CFDREQ-31: the share of the cell it stands on that cfMesh builds ONE wall-function layer at, with CFDREQ-30's
#: optimiser block (relThicknessTol 0.02), whatever maxFirstLayerThickness asks. Measured (testlab-1, cartesianMesh on
#: the kept cases, the product's layer census): 0.46-0.56 on eight batch-7 parts at their own cells, 0.48-0.50 on
#: 00134075 / 00163330 at four wall cells each (a layer planned at 0.37 of the cell still came out at 0.50), 0.35-0.39
#: on 00170318; the median 0.49. Under it the optimiser can collapse the layer towards a thin spot (00172106: 0.24 or
#: 0.56 of the cell for a 0.01% change of the cell; 00184523: 0.24 under its narrow-passage boxes) - not a share.
CFMESH_ONE_LAYER_SHARE = 0.5


def cfmesh_one_layer_cells(ask: Ask | None, *, bore_D: float, max_cell: float, wall_cell: float,
                           growth: float = 1.2, min_background: float = 0.0) -> tuple[float, float] | None:
    """CFDREQ-31, cfMesh's ONE WALL-FUNCTION LAYER PLANNED TO WHAT IT BUILDS: (wall cell, background cell) for an
    internal flow's wall-function wall on cfMesh, or None (the plan's `wall_cell` - CFDREQ-18's, landed on the
    background - stands). cfMesh builds one layer at CFMESH_ONE_LAYER_SHARE of the cell it stands on, so where the y+
    layer is thinner than that share of the plan's wall cell the wall cell is refined to the layer over the share
    (00134075: a 2.16 mm layer planned on 5.87 mm cells came out 2.96 mm, 1.37 x the plan; at 4.31 mm cells 0.99 x),
    and the background is set to it times the nearest whole power of two (never finer than `min_background`, the
    budget's) so cfMesh's halvings land on it. Never coarsened: a coarser wall than the plan's moves the passages under
    the gap plan's cells across and its boxes refine them back (00185618: 102k -> 503k cells in the first cut). Where
    the y+ layer is thicker than the share of the plan's wall cell, the cell stands and the y+ row says the layer is
    half of it, the y+ it gives and why (the nearest the passage's cells allow) - the wall cell actually built, not
    CFDREQ-18's figure the background may have held finer (00184523: said 3.65 mm, built 3.16 mm)."""
    typed = wall_functions_typed(ask)
    if (ask is None or (ask.treatment != "functions" and not typed) or ask.y_plus is None or ask.first_m is not None
            or ask.total_m is not None or ask.wall_m is not None or ask.min_m is not None or ask.n_layers is not None
            or not bore_D or not max_cell or not wall_cell
            or (ask.req.get("physics") or {}).get("flow") != "internal"):
        return None
    h, missing, how = y_plus_height(float(ask.y_plus), ask.req.get("physics"), length_m=float(bore_D),
                                    length_from="the bore")
    if h is None or missing or how.get("laminar"):
        return None
    deepest = WALL_FUNCTION_BL_SHARE * 0.5 * float(bore_D)
    if h > deepest:
        if not typed or y_plus_of(deepest, how, float(ask.y_plus)) < WALL_FUNCTIONS_BAND[0] * (1 - 1e-9):
            return None
        h = deepest
    share = CFMESH_ONE_LAYER_SHARE
    want = float(h) / share
    plan_wall = min(float(wall_cell), float(max_cell))          # cfMesh never builds a wall coarser than the background
    g = max(1.0, float(growth or 1.0))
    if float(h) * (1.0 + g) <= CFMESH_STACK_REL * plan_wall * (1 + 1e-9):
        # two layers or more fit the plan's stack (cfmesh_layer_fit counts them): cfMesh obeys the cap from two up
        # (CFDREQ-30), so the plan's cell stands - refining it here made 00152055's 3-layer plan one layer on
        # 29 x the cells (25.6k -> 748k)
        return None
    if want >= plan_wall * (1 - 1e-6):
        if float(h) > share * plan_wall * (1 + 1e-6):
            built = share * plan_wall
            ask.wall_level_note = (
                f"the wall cells are {_mm(plan_wall)} ({MR._g(round(float(bore_D) / plan_wall, 1))} across the "
                f"{_mm(float(bore_D))} bore, the cells the passage keeps) and this mesher builds one layer about "
                f"{MR._g(share)} of the cell it stands on, so the first layer is {_mm(built)}, not the {_mm(h)} a "
                f"y+ {MR._g(float(ask.y_plus))} needs: y+ about {MR._g(round(y_plus_of(built, how), 1))}, the nearest "
                "the passage's cells allow")
        ask.cfmesh_one_layer = {"wall_m": plan_wall, "how": how}
        return None
    wall = want
    k = max(0, int(round(math.log2(float(max_cell) / wall))))
    while wall * 2 ** k < float(min_background) * (1 - 1e-9):
        k += 1
    ask.wall_level_note = (f"the wall cells are {_mm(wall)} ({MR._g(round(float(bore_D) / wall, 1))} across the "
                           f"bore): this mesher builds one layer about {MR._g(share)} of the cell it stands on, so "
                           f"that is the {_mm(h)} the y+ asks")
    ask.cfmesh_one_layer = {"wall_m": wall, "how": how}
    return (wall * 1.001 if k > 0 else wall), wall * 2 ** k


def cfmesh_thin_spot_layer(ask: Ask | None, box_cells) -> None:
    """CFDREQ-31: narrow-passage boxes finer than the wall cell under cfMesh's one wall-function layer. The layer there
    is about CFMESH_ONE_LAYER_SHARE of the box's cell, and cfMesh's optimiser evens the layer across the wall towards
    it (00184523: 32 boxes at 1.27-1.72 mm under a 1.58 mm layer on 3.16 mm cells - built 0.75 mm, y+ 19). The
    passages keep the cells across they need; the y+ row says what the layer will be and why (the nearest)."""
    one = ask.cfmesh_one_layer if ask is not None else None
    cells = [float(c) for c in box_cells or [] if c and float(c) > 0.0]
    if not one or not cells or min(cells) >= float(one["wall_m"]) * (1 - 1e-3):
        return
    rid = ask.rid("layers:y_plus") if ask is not None else None
    row = ask.rows.get(rid) if ask is not None and rid else None
    if not row or row.get("treatment") != "functions":
        return
    finest = min(cells)
    thin = CFMESH_ONE_LAYER_SHARE * finest
    yp = y_plus_of(thin, one["how"])
    words = (f"; {len(cells)} narrow passage{'s are' if len(cells) != 1 else ' is'} meshed with cells down to "
             f"{_mm(finest)} (the cells across they need), and this mesher's one layer is about "
             f"{MR._g(CFMESH_ONE_LAYER_SHARE)} of the cell it stands on and is evened across the wall towards its "
             f"thinnest spot, so the first layer may come out near {_mm(thin)}: y+ about {MR._g(round(yp, 1))}")
    if yp < WALL_FUNCTIONS_BAND[0]:
        words += (f", under the {MR._g(WALL_FUNCTIONS_BAND[0])} wall functions need - these passages are too narrow "
                  "for the y+ asked")
    row["built"] = str(row.get("built") or "") + words
    row["status"] = "nearest"


@guarded(lambda ask, wake, **_k: wake)
def snappy_wake(ask: Ask, wake, *, wall_cell: float):
    """The wake request with the user's wake size or refinement (levels under the wall's), or unchanged."""
    import dataclasses

    for e in ask.local:
        if "wake" not in str(e.get("where") or "").casefold():
            continue
        if wake is None:
            ask.not_here(e["id"], "this mesh has no wake behind a body")
            continue
        if e["what"] == "size":
            n = int(min(4, max(0, round(math.log2(max(float(e["mm"]) * 1e-3, 1e-30) / wall_cell)))))
        else:
            n = 0 if e["what"] == "finer" else 2
        knobs = dict(wake.knobs) if isinstance(wake.knobs, Mapping) else {}
        knobs["levels_below_surface"] = n
        wake = dataclasses.replace(wake, knobs=knobs)
        ask.done(e["id"], f"the wake's finest cells are {_mm(wall_cell * 2 ** n)}, shortened if the cell budget "
                          "cannot hold its whole length", built_m=wall_cell * 2 ** n)
    return wake


#: the roles an opening is confirmed with at intake that make it an opening (port_declaration.json's "type")
OPENING_ROLES = ("inlet", "outlet", "opening", "farfield", "symmetry")


def port_roles(workspace) -> dict[str, str]:
    """CFDREQ-12: each declared opening's confirmed role by its name ({"p_1": "inlet", "p_2": "outlet"}), from the
    intake's port declaration (engines/workspace_facts.port_declaration); {} when none was confirmed."""
    from meshpipeline.engines.workspace_facts import port_declaration
    out: dict[str, str] = {}
    for p in port_declaration(workspace) if workspace else []:
        if isinstance(p, Mapping) and str(p.get("type") or "").lower() in OPENING_ROLES and p.get("name"):
            out[str(p["name"])] = str(p["type"]).lower()
    return out


def _role_of(name: str, roles: Mapping[str, str]) -> str:
    """An opening's confirmed role, by its name as declared or as the mesher spells it (p-1 / p_1)."""
    if name in roles:
        return roles[name]
    flat = {re.sub(r"[^A-Za-z0-9]+", "_", k).strip("_").lower(): v for k, v in roles.items()}
    return flat.get(re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower(), "")


def openings_named(where: str, openings: list[str], roles: Mapping[str, str]) -> tuple[list[str], str]:
    """CFDREQ-12: the openings a place's words name, and how ("" by name). A ROLE the user confirmed at intake comes
    first ("the outlet": the opening confirmed as the outlet, whatever the CAD called it; "outlet 2": the second
    of them; "the outlets", or one role several openings share with no number: all of them, said), then the name.
    The role wins over a name that is another opening's (said which was used). ([], "") when neither matches."""
    words = re.findall(r"[a-z0-9]+", str(where).casefold())
    have = {o: _role_of(o, roles) for o in openings}
    for role in dict.fromkeys(r for r in have.values() if r):
        if role not in words and f"{role}s" not in words:
            continue
        mine = [o for o in openings if have[o] == role]
        at = words.index(role) if role in words else words.index(f"{role}s")
        n = words[at + 1] if at + 1 < len(words) and words[at + 1].isdigit() else ""
        by_name = _region_of(where, openings)
        if n and 1 <= int(n) <= len(mine):
            mine = [mine[int(n) - 1]]
        how = f"the {role} you confirmed"
        if len(mine) > 1:
            how = f"all {len(mine)} openings you confirmed as {role}s"
        if by_name is not None and by_name not in mine:
            how += f"; the opening named '{by_name}' is your {have[by_name] or 'other opening'}, so it was not used"
        return mine, how
    hit = _region_of(where, openings)
    return ([hit], "") if hit is not None else ([], "")


def _openings_words(openings: list[str], roles: Mapping[str, str]) -> str:
    return ", ".join(f"{o} ({_role_of(o, roles)})" if _role_of(o, roles) else o for o in sorted(openings)) or "none"


@guarded(lambda ask, port_lvls, *a, **_k: dict(port_lvls))
def snappy_port_levels(ask: Ask, port_lvls: dict, names: Mapping, wall_key: str, *, base_m: float,
                       hard_max: int = 10, roles: Mapping[str, str] | None = None) -> dict:
    """An internal flow's sizes in named places: a port the user named - by the role they confirmed for it, or by
    its name (CFDREQ-12) - gets their size (or one level finer or coarser) on its own surface; the walls by name
    take the wall's own size; anything else is told, with every opening and its role."""
    out = dict(port_lvls)
    ports = {str(names[p]): p for p in names if p != wall_key}
    roles = dict(roles or {})
    for e in ask.local:
        where = str(e.get("where") or "")
        if e.get("faces"):
            continue                                   # snappy_picked (CFDREQ-34)
        if "wake" in where.casefold():
            ask.not_here(e["id"], "an internal flow has no wake behind a body")
            continue
        hits, how = openings_named(where, list(ports), roles)
        if not hits:
            if re.search(r"\b(wall|walls|surface|pipe|duct|passage|inside)\b", where, re.I):
                ask.not_here(e["id"], f"I could not tell which wall '{where}' is: {CLICK_WORDS}; for every wall, give "
                                      "it as a wall cell size")
            else:
                ask.not_here(e["id"], f"I could not tell where '{where}' is: no opening has that name (its openings: "
                                      f"{_openings_words(list(ports), roles)}); {CLICK_WORDS}", rule=RULE_PLACE_TOLD)
            continue
        made, held = [], []
        before = base_m / 2 ** int(out.get(ports[hits[0]], 1))
        for hit in hits:
            p = ports[hit]
            now = int(out.get(p, 1))
            if e["what"] == "size":
                lv = level_for(base_m, float(e["mm"]) * 1e-3, lo=1, hi=hard_max)
            else:
                lv = min(hard_max, now + 1) if e["what"] == "finer" else max(1, now - 1)
            if lv < now:
                # an opening is held at the level that keeps it open (coarser, castellation can close it)
                held.append((hit, now))
                continue
            out[p] = lv
            made.append((hit, lv))
        tail = f" ({how})" if how else ""
        if not made:
            hit, now = held[0]
            ask.nearest(e["id"], f"cells of {_mm(base_m / 2 ** now)} on {' and '.join(h for h, _n in held)}: larger "
                                 f"cells could close the opening{tail}", built_m=base_m / 2 ** now)
            continue
        lv = made[0][1]
        said = f"cells of {_mm(base_m / 2 ** lv)} on {' and '.join(h for h, _n in made)}{tail}"
        if held:
            said += (f"; {' and '.join(h for h, _n in held)} kept at cells of {_mm(base_m / 2 ** held[0][1])}: larger "
                     "cells could close the opening")
        ask.done(e["id"], said, built_m=base_m / 2 ** lv, patches=[str(names[ports[h]]) for h, _n in made],
                 **({"before_m": before} if e["what"] != "size" else {}))
    return out


@guarded(lambda ask, max_cells, estimate: int(max_cells))
def budget_for_sizes(ask: Ask, max_cells: int, estimate: float | None) -> int:
    """CFDREQ-3: the cell budget for the user's sizes. Theirs (a count they set) stands, and the sizes are said to
    give way to it where it is short; ours is raised to what their sizes need, up to the largest machine."""
    sized = [f for f in ("global", "near_wall", "min_edge", "local") if ask.rid(f)]
    if not sized or not estimate or estimate <= max_cells:
        return int(max_cells)
    hard = _hard()
    if ask.user_budget():
        for f in sized:
            rid = ask.rid(f)
            if rid in ask.rows:
                ask.rows[rid]["note"] = (f"your cell budget comes first: these sizes need about {_cells(estimate)} "
                                         "cells, so refinement stops at your count")
        return int(max_cells)
    new = int(min(hard, 1.1 * float(estimate)))
    ask.raised = True
    for f in sized:
        rid = ask.rid(f)
        if rid in ask.rows:
            ask.rows[rid]["note"] = (f"our cell budget was raised to {_cells(new)} for them" if estimate * 1.1 <= hard
                                     else f"they need about {_cells(estimate)} cells, more than the largest machine we "
                                          f"run holds: refinement stops at {_cells(new)}, so some cells may be larger")
            if estimate * 1.1 > hard and ask.rows[rid]["status"] == "applied":
                ask.rows[rid]["status"] = "nearest"
    return max(int(max_cells), new)


# #
# cfMesh (engines/cfmesh/cfmesh_runner.render_cfmesh_case): maxCellSize, the wall's localRefinement, the layers
# #

@guarded(lambda ask, strategy, **_k: dict(strategy))
def cfmesh_strategy(ask: Ask, strategy: Mapping, *, length_m: float | None = None) -> dict:
    """The plan with the user's layers in it: their count, their growth (thicknessRatio) and their first layer
    (maxFirstLayerThickness; a y+ turned into one). cfMesh sizes the stack from the wall cells: a stack thickness
    is not a setting it has."""
    out = dict(strategy)
    if ask.n_layers is not None:
        out["n_layers"] = int(ask.n_layers)
        ask.done("layers:count", "no boundary layers" if int(ask.n_layers) == 0 else
                 f"{int(ask.n_layers)} layers on every wall")
    n = int(out.get("n_layers", 0) or 0)
    if ask.layer_growth is not None:
        out["thickness_ratio"] = float(ask.layer_growth)
        ask.done("layers:growth", f"each layer {MR._g(ask.layer_growth)} times the one under it")
    if length_m and not ask.length_m:
        ask.length_m, ask.length_from = float(length_m), "the body's length"
    first = ask.first_layer(length_m=ask.length_m, length_from=ask.length_from)
    if first is not None:
        out["first_layer_thickness"] = float(first)
        ask.done("layers:first_height", f"first layer at most {_mm(first)} high")
    if ask.total_m is not None:
        ask.not_here("layers:total_thickness", "this mesher sizes the layer stack from the wall cells; it has no "
                                               "setting for the stack's thickness")
    if n == 0 and (first is not None or ask.layer_growth is not None) and ask.n_layers is None:
        out["n_layers"] = 3
        for f in ("layers:first_height", "layers:growth", "layers:y_plus"):
            rid = ask.rid(f)
            if rid in ask.rows:
                ask.rows[rid]["note"] = "3 layers were added for it: the plan had none"
    if ask.growth is not None:
        ask.not_here("growth", "this mesher halves cells from one size to the next and has no growth setting")
    cfmesh_limits(ask)
    return out


#: CFDREQ-13: cfMesh splits a stack about this share of its wall cell deep into its layers (measured on a 50 mm pipe
#: with 1.25 mm wall cells: 4 layers at 1.2 made 0.64 mm, the first 0.119 mm; the CFD board, tests/known_layers)
CFMESH_STACK_REL = 0.5

#: CFDREQ-30: cfMesh's layer optimiser, written out in full in every meshDict with layers. With one layer cfMesh
#: builds it at about 0.63 of the wall cell whatever maxFirstLayerThickness says, and with no optimisationParameters
#: block its own defaults are not the documented ones (the block below at relThicknessTol 0.1 already moved the
#: built layer). It is relThicknessTol that holds the layer to the height asked: measured on eight of batch 7's cfMesh
#: parts (one wall-function layer, nothing else changed), with no block -> 0.02 the first cell over the plan went
#: 1.27 -> 1.07 (00180172), 1.32 -> 1.05 (00188075), 1.34 -> 0.93 (00142033), 1.36 -> 1.07 (00178392), 1.25 -> 0.98
#: (00185618), 1.29 -> 1.11 (00172106), 1.70 -> 1.37 (00134075), 1.62 -> 1.30 (00163330), and the wall at plan with no
#: thinner cell above it 70 -> 86, 79 -> 88, 74 -> 90, 78 -> 98, 76 -> 83, 58 -> 74, 2 -> 66, 5 -> 73%. Tighter is not
#: safe: the optimiser evens the layer towards its thinnest spot, and at 0.01 / 0.005 the first cell fell to 0.47 /
#: 0.24 of the plan on 00142033 and 0.58 / 0.29 on 00178392 (a y+ under the band). featureSizeFactor 0.1-0.4,
#: maxNumIterations 1-20 and nSmoothNormals 3-5 moved nothing; with two layers or more the cap is obeyed already.
CFMESH_LAYER_OPTIMISATION = {"nSmoothNormals": 3, "maxNumIterations": 5, "featureSizeFactor": 0.4,
                             "reCalculateNormals": 1, "relThicknessTol": 0.02}


@guarded(lambda ask, strategy, wall_cell_m, **_kw: dict(strategy))
def cfmesh_layer_fit(ask: Ask | None, strategy: Mapping, wall_cell_m: float, *,
                     cells_across: float | None = None) -> dict:
    """CFDREQ-13 on cfMesh, once its wall cell is settled: the wall treatment's first layer fitted to it (wall_fit;
    its thickest first layer is the whole stack it splits, half the wall cell), and the layers counted so the stack
    starts at that height (maxFirstLayerThickness caps it; fewer layers make the first one thicker). Unchanged with no
    wall treatment's y+, a height of the user's own, or no layers."""
    out = dict(strategy)
    first = out.get("first_layer_thickness")
    if ask is None or not ask.treatment or ask.first_m is not None or first is None or not wall_cell_m:
        return out
    g = float(out.get("thickness_ratio", 1.2))
    stack = CFMESH_STACK_REL * float(wall_cell_m)
    if ask.treatment == "functions":
        fit = wall_fit(ask, float(first), float(wall_cell_m), cap_rel=CFMESH_STACK_REL)
    else:
        fit = {"treatment": "resolved", "first_m": float(first), "y_plus": y_plus_of(float(first), ask.how),
               "why": ""}
    if ask.n_layers is None and fit["treatment"] == "functions":
        if g > 1.0001:
            n = int(math.floor(math.log(1.0 + stack * (g - 1.0) / fit["first_m"]) / math.log(g)))
        else:
            n = int(stack // fit["first_m"])
        out["n_layers"] = max(1, min(MAX_WALL_FUNCTION_LAYERS, n))
    elif ask.n_layers is None and fit["treatment"] == "resolved":
        # CFDREQ-16: A RESOLVED WALL GROWS ITS STACK OUT TO THE CORE. cfMesh splits a stack about half the wall
        # cell deep into nLayers; the plan's two layers kept the 0.02 mm first layer but stepped from a 0.04 mm
        # stack straight into 5 mm core cells - those core cells read 84.7-88.7 degrees on the CFD board's five
        # resolve parts. As many layers as the stack holds from the asked first layer at the growth
        # CFMESH_RESOLVED_GROWTH, rounded DOWN, so the first layer the split gives is never thinner than asked (the
        # round bend reduction: 12 layers at 1.4, valid, 65.7 degrees, first cell at plan; 20 at 1.25 thinned it to
        # 0.37 of the plan). Never fewer than the plan's, never more than MAX_BRIDGE_LAYERS.
        n_plan = int(out.get("n_layers") or 0)
        if cells_across is not None and cells_across < GROW_MIN_CELLS_ACROSS:
            # ...ONLY WHERE THE PASSAGE HAS ROOM FOR IT: under the resolution floor's cells across the narrowest 5%
            # nearly every cell touches a wall, and a stack half a cell deep on each leaves no core (static_mixer_004:
            # 4.0 cells across at p05, 14 layers, 81 minutes of cartesianMesh and 179 degrees, no mesh; its 2-layer
            # mesh had delivered). The plan's own count stands, said on the row.
            ask.layer_note = ask.layer_note or (
                f"the passage is {cells_across:.1f} wall cells across at its narrowest, under the "
                f"{GROW_MIN_CELLS_ACROSS} a deeper stack needs, so the plan's {n_plan} layers were kept; finer cells "
                "in the passage would let the stack grow out to the core")
        else:
            g = max(g, CFMESH_RESOLVED_GROWTH)
            need = math.log(1.0 + stack * (g - 1.0) / float(fit["first_m"])) / math.log(g)
            out["n_layers"] = max(n_plan, min(MAX_BRIDGE_LAYERS, int(math.floor(need))))
            out["thickness_ratio"] = g
    n = int(out.get("n_layers") or 0)
    out["first_layer_thickness"] = float(fit["first_m"])
    natural = stack * (g - 1.0) / (g ** max(n, 1) - 1.0) if g > 1.0001 else stack / max(n, 1)
    built = min(float(fit["first_m"]), natural)
    yp = y_plus_of(built, ask.how, float(ask.y_plus or 1.0)) if ask.how else fit["y_plus"]
    _say_wall(ask, dict(fit, first_m=built, y_plus=yp), n, g)
    return out


#: CFDREQ-16: the growth a resolved wall's cfMesh stack takes from its first layer (snappy's capped stack's steepest,
#: MAX_CAPPED_GROWTH)
CFMESH_RESOLVED_GROWTH = 1.3
#: CFDREQ-16: a built first cell under this share of the asked one is told as thinner than asked (the census still
#: counts it as a layer at the height asked: finer is fine for the flow, but the number said is the built one)
THINNER_THAN_ASKED = 0.85
#: CFDREQ-16: the cells across the passage's narrowest 5% (2 x passage radius p05 / wall cell) a grown stack needs -
#: the resolution floor's own number (engines/resolution_policy, LAD-cfd-1)
GROW_MIN_CELLS_ACROSS = 12


#: cfMesh's own edge (engines/cfmesh/rescue.NEAR_EDGE_NON_ORTHO): past it the engine's optimiser is tried
CFMESH_EDGE_NON_ORTHO = 75.0


def non_ortho_limit(workspace) -> float | None:
    """CFDREQ-5 on cfMesh: the non-orthogonality the user asked for when it is under the engine's own edge (its
    optimiser and a protective re-mesh are then tried for theirs too), else None."""
    try:
        req = requirements_for(workspace)
    except Exception:  # noqa: BLE001
        return None
    lim = next((x for x in (req or {}).get("limits") or [] if x.get("measure") == "non_orthogonality"), None)
    if lim is None or float(lim["target"]) >= CFMESH_EDGE_NON_ORTHO:
        return None
    return float(lim["target"])


def cfmesh_limits(ask: Ask) -> None:
    """What cfMesh does with the user's quality limits: no setting while meshing; a non-orthogonality under its own
    edge sends the mesh through its optimiser (and a protective re-mesh) when it comes out past it."""
    lim = ask.limit("non_orthogonality")
    if lim is not None and float(lim["target"]) < CFMESH_EDGE_NON_ORTHO:
        ask.done(lim["id"], "if a cell comes out past it, the mesher's own optimiser and a re-mesh that protects the "
                            "layers are tried for it")


@guarded(lambda ask, max_cell, volume: max_cell)
def cfmesh_max_cell(ask: Ask, max_cell: float, volume: float) -> float:
    """maxCellSize for the user's global size (or largest cell): theirs, unless the background alone would pass half
    the largest machine - then the nearest that fits, said."""
    want, field = ask.global_m, "global"
    if want is None:
        if ask.max_m is None:
            return max_cell
        if max_cell <= ask.max_m * 1.0001:
            ask.done("max_edge", f"the largest cells are {_mm(max_cell)}, already under it")
            return max_cell
        want, field = ask.max_m, "max_edge"
    least = (float(volume) / (0.5 * _hard())) ** (1.0 / 3.0)
    if want < least:
        ask.nearest(field, f"cells of {_mm(least)} away from the walls: {_mm(want)} everywhere would be more cells "
                           "than the largest machine we run holds", built_m=least)
        want = least
    else:
        ask.done(field, f"cells of {_mm(want)} away from the walls", built_m=want)
    if field == "global" and ask.max_m is not None:
        ask.done("max_edge", f"the largest cells are {_mm(want)}")
    return float(want)


@guarded(lambda ask, wall_cell, max_cell, floor: (wall_cell, floor, True, max_cell))
def cfmesh_wall(ask: Ask, wall_cell: float, max_cell: float, floor: float) -> tuple[float, float, bool, float]:
    """(the wall cell, the finest cell any refinement may make, whether the engine may still coarsen the wall to fit
    its time, the background cell) for the user's size at the walls and their smallest cell. cfMesh lands on
    max / 2^n: with no background size of the user's, the background is nudged (under 1.42 times either way) to the
    wall size times a whole power of two, so the walls land on the size asked; with one, the nearest halving, said."""
    from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell, snap_to_octree

    fit = True
    if ask.wall_m is not None:
        w = float(ask.wall_m)
        if ask.global_m is None and ask.max_m is None and 0 < w < max_cell:
            max_cell = w * 2.0 ** round(math.log2(max_cell / w))
        n = max(0, round(math.log2(max_cell / w))) if w < max_cell else 0
        target = max_cell / 2.0 ** n
        wall_cell, fit = snap_to_octree(max_cell, target), False
        got = octree_cell(max_cell, wall_cell)
        if abs(got - w) / w > 0.05:
            ask.nearest("near_wall", f"cells of {_mm(got)} at the walls: the {_mm(max_cell)} background cell halves "
                                     f"to {_mm(got)}, the nearest to {_mm(w)}", built_m=got)
        else:
            ask.done("near_wall", f"cells of {_mm(got)} at the walls", built_m=got)
    if ask.min_m is not None:
        floor = max(float(floor), float(ask.min_m))
        if wall_cell < ask.min_m:
            wall_cell = float(ask.min_m)
            fit = False
        ask.done("min_edge", f"no refinement finer than {_mm(ask.min_m)} (boundary layer cells are thinner, by design)",
                 built_m=ask.min_m)
    return wall_cell, floor, fit, max_cell


@guarded(lambda ask, **k: k['wall_cell'])
def cfmesh_fit_planned(ask: Ask, *, wall_cell: float, max_cell: float, planned: float | None) -> float:
    """The wall cell under the user's cell ceiling (or target) when the case's own estimate (`planned`, the cells the
    case plans, walls and layers and boxes in) passes it: one octree level coarser per factor of 4 over (the wall's
    cells go as 1 / size^2), never coarser than the background - the sizes said to give way to their count."""
    limit = ask.ceiling if ask.ceiling is not None else ask.target
    if limit is None or not planned or planned <= float(limit):
        return wall_cell
    w = float(wall_cell)
    est = float(planned)
    while est > float(limit) and w * 2.0 <= max_cell:
        w *= 2.0
        est /= 4.0
    if w > wall_cell:
        for f in ("near_wall", "global", "min_edge"):
            rid = ask.rid(f)
            if rid in ask.rows:
                ask.rows[rid]["note"] = f"your cell count comes first: the walls were made {_mm(w)} to fit it"
                if ask.rows[rid]["status"] == "applied":
                    ask.rows[rid]["status"] = "nearest"
    return w


@guarded(lambda ask, **k: k['wall_cell'])
def cfmesh_fit_count(ask: Ask, *, volume: float, max_cell: float, wall_cell: float, wall_area: float | None,
                     n_layers: int) -> float:
    """The wall cell under the user's cell ceiling: one octree level coarser at a time while the estimate passes it
    (never coarser than the background), the sizes said to give way to their count."""
    if ask.ceiling is None and ask.target is None:
        return wall_cell
    from meshpipeline.engines.cfmesh.cfmesh_runner import estimate_cells

    limit = float(ask.ceiling if ask.ceiling is not None else (ask.target or 0.0))
    est = estimate_cells(volume=volume, max_cell=max_cell, wall_cell=wall_cell, wall_area=wall_area,
                         n_layers=n_layers)
    w = float(wall_cell)
    while est is not None and est > limit and w * 2.0 <= max_cell:
        w *= 2.0
        est = estimate_cells(volume=volume, max_cell=max_cell, wall_cell=w, wall_area=wall_area, n_layers=n_layers)
    if w > wall_cell:
        for f in ("near_wall", "global", "min_edge"):
            rid = ask.rid(f)
            if rid in ask.rows:
                ask.rows[rid]["note"] = f"your cell count comes first: the walls were made {_mm(w)} to fit it"
    return w


@guarded(lambda ask, patches, wall_patch, **k: ('', [], k['wall_cell']))
def cfmesh_local(ask: Ask, patches, wall_patch: str, *, max_cell: float, wall_cell: float, floor: float,
                 body_bbox=None, domain=None, budget: int | None = None,
                 roles: Mapping[str, str] | None = None,
                 internal: bool | None = None) -> tuple[str, list[dict], float]:
    """(localRefinement entries for the patches the user named, objectRefinements for a wake box). A coarser cell
    than the wall's on a named wall is not a refinement cfMesh makes: said. `internal`: the case is a flow through
    a passage (None: the flow the user's physics names) - then a place no part or opening is named for is told,
    never read as the body's walls (CFDREQ-35)."""
    from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell

    inside = _internal_flow(ask, internal)
    names = [str(p.get("name")) for p in (patches or []) if p.get("name")]
    # CFDREQ-12: the roles confirmed at intake - the patches' own types, over the port declaration's
    roles = {**dict(roles or {}), **{str(p["name"]): str(p.get("type") or "").lower() for p in (patches or [])
                                     if p.get("name") and str(p.get("type") or "").lower() in OPENING_ROLES}}
    entries, boxes = "", []
    for e in ask.local:
        where = str(e.get("where") or "")
        if e.get("faces"):
            continue                                   # cfmesh_picked (CFDREQ-34)
        size = float(e["mm"]) * 1e-3 if e["what"] == "size" else (wall_cell / 2.0 if e["what"] == "finer" else None)
        if "wake" in where.casefold():
            if body_bbox is None or domain is None:
                ask.not_here(e["id"], "this mesh has no wake behind a body")
                continue
            (b0, b1), (_d0, d1) = body_bbox, domain
            L = float(b1[0]) - float(b0[0])
            # the wake's cells: the user's size, else ("finer") twice the wall's - the near wake as fine as the
            # cells a wall cell from the body
            cell = max(floor, float(e["mm"]) * 1e-3 if e["what"] == "size" else 2.0 * wall_cell)
            x0, x1 = float(b1[0]), min(float(d1[0]), float(b1[0]) + 3.0 * L)
            span = [1.2 * (float(b1[k]) - float(b0[k])) for k in range(3)]
            # a box refines EVERY cell inside it: held to a quarter of the budget, made coarser one octree level
            # at a time when it would not fit (the nearest, said)
            vol = max(x1 - x0, 0.0) * span[1] * span[2]
            room = 0.25 * float(budget or _hard())
            asked_cell = cell
            while vol / cell ** 3 > room and cell * 2.0 <= max_cell:
                cell *= 2.0
            if e["what"] == "coarser":
                ask.not_here(e["id"], "this mesher refines a box; a coarser wake than the cells around it is not a "
                                      "refinement it makes")
                continue
            boxes.append({"name": "notesWake", "type": "box", "cellSize": cell,
                          "centre": [0.5 * (x0 + x1), 0.5 * (float(b0[1]) + float(b1[1])),
                                     0.5 * (float(b0[2]) + float(b1[2]))],
                          "lengthX": x1 - x0, "lengthY": span[1], "lengthZ": span[2]})
            said = (f"a box behind the body, 3 body lengths long, with cells of {_mm(octree_cell(max_cell, cell))} "
                    "(along +x, the flow)")
            if cell > asked_cell * 1.0001:
                ask.nearest(e["id"], said + f": {_mm(asked_cell)} cells in all of it would be more than a quarter of "
                                            "the cell budget", built_m=octree_cell(max_cell, cell))
            else:
                ask.done(e["id"], said, built_m=octree_cell(max_cell, cell))
            continue
        # CFDREQ-12: an opening by the role the user confirmed for it first ("the outlet"), then by its name
        hits, how = openings_named(where, [n for n in names if n != wall_patch], roles)
        if len(hits) > 1 or (hits and how):
            if size is None:
                ask.not_here(e["id"], "this mesher refines a surface; a coarser cell than the wall's is not a "
                                      "refinement it makes")
                continue
            size = max(size, floor)
            entries += "".join(f"    {h} {{ cellSize {size:.6g}; }}\n" for h in hits)
            ask.done(e["id"], f"cells of {_mm(octree_cell(max_cell, size))} on {' and '.join(hits)}"
                              + (f" ({how})" if how else ""), built_m=octree_cell(max_cell, size), patches=list(hits),
                     **({"before_m": octree_cell(max_cell, wall_cell)} if e["what"] != "size" else {}))
            continue
        hit = hits[0] if hits else _region_of(where, names)
        if hit is None:
            if (not inside and body_bbox is not None and len([n for n in names if n == wall_patch])
                    and len(names) <= 3 and e["what"] != "coarser"):
                hit = wall_patch          # one body in the flow: "around the wing" is its wall
            else:
                # CFDREQ-35: an internal flow's "the bend" is no place this mesher knows - told, never guessed onto
                # the walls (the elbow's "finer around the bend" was told applied while nothing there was finer). The
                # runner hands an internal case its fluid's box as `body_bbox` too, so the flow kind decides, not it
                ask.not_here(e["id"], f"I could not tell where '{where}' is: no part or opening has that name (its "
                                      f"parts: {_openings_words(names, roles)}); {CLICK_WORDS}", rule=RULE_PLACE_TOLD)
                continue
        if size is None:
            ask.not_here(e["id"], "this mesher refines a surface; a coarser cell than the wall's is not a refinement it "
                                  "makes")
            continue
        size = max(size, floor)
        got = octree_cell(max_cell, size)
        if hit == wall_patch:
            before = octree_cell(max_cell, wall_cell)
            if ask.wall_m is None:
                wall_cell = size
            # CFDREQ-35: a relative ask is judged against the cell the walls had without it (`before_m`)
            ask.done(e["id"], f"cells of {_mm(octree_cell(max_cell, wall_cell))} on the body's walls", built_m=got,
                     patches=[wall_patch], **({"before_m": before} if e["what"] != "size" else {}))
            continue
        entries += f"    {hit} {{ cellSize {size:.6g}; }}\n"
        ask.done(e["id"], f"cells of {_mm(got)} on {hit}", built_m=got, patches=[hit],
                 **({"before_m": octree_cell(max_cell, wall_cell)} if e["what"] != "size" else {}))
    return entries, boxes, wall_cell


#: CFDREQ-35: what the user is told to do when the place they named is not one the product knows
CLICK_WORDS = "click the faces on the model and send the size with them"
#: CFDREQ-35: a size in a place is applied only when measured there, and a place the product cannot tell is told
RULE_PLACE_TOLD = "CFDREQ-35"


def _internal_flow(ask: Ask, internal: bool | None) -> bool:
    """CFDREQ-35: whether the case is a flow through a passage - the runner's own word, else the user's physics."""
    if internal is not None:
        return bool(internal)
    return str(((ask.req or {}).get("physics") or {}).get("flow") or "").lower() == "internal"
#: CFDREQ-34: a picked face's refinement reaches this many of its own cells off the wall (the first cells too)
PICKED_BAND_CELLS = 3.0
#: CFDREQ-36: a size on a picked face is built as asked whenever the whole mesh then fits the run's cell limit
RULE_PICKED_PRICE = "CFDREQ-36"
#: CFDREQ-36: the octree cells cfMesh's surfaceMeshRefinement makes per wall face it adds, its refinementThickness
#: PICKED_BAND_CELLS cells (measured on the kept elbow case at 4.957 mm under a 9.91 mm wall: 219,559 octree cells
#: against 85,092 without the entry - 134 k for 17.8 k added faces; the boxes the surface cuts and the step down to
#: the wall's band are in it). snappy's distance band is PICKED_BAND_CELLS (19 a face with 16 layers, measured)
CFMESH_PICKED_BAND_CELLS = 7.5
#: CFDREQ-36: cfMesh halves its largest cell while a box is larger than the size asked, and a size written exactly
#: at a level (max / 2^n, to the dictionary's six digits) is read as over it - one level finer, 8x the band's cells
#: (the elbow: 1,089,248 octree cells for cellSize 0.0049573 under maxCellSize 0.0198292; 219,559 for
#: additionalRefinementLevels 2, the same level). The size written is nudged this much over the level priced
CFMESH_LEVEL_NUDGE = 1.001


def picked_faces_added(area_m2: float, cell_m: float, wall_cell_m: float) -> float:
    """CFDREQ-36: the wall faces a picked place of `area_m2` gains at `cell_m` over the plan's `wall_cell_m`."""
    return max(0.0, float(area_m2) * (1.0 / max(float(cell_m), 1e-30) ** 2 - 1.0 / max(float(wall_cell_m), 1e-30) ** 2))


def picked_estimate(*, asked_m: float, wall_cell_m: float, layers: int, limit: float,
                    area_m2: float | None = None, share: float | None = None, model_area_m2: float | None = None,
                    plan_cells: float | None = None, band_cells: float = PICKED_BAND_CELLS) -> dict:
    """CFDREQ-36, pure (the intake may call it at the plan step to warn before the run): the cells a size of
    `asked_m` on a picked face adds, and whether the whole mesh then fits `limit` (the count the user typed, else the
    product's own limit for the run).

    The face is `area_m2`, or `share` of a model of `model_area_m2`. Every wall face the refinement adds over the
    plan's `wall_cell_m` carries the plan's `layers` and about `band_cells` cells of the band off it (snappy:
    PICKED_BAND_CELLS; cfMesh's octree: CFMESH_PICKED_BAND_CELLS, measured) (the pipe
    elbow, measured on snappy with 16 layers: 4.34 M cells for 231 k added faces at 1.53 mm, 0.88 M for 46 k at
    3.08 mm - 19 a face both times). The plan's own count is `plan_cells` when the caller has it, else the model's
    wall priced the same way (its faces at the plan's wall cell, each with its layers and band) - the share is what
    makes that right (engines/cfd_picked.resolve `model_share`); with neither, the face's own wall.
    Returns {rule, cells_added, plan_cells, total, limit, fits}."""
    area = area_m2
    s = None
    try:
        s = float(share) if share is not None else None
    except (TypeError, ValueError):
        s = None
    if s is not None and not (s > 0.0 and math.isfinite(s)):
        s = None
    if area is None and s is not None and model_area_m2:
        area = s * float(model_area_m2)
    a = max(0.0, float(area or 0.0))
    per_face = max(0, int(layers)) + float(band_cells)
    added = picked_faces_added(a, float(asked_m), float(wall_cell_m)) * per_face
    if plan_cells is None:
        whole = (a / min(1.0, s)) if s is not None else (float(model_area_m2) if model_area_m2 else a)
        plan_cells = whole / max(float(wall_cell_m), 1e-30) ** 2 * per_face
    total = float(plan_cells) + added
    return {"rule": RULE_PICKED_PRICE, "cells_added": float(added), "plan_cells": float(plan_cells),
            "total": float(total), "limit": float(limit), "fits": bool(total <= float(limit))}


def _priced(ask: Ask, area: float, cell: float, coarser, *, wall_cell: float, share: float | None = None,
            layers: int = 0, plan_cells: float | None = None,
            band_cells: float = PICKED_BAND_CELLS) -> tuple[float, dict | None]:
    """CFDREQ-36: the picked place's cell - as asked when the whole mesh then fits the run's cell limit, else made
    coarser (by `coarser`) to the finest that fits. (cell, the estimate at the size asked when it was held coarser,
    else None)."""
    limit = float(ask.cell_limit(_hard()))

    def est(c: float) -> dict:
        return picked_estimate(asked_m=c, wall_cell_m=wall_cell, layers=layers, limit=limit, area_m2=area,
                               share=share, plan_cells=plan_cells, band_cells=band_cells)
    first = est(cell)
    if area <= 0 or first["fits"]:
        return cell, None
    while True:
        nxt = coarser(cell)
        if nxt is None:
            break
        cell = nxt
        if est(cell)["fits"]:
            break
    return cell, first


def _held_why(ask: Ask, asked: float, cell: float, need: dict) -> str:
    """CFDREQ-36: what the user is told when the size asked cannot fit the run's cell limit: asked, built, the cells
    the asked size would need, and how to get it."""
    theirs = ask.user_budget()
    lim = (f"the count you gave is {_cells(need['limit'])}" if theirs else
           f"this run's limit is {_cells(need['limit'])}")
    return (f" ({_mm(asked)} on that face needs about {_cells(need['total'])} cells; {lim}, so it was built at "
            f"{_mm(cell)}. Give a higher cell count to get {_mm(asked)}.)")


def _picked_words(ask: Ask, where: str, cell: float, asked: float | None, *, halves: str,
                  need: dict | None) -> str:
    said = f"cells of {_mm(cell)} on {where} and in the first cells off it"
    if asked is None:
        return said
    if need is not None:
        return said + _held_why(ask, asked, cell, need)
    if cell < asked * 0.999:
        return said + f" (you asked {_mm(asked)}: {halves}, and the next size up, {_mm(2.0 * cell)}, is over your size)"
    return said


@guarded(lambda ask, ws, **_k: [])
def snappy_picked(ask: Ask, ws, *, base_m: float, wall_level: int, hard_max: int = 10, box=None, layers: int = 0,
                  plan_cells: float | None = None) -> list[dict]:
    """CFDREQ-34: a size typed with faces picked, on snappyHexMesh: each picked place's own surface (picked_<n>.stl)
    refines the cells within PICKED_BAND_CELLS cells of it - a distance refinement, never a refinementSurface, so
    never a patch and the layers stay the wall's. Its level is the coarsest whose cell is AT OR UNDER the size asked
    (a size typed is a maximum: 0.62 mm for 0.5 asked would be over it, so 0.31 mm, said why), held to the hard
    level and to the price guard (coarser, said). Returns [{name, file, level, distance_m}]."""
    from meshpipeline.engines import cfd_picked as CP

    got = CP.framed(ws, box)
    out: list[dict] = []
    wall_cell = base_m / 2 ** int(wall_level)
    for e in ask.local:
        if not e.get("faces"):
            continue
        r = got.get(str(e["id"])) or {}
        where = str(e.get("where") or "the faces you picked")
        if not r.get("file"):
            ask.not_here(e["id"], r.get("gone") or f"{where} could not be found on this model")
            continue
        if e["what"] == "coarser":
            ask.not_here(e["id"], "this mesher refines near a face; cells coarser there than the walls' are not a "
                                  "refinement it makes")
            continue
        asked = float(e["mm"]) * 1e-3 if e["what"] == "size" else None
        want = (int(math.ceil(math.log2(base_m / asked) - 1e-9)) if asked else int(wall_level) + 1)
        lv = min(int(hard_max), max(0, want))
        area = float(r.get("area_m2") or 0.0)
        share = r.get("model_share")
        cell, need = _priced(ask, area, base_m / 2 ** lv, lambda c: 2.0 * c if 2.0 * c <= wall_cell * 1.0001 else None,
                             wall_cell=wall_cell, share=share, layers=layers, plan_cells=plan_cells)
        rule = RULE_PICKED_PRICE if need is not None else CP.RULE_BUILT
        facts = {"needs_cells": int(need["total"]), "cell_limit": int(need["limit"])} if need is not None else {}
        lv = int(round(math.log2(base_m / cell)))
        # CFDREQ-35: a relative ask is judged against the cell the face had without it
        rel = {"before_m": wall_cell} if asked is None else {}
        if lv <= int(wall_level):
            said = f"the walls already have cells of {_mm(wall_cell)} on {where}"
            if asked is not None and wall_cell <= asked * 1.0001:
                ask.done(e["id"], said, built_m=wall_cell, picked=r["file"], rule=CP.RULE_BUILT)
            else:
                # "finer" held at the walls' own cell is not finer: never told applied
                ask.nearest(e["id"], said + (_held_why(ask, asked, wall_cell, need) if need is not None and asked
                                             else ""), built_m=wall_cell, picked=r["file"], rule=rule, **facts, **rel)
            continue
        # the next place is priced on top of this one (CFDREQ-36: the whole mesh stays inside the limit)
        plan_cells = picked_estimate(asked_m=cell, wall_cell_m=wall_cell, layers=layers, limit=0.0, area_m2=area,
                                     share=share, plan_cells=plan_cells)["total"]
        out.append({"name": r["file"], "file": f"{r['file']}.stl", "level": lv,
                    "distance_m": PICKED_BAND_CELLS * cell})
        said = _picked_words(ask, where, cell, asked, halves=f"this mesher halves its {_mm(base_m)} background cell",
                             need=need)
        if want > hard_max or need is not None:
            ask.nearest(e["id"], said, built_m=cell, picked=r["file"], level=lv, rule=rule, **facts, **rel)
        else:
            ask.done(e["id"], said, built_m=cell, picked=r["file"], level=lv, rule=CP.RULE_BUILT, **rel)
    return out


@guarded(lambda ask, ws, **_k: "")
def cfmesh_picked(ask: Ask, ws, *, max_cell: float, wall_cell: float, floor: float, box=None, layers: int = 0,
                  plan_cells: float | None = None) -> str:
    """CFDREQ-34: a size typed with faces picked, on cfMesh: each picked place's own surface (picked_<n>.stl) as a
    surfaceMeshRefinement of the size asked over PICKED_BAND_CELLS of its cells - a refinement, never a patch. The
    octree lands on max / 2^n at or under it; held to the passage floor and the price guard (coarser, said). Returns
    the entries' text for meshDict's surfaceMeshRefinement."""
    from meshpipeline.engines import cfd_picked as CP
    from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell

    got = CP.framed(ws, box)
    text = ""
    for e in ask.local:
        if not e.get("faces"):
            continue
        r = got.get(str(e["id"])) or {}
        where = str(e.get("where") or "the faces you picked")
        if not r.get("file"):
            ask.not_here(e["id"], r.get("gone") or f"{where} could not be found on this model")
            continue
        if e["what"] == "coarser":
            ask.not_here(e["id"], "this mesher refines near a face; cells coarser there than the walls' are not a "
                                  "refinement it makes")
            continue
        asked = float(e["mm"]) * 1e-3 if e["what"] == "size" else None
        size = asked if asked else 0.5 * wall_cell
        floored = size < floor
        size = max(size, floor)
        area = float(r.get("area_m2") or 0.0)
        cell = octree_cell(max_cell, size)
        share = r.get("model_share")
        cell, need = _priced(ask, area, cell, lambda c: 2.0 * c if 2.0 * c <= wall_cell * 1.0001 else None,
                             wall_cell=wall_cell, share=share, layers=layers, plan_cells=plan_cells,
                             band_cells=CFMESH_PICKED_BAND_CELLS)
        rule = RULE_PICKED_PRICE if need is not None else CP.RULE_BUILT
        facts = {"needs_cells": int(need["total"]), "cell_limit": int(need["limit"])} if need is not None else {}
        # CFDREQ-35: a relative ask is judged against the cell the face had without it
        rel = {"before_m": wall_cell} if asked is None else {}
        if cell >= wall_cell * 0.9999:
            said = f"the walls already have cells of {_mm(octree_cell(max_cell, wall_cell))} on {where}"
            if asked is not None and wall_cell <= asked * 1.0001:
                ask.done(e["id"], said, built_m=wall_cell, picked=r["file"], rule=CP.RULE_BUILT)
            else:
                # "finer" held at the walls' own cell is not finer: never told applied
                ask.nearest(e["id"], said + (_held_why(ask, asked, wall_cell, need) if need is not None and asked
                                             else ""), built_m=wall_cell, picked=r["file"], rule=rule, **facts, **rel)
            continue
        got_e = picked_estimate(asked_m=cell, wall_cell_m=wall_cell, layers=layers, limit=0.0, area_m2=area,
                                share=share, plan_cells=plan_cells, band_cells=CFMESH_PICKED_BAND_CELLS)
        plan_cells = got_e["total"]
        # the refinement is in the plan the size brake measures the run against (render_cfmesh_case adds it)
        ask.picked_cells = float(getattr(ask, "picked_cells", 0.0) or 0.0) + got_e["cells_added"]
        text += (f'    {r["file"]} {{ surfaceFile "constant/triSurface/{r["file"]}.stl"; '
                 f"cellSize {cell * CFMESH_LEVEL_NUDGE:.6g}; refinementThickness {PICKED_BAND_CELLS * cell:.6g}; }}\n")
        said = _picked_words(ask, where, cell, asked, halves=f"this mesher halves its {_mm(max_cell)} largest cell",
                             need=need)
        if floored:
            ask.nearest(e["id"], said + f"; cells finer than {_mm(floor)} would cut the narrowest passage too fine",
                        built_m=cell, picked=r["file"], rule=rule, **facts, **rel)
        elif need is not None:
            ask.nearest(e["id"], said, built_m=cell, picked=r["file"], rule=rule, **facts, **rel)
        else:
            ask.done(e["id"], said, built_m=cell, picked=r["file"], rule=CP.RULE_BUILT, **rel)
    return text


def _place_row(workspace, req: Mapping, row: Mapping, p: Mapping) -> dict | None:
    """CFDREQ-35: a size in a place, judged on the BUILT mesh there: the median wall cell on the picked face (or the
    named patches) against the size asked - met at or under it; over it the size was not applied, said with the
    number built. None when the place was not measured (the row then says so)."""
    from meshpipeline.engines import cfd_picked as CP

    e = next((x for x in req.get("local") or () if str(x.get("id")) == str(row["id"])), None)
    if e is None or not (p.get("picked") or p.get("patches")):
        return None
    asked = float(e["mm"]) * 1e-3 if e.get("what") == "size" and e.get("mm") is not None else None
    limit = asked if asked is not None else (float(p["built_m"]) if p.get("built_m") else None)
    got = CP.measure(workspace, picked=p.get("picked"), patches=p.get("patches"), size_m=limit)
    if got is None or limit is None:
        return None
    med, p90, nf = got["median_m"], got["p90_m"], got["n_faces"]
    where = str(e.get("where") or "") or "there"
    on = f"median of {nf:,} wall faces on {where}; 90% at or under {_mm(p90)}"
    what = str(e.get("what") or "size")
    before = float(p["before_m"]) if asked is None and p.get("before_m") else None
    if what == "coarser" and asked is None:
        # CFDREQ-35: "coarser" is met by cells at least as large as planned and larger than without the ask
        met = med * CP.MET_SLACK >= limit and (before is None or med > CP.MET_SLACK * before)
    else:
        # a size is a maximum; "finer" is also finer than the cells the place had without the ask
        met = med <= CP.MET_SLACK * limit and (before is None or med * CP.MET_SLACK < before)
    head = f"asked {_mm(asked)}, built {_mm(med)}" if asked is not None else f"built {_mm(med)} (planned {_mm(limit)})"
    if before is not None:
        head = f"asked {what}, built {_mm(med)} (planned {_mm(limit)}; {_mm(before)} without the ask)"
    built = str(p.get("built") or "")
    if not met:
        return {"applied": False, "met": False, "measured": float(f"{med * 1e3:.4g}"),
                "delivered": f"not applied - {head} ({on}); the mesher was set for {built}" if built else
                f"not applied - {head} ({on})"}
    return {"applied": True, "met": True if p.get("status") == "applied" else None,
            "measured": float(f"{med * 1e3:.4g}"), "delivered": f"{head} ({on}): {built}" if built else f"{head} ({on})"}


# #
# the gmsh flow mesher (engines/gmsh/driver.py): the body / fill size, the smallest and largest tetrahedron
# #

@guarded(lambda ask, h, **_k: (h, None))
def gmsh_sizes(ask: Ask, h: float, *, external: bool) -> tuple[float, float | None]:
    """(the element size, the smallest it may take or None) for the user's sizes: their size at the walls (else
    their global size) is the body's size on an external flow and the fill's on an internal one - over our
    cells-across rule when it is coarser (their size beats our accuracy minimum, warned). Their largest caps the far
    field; boundary layers, a cell count, a growth and sizes in named places are not settings this mesher has."""
    want = ask.wall_m if ask.wall_m is not None else ask.global_m
    field = "near_wall" if ask.wall_m is not None else "global"
    if want is not None:
        if want > h * 1.0001:
            ask.done(field, f"tetrahedra of {_mm(want)} {'at the body' if external else 'in the passage'} - coarser "
                            f"than our rule's {_mm(h)} for this shape, so results may be less exact", built_m=want)
        else:
            ask.done(field, f"tetrahedra of {_mm(want)} {'at the body' if external else 'in the passage'}",
                     built_m=want)
        h = float(want)
        if ask.wall_m is not None and ask.global_m is not None:
            ask.done("global", f"tetrahedra grow from {_mm(h)} at the body to the far field")
    if ask.max_m is not None:
        if h > ask.max_m:
            h = float(ask.max_m)
        ask.done("max_edge", f"no tetrahedron edge aimed over {_mm(ask.max_m)}", built_m=ask.max_m)
    lo = None
    if ask.min_m is not None:
        lo = float(ask.min_m)
        if h < lo:
            h = lo
        ask.done("min_edge", f"no tetrahedron edge aimed under {_mm(lo)}", built_m=lo)
    for f in ("cells:target", "cells:ceiling", "cells:floor"):
        if ask.rid(f):
            ask.not_here(f, "this mesher sizes by element size, not by a count; the count is measured")
    for f in ("layers:count", "layers:first_height", "layers:total_thickness", "layers:growth", "layers:y_plus"):
        if ask.rid(f):
            ask.not_here(f, "this mesher makes tetrahedra only, with no boundary layers")
    if ask.growth is not None:
        ask.not_here("growth", "this mesher grades sizes by distance from the body and has no growth setting")
    for e in ask.local:
        ask.not_here(e["id"], "this mesher has no setting for a size in one named place")
    return h, lo


def gmsh_far(workspace, h_far: float) -> float:
    """The far field's largest tetrahedron under the user's largest cell (unchanged with none asked)."""
    req = requirements_for(workspace)
    hi = ((req or {}).get("sizes") or {}).get("max_edge_mm")
    return min(float(h_far), float(hi) * 1e-3) if hi else float(h_far)


# #
# the snap-grid mesher (engines/snapgrid, electronics thermal): the background cell, the growth, the budget
# #

@guarded(lambda ask, plan: dict(plan))
def snapgrid_plan(ask: Ask, plan: Mapping) -> dict:
    """The detail level's plan with the user's background cell (global or largest), their growth and their cell
    count. The snap grid sizes the rest from the parts' own thicknesses, gaps and heat (its thermal rules): a size
    at the walls, a smallest cell, boundary layers and sizes in named places are not settings it has."""
    out = dict(plan)
    size = ask.global_m if ask.global_m is not None else ask.max_m
    if size is not None:
        out["background_size"] = float(size)
        field = "global" if ask.global_m is not None else "max_edge"
        ask.done(field, f"background cells of {_mm(size)} (finer round the parts, by their thicknesses and gaps; "
                        "coarser only if the cell budget cannot hold them, said)", built_m=size)
        if field == "global" and ask.max_m is not None:
            ask.done("max_edge", f"the background cells are {_mm(size)}")
    if ask.growth is not None:
        g = max(1.05, float(ask.growth))
        out["growth"] = g
        if g > float(ask.growth):
            ask.nearest("growth", f"sizes grow by at most {MR._g(g)}, the least this grid grades by")
        else:
            ask.done("growth", f"sizes grow by at most {MR._g(g)} from one cell to the next")
    if ask.user_budget() or ask.floor is not None:
        out["max_cells"] = ask.budget(int(out.get("max_cells") or _hard()))
    why = "the snap grid sizes cells from the parts' own thicknesses, gaps and heat"
    for f in ("near_wall", "min_edge"):
        if ask.rid(f):
            ask.not_here(f, why)
    for f in ("layers:count", "layers:first_height", "layers:total_thickness", "layers:growth", "layers:y_plus"):
        if ask.rid(f):
            ask.not_here(f, "the snap grid has no prism layers; its cells at heated faces follow the detail level")
    for e in ask.local:
        ask.not_here(e["id"], why)
    return out


# #
# the vessel mesher (engines/vmtk): edge-length clamps on its radius-adaptive size, the layers, the budget
# #

#: CFDREQ-13: VMTK's stack is a share of the local radius; never deeper than our own 0.10 (vmtk/pack.py: thicker stacks
#: fold at branch crotches; the board, 20 Nov: 0.148 with 4 layers needed untangling on a transition duct, VMTK failed)
VMTK_STACK_MAX = 0.10
VMTK_GROWTH = 1.25


def _vmtk_wall(ask: Ask, out: dict, radius_m: float) -> bool:
    """CFDREQ-13 on VMTK: the wall treatment's first layer as its stack (a share of the vessel's radius, over the
    reference radius). True when it decided the y+ row."""
    if not ask.treatment or ask.first_m is not None or ask.y_plus is None or not radius_m:
        return False
    first = ask.first_layer(length_m=2.0 * float(radius_m), length_from="the vessel's diameter")
    if first is None:
        return True                                       # laminar, or numbers missing: the row says which
    R = float(radius_m)
    ef = float(out.get("edge_length_factor") or 0.15)
    g = float(out.get("layer_growth") or VMTK_GROWTH)
    _bl = out.get("boundary_layers")
    n0 = int(_bl) if _bl is not None else 5
    t0 = float(out.get("boundary_layer_thickness_factor") or 0.10)
    if n0 <= 0:
        return False
    if ask.treatment == "functions":
        fit = wall_fit(ask, float(first), ef * R, cap_rel=min(FIRST_CAP_REL, VMTK_STACK_MAX / ef))
    else:
        fit = {"treatment": "resolved", "first_m": float(first), "y_plus": y_plus_of(float(first), ask.how),
               "why": ""}
    if fit["treatment"] == "functions":
        n = n0 if ask.n_layers is not None else stack_count(fit["first_m"], ef * R, g)
        while True:
            t = fit["first_m"] * ((g ** n - 1.0) / (g - 1.0) if g > 1.0001 else n) / R
            if t <= VMTK_STACK_MAX or n <= 1 or ask.n_layers is not None:
                break
            n -= 1
        t = min(t, VMTK_STACK_MAX)
        out["boundary_layers"], out["boundary_layer_thickness_factor"] = int(n), float(f"{t:.4g}")
        built = t * R * ((g - 1.0) / (g ** n - 1.0) if g > 1.0001 else 1.0 / n)
        _say_wall(ask, dict(fit, first_m=built, y_plus=y_plus_of(built, ask.how, float(ask.y_plus))), n, g)
        return True
    # a resolved wall: a stack thin enough for y+ 1 makes slivers here (under 5% of the radius), so the stack stays
    # ours and the row says the y+ it gives
    built = t0 * R * ((g - 1.0) / (g ** n0 - 1.0) if g > 1.0001 else 1.0 / n0)
    yp = y_plus_of(built, ask.how, float(ask.y_plus))
    _say_wall(ask, dict(fit, first_m=built, y_plus=yp,
                        why=(fit.get("why", "") + ("; " if fit.get("why") else "")
                             + "this mesher's thinnest stack that does not fold is ours")), n0, g)
    return True


@guarded(lambda ask, s, **kw: dict(s))
def vmtk_spec(ask: Ask, s: Mapping, *, radius_m: float | None = None) -> dict:
    """The VMTK spec with the user's sizes as clamps on its radius-adaptive edge length (metres), their layer count
    and growth, and their cell count as its budget. A size in a named place, a volume growth, a stack thickness and
    a first layer height are not settings it has (it sizes everything from the local vessel radius): said."""
    out = dict(s)
    lo, hi = out.get("min_edge_length"), out.get("max_edge_length")
    if ask.wall_m is not None:
        lo = hi = ask.wall_m
        ask.done("near_wall", f"wall triangles of {_mm(ask.wall_m)} (the cells inside follow them)", built_m=ask.wall_m)
    elif ask.global_m is not None:
        lo, hi = 0.5 * ask.global_m, 1.5 * ask.global_m
        ask.done("global", f"cells between {_mm(lo)} and {_mm(hi)}, sized by the vessel's radius inside that band",
                 built_m=ask.global_m)
    if ask.min_m is not None:
        lo = max(float(lo or 0.0), ask.min_m)
        ask.done("min_edge", f"no cell edge under {_mm(ask.min_m)}", built_m=ask.min_m)
    if ask.max_m is not None:
        hi = min(float(hi), ask.max_m) if hi else ask.max_m
        ask.done("max_edge", f"no cell edge over {_mm(ask.max_m)}", built_m=ask.max_m)
    if lo is not None and hi is not None and float(lo) > float(hi):
        lo = hi
    if lo is not None:
        out["min_edge_length"] = float(lo)
    if hi is not None:
        out["max_edge_length"] = float(hi)
    # NOTE-13: the controls the user's words set - cells across every passage is the edge factor (2 / N)
    out = ask.controls(out)
    if ask.user_budget() or ask.floor is not None:
        out["max_cells"] = ask.budget(int(out.get("max_cells") or _hard()))
    if ask.n_layers is not None:
        out["boundary_layers"] = int(ask.n_layers)
        ask.done("layers:count", "no boundary layers" if int(ask.n_layers) == 0 else
                 f"{int(ask.n_layers)} layers on the vessel wall (the budget may take some back, said if it does)")
    if ask.layer_growth is not None:
        out["layer_growth"] = float(ask.layer_growth)
        ask.done("layers:growth", f"each layer {MR._g(ask.layer_growth)} times the one under it")
    walled = _vmtk_wall(ask, out, float(radius_m or 0.0))
    for f in ("layers:first_height", "layers:total_thickness", "layers:y_plus"):
        if ask.rid(f) and not (walled and f == "layers:y_plus"):
            ask.not_here(f, "this mesher sizes the layer stack as a share of the local vessel radius; an absolute layer "
                            "thickness is not a setting it has")
    if ask.growth is not None:
        ask.not_here("growth", "this mesher sizes every cell from the local vessel radius and has no growth setting")
    for e in ask.local:
        ask.not_here(e["id"], "this mesher sizes cells from the local vessel radius; it has no setting for one named place")
    return out


def cfmesh_cells_built(ask: Ask, asked: Mapping, *, passage_d: float, wall_cell: float) -> None:
    """NOTE-13 on cfMesh: the cells across the passage the octree really builds (its width over the cell cfMesh lands
    on at the walls) against the count asked - said with the number either way."""
    rid, n = asked.get("id"), asked.get("value")
    row = ask.rows.get(str(rid or ""))
    if not row or row.get("status") != "applied" or not n or wall_cell <= 0 or passage_d <= 0:
        return
    got = passage_d / float(wall_cell)
    said = f"{MR._g(round(got, 1))} cells across the passage (cells of {_mm(wall_cell)} at its walls)"
    if got < 0.95 * float(n):
        ask.nearest(str(rid), f"{said}: the finest the passage's own cap and the run's time allow")
    else:
        ask.done(str(rid), said)


def vmtk_cells_built(ask: Ask, edge_length_factor: float) -> None:
    """NOTE-13 on VMTK: the cells across every passage the spec really carries (2 / its edge factor, after the cell
    budget's fit) against the count asked - kept as asked, or the nearest with the number."""
    c = (ask.req.get("controls") or {}).get("cells_across") or {}
    rid, n = c.get("id"), c.get("value")
    row = ask.rows.get(str(rid or ""))
    if not row or row.get("status") != "applied" or not n or edge_length_factor <= 0:
        return
    got = 2.0 / float(edge_length_factor)
    if got < 0.95 * float(n):
        ask.nearest(str(rid), f"{MR._g(round(got, 1))} cells across every passage: {int(n)} would not fit the cell "
                              "budget")


# #
# the internal-flow tetrahedra + prism mesher (engines/netgen): size clamps, the budget, the prism stack
# #

@guarded(lambda ask, *, layers, **_k: {"layers": int(layers)})
def netgen_inputs(ask: Ask, *, layers: int, port_diameters, soft_budget: int, cell_limit: int) -> dict:
    """make_plan's keywords (engines/netgen/plan.py) for the user's requirements: their size at the walls (every
    point Netgen sizes is a wall point), else their global size as a band, their smallest and largest edge as
    clamps on the passage-sized edge; their cell count as the plan's budget (a target also refines toward it);
    their layer count, first layer (or y+, with the widest opening as the reference length), stack and growth.
    Netgen grades its cells by its own rate: a growth ratio is not a setting it has, nor is a size in one named
    place. Nothing asked: {"layers": layers}, the plan exactly as the rules make it."""
    out: dict = {"layers": int(layers)}
    lo = hi = None
    if ask.wall_m is not None:
        lo = hi = ask.wall_m
        ask.done("near_wall", f"wall cells of {_mm(ask.wall_m)} (the cells inside grade from them)", built_m=ask.wall_m)
        if ask.global_m is not None:
            ask.done("global", f"the cells grow from {_mm(ask.wall_m)} at the walls into the passage")
    elif ask.global_m is not None:
        lo, hi = 0.5 * ask.global_m, 1.5 * ask.global_m
        ask.done("global", f"cells between {_mm(lo)} and {_mm(hi)}, sized by the local passage inside that band",
                 built_m=ask.global_m)
    if ask.min_m is not None:
        lo = max(float(lo or 0.0), ask.min_m)
        ask.done("min_edge", f"no cell edge aimed under {_mm(ask.min_m)}", built_m=ask.min_m)
    if ask.max_m is not None:
        hi = min(float(hi), ask.max_m) if hi is not None else ask.max_m
        ask.done("max_edge", f"no cell edge aimed over {_mm(ask.max_m)}", built_m=ask.max_m)
    if lo is not None and hi is not None and float(lo) > float(hi):
        lo = hi
    if lo is not None:
        out["size_lo"] = float(lo)
    if hi is not None:
        out["size_hi"] = float(hi)
    if ask.user_budget() or ask.floor is not None:
        out["soft_budget"] = ask.budget(int(soft_budget), hard=int(cell_limit))
        out["user_budget"] = ask.user_budget()
        if ask.target is not None:
            out["target_cells"] = min(int(ask.target), int(cell_limit))
    n = int(layers)
    if ask.n_layers is not None:
        n = int(ask.n_layers)
        out["layers"] = n
        ask.done("layers:count", "no boundary layers" if n == 0 else
                 f"{n} prism layers on every wall (a thinner stack, then none, only if the mesher cannot build them; "
                 "said if so)")
    if n == 0:
        for f in ("layers:first_height", "layers:total_thickness", "layers:growth", "layers:y_plus"):
            if ask.rid(f):
                ask.not_here(f, "no boundary layers are built on this mesh")
    else:
        g = float(ask.layer_growth) if ask.layer_growth is not None else None
        if g is not None:
            out["growth"] = g
            ask.done("layers:growth", f"each layer {MR._g(g)} times the one under it")
        d = max([float(x) for x in port_diameters or []] or [0.0]) or None
        first = ask.first_layer(length_m=d, length_from="the widest opening's diameter")
        if first is not None:
            out["first_layer"] = float(first)
            if ask.first_m is not None:
                ask.done("layers:first_height", f"a first layer {_mm(first)} high")
        if ask.total_m is not None:
            if first is not None and ask.n_layers is not None:
                gg = g if g is not None else 1.2
                got = float(first) * sum(gg ** k for k in range(n))
                ask.nearest("layers:total_thickness", f"your first layer, count and growth make a stack {_mm(got)} "
                                                      "thick, so that is built")
            else:
                out["layer_stack"] = float(ask.total_m)
                ask.done("layers:total_thickness", f"a layer stack {_mm(ask.total_m)} thick")
    if ask.growth is not None:
        ask.not_here("growth", "this mesher grades its cells from the walls inward at its own rate; it has no growth "
                               "setting")
    for e in ask.local:
        ask.not_here(e["id"], "this mesher sizes every wall point from the local passage; it has no setting for one "
                              "named place")
    return out


def netgen_planned(ask: Ask, plan: Mapping) -> None:
    """After make_plan: when the user's own sizes held every size against their cell count (a wall size and a count
    that do not fit together), the sizes are kept and the count is the nearest, said with the plan's estimate."""
    if not plan.get("sizes_held"):
        return
    est = int(plan.get("estimated_cells") or 0)
    for f in ("cells:target", "cells:ceiling", "cells:floor"):
        if ask.rid(f):
            ask.nearest(f, f"your sizes make about {_cells(est)} cells, so the mesh follows your sizes, not the count",
                        planned=est)


def netgen_built(workspace, built: Mapping | None) -> None:
    """After the native run (NG-5's ladder): a mesh built on a thinner rung than planned changes what the layer
    rows say - the stack half as thick, or no layers. Nothing to do on the planned rung or with no record."""
    rung = int((built or {}).get("rung") or 1)
    path = Path(workspace) / PLAN_FILE
    if rung <= 1 or not path.exists():
        return
    try:
        rec = json.loads(path.read_text())
        req = requirements_for(workspace) or {}
        ids = {r["field"]: r["id"] for r in req.get("rows") or []}
        why = ("the planned layers could not be built, so they were built half as thick" if int(
            (built or {}).get("layers") or 0) else "no layers could be built here, so the mesh has none")
        for f in ("layers:count", "layers:first_height", "layers:total_thickness", "layers:y_plus"):
            rid = ids.get(f)
            row = (rec.get("rows") or {}).get(rid) if rid else None
            if row and row.get("status") in ("applied", "nearest"):
                row.update(status="nearest", built=f"{row.get('built')}; {why}")
        path.write_text(json.dumps(rec, indent=1, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("cfd requirements: the Netgen rung was not told", exc_info=True)


# #
# the multi-region mesher (engines/snappy_multiregion): the background cell, the regions' levels
# #

@guarded(lambda ask, base_cell, surface_level, **_k: (base_cell, tuple(surface_level), {}))
def multiregion_sizes(ask: Ask, base_cell: float, surface_level, *, extent, regions) -> tuple[float, tuple, dict]:
    """(the background cell, the surface levels, levels per named region) for the user's global or largest cell,
    their size at the walls and their sizes in named regions. A background past half the largest machine is the
    nearest that fits, said."""
    lo, hi = int(surface_level[0]), int(surface_level[1])
    base = float(base_cell)
    want, field = ask.global_m, "global"
    if want is None and ask.max_m is not None:
        want, field = (None, "") if base <= ask.max_m * 1.0001 else (ask.max_m, "max_edge")
        if want is None:
            ask.done("max_edge", f"the largest cells are {_mm(base)}, already under it")
    if want is not None:
        vol = float(math.prod(max(float(e), 1e-9) for e in extent))
        least = (vol / (0.5 * _hard())) ** (1.0 / 3.0)
        new = max(float(want), least)
        k = int(round(math.log2(new / base)))
        if ask.wall_m is None:
            lo, hi = max(0, lo + k), max(0, hi + k)
        base = new
        if new > want * 1.0001:
            ask.nearest(field, f"background cells of {_mm(new)}: smaller ones would be more cells than the largest "
                               "machine we run holds", built_m=new)
        else:
            ask.done(field, f"background cells of {_mm(new)}, finer at the walls", built_m=new)
        if field == "global" and ask.max_m is not None:
            ask.done("max_edge", f"the largest cells are {_mm(new)}")
    if ask.wall_m is not None:
        if want is None:
            # a wall size alone: the background nudged (under 1.42 times either way) so the walls land on it
            base = float(ask.wall_m) * 2.0 ** round(math.log2(base / float(ask.wall_m)))
        lv = level_for(base, ask.wall_m)
        lo = hi = lv
        got = base / 2 ** lv
        if abs(got - ask.wall_m) / ask.wall_m > 0.05:
            ask.nearest("near_wall", f"cells of {_mm(got)} at the walls: the {_mm(base)} background cell halves to "
                                     f"{_mm(got)}, the nearest to {_mm(ask.wall_m)}", built_m=got)
        else:
            ask.done("near_wall", f"cells of {_mm(got)} at the walls", built_m=got)
    levels: dict = {}
    for e in ask.local:
        where = str(e.get("where") or "")
        r = None if "wake" in where.casefold() else _region_of(where, regions)
        if r is None:
            ask.not_here(e["id"], f"no region named '{where}' in this model (its regions: {', '.join(regions)})")
            continue
        if e["what"] == "size":
            lv = level_for(base, float(e["mm"]) * 1e-3)
        else:
            lv = min(10, hi + 1) if e["what"] == "finer" else max(0, lo - 1)
        levels[r] = [lv, lv]
        ask.done(e["id"], f"cells of {_mm(base / 2 ** lv)} on {r}", built_m=base / 2 ** lv)
    return base, (lo, hi), levels


@guarded(lambda ask, base_cell, surface_level, region_refinement, held: (tuple(surface_level), region_refinement))
def multiregion_cap(ask: Ask, base_cell: float, surface_level, region_refinement: dict, held: dict
                    ) -> tuple[tuple, dict]:
    """The levels under the user's smallest cell. A region the mesher must refine to keep it whole (`held`: a thin
    wall under one cell across loses its zone) keeps its level - validity - and that is said."""
    if ask.min_m is None:
        return tuple(surface_level), region_refinement
    cap = max(0, int(math.floor(math.log2(float(base_cell) / ask.min_m) + 1e-9)))
    lo, hi = min(int(surface_level[0]), cap), min(int(surface_level[1]), cap)
    out, kept = {}, []
    for k, (a, b) in region_refinement.items():
        if k in held and int(b) > cap:
            out[k] = [a, b]
            kept.append(k)
        else:
            out[k] = [min(int(a), cap), min(int(b), cap)]
    cell = float(base_cell) / 2 ** cap
    if kept:
        ask.nearest("min_edge", f"the finest cells are {_mm(cell)}, except in {', '.join(kept)}, whose thin walls "
                                "need finer cells to stay whole", built_m=cell)
    else:
        ask.done("min_edge", f"the finest cells are {_mm(cell)} (boundary layer cells are thinner, by design)",
                 built_m=cell)
    return (lo, hi), out


# #
# snappyHexMesh's dictionaries (external, internal, multi-region): the layers, the growth and the quality limits
# #

#: the measures meshQualityControls holds, and the entry that holds each: (key, the user's limit is tighter when)
SNAPPY_QUALITY = {"non_orthogonality": ("maxNonOrtho", "lower"), "skewness": ("maxInternalSkewness", "lower"),
                  "determinant": ("minDeterminant", "higher"), "min_volume": ("minVol", "higher")}
LAYER_NON_ORTHO_MARGIN = 2


def _sub(text: str, key: str, value: str, *, inside: str | None = None, count: int = 0) -> str:
    """Every `key <number>;` in `text` (or in the block named `inside`) set to `value`."""
    pat = re.compile(rf"(\b{key}\s+)([-+0-9.eE]+)(\s*;)")
    if inside is None:
        return pat.sub(lambda m: m.group(1) + value + m.group(3), text, count=count)
    i = text.find(inside)
    if i < 0:
        return text
    depth, j = 0, text.find("{", i)
    k = j
    while k < len(text):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            break
        k += 1
    return text[:j] + pat.sub(lambda m: m.group(1) + value + m.group(3), text[j:k], count=count) + text[k:]


def _num(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


@guarded(lambda ask, text, **_k: text)
def snappy_dict(ask: Ask | None, text: str, *, wall_cell_m: float | None = None,
                bridge: Mapping | None = None, band: bool = False, relative_ok: bool = False) -> str:
    """The snappyHexMeshDict text with the user's growth, layer sizes and quality limits applied (CFDREQ-2, 4, 5).
    The count of layers, the sizes and the budget are the plan's (the drivers set them before the dict is
    written). Unchanged with nothing asked. `bridge`: {"faces": wall faces, "room": cells the layers may add} on a
    case whose first layer may be far thinner than its wall cells (layer_bridge, CFDREQ-9); None leaves the count
    the plan's. `band`: a laminar resolved body wall may get its near-wall band (CFDREQ-23; the external body case)."""
    if ask is None:
        return text
    out = text
    if ask.growth is not None and "nCellsBetweenLevels" in out:
        n = cells_between_levels(float(ask.growth))
        out = _sub(out, "nCellsBetweenLevels", str(n))
        ask.done("growth", f"{n} cells between refinement levels, so sizes grow about {MR._g(2 ** (1 / n))} from one "
                           "cell to the next", cells_between_levels=n)
    if "addLayersControls" in out and (ask.first_m or ask.total_m or ask.layer_growth or ask.y_plus):
        out = _snappy_layers(ask, out, wall_cell_m, bridge, band=band, relative_ok=relative_ok)
    for lim in ask.limits:
        key = SNAPPY_QUALITY.get(lim["measure"])
        if key is None:
            continue
        name, tighter = key
        ours = _num(out, name)
        want = float(lim["target"])
        if ours is None:
            continue
        rel = re.search(r"relaxed\s*\{[^}]*\}", out)
        loosest = _num(rel.group(0), name) if rel else None
        if loosest is not None and tighter == "lower":
            # the layer stage's relaxed bar is the one the finished mesh is held to: ours is that, not the snap's
            ours = max(ours, loosest)
        if (tighter == "lower" and want >= ours) or (tighter == "higher" and want <= ours):
            ask.done(lim["id"], f"our own limit while meshing is already {MR._g(ours)}, tighter than yours",
                     ours=ours)
            continue
        snap = _num(out, name)
        if snap is not None and ((tighter == "lower" and want < snap) or (tighter == "higher" and want > snap)):
            out = _sub(out, name, f"{want:.6g}", inside="meshQualityControls", count=1)
        if lim["measure"] == "skewness":
            bs = _num(out, "maxBoundarySkewness")
            if bs is not None and want < bs:
                out = _sub(out, "maxBoundarySkewness", f"{want:.6g}", count=1)
        if lim["measure"] == "non_orthogonality":
            # the layer stage's bar sits a little under the limit (the finished mesh reads just past it)
            relaxed = max(5.0, want - LAYER_NON_ORTHO_MARGIN)
            m = re.search(r"relaxed\s*\{[^}]*\}", out)
            if m:
                block = _sub(m.group(0), "maxNonOrtho", f"{min(relaxed, _num(m.group(0), 'maxNonOrtho') or relaxed):.6g}")
                out = out[:m.start()] + block + out[m.end():]
            if ask.n_layers is None or int(ask.n_layers) > 0:
                # the user's bar holds while the layers are added too: layers that would pass it are taken back
                ask.layer_note = (f"your non-orthogonality limit ({MR._g(want)}°) is held while the layers are added, "
                                  "so layers that would pass it are taken back")
        if lim["measure"] == "skewness":
            m = re.search(r"relaxed\s*\{[^}]*\}", out)
            if m and (_num(m.group(0), "maxInternalSkewness") or 0) > want:
                out = out[:m.start()] + _sub(m.group(0), "maxInternalSkewness", f"{want:.6g}") + out[m.end():]
        ask.done(lim["id"], f"the mesher holds every cell it makes to {MR._g(want)} (ours is {MR._g(ours)})",
                 lever=name, ours=ours)
    return out


def snappy_layers_written(ask: Ask | None, n_layers: int, *, wall_cell_m: float | None = None,
                          bridge: Mapping | None = None, last_rel: float = 0.3, band: bool = False,
                          relative_ok: bool = False) -> int:
    """CFDREQ-36: the layers snappy_dict will write on the wall of a plan with `n_layers` - snappy_dict itself run on
    a stand-in addLayersControls with the same inputs (a copy of the ask, so no row is touched), so a picked face is
    priced with the stack the case gets (the elbow: the plan's 3, the y+ stack 16). The plan's count when nothing
    the user asked changes it, or when it cannot be read."""
    import copy

    n = max(0, int(n_layers))
    if ask is None or n == 0:
        return n
    text = (f"addLayersControls {{ relativeSizes true; layers {{ wall {{ nSurfaceLayers {n}; }} }} "
            f"expansionRatio 1.2; finalLayerThickness {float(last_rel):g}; minThickness 0.05; }}\n")
    try:
        out = snappy_dict(copy.deepcopy(ask), text, wall_cell_m=wall_cell_m, bridge=bridge, band=band,
                          relative_ok=relative_ok)
    except Exception:  # noqa: BLE001 - priced with the plan's count, never a failed plan
        logger.warning("cfd requirements: the layer stack to price was not read; the plan's count is used",
                       exc_info=True)
        return n
    counts = [int(x) for x in re.findall(r"\bnSurfaceLayers\s+(\d+)\s*;", out)]
    return max(counts) if counts else n


#: CFDREQ-9: a first layer more than this many times thinner than the wall cells it grows into is not kept by
#: snappyHexMesh with the plan's few layers (measured: a 0.011 mm first layer under 2 mm wall cells kept layers on
#: 1.8% of a wing; a 0.022 mm one under 4.4 mm cells on 0% of a pipe reducer, job f4019933; 0.027 mm under 1.25 mm
#: cells, 46 times, kept 81.6% of a rotor, job 9c638f7a)
THIN_LAYER_RATIO = 50.0
#: CFDREQ-9: the most layers a stack bridged that way gets; past it the first layer is the thinnest it still fills
MAX_BRIDGE_LAYERS = 25
#: CFDREQ-33: how many levels finer than the plain wall cell a relative stack's volume bar allows for (feature edges
#: refine the wall two levels past it, curvature more: faces 8 times finer measured on abc 00008237)
RELATIVE_FINER_LEVELS = 3
#: CFDREQ-33: the shallowest stack, as a share of the plain wall cell, that is written relative. A stack at least half
#: the plain cell deep is at least as deep as the cells of a face one level finer, and snappyHexMesh dropped it there
#: (00008746, 00058385, 00039088: stacks 0.68-1.15 of the plain cell, kept on the finer faces only when relative). A
#: shallower one already fits the finer faces in metres: kept there absolute (00078646, 0.33 of the cell: the full 10
#: layers on 79-85% of the area of faces one to three levels finer), while relative it was halved there and the full
#: stack fell to 62-64% of that area, the wall with 10 cells across its boundary layer from 85.4 to 63.2% (ENGINEER 27)
RELATIVE_MIN_DEPTH = 0.5


def _one_wall_level(text: str) -> bool:
    """CFDREQ-33: every wall surface of the dictionary is refined to one level (level (a a)), so no wall face is
    coarser than the plain wall cell - only finer, at features and curvature."""
    i = text.find("refinementSurfaces")
    if i < 0:
        return False
    j = text.find("resolveFeatureAngle", i)
    pairs = re.findall(r"\blevel\s*\(\s*(\d+)\s+(\d+)\s*\)", text[i:j if j > i else None])
    return bool(pairs) and all(a == b for a, b in pairs)


def _stack_depth(first: float, growth: float, n: int, total: float | None) -> float:
    """CFDREQ-33: the planned stack's depth in metres - the thickness given, else n layers from `first` growing by
    `growth`."""
    if total is not None:
        return float(total)
    g = max(float(growth), 1.0)
    return float(first) * sum(g ** k for k in range(max(1, int(n))))


def _relative_stack(ask: Ask, text: str, first: float, wall_cell_m: float, thinnest: float,
                    total: float | None) -> str:
    """CFDREQ-33: the stack planned in metres on the plain wall cell, written relative to it (relativeSizes true):
    the plain faces get exactly the heights planned (or typed), a face 2^k times finer the same stack 2^k times
    thinner, as thick in its own cells as the plain wall's. The plain cell rides in the dictionary as a comment
    (layer_census.PLAIN_CELL_KEY) so every reader turns the shares back into the heights planned on each face; the
    volume bar is lowered for the finest faces' layer cells. Said on the layer row when the user typed the height."""
    from meshpipeline.engines.layer_census import PLAIN_CELL_KEY
    h = float(wall_cell_m)
    m = re.search(r"addLayersControls\s*\{", text)
    if not m:
        return text
    head, rest = text[:m.end()], text[m.end():]
    if not re.search(r"\brelativeSizes\s+false\s*;", rest):
        return text
    rest = re.sub(r"\brelativeSizes\s+false\s*;", f"relativeSizes true; /* {PLAIN_CELL_KEY} {h:.6g}; */", rest, count=1)
    rest = re.sub(r"\bfirstLayerThickness\s+[-+0-9.eE]+\s*;", f"firstLayerThickness {first / h:.6g};", rest, count=1)
    if total is not None:
        rest = re.sub(r"(?<![A-Za-z])thickness\s+[-+0-9.eE]+\s*;", f"thickness {float(total) / h:.6g};", rest, count=1)
    rest = re.sub(r"\bminThickness\s+[-+0-9.eE]+\s*;", f"minThickness {0.1 * thinnest / h:.6g};", rest, count=1)
    out = head + rest
    ours = _num(out, "minVol")
    if ours is not None:
        out = _sub(out, "minVol", f"{max(1e-30, ours / 8.0 ** RELATIVE_FINER_LEVELS):.3g}",
                   inside="meshQualityControls", count=1)
    rid = ask.rid("layers:first_height") or ask.rid("layers:y_plus")
    if rid and rid in ask.rows:
        said = (f"on wall faces finer than the {_mm(h)} wall cells (edges, curves) the stack is sized on their own "
                f"cells, so the first layer there is thinner in step: {_mm(first / 2)} on faces twice as fine, "
                f"{_mm(first / 4)} on faces four times as fine")
        row = ask.rows[rid]
        row["note"] = f"{row['note']}; {said}" if row.get("note") else said
    return out


def layer_bridge(first_m: float, wall_cell_m: float, *, n_plan: int, growth: float, last_rel: float,
                 max_layers: int = MAX_BRIDGE_LAYERS, faces: float | None = None,
                 room: float | None = None) -> tuple[int, float, float]:
    """CFDREQ-9: (layer count, first layer height, stack thickness) for a first layer far thinner than the wall
    cells. The stack keeps the thickness our own plan gives the wall - `n_plan` layers growing by `growth` up to a
    last layer `last_rel` times the wall cell (snappy's relative finalLayerThickness) - and the asked first layer
    starts it: as many layers as the growth needs to fill it, so the last layer meets the cells beside it the way
    our own stack does. Never fewer than the plan's. Past `max_layers`, or past the cells `room` lets the layers add
    on `faces` wall faces, the count stops there and the first layer is the thinnest that count still fills the
    stack with (never thinner than asked)."""
    g, n0 = max(float(growth), 1.0), max(1, int(n_plan))
    total = float(last_rel) * float(wall_cell_m) * sum(g ** -k for k in range(n0))
    first = float(first_m)
    if g <= 1.0001:
        need = total / first
    else:
        need = math.log(1.0 + total * (g - 1.0) / first) / math.log(g)
    n = max(n0, int(round(need)))
    cap = int(max_layers)
    if faces and room is not None and faces > 0:
        cap = min(cap, n0 + int(max(0.0, float(room)) // float(faces)))
    cap = max(n0, cap)
    if n <= cap:
        return n, first, total
    n = cap
    fill = total / n if g <= 1.0001 else total * (g - 1.0) / (g ** n - 1.0)
    return n, max(first, fill), total


#: CFDREQ-9, the stack's ceiling on a curved or tight wall: the share of the narrowest gap one wall's stack may fill
#: (two walls face each other across it, and the core between them keeps some cells)
STACK_GAP_SHARE = 0.25
#: CFDREQ-9: the steepest growth a capped stack may take to close its step into the core cell
MAX_CAPPED_GROWTH = 1.3
#: CFDREQ-9: the last layer against the core cell a stack aims for (snappy's own relative finalLayerThickness)
STACK_LAST_TO_CORE = 0.3


def capped_stack(first_m: float, wall_cell_m: float, stack_m: float, cap_m: float,
                 growth: float) -> tuple[int, float, float, float]:
    """THE FIRST HEIGHT IS KEPT, THE STACK IS SHORTENED (CFDREQ-9 on a curved or tight wall): (layers, growth,
    stack, last layer against the core cell) for a stack from `first_m` no thicker than `cap_m`; (0, growth,
    stack, 0) when it fits already. snappyHexMesh cannot keep a stack thicker than the wall's own cells where the
    surface curves or the gap is narrow: it cuts the count and spreads what is left over fewer, thicker layers,
    so the first height asked is lost (a rotor: 19 planned, 5.4 kept, the first cell 0.36 mm against 0.029 mm).
    The order is the user's: the first height, then layers on the wall, then the stack's shape, then its count.
    So the stack is capped from the asked first height, and the growth rises - up to MAX_CAPPED_GROWTH - only as
    far as the step into the core needs to reach STACK_LAST_TO_CORE; past that the step is left short."""
    first, cell = float(first_m), float(wall_cell_m)
    if stack_m <= cap_m or first <= 0 or cell <= 0 or cap_m <= first:
        return 0, float(growth), float(stack_m), 0.0
    g = max(1.05, float(growth))
    best = (1, round(g, 2), first, first / cell)
    while g <= MAX_CAPPED_GROWTH + 1e-9:
        n = max(1, int(math.floor(math.log(1.0 + cap_m * (g - 1.0) / first) / math.log(g))))
        last = first * g ** (n - 1)
        best = (n, round(g, 2), first * (g ** n - 1.0) / (g - 1.0), last / cell)
        if last / cell >= STACK_LAST_TO_CORE:
            break
        g += 0.05
    return best


def _snappy_layers(ask: Ask, text: str, wall_cell_m: float | None, bridge: Mapping | None = None, *,
                   band: bool = False, relative_ok: bool = False) -> str:
    """The layer thicknesses in absolute sizes when the user gave one (first height, stack, or a y+ turned into a
    first height); the growth alone keeps our relative sizes. With `bridge` (CFDREQ-9), a first layer far thinner
    than the wall cells gets the layers that fill our own stack from it (layer_bridge), said on its row."""
    m = re.search(r"addLayersControls\s*\{", text)
    if not m:
        return text
    g = float(ask.layer_growth) if ask.layer_growth is not None else (_num(text, "expansionRatio") or 1.2)
    first = ask.first_layer(length_m=ask.length_m, length_from=ask.length_from)
    total = ask.total_m
    out = text
    if first is None and total is None:
        if ask.layer_growth is not None:
            out = _sub(out, "expansionRatio", f"{g:g}", count=1)
            ask.done("layers:growth", f"each layer {MR._g(g)} times the one under it")
        return out
    n = int(_num(out, "nSurfaceLayers") or 3)
    # CFDREQ-13: a y+ that is the wall treatment's (the conditions) is fitted to the wall cells - wall functions keep
    # their first cell in the band or the wall is resolved (wall_fit); a resolved wall goes on to the bridge below
    wf = None
    if first is not None and ask.treatment and ask.first_m is None and total is None and wall_cell_m and n > 0:
        if ask.treatment == "functions":
            wf = wall_fit(ask, float(first), float(wall_cell_m))
            first = wf["first_m"]
            if wf["treatment"] == "functions" and ask.n_layers is None:
                n_wf = stack_count(first, float(wall_cell_m), g)
                out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_wf};", out)
                n = n_wf
        else:
            wf = {"treatment": "resolved", "first_m": float(first), "y_plus": y_plus_of(float(first), ask.how),
                  "why": ""}
    if wf is None and ask.fallback is not None and first is not None:
        # CFDREQ-17: a typed wall-function y+ on a body wall functions do not suit - resolved, the nearest there is
        wf = {"treatment": "resolved", "first_m": float(first), "y_plus": y_plus_of(float(first), ask.how),
              "why": "", "fallback": ask.fallback.get("why"), "said": bool(ask.fallback.get("said"))}
    # CFDREQ-22: a laminar resolved wall's stack is sized on its boundary layer below (laminar_stack), not bridged
    # or thinned from a y+ height
    lam = bool(ask.fallback is not None and ask.fallback.get("bl_delta_m") and first is not None and total is None
               and wall_cell_m and n > 0)
    bridged = None
    if (bridge is not None and first is not None and total is None and ask.n_layers is None and n > 0 and not lam
            and wall_cell_m and wall_cell_m / first > THIN_LAYER_RATIO):
        last_rel = _num(out, "finalLayerThickness") or 0.3
        n_new, first_new, stack = layer_bridge(first, float(wall_cell_m), n_plan=n, growth=g, last_rel=last_rel,
                                               faces=bridge.get("faces"), room=bridge.get("room"))
        if n_new > n:
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_new};", out)
            bridged = (n, n_new, first, first_new, stack)
            n, first = n_new, first_new
    capped = None
    cap = bridge.get("stack_cap") if bridge is not None else None
    if bridged is not None and cap and bridged[4] > float(cap):
        # CFDREQ-9 ON A CURVED OR TIGHT WALL: the ASKED first height kept (a bridge that ran out of layers had
        # thickened it), the stack shortened to the cap
        n_c, g_c, stack_c, last_c = capped_stack(bridged[2], float(wall_cell_m or 0.0), bridged[4], float(cap), g)
        if n_c and n_c <= MAX_BRIDGE_LAYERS:
            first = bridged[2]
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_c};", out)
            capped = (bridged[0], n_c, g_c, bridged[4], stack_c, last_c, float(cap))
            bridged = None
            n, g = n_c, g_c
    thick = None
    if (bridged is None and capped is None and first is not None and total is None and ask.n_layers is None and n > 0
            and not lam and wall_cell_m and not (wf is not None and wf["treatment"] == "functions")):
        # CFDREQ-9, THE THICK SIDE: a first layer so thick that the plan's layers make a stack several wall cells
        # deep, which snappyHexMesh cannot shrink the mesh for (a y+ 50 layer, 1.35 mm under 1.72 mm cells: 5 layers
        # are 10 mm, and grew on 5.7% of the wall) - as few layers as fill the stack our own plan gives the wall
        last_rel = _num(out, "finalLayerThickness") or 0.3
        n_new, first_new, stack = layer_fit(first, float(wall_cell_m), n_plan=n, growth=g, last_rel=last_rel)
        if n_new < n or first_new < first:
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_new};", out)
            thick = (n, n_new, first, first_new, stack)
            n, first = n_new, first_new
    across = None
    if lam and ask.fallback is not None:
        # CFDREQ-22: the fewest layers, from the thickest first layer, that put BL_CELLS across the laminar boundary
        # layer with the wall cells this plan has - a count or a growth of the user's kept
        if ask.layer_growth is None:
            g = min(g, LAMINAR_GROWTH)
        room = None
        if bridge is not None and bridge.get("faces") and bridge.get("room") is not None:
            room = n + int(max(0.0, float(bridge["room"])) // max(1.0, float(bridge["faces"])))
        n_lam, first, across = laminar_stack(
            float(ask.fallback["bl_delta_m"]), float(wall_cell_m or 0.0), g, n_min=1,
            n_max=min(BL_CELLS, room) if room else BL_CELLS, n_fixed=n if ask.n_layers is not None else None,
            stack_max_m=float(cap) if cap else None)
        bl_delta = float(ask.fallback["bl_delta_m"])
        if (band and ask.n_layers is None and wall_cell_m and n_lam > LAMINAR_BAND_LAYERS and bridge is not None
                and bridge.get("faces") and bridge.get("room") is not None):
            # CFDREQ-23: finer cells across the layer when the wall cells are about as thick as it, if the mesh can
            # afford them; else the stack above stands and the cost is said where the row is judged
            bd = laminar_band(bl_delta, float(wall_cell_m), g, faces=float(bridge["faces"]),
                              room=float(bridge["room"]), n_now=n_lam, stack_max_m=float(cap) if cap else None)
            banded = band_regions(out, bl_delta, int(bd["levels"])) if bd["used"] else None
            if banded is not None:
                out = banded
                for key in ("maxLocalCells", "maxGlobalCells"):
                    was = _num(out, key)
                    if was is not None:
                        out = _sub(out, key, str(int(was + bd["cells"])), count=1)
                n_lam, first, across = int(bd["layers"]), float(bd["first_m"]), float(bd["across"])
                wall_cell_m = float(bd["cell_m"])
                ask.fallback["band"] = {"levels": int(bd["levels"]), "cell_m": float(bd["cell_m"]),
                                        "cells": int(bd["cells"])}
            elif bd["used"]:
                pass
            elif bd.get("cells") is not None and across >= BL_CELLS - 1e-9:
                # the band would have let a shorter stack do it: what it costs, said if the stack is not kept
                ask.fallback["band_note"] = (
                    f"cells {2 ** int(bd['levels'])} times finer near the wall would let fewer layers do it, but "
                    f"they would add about {MR._g(round(bd['cells'] / 1e6, 1))} M cells, more than the "
                    f"{MR._g(round(float(bridge['room']) / 1e6, 1))} M this mesh may add for its wall")
            elif across < BL_CELLS - 1e-9:
                ask.fallback["band_note"] = (
                    "even cells four times finer near the wall would not put enough cells across it within what "
                    "this mesh may add for its wall" if bd.get("cells") is None else
                    f"cells {2 ** int(bd['levels'])} times finer near the wall would, but they would add about "
                    f"{MR._g(round(bd['cells'] / 1e6, 1))} M cells, more than the "
                    f"{MR._g(round(float(bridge['room']) / 1e6, 1))} M this mesh may add for its wall")
        if n_lam != n:
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_lam};", out)
            n = n_lam
        # CFDREQ-23: THE BUFFER STAYS AS THE CASE HAS IT (nBufferCellsNoExtrude 0). -1 ("the stack ends in one
        # step") was tried on the 12-part external sample (extlam2_s12_8f2765ba): the wall with 10 cells across
        # its layer FELL on 6 parts (00000039 79 -> 2%, 00069804 73 -> 0%, 00116175 65 -> 0%), rose about a point
        # on 2, and the runs were 2-10x slower (00000039 8 -> 83 min; 00032019 250 min and no mesh). Not set
        ask.fallback.update(layers=int(n), cells_across=round(across, 1), first_m=float(first))
    elif ask.fallback is not None and first is not None and wall_cell_m and n > 0:
        # CFDREQ-17: THE RESOLVED BODY WALL RESOLVES ITS BOUNDARY LAYER - BL_CELLS across it, the layers counted up
        # from the plan's (never past the stack the wall's cells hold, nor past the cap a tight wall set), unless the
        # user gave a count
        delta = float(ask.fallback.get("delta_m") or 0.0)
        if delta > 0:
            if (ask.n_layers is None and total is None and bridged is None and capped is None
                    and thick is None):
                last_rel = _num(out, "finalLayerThickness") or 0.3
                held = float(last_rel) * float(wall_cell_m) * sum(g ** -k for k in range(max(1, n)))
                most = min(held, float(cap)) if cap else held
                n_bl = bl_layers(first, g, n, float(wall_cell_m), delta, stack_max_m=most)
                if bridge is not None and bridge.get("faces") and bridge.get("room") is not None:
                    n_bl = min(n_bl, n + int(max(0.0, float(bridge["room"])) // max(1.0, float(bridge["faces"]))))
                if n_bl > n:
                    out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_bl};", out)
                    n = n_bl
            across = cells_across(first, g, n, float(wall_cell_m), delta)
            ask.fallback.update(layers=int(n), cells_across=round(across, 1))
    spec = []
    if first is not None and total is not None and ask.layer_growth is None:
        spec = [f"firstLayerThickness {first:.6g};", f"thickness {total:.6g};"]
        ask.done("layers:first_height", f"first layer {_mm(first)} high", lever="firstLayerThickness")
        ask.done("layers:total_thickness", f"the stack {_mm(total)} thick", lever="thickness")
    elif first is not None:
        spec = [f"firstLayerThickness {first:.6g};", f"expansionRatio {g:g};"]
        ask.done("layers:first_height", f"first layer {_mm(first)} high, each next one {MR._g(g)} times thicker",
                 lever="firstLayerThickness")
        if total is not None:
            ask.nearest("layers:total_thickness", "the first height and the growth set the stack, so its thickness "
                                                  "follows from them")
        if ask.layer_growth is not None:
            ask.done("layers:growth", f"each layer {MR._g(g)} times the one under it")
    else:
        total = float(total or 0.0)
        spec = [f"thickness {total:.6g};", f"expansionRatio {g:g};"]
        ask.done("layers:total_thickness", f"the stack {_mm(total)} thick", lever="thickness")
        if ask.layer_growth is not None:
            ask.done("layers:growth", f"each layer {MR._g(g)} times the one under it")
    thinnest = first if first is not None else (float(total or 0.0) / max(1, n)) / g ** max(0, n - 1)
    head, rest = out[:m.end()], out[m.end():]
    rest = re.sub(r"\brelativeSizes\s+true\s*;", "relativeSizes false;", rest, count=1)
    rest = re.sub(r"\s*\bexpansionRatio\s+[-+0-9.eE]+\s*;", "", rest, count=1)
    rest = re.sub(r"\s*\bfinalLayerThickness\s+[-+0-9.eE]+\s*;", "", rest, count=1)
    rest = re.sub(r"\bminThickness\s+[-+0-9.eE]+\s*;", f"{' '.join(spec)} minThickness {0.1 * thinnest:.6g};",
                  rest, count=1)
    out = head + rest
    if wall_cell_m and wall_cell_m > 0:
        # minVol, the one absolute bar: under the thinnest layer cell this stack makes (never above ours)
        ours = _num(out, "minVol")
        if ours is not None:
            pyramid = wall_cell_m * wall_cell_m * 0.5 * 0.1 * thinnest / 6.0
            new = max(1e-30, min(ours, 1e-3 * pyramid))
            if new < ours:
                out = _sub(out, "minVol", f"{new:.3g}", inside="meshQualityControls", count=1)
        out = _layer_determinant(ask, out, thinnest, float(wall_cell_m))
        if bridged is None and capped is None and first is not None and wall_cell_m / first > THIN_LAYER_RATIO:
            # a stack far thinner than the cells it grows into is hard for the mesher to keep (measured: a 0.011 mm
            # first layer under 2 mm wall cells kept layers on 1.8% of a wing): said where it is set and where the
            # layers are reported, with what keeps more of them
            thin = (f"the first layer is {int(wall_cell_m / first):,} times thinner than the wall cells "
                    f"({_mm(wall_cell_m)}): layers that thin are hard to keep; finer cells at the walls keep more of them")
            ask.rows.get(ask.rid("layers:first_height") or ask.rid("layers:y_plus") or "", {}).update(note=thin)
            if not ask.layer_note:
                ask.layer_note = thin
    if (relative_ok and first is not None and wall_cell_m and wall_cell_m > 0 and _one_wall_level(out)
            and ((wf["treatment"] == "resolved") if wf is not None else
                 (bridged is not None or capped is not None or ask.treatment == "resolved"))
            and _stack_depth(float(first), g, n, total) >= RELATIVE_MIN_DEPTH * float(wall_cell_m)):
        # CFDREQ-33: A RESOLVED WALL'S STACK FITS EACH WALL FACE - sized on the plain wall cell, relative, so a face
        # finer than that cell gets the same stack in its own cells. Only where the caller's wall cell is the wall's
        # plain cell (relative_ok: the external body case); an internal case sizes on the near-wall band, finer than
        # the surface, and a relative stack there would thicken the surface's own faces. And only a stack at least
        # RELATIVE_MIN_DEPTH of the plain cell deep: a shallower one fits the finer faces' cells as it is
        out = _relative_stack(ask, out, float(first), float(wall_cell_m), float(thinnest), total)
    if bridged is not None:
        _say_bridge(ask, bridged, g, float(wall_cell_m or 0.0))
    if capped is not None:
        _say_capped(ask, capped, float(wall_cell_m or 0.0))
    if thick is not None:
        _say_thick(ask, thick, g, float(wall_cell_m or 0.0))
    if wf is not None and first is not None:
        built = dict(wf, first_m=float(first), y_plus=y_plus_of(float(first), ask.how) if ask.how else wf["y_plus"])
        if ask.fallback is not None:
            built["why"] = ""
            built["words"] = fallback_words(ask, float(first), n, across)
            if across is not None:
                built["cells_across_bl"] = round(across, 1)
            if ask.fallback.get("bl_delta_m"):
                built["bl_delta_m"] = float(f"{float(ask.fallback['bl_delta_m']):.4g}")
            if ask.fallback.get("band_note"):
                built["band_note"] = ask.fallback["band_note"]
        _say_wall(ask, built, n, g)
    # CFDREQ-15: decided on the dict's final layer count, after the bridge, the cap and the thick side
    resolved = (wf["treatment"] == "resolved") if wf is not None else (
        bridged is not None or capped is not None or ask.treatment == "resolved")
    return _layer_twist(ask, out, resolved=resolved)


#: ENG-asks: the engines that set an absolute first layer (a height, or a y+ turned into one) - snappyHexMesh and the
#: multi-region mesher (this module's snappy_dict), cfMesh (maxFirstLayerThickness), Netgen (its first layer). VMTK
#: sizes its stack from the vessel radius; gmsh flow and the snap grid build no prism layers.
FIRST_LAYER_ENGINES = frozenset({"snappy", "snappy_multiregion", "cfmesh", "netgen"})
#: CFDREQ-10: the asks of an absolute layer height, and the engines that set one in the order they deliver it over the
#: whole wall, measured (snappyHexMesh 100% coverage at y+ 1 on s_duct_004, cfMesh 78%; the order only ranks engines
#: of the job's own ladder, never adds one)
LAYER_HEIGHT_FIELDS = frozenset({"layers:first_height", "layers:y_plus", "layers:total_thickness"})
LAYER_HEIGHT_DELIVERS = ("snappy", "snappy_multiregion", "cfmesh", "netgen")
#: what each such ask is called in the line that says why the engine was chosen
_ASK_NAMES = {"layers:first_height": "the first layer height", "layers:y_plus": "the first layer for the y+",
              "layers:total_thickness": "the layer stack's thickness"}


#: ENG-10: WHAT EACH CFD ENGINE HAS NO SETTING FOR, by requirement field - the one declaration the engine pick reads
#: (each engine's applier below says "not applied" for the same fields: cfmesh_strategy, gmsh_sizes, snapgrid_plan,
#: vmtk_spec, netgen_inputs). The layer heights are FIRST_LAYER_ENGINES' (above); a control is its lane's NotHere.
#: no prism layers at all: every layer field
NO_LAYER_ENGINES = frozenset({"gmsh", "snapgrid"})
#: a layer stack's total thickness as a setting
STACK_ENGINES = frozenset({"snappy", "snappy_multiregion", "netgen"})
#: a growth of the cells from one size to the next (snappy's cells between levels, the snap grid's grading)
CELL_GROWTH_ENGINES = frozenset({"snappy", "snappy_multiregion", "snapgrid"})
#: a size in one named place - a face, a part, a region (snappy's surface levels, cfMesh's local refinement)
PLACE_SIZE_ENGINES = frozenset({"snappy", "snappy_multiregion", "cfmesh"})
#: the other fields one engine has no setting for
NO_SETTING = {"gmsh": frozenset({"cells:target", "cells:ceiling", "cells:floor"}),
              "snapgrid": frozenset({"near_wall", "min_edge"})}
#: ENG-10: a requirement the user gave a number for counts twice a vague one ("finer around the bend")
TYPED_WEIGHT = 2
VAGUE_WEIGHT = 1


def _cannot(engine: str, field: str) -> bool:
    """ENG-10: whether `engine` has no setting for a requirement row's field (the declaration above, the registry's
    NotHere for a control, the first-layer engines for a layer height or a y+)."""
    if field.startswith("layers:") and engine in NO_LAYER_ENGINES:
        return True
    if field in ("layers:first_height", "layers:y_plus"):
        return engine not in FIRST_LAYER_ENGINES
    if field == "layers:total_thickness":
        return engine not in STACK_ENGINES
    if field == "growth":
        return engine not in CELL_GROWTH_ENGINES
    if field == "local":
        return engine not in PLACE_SIZE_ENGINES
    if field.startswith("control:"):
        from meshpipeline.contracts import mesh_controls as MC
        return isinstance(MC.lane_of(LANES.get(engine, engine), field.split(":", 1)[1]), MC.NotHere)
    return field in NO_SETTING.get(engine, frozenset())


def asks_of(sec: Mapping | None, *, conditions_y_plus: bool = False) -> list[dict]:
    """ENG-10: the requirements the reading holds, one {field, asked, weight} each (TYPED_WEIGHT when the user gave a
    number, VAGUE_WEIGHT when not), plus a y+ for a resolved wall the user chose in the conditions. [] = nothing asked."""
    req = MR.requirements_of(sec) or {}
    local_mm = {str(e.get("id")): e.get("mm") for e in req.get("local") or [] if isinstance(e, Mapping)}
    out = []
    for r in req.get("rows") or []:
        if not isinstance(r, Mapping) or not r.get("field"):
            continue
        field = str(r["field"])
        typed = (local_mm.get(str(r.get("id"))) is not None if field == "local"
                 else any(ch.isdigit() for ch in str(r.get("quote") or r.get("asked") or "")))
        out.append({"field": field, "asked": str(r.get("asked") or r.get("quote") or field),
                    "weight": TYPED_WEIGHT if typed else VAGUE_WEIGHT})
    if conditions_y_plus and not any(a["field"] in ("layers:y_plus", "layers:first_height") for a in out):
        out.append({"field": "layers:y_plus", "asked": "a resolved wall", "weight": TYPED_WEIGHT})
    return out


def _settable(engine: str, ask: Mapping, *, functions: bool, conditions_y_plus: bool) -> bool:
    if ask["field"] == "layers:y_plus" and engine == "vmtk" and functions and not conditions_y_plus:
        # CFDREQ-13: VMTK sizes a wall-function stack itself (_vmtk_wall); only a resolved wall is past it
        return True
    return not _cannot(engine, str(ask["field"]))


def steer(sec: Mapping | None, engine: str, candidates, *, conditions_y_plus: bool = False) -> dict:
    """ENG-10: THE ENGINE THAT APPLIES THE MOST OF WHAT THE USER ASKED, among `candidates` (the engines valid for this
    geometry and flow, in the ladder's order). Each candidate scores the weights of the asks it has a setting for; the
    engine stays unless another scores more; ties go to the engine that delivers an absolute layer height over the
    whole wall first when one is asked and `engine` cannot set it (LAYER_HEIGHT_DELIVERS), then the ladder's order.
    Returns {"engine", "before", "order", "scores", "asks", "missing" (what the chosen engine cannot set), "words"}:
    "words" is "" when the engine stays. Nothing asked: `engine` itself, scores {}."""
    asks = asks_of(sec, conditions_y_plus=conditions_y_plus)
    out = {"engine": engine, "before": engine, "order": [], "scores": {}, "asks": asks, "missing": [], "words": ""}
    if not asks:
        return out
    functions = ((MR.requirements_of(sec) or {}).get("layers") or {}).get("treatment") == "functions"

    def can(e: str) -> list[bool]:
        return [_settable(e, a, functions=functions, conditions_y_plus=conditions_y_plus) for a in asks]

    order = [engine, *[str(e) for e in candidates or () if str(e) != engine]]
    missing_here = [a for a, ok in zip(asks, can(engine), strict=True) if not ok]
    if any(a["field"] in LAYER_HEIGHT_FIELDS for a in missing_here):
        # an absolute first layer (or a y+ made one) goes to the engine that DELIVERS it over the whole wall first: on
        # s_duct_004, y+ 1 on cfMesh had layers on 78% of the wall (the rest none, not met), on snappyHexMesh on 100%
        # (m483 round 9, 2e against 2c)
        rest = sorted(order[1:], key=lambda e: LAYER_HEIGHT_DELIVERS.index(e) if e in LAYER_HEIGHT_DELIVERS
                      else len(LAYER_HEIGHT_DELIVERS))
        order = [engine, *rest]
    scores = {e: sum(a["weight"] for a, ok in zip(asks, can(e), strict=True) if ok) for e in order}
    from meshpipeline.contracts import decision_record as _dr
    # the record's own rule function picks, so the replay re-decides exactly this
    chosen = _dr.engine_for_asks({"order": order, "scores": scores})[0] or engine
    missing = [str(a["asked"]) for a, ok in zip(asks, can(chosen), strict=True) if not ok]
    out.update(engine=chosen, order=order, scores=scores, missing=missing)
    if chosen != engine:
        out["words"] = _steer_words(engine, chosen, missing_here, missing)
    return out


def _label(engine: str) -> str:
    from meshpipeline.engines.registry import engine_label
    try:
        return engine_label(str(engine))
    except Exception:  # noqa: BLE001 - the engine's own name still says which
        return str(engine)


def _steer_words(before: str, chosen: str, missing_before: list[dict], missing_after: list[str]) -> str:
    """The plain line the run's engine line and result say: "Meshed with snappyHexMesh, because it can set the first
    layer for the y+ you asked for, which VMTK has no setting for." (+ what the chosen engine cannot set either)."""
    first = missing_before[0] if missing_before else {"field": "", "asked": "what"}
    what = _ASK_NAMES.get(str(first["field"])) or str(first["asked"])
    more = len(missing_before) - 1
    if more > 0:
        what += f" and {more} other thing{'' if more == 1 else 's'}"
    words = f"Meshed with {_label(chosen)}, because it can set {what} you asked for, which {_label(before)} has no " \
            "setting for."
    if missing_after:
        n = len(missing_after)
        words += (f" {_label(chosen)} has no setting for {n} of the things you asked for either "
                  "(the result says which); no engine that can mesh this part sets more of them.")
    return words


def engine_that_can(sec: Mapping | None, engine: str, ladder, *, conditions_y_plus: bool = False) -> tuple[str, str]:
    """CFDREQ-10 (the request card's reading, before the run) through ENG-10's pick: (the engine to mesh with, the
    plain line that says why - "" when it is `engine`). Nothing asked, or no engine of `ladder` that sets more of it:
    `engine` itself, as always."""
    got = steer(sec, engine, ladder, conditions_y_plus=conditions_y_plus)
    return str(got["engine"]), str(got["words"])


def stack_count(first_m: float, wall_cell_m: float, g: float) -> int:
    """CFDREQ-13: the layers a wall-function stack gets - from its first layer, growing `g`, until the last one is
    about STACK_TOP_REL of the wall cell (a smooth step into the core); one when the first layer is that thick."""
    top = STACK_TOP_REL * float(wall_cell_m)
    if first_m >= top or g <= 1.0001:
        return 1
    return max(1, min(MAX_WALL_FUNCTION_LAYERS, 1 + int(round(math.log(top / float(first_m)) / math.log(g)))))


def _say_wall(ask: Ask, fit: Mapping, n: int, g: float, *, engine_words: str = "") -> None:
    """CFDREQ-13 on the y+ row: which wall treatment was built and the y+ it expects - applied when it is the one the
    conditions asked for, the nearest (with why) when the wall had to be resolved instead or the y+ moved."""
    rid = ask.rid("layers:y_plus")
    if not rid:
        return
    words = fit.get("words") or wall_words(ask, fit, n, g, engine_words=engine_words)
    # CFDREQ-17: a resolved wall for a body wall functions do not suit is what the accepted proposal itself promised
    # ("where ... too fine for that, the wall is resolved instead, and the result says which"): applied, said
    moved = ((fit["treatment"] != ask.treatment or bool(fit.get("why")))
             and not (fit.get("fallback") and not fit.get("said")))
    prev = ask.rows.get(rid, {})
    extra = prev.get("built", "") if "layers" in prev and "stack fills" in prev.get("built", "") else ""
    row = {"first_m": float(f"{float(fit['first_m']):.4g}"), "layers": int(n), "treatment": fit["treatment"],
           "y_plus_expected": float(f"{float(fit['y_plus']):.3g}"), **{k: v for k, v in (ask.how or {}).items()
                                                                       if k in ("reynolds", "cf", "friction")}}
    for k in ("fallback", "cells_across_bl", "bl_delta_m", "band_note"):
        if fit.get(k) is not None:
            row[k] = fit[k]
    if ask.wall_level_note and fit["treatment"] == "functions":
        words += f"; {ask.wall_level_note}"
    if moved:
        ask.nearest(rid, words + (f"; {extra.split('; ', 1)[1]}" if "; " in extra else ""), **row)
    else:
        ask.done(rid, words + (f"; {extra.split('; ', 1)[1]}" if "; " in extra else ""), **row)


def _say_thick(ask: Ask, thick: tuple, g: float, wall_cell_m: float) -> None:
    """CFDREQ-9 (the thick side) in the rows' words: how many layers the stack got and why, and - when even one layer
    of the height asked is past the stack our plan gives the wall - the first layer built and the y+ it gives."""
    n0, n, asked, built, stack = thick
    deep = asked * (g ** n0 - 1.0) / (g - 1.0) if g > 1.0001 else asked * n0
    field = "layers:y_plus" if ask.first_m is None and ask.y_plus is not None else "layers:first_height"
    rid = ask.rid(field)
    if built >= asked * (1 - 1e-6):
        how = (f"{n} layer{'s' if n != 1 else ''} (not our usual {n0}): from a first layer that high our usual {n0} "
               f"would make a stack {_mm(deep)} deep under {_mm(wall_cell_m)} wall cells, more than the mesher can grow; "
               f"{n} fill{'s' if n == 1 else ''} the {_mm(stack)} our plan gives the wall")
        row = ask.rows.get(rid or "")
        if row is not None:
            row["built"] = f"{row['built']}; {how}"
            row["layers"] = n
        ask.layer_note = ask.layer_note or how
        return
    yp = ask.y_plus
    got = (f"y+ about {MR._g(float(yp) * built / asked)}, not {MR._g(float(yp))}"
           if field == "layers:y_plus" and yp is not None else f"not the {_mm(asked)} asked")
    ask.nearest(field, f"first layer {_mm(built)} high ({got}), one layer: a thicker one would not fit the {_mm(stack)} "
                       f"stack our plan gives the {_mm(wall_cell_m)} wall cells, and the mesher could not grow it",
                first_m=float(f"{built:.4g}"), asked_m=float(f"{asked:.4g}"), layers=n)
    ask.layer_note = ask.layer_note or f"one layer {_mm(built)} high"


def layer_fit(first_m: float, wall_cell_m: float, *, n_plan: int, growth: float,
              last_rel: float) -> tuple[int, float, float]:
    """CFDREQ-9, the thick side: (layer count, first layer height, stack thickness) for a first layer so thick that
    the plan's count would stack it past the thickness our own plan gives the wall (`n_plan` layers up to a last one
    `last_rel` of the wall cell): the most layers, from the height asked, that stay inside it (at least one); one
    layer of that whole thickness when even one of the height asked is past it. The plan's count and the height
    asked when they fit already."""
    g, n0 = max(float(growth), 1.0), max(1, int(n_plan))
    stack = float(last_rel) * float(wall_cell_m) * sum(g ** -k for k in range(n0))
    first = float(first_m)

    def height(k: int) -> float:
        return first * k if g <= 1.0001 else first * (g ** k - 1.0) / (g - 1.0)

    if height(n0) <= stack * (1 + 1e-9):
        return n0, first, stack
    if first > stack:
        return 1, stack, stack
    n = max(k for k in range(1, n0 + 1) if height(k) <= stack * (1 + 1e-9))
    return n, first, stack


#: CFDREQ-9: the cell determinant snappyHexMesh checks is about this times (thickness / width)^2 for a flat cell, so a
#: layer cell is "bad" by its flatness alone - a 0.022 mm layer under 4.4 mm cells reads 0.0007, under our 0.001 bar
#: (job f4019933: every wall face's layers taken back for it, 633,244 faces)
FLAT_CELL_DETERMINANT = 27.0
#: the lowest the layer stage's determinant bar goes for a thin first layer (cells still have positive volume)
LAYER_DETERMINANT_FLOOR = 1e-6


#: CFDREQ-15: past this many layers a resolved wall's stack is kept by the layer stage without the face-twist bar
TWIST_FREE_MIN_LAYERS = 6
#: ...the twist bar the layer stage's relaxed block then gets (snappyHexMesh: below 0 the check is off)
LAYER_STAGE_TWIST = -1


def _layer_twist(ask: Ask, text: str, *, resolved: bool) -> str:
    """CFDREQ-15: A RESOLVED STACK IS NOT TAKEN BACK FOR FACE TWIST. snappyHexMesh steps the layer count down one
    layer per cell ring round every face its layer stage refuses (nBufferCellsNoExtrude 0), and on a deep resolved
    stack the first refusals are mostly face twist under 0.02 (the duct transition_005: 104 of 147 in pass 0; an
    aircraft, abc 00049961: 87 of 172) - a few faces that cost a quarter to a third of the layer cells. The layer
    stage's relaxed block (meshQualityControls/relaxed) gets minTwist -1 when the FINAL layer count in the dict is
    over 5 on a resolved wall: snapping and the final check keep the main block's 0.02, the buffer stays 0 (-1, the
    'terminate in one go', removed every layer and broke the duct and the rotor). Replays: the aircraft 76.5% ->
    98.3% of the layer cells, wall at plan 79% -> 92%; the duct 64.2% -> 83.1%, 31% -> 59%, flat walls 4.45 ->
    1.01 times the planned first cell. Never with a twist limit of the user's, never on wall functions."""
    if not resolved or ask.limit("twist") is not None:
        return text
    counts = [int(x) for x in re.findall(r"\bnSurfaceLayers\s+(\d+)\s*;", text)]
    if not counts or max(counts) < TWIST_FREE_MIN_LAYERS:
        return text
    m = re.search(r"relaxed\s*\{[^}]*\}", text)
    if m is None:
        return text
    block = m.group(0)
    if re.search(r"\bminTwist\s+", block):
        block = _sub(block, "minTwist", str(LAYER_STAGE_TWIST))
    else:
        block = re.sub(r"relaxed\s*\{", f"relaxed {{ minTwist {LAYER_STAGE_TWIST};", block, count=1)
    return text[:m.start()] + block + text[m.end():]


def _layer_determinant(ask: Ask, text: str, thinnest: float, wall_cell_m: float) -> str:
    """CFDREQ-9: the layer stage's determinant bar (meshQualityControls/relaxed minDeterminant) set under what the
    thinnest layer cell reads - for a cell as flat as the asked first layer the check only measures flatness, which
    is what a thin layer is. Faces up to twice the wall cell wide and half of that as margin; never above ours, never
    with a determinant limit of the user's (theirs holds), and unchanged when the layers clear our bar already."""
    if ask.limit("determinant") is not None or thinnest <= 0 or wall_cell_m <= 0:
        return text
    ours = _num(text, "minDeterminant")
    m = re.search(r"relaxed\s*\{[^}]*\}", text)
    if ours is None or m is None:
        return text
    reads = FLAT_CELL_DETERMINANT * (thinnest / (2.0 * wall_cell_m)) ** 2
    if reads >= 2.0 * ours:
        return text
    want = max(LAYER_DETERMINANT_FLOOR, min(ours, 0.5 * reads))
    block = m.group(0)
    if re.search(r"\bminDeterminant\s+", block):
        block = _sub(block, "minDeterminant", f"{want:.3g}")
    else:
        block = re.sub(r"relaxed\s*\{", f"relaxed {{ minDeterminant {want:.3g};", block, count=1)
    return text[:m.start()] + block + text[m.end():]


def _say_capped(ask: Ask, capped: tuple, wall_cell_m: float) -> None:
    """CFDREQ-9 on a curved or tight wall, in the rows' words: the first height kept, the stack shortened, and the
    step into the core where the cap leaves it short."""
    n0, n, g, stack, stack_c, last_c, cap = capped
    how = (f"{n} layers, each {MR._g(g)} times the one under it, in a {_mm(stack_c)} stack (not the {_mm(stack)} "
           f"that carries the layers out to the {_mm(wall_cell_m)} wall cells): a thicker stack is not kept where "
           f"the surface curves or the gap is narrow, and the mesher would thicken the first layer to keep it")
    if last_c < STACK_LAST_TO_CORE:
        how += (f"; the last layer is {MR._g(round(last_c, 2))} of the wall cell, short of the "
                f"{MR._g(STACK_LAST_TO_CORE)} we aim for - finer cells at the walls close that step")
    field = "layers:y_plus" if ask.first_m is None and ask.y_plus is not None else "layers:first_height"
    row = ask.rows.get(ask.rid(field) or "")
    if row is not None:
        row["built"] = f"{row['built']}; {how}"
        row["layers"] = n
    ask.layer_note = ask.layer_note or how


def _say_bridge(ask: Ask, bridged: tuple, g: float, wall_cell_m: float) -> None:
    """CFDREQ-9 in the rows' words: how many layers the stack got and why, and - when the count could not fill it
    from the height asked - the first layer built and the y+ it gives."""
    n0, n, asked, built, stack = bridged
    how = (f"{n} layers (not our usual {n0}), each {MR._g(g)} times the one under it, so the stack fills the "
           f"{_mm(stack)} our plan gives the wall: a first layer {int(wall_cell_m / asked):,} times thinner than the "
           f"{_mm(wall_cell_m)} wall cells is not kept with only {n0}")
    field = "layers:y_plus" if ask.first_m is None and ask.y_plus is not None else "layers:first_height"
    rid = ask.rid(field)
    if built <= asked * (1 + 1e-6):
        row = ask.rows.get(rid or "")
        if row is not None:
            row["built"] = f"{row['built']}; {how}"
            row["layers"] = n
        ask.layer_note = ask.layer_note or how
        return
    yp = ask.y_plus
    if field == "layers:y_plus" and yp is not None:
        got = f"y+ about {MR._g(float(yp) * built / asked)}, not {MR._g(float(yp))}"
    else:
        got = f"not the {_mm(asked)} asked"
    ask.nearest(field, f"first layer {_mm(built)} high ({got}): {n} layers are the most this mesh takes "
                       f"(each {MR._g(g)} times the one under it), and from a thinner first layer they would not "
                       f"fill the {_mm(stack)} stack our plan gives the wall, which is how layers that thin are kept",
                first_m=float(f"{built:.4g}"), asked_m=float(f"{asked:.4g}"), layers=n)
    ask.layer_note = ask.layer_note or f"{n} layers from a first layer {_mm(built)} high"


# #
# CFDREQ-6: asked against delivered
# #

#: the worst figure each engine's quality record carries for a measure (checkMesh's lines)
WORST_KEYS = {"non_orthogonality": ("max_non_ortho", False), "skewness": ("max_skewness", False),
              "aspect_ratio": ("max_aspect_ratio", False), "determinant": ("min_determinant", True),
              "min_volume": ("min_volume", True), "orthogonal_quality": ("min_orthogonal_quality", True)}
_OPENFOAM = ("snappy", "cfmesh", "snapgrid", "snappy_multiregion", "netgen")


def _plan(workspace) -> dict:
    try:
        got = json.loads((Path(workspace) / PLAN_FILE).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _cells_words(s: str) -> str:
    return s.replace("of elements", "of cells")


def report(workspace, quality: Mapping, engine: str) -> dict | None:
    """CFDREQ-6: for every requirement read from the notes, what was asked and what the built flow mesh delivers
    (the shape of engines/mesh_requirements.finish_report). Every ask the reading holds gets a row, whatever its
    outcome - one the checks set aside too ("not applied - <why>"), so an attempt whose only ask was set aside is
    never silent (5b/5c of the round-4 matrix had no tally at all). None with no ask."""
    sec = MR.read(workspace) if workspace else {}
    req = requirements_for(workspace) or {}
    aside = [str(d) for d in (sec.get("dropped") or [])] if sec.get("status") == "used" else []
    # NOTE-15: the engine the notes chose is an ask too, met when it built this mesh
    from meshpipeline.engines import notes_acted as NA
    engine_row = NA.report_row(sec, engine)
    if not req.get("rows") and not aside and engine_row is None:
        return None
    plan = (_plan(workspace).get("rows") or {})
    q = dict(quality or {})
    n = int(q.get("cells") or 0)
    figs = None
    wants_cells = any(lim.get("measure") in WORST_KEYS for lim in req.get("limits") or [])
    if wants_cells and engine in _OPENFOAM:
        from meshpipeline.engines import cfd_cell_measures as CM
        figs = CM.measure(workspace)
    n_meas = int(figs["n_cells"]) if figs else n
    limits = {lim["id"]: lim for lim in req.get("limits") or []}
    rows = []
    for row in req.get("rows") or []:
        field, p = str(row["field"]), plan.get(row["id"]) or {}
        status, built = p.get("status"), str(p.get("built") or "")
        if field.startswith("limit:"):
            got = _limit_row(limits[row["id"]], q, figs, n_meas, p)
        elif field.startswith("cells:"):
            # a count is measured on every mesh, a setting of this mesher or not
            got = _count_row(field, req, n, p)
            if status == "not_applied":
                got["delivered"] += f" ({built})" if built else ""
        elif status == "not_applied" or not p:
            got = {"applied": False, "met": None, "delivered": f"not applied - {built or 'no setting of this mesher matches it'}"}
        elif field == "layers:count":
            got = _layers_row(req, q, built)
            if p.get("note") and got.get("met") is False:
                got["delivered"] += f"; {p['note']}"
        elif field == "layers:y_plus" and status in ("applied", "nearest") and p.get("bl_delta_m") and (
                br := _bl_cells_row(workspace, p)) is not None:
            # CFDREQ-22: a laminar resolved wall is judged by the cells across its boundary layer on the built mesh;
            # the switch from a typed wall-function ask alone is no miss (CFDREQ-17), cells missing still are
            got = br
            if status == "nearest":
                got = {**br, "met": False if br["met"] is False else None,
                       "delivered": f"not possible as asked - {built}; {br['delivered']}"}
        elif field == "layers:y_plus" and status in ("applied", "nearest") and not p.get("fallback") and (
                yr := _y_plus_row(workspace, req, q, p)) is not None:
            # CFDREQ-19: a wall-function y+ is judged by the y+ the built mesh's wall cells give
            got = yr
        elif field == "layers:y_plus" and status == "applied" and p.get("fallback") == "cells" and (
                fl := _first_layer_row(workspace, req, q, p)) is not None:
            # CFDREQ-25: accepted wall functions that did not fit the passage's cells: the resolved wall the proposal
            # promised, judged on its own first layer, told with why
            got = {**fl, "delivered": f"{built}; {fl['delivered']}"}
        elif field in ("layers:y_plus", "layers:first_height") and status == "applied" and (
                fl := _first_layer_row(workspace, req, q, p)) is not None:
            got = fl
        elif field == "layers:y_plus" and status == "nearest" and p.get("fallback") and (
                fl := _first_layer_row(workspace, req, q, p)) is not None:
            # CFDREQ-17: a typed wall-function ask on a body wall functions do not suit: the nearest there is, told with
            # the first cell built - the switch alone is no miss (met None); resolved layers lost still are
            got = {**fl, "met": False if fl.get("met") is False else None,
                   "delivered": f"not possible as asked - {built}; {fl['delivered']}"}
        elif field == "control:surface_level" and status in ("applied", "nearest") and (
                lv := _level_row(workspace, req)) is not None:
            got = lv
        elif field == "local" and status in ("applied", "nearest") and (
                pl := _place_row(workspace, req, row, p)) is not None:
            # CFDREQ-35: a size in a place is applied when the built mesh has it there, measured
            got = pl
        elif field == "local" and status in ("applied", "nearest") and p.get("picked") and Path(
                workspace, "constant", "polyMesh", "boundary").exists():
            # CFDREQ-35: no wall face of the built mesh lies on the face picked (it does not touch the flow): nothing
            # was built there, so it is not applied - never "applied" on a plan alone
            got = {"applied": False, "met": None,
                   "delivered": "not applied - no wall of the built mesh lies on the face you picked (it may not touch "
                                "the flow: pick a face the fluid wets)"}
        elif field == "local" and status in ("applied", "nearest") and (p.get("picked") or p.get("patches")):
            got = {"applied": True, "met": None,
                   "delivered": f"{built}; the cells there were not measured on the built mesh"}
        else:
            got = {"applied": True, "met": False if status == "nearest" else None,
                   "delivered": (f"not possible as asked - {built}" if status == "nearest" else built)
                   + (f"; {p['note']}" if p.get("note") else "")}
            if field == "max_edge" and figs is not None and len(figs["cell_size_m"]):
                big = float(figs["cell_size_m"].max())
                lim = float(req["sizes"]["max_edge_mm"]) * 1e-3
                got.update(measured=float(f"{big * 1e3:.4g}"), met=bool(big <= 1.05 * lim) and status != "nearest",
                           delivered=got["delivered"] + f"; the largest cell is {_mm(big)}")
        rows.append({"id": row["id"], "field": field, "asked": row["asked"], **got,
                     **({"from": row["from"]} if row.get("from") else {})})
    for i, d in enumerate(aside, 1):
        # an ask the checks set aside: '"<the user's words>": <why>: not applied' (engines/mesh_requirements.check)
        asked, _sep, why = d.partition(": ")
        why = why.removesuffix(": not applied").removesuffix(" not applied").strip() or "it could not be read"
        rows.append({"id": f"x{i}", "field": "set_aside", "asked": asked.strip().strip('"'), "applied": False,
                     "met": None, "delivered": f"not applied - {why}"})
    if engine_row is not None:
        rows.insert(0, engine_row)
    return MR.finish_report(rows, n_meas or n, DEFINITIONS)


def _count_row(field: str, req: Mapping, n: int, p: Mapping) -> dict:
    what = field.split(":", 1)[1]
    asked = float((req.get("cells") or {}).get(what) or 0.0)
    if not n:
        return {"applied": True, "met": None, "delivered": "the cell count was not measured"}
    if what == "ceiling":
        met = n <= asked * 1.0001
    elif what == "floor":
        met = n >= asked
    else:
        met = 0.75 * asked <= n <= 1.33 * asked
    said = f"the mesh has {n:,} cells"
    if p.get("status") == "nearest":
        said += f"; {p.get('built')}"
        met = False
    return {"applied": True, "measured": n, "met": bool(met), "delivered": said}




def _bl_cells_row(workspace, p: Mapping) -> dict | None:
    """CFDREQ-22: A LAMINAR RESOLVED WALL IS JUDGED BY WHAT IT PROMISED - BL_CELLS across its boundary layer (the
    plan's `bl_delta_m`, a fifth of the way along the body) on BL_WALL_SHARE of the wall, counted on the built mesh
    (layer_census.bl_cells: layer and core cells alike, to the nearest cell). Met, or missed with the share and the
    cells there are. None when the mesh cannot be counted (the row is judged as before)."""
    delta = _num_or_none(p.get("bl_delta_m"))
    if not delta:
        return None
    got = built_bl_cells(workspace, delta)
    if not got:
        return None
    share, med = float(got["bl_area_met_pct"]), float(got["bl_cells_median"])
    ok = share >= BL_WALL_SHARE
    said = (f"the wall is resolved; on the built mesh {BL_CELLS} or more cells (to the nearest cell) stand across "
            f"its boundary layer ({_mm(delta)} thick, a fifth of the way along the body) on {MR._g(share)}% of the "
            f"wall (median {MR._g(round(med, 1))})")
    if not ok:
        said += (f" - under the {MR._g(BL_WALL_SHARE)}% needed: elsewhere the layers were not built, so the boundary "
                 "layer there has fewer cells across it than it needs")
        if p.get("band_note"):
            # CFDREQ-23: why no finer cells at the wall made up for it
            said += f"; {p['band_note']}"
    return {"applied": True, "met": bool(ok), "measured": float(f"{med:.3g}"), "delivered": said,
            "bl_cells_median": med, "bl_area_met_pct": share, "bl_cells_p10": got.get("bl_cells_p10"),
            "bl_delta_m": float(f"{delta:.4g}")}


def built_bl_cells(workspace, delta_m: float) -> dict:
    """CFDREQ-22: the cells across a boundary layer `delta_m` thick on the delivered polyMesh's walls
    (layer_census.bl_cells); {} when it cannot be counted. Counted on the body's walls only (CFDREQ-19): the
    ground plane the domain lays is a far wall on background cells, not the boundary layer the plan resolves."""
    from meshpipeline.engines import layer_census as LC
    from meshpipeline.engines.workspace_facts import contract_body_walls
    ws = Path(workspace)
    try:
        walls = contract_body_walls(ws)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        walls = []
    if not walls or not (ws / "constant" / "polyMesh" / "owner").exists():
        return {}
    try:
        got, _why = LC.bl_cells(ws / "constant" / "polyMesh", walls, float(delta_m), met_at=BL_CELLS_MET)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("boundary-layer cell count failed; the row is judged as before", exc_info=True)
        return {}
    return got


def _first_layer_row(workspace, req: Mapping, q: Mapping, p: Mapping) -> dict | None:
    """CFDREQ-6: the first layer height row (_first_layer_facts), without the wall share it was judged on."""
    got = _first_layer_facts(workspace, req, q, p)
    return got[0] if got is not None else None


#: CFDREQ-19: the y+ range wall functions model (the log layer: y+ 30 to about 300; Pope, Turbulent Flows; the solvers'
#: guides), and how near the asked y+ the built one must be to meet it (0.6 to 2 times: 30 to 100 for the usual 50 -
#: the band wall_fit already builds in)
WALL_FUNCTION_RANGE = (30.0, 300.0)
Y_PLUS_ASKED_BAND = (0.6, 2.0)
#: CFDREQ-19: the share of the wall that must carry the first cell judged (the layer count's 80%, CFDREQ-6)
Y_PLUS_WALL_SHARE = 80.0


def y_plus_met(asked: float, built: float) -> bool:
    """CFDREQ-19: a wall-function y+ about `asked` is met by a built first-cell centre at `built`: inside the
    wall-function range and within Y_PLUS_ASKED_BAND of the ask."""
    lo, hi = WALL_FUNCTION_RANGE
    return (max(lo, Y_PLUS_ASKED_BAND[0] * float(asked)) <= float(built)
            <= min(hi, Y_PLUS_ASKED_BAND[1] * float(asked)))


def _y_plus_row(workspace, req: Mapping, q: Mapping, p: Mapping) -> dict | None:
    """CFDREQ-19: AN ASKED WALL-FUNCTION Y+ IS JUDGED ON THE BUILT MESH. The wall's first cell (median, the census
    CFDREQ-14 already takes) turned into the y+ of its centre by the same estimate the plan used (the plan's first
    layer and the y+ it expected): met when that y+ is in the wall-function range and near the ask (y_plus_met) on
    at least Y_PLUS_WALL_SHARE of the wall; else missed with the y+ reached and why. Before, a first layer capped by
    its wall cell (the thickest that fits, y+ 47 against 50) was told "not possible as asked" whatever was built, and
    a first cell half the plan (y+ 25, the buffer layer) counted as at the height asked. None when the ask is not a
    wall-function y+ or the plan has no first layer to scale by (the row is judged as before)."""
    asked = _num_or_none((req.get("layers") or {}).get("y_plus"))
    first, expected = _num_or_none(p.get("first_m")), _num_or_none(p.get("y_plus_expected"))
    if not asked or asked < WALL_FUNCTION_RANGE[0] or not first or not expected:
        return None
    got = _first_layer_facts(workspace, req, q, p)
    if got is None:
        return None
    fl, cover = got
    # CFDREQ-24: the y+ holds where the first cell is at the planned height with no jump to the cell above - the
    # layer's shape (taller than its wall faces are wide) is no reason to say it is not there
    share = _num_or_none(fl.get("area_first_at_plan_pct"))
    if share is not None:
        cover = share
    under = _num_or_none(fl.get("area_under_jump_pct")) or 0.0
    cell = _num_or_none(fl.get("first_cell_m"))
    if cell:
        said = f"on the built mesh the wall's first cell is {_mm(cell)} (median)"
    else:
        cell = float(fl["measured"])
        said = f"the first layer is set {_mm(cell)} high (its cells on the built mesh were not measured)"
    y = expected * cell / first
    y_s = MR._g(round(y) if y >= 2 else round(y, 2))
    ok_y, wide = y_plus_met(asked, y), cover >= Y_PLUS_WALL_SHARE
    said += f", so its centre sits at y+ about {y_s}"
    if ok_y:
        said += f", in the wall-function range and near the {MR._g(asked)} asked"
    else:
        lo = max(WALL_FUNCTION_RANGE[0], Y_PLUS_ASKED_BAND[0] * asked)
        hi = min(WALL_FUNCTION_RANGE[1], Y_PLUS_ASKED_BAND[1] * asked)
        said += f", not {MR._g(asked)} (y+ {MR._g(lo)} to {MR._g(hi)} meets it"
        said += (", and under 30 wall functions do not model the wall)" if y < WALL_FUNCTION_RANGE[0] else ")")
    if share is None:
        said += f"; layers of that height stand on {MR._g(cover)}% of the wall"
    else:
        said += f"; a first cell of that height stands on {MR._g(cover)}% of the wall"
    if under > 0.0:
        ratio = _num_or_none(fl.get("next_over_first"))
        jump = f" (the cell above about {MR._g(round(1.0 / ratio, 1))} times thinner)" if ratio else ""
        said += (f"; on {MR._g(under)}% it sits under much thinner cells{jump} - a jump in cell height going away "
                 "from the wall, which the wall function's first cell should not have")
    if not wide and under < 100.0 - cover - 1e-9:
        said += " - elsewhere the first cell is a different height, so y+ there is not the one said"
    if not ok_y and p.get("status") == "nearest" and p.get("built"):
        said += f"; {p['built']}"
    return {**fl, "met": bool(ok_y and wide), "y_plus_measured": float(f"{y:.4g}"), "delivered": said}


def _first_layer_facts(workspace, req: Mapping, q: Mapping, p: Mapping) -> tuple[dict, float] | None:
    """CFDREQ-6: a first layer height asked (or a y+ turned into one) against the built mesh - the height the
    mesher's dictionary was given, and the layers it really grew (the wall's coverage, the layer count's 80%): met,
    or not met with the numbers. None when either was not measured (the row stays "applied as a setting"). The
    reducer of job 63b0fd25 was built at 0.0224 mm with 100% of its wall covered and read "0 met; 1 applied as a
    setting"."""
    planned = p.get("first_m")
    if planned is None:
        first_mm = (req.get("layers") or {}).get("first_mm")
        planned = float(first_mm) * 1e-3 if first_mm is not None else None
    cover = q.get("layer_coverage_pct")
    if not planned or not isinstance(cover, (int, float)) or isinstance(cover, bool):
        return None
    # CFDREQ-33: the height the dictionary gave the plain wall cell - a relative stack's share read as that height,
    # never as metres (layer_census.planned_first_of reads both)
    from meshpipeline.engines.layer_census import planned_first_of, planned_plain_cell_of
    written = planned_first_of(workspace)
    if written is None:
        return None
    as_asked = abs(written - float(planned)) <= 0.01 * float(planned)
    # CFDREQ-14: the layers are counted at the height asked - the wall's first cell on the built mesh, every engine
    tall = built_first_cells(workspace, q, written)
    at_plan, first_cell = _num_or_none(tall.get("layer_area_at_plan_pct")), _num_or_none(tall.get("layer_first_cell_m"))
    measured_height = at_plan is not None and bool(first_cell)
    first_cell = first_cell or 0.0
    if at_plan is not None and measured_height:
        cover = at_plan
    grown = float(cover) >= 80.0
    said = (f"first layer {_mm(written)} high, as asked; layers on {MR._g(float(cover))}% of the wall" if as_asked
            else f"first layer {_mm(written)} high, not {_mm(float(planned))}; layers on "
                 f"{MR._g(float(cover))}% of the wall")
    if measured_height:
        said = (f"first layer {_mm(written)} set{', as asked' if as_asked else f', not {_mm(float(planned))}'}; "
                f"on the built mesh the wall's first cell is {_mm(float(first_cell))} (median)")
        ratio = float(first_cell) / float(written)
        if planned_plain_cell_of(workspace) and _num_or_none(tall.get("layer_first_vs_plan")):
            # CFDREQ-33: against each face's own plan - a finer face's thinner first cell is the plan, not thinner
            ratio = float(tall["layer_first_vs_plan"])
        from meshpipeline.engines.layer_census import PLAN_FIRST_TOL
        if ratio > PLAN_FIRST_TOL:
            said += f", {ratio:.3g} times that"
        elif ratio < THINNER_THAN_ASKED:
            # thinner than asked is said too: the y+ there is under the one planned (cfMesh thins the first
            # layer of a stack with more layers than its depth holds at that height; transition_005's rescue
            # re-mesh: 0.78 of the plan, which the 1.5x band alone read as "at plan")
            said += f", {ratio:.2g} times that - thinner than asked, so y+ there is lower than planned"
        said += f", and layers of that height stand on {MR._g(float(cover))}% of the wall"
        if not grown:
            said += (" - elsewhere the first cell is thicker than asked, so y+ there is higher than planned"
                     if float(cover) > 0 else " - the layers were not built at the height asked")
    elif as_asked and not grown:
        said += " - the rest of the wall has none, so the first cell there is not this height"
    out = {"applied": True, "met": bool(as_asked and grown), "measured": float(f"{written:.4g}"), "delivered": said}
    if measured_height:
        out["first_cell_m"] = float(first_cell)
        # CFDREQ-24: the wall share at the planned height with no jump above it, the share under a jump, the ratio
        for k in _AT_HEIGHT_KEYS:
            if (v := _num_or_none(tall.get(k))) is not None:
                out[k.removeprefix("layer_")] = v
    return out, float(cover)


def _num_or_none(v) -> float | None:
    """A plain number, or None (a bool is not one)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


#: CFDREQ-24: the census's reading of the first cell at the planned height and the cell above it (height_quality)
_AT_HEIGHT_KEYS = ("layer_area_first_at_plan_pct", "layer_area_under_jump_pct", "layer_next_over_first")
_PLAN_KEYS = ("layer_area_at_plan_pct", "layer_first_cell_m", "layer_first_vs_plan")


def built_first_cells(workspace, q: Mapping, planned_m: float) -> dict:
    """CFDREQ-14 on every engine: the wall's first cell against the planned first layer - the quality record's
    figures when its census measured them at that height (cfMesh), else measured here on the delivered polyMesh
    (engines/layer_census: any ascii OpenFOAM mesh up to its cell limit). {} when it cannot be measured.

    CFDREQ-19: measured on the walls of the body being studied, never on the ground plane the domain lays (the
    box's floor, a wall on background cells): abc 00034783_002's ground set the median first cell to 422 mm, y+
    about 5709, and the row read missed at 96% layer coverage. cfMesh's own census leaves the ground out too."""
    if q.get("layer_coverage_basis") == "height" and q.get("layer_first_planned_m"):
        try:
            if abs(float(q["layer_first_planned_m"]) - float(planned_m)) <= 0.01 * float(planned_m):
                return {k: q.get(k) for k in ("layer_area_at_plan_pct", "layer_first_cell_m", "layer_first_vs_plan",
                                              *_AT_HEIGHT_KEYS) if k in q or k in _PLAN_KEYS}
        except (TypeError, ValueError):
            pass
    from meshpipeline.engines import layer_census as LC
    from meshpipeline.engines.workspace_facts import contract_body_walls
    ws = Path(workspace)
    try:
        walls = contract_body_walls(ws)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        walls = []
    if not walls or not (ws / "constant" / "polyMesh" / "owner").exists():
        return {}
    try:
        # CFDREQ-33: a stack sized on each face is judged on each face's own plan
        per, _why = LC.census(ws / "constant" / "polyMesh", 1, walls, planned_first_m=float(planned_m),
                              plain_cell_m=LC.planned_plain_cell_of(ws))
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("first-cell census failed; the row reads the setting", exc_info=True)
        return {}
    return LC.height_quality(per) if per else {}


#: CFDREQ-27: the share of the wall under a jump past which a wall-function layer cannot meet the y+ row - the row
#: needs Y_PLUS_WALL_SHARE of the wall with its first cell in the band and no jump (CFDREQ-26), so over the rest of
#: the wall under a jump it is missed whatever else the layer does.
WALL_FUNCTION_JUMP_SWITCH_PCT = 100.0 - Y_PLUS_WALL_SHARE


def wall_function_jump(workspace, quality: Mapping | None) -> dict | None:
    """CFDREQ-27: the planned wall-function layer measured under a jump on more of the wall than the y+ row can
    leave out (WALL_FUNCTION_JUMP_SWITCH_PCT): {"under_jump_pct", "next_over_first", "first_m"}. None when the plan
    has no wall-function layer, when the built wall cannot be measured, or when the share is within the row's.

    snappyHexMesh lays its layer on the snapped wall cells and pushes them back by the layer's height; those cells
    are cut by the surface, a third of the castellated cell at the median (00122027: 0.64 mm under a 1.92 mm layer
    before any layer was added). Its layer-stage levers do not lift that (measured: nSmoothDisplacement 90, a
    displacementLaplacian shrinker, castellatedBufferLayer: 69-81% of that wall still under a jump), so the mesh of
    another engine - cfMesh splits its wall cell - is the one that can meet the row."""
    rows = _plan(workspace).get("rows")
    wf = next((r for r in (rows.values() if isinstance(rows, Mapping) else ())
               if isinstance(r, Mapping) and r.get("treatment") == "functions" and _num_or_none(r.get("first_m"))),
              None)
    if wf is None:
        return None
    first = float(wf["first_m"])
    got = built_first_cells(workspace, quality if isinstance(quality, Mapping) else {}, first)
    jump = _num_or_none(got.get("layer_area_under_jump_pct"))
    if jump is None or jump <= WALL_FUNCTION_JUMP_SWITCH_PCT:
        return None
    return {"under_jump_pct": round(jump, 1), "next_over_first": _num_or_none(got.get("layer_next_over_first")),
            "first_m": first}


#: CFDREQ-28: the share of the wall the planned stack must stand on - the layer row's own bar (_layers_row)
STACK_KEPT_PCT = 80.0


def stack_lost(workspace, quality: Mapping | None) -> dict | None:
    """CFDREQ-28: the planned layer stack (the y+ row's, any wall treatment) kept on less of the wall than the layer
    row needs (STACK_KEPT_PCT), on an external flow's body: {"coverage_pct", "layers", "treatment", "first_m"}. None
    for internal flow (CFDREQ-27 and cfMesh's own home there answer it), when the plan has no stack, when the
    coverage was not measured, or when it is kept on enough of the wall.

    snappyHexMeshDict's layer stage drops a stack wherever a layer would fold a cell past its quality bars; on real
    bodies a resolved 8-16 layer stack, or a laminar one sized on its boundary layer (CFDREQ-22), is lost on 20-50%
    of the wall (CFD board batch 7, external: 45 of 110 laminar bodies kept it on under 80%), and its levers - the
    band, the cap, the buffer cells - did not lift that. cfMesh splits its wall cell into the layers, so the next
    engine builds the stack."""
    from meshpipeline.engines.workspace_facts import read_flow_topology
    if read_flow_topology(workspace) != "external":
        return None
    rows = _plan(workspace).get("rows")
    stack = next((r for r in (rows.values() if isinstance(rows, Mapping) else ())
                  if isinstance(r, Mapping) and (_num_or_none(r.get("layers")) or 0) >= 1
                  and _num_or_none(r.get("first_m"))), None)
    cover = _num_or_none((quality or {}).get("layer_coverage_pct")) if isinstance(quality, Mapping) else None
    if stack is None or cover is None or cover >= STACK_KEPT_PCT:
        return None
    return {"coverage_pct": round(cover, 1), "layers": int(stack["layers"]),
            "treatment": str(stack.get("treatment") or ""), "first_m": float(stack["first_m"])}


_WALL_LEVEL = re.compile(r"refinementSurfaces\s*\{\s*[\w.]+\s*\{\s*level\s*\(\s*(\d+)\s+(\d+)\s*\)")
_BLOCKS = re.compile(r"hex\s*\([^)]*\)\s*\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)")


def _level_row(workspace, req: Mapping) -> dict | None:
    """NOTE-13 x CFDREQ-6: the wall's refinement level asked against the level snappyHexMesh's dictionary really
    carries (its first refinement surface, the wall) and the cell that gives at the walls - met, or not met with
    both levels and why. None when the dictionary cannot be read (the row stays a setting)."""
    n = ((req.get("controls") or {}).get("surface_level") or {}).get("value")
    try:
        shm = (Path(workspace) / "system" / "snappyHexMeshDict").read_text(errors="replace")
    except OSError:
        return None
    m = _WALL_LEVEL.search(shm)
    if n is None or m is None:
        return None
    built = int(m.group(2))
    cell = ""
    try:
        blk = (Path(workspace) / "system" / "blockMeshDict").read_text(errors="replace")
        pts = [tuple(float(v) for v in t.split()) for t in re.findall(r"\(([-+\d.eE]+ [-+\d.eE]+ [-+\d.eE]+)\)", blk)]
        blocks = _BLOCKS.search(blk)
        div = [int(v) for v in blocks.groups()] if blocks else []
        if len(pts) >= 7 and div:
            base = max(abs(pts[6][i] - pts[0][i]) / max(div[i], 1) for i in range(3))
            cell = f" (cells of {_mm(base / 2 ** built)} at the walls)"
    except (OSError, ValueError, IndexError):
        cell = ""
    if built >= int(n):
        return {"applied": True, "met": True, "measured": built, "delivered": f"level {built} at the walls{cell}"}
    return {"applied": True, "met": False, "measured": built,
            "delivered": f"level {built} at the walls{cell}, not {int(n)}: the level was counted again on the background "
                         "cell the cell budget allows"}


def _layers_row(req: Mapping, q: Mapping, built: str) -> dict:
    asked = int((req.get("layers") or {}).get("count") or 0)
    cover = q.get("layer_coverage_pct")
    in_dict = q.get("layers_in_meshdict")
    if asked == 0:
        return {"applied": True, "met": True, "delivered": "the mesh has no boundary layers"}
    said = built or f"{asked} layers were set"
    if isinstance(in_dict, (int, float)) and not isinstance(in_dict, bool) and in_dict < asked:
        return {"applied": True, "met": False, "delivered": f"{said}; {int(in_dict)} were built"}
    if isinstance(cover, (int, float)) and not isinstance(cover, bool):
        return {"applied": True, "met": bool(cover >= 80.0), "measured": float(cover),
                "delivered": f"{said}; {MR._g(cover)}% of the wall has them"}
    return {"applied": True, "met": None, "delivered": said}


def _limit_row(lim: Mapping, q: Mapping, figs: dict | None, n: int, p: Mapping) -> dict:
    measure = str(lim["measure"])
    _d, _r, unit, name = MR.MEASURES[measure]
    if measure not in WORST_KEYS:
        return {"applied": False, "met": None, "delivered": f"not applied - {name} is not a measure of CFD cells"}
    lever = (f" ({p['built']})" if p.get("status") == "applied" and p.get("built") else "")
    key, low = WORST_KEYS[measure]
    if figs is not None and measure in figs:
        got = MR.limit_row(lim, figs[measure], n)
        got["by"] = "every cell"
        got["delivered"] = _cells_words(got["delivered"]).replace("every element", "every cell")
        theirs = q.get(key)
        if isinstance(theirs, (int, float)) and not isinstance(theirs, bool) and got.get("worst") is not None:
            # the engine's own checkMesh line is the figure a user checks first: a disagreement is said, and a
            # limit it reads as missed is never told as met
            t, o = float(lim["target"]), lim.get("outer")
            edge = t if not float(lim.get("share") or 0.0) else (float(o) if o is not None else None)
            if edge is not None and (float(theirs) < edge if low else float(theirs) > edge) and got.get("met") is True:
                got["met"] = None
            if abs(float(theirs) - float(got["worst"])) > 0.01 * max(abs(float(theirs)), 1e-12):
                got["delivered"] += f" (the mesher's own check reads the worst as {MR._g(float(theirs))}{unit})"
        got["delivered"] += lever
        return got
    worst = q.get(key)
    if not isinstance(worst, (int, float)) or isinstance(worst, bool):
        return {"applied": True, "met": None, "delivered": f"{name} was not measured on this mesh{lever}"}
    worst = float(worst)
    target, outer, share = float(lim["target"]), lim.get("outer"), float(lim.get("share") or 0.0)
    inside = worst >= target if low else worst <= target
    past_outer = outer is not None and (worst < float(outer) if low else worst > float(outer))
    if inside:
        met, said = True, f"every cell is inside it (the worst is {MR._g(worst)}{unit})"
    elif share == 0.0 or past_outer:
        met, said = False, f"the worst cell is {MR._g(worst)}{unit}"
    else:
        met, said = None, (f"the worst cell is {MR._g(worst)}{unit}; the share of cells past {MR._g(target)}{unit} was "
                           "not measured on this mesh")
    return {"applied": True, "met": met, "worst": worst, "outside": 0 if inside else None, "beyond": 0, "by": "the worst cell",
            "share": share, "delivered": said + lever}


def summary(rep: Mapping | None, not_applied: int = 0) -> str:
    """The one line the closing message carries ("From your notes: ...")."""
    return MR.summary(rep, kind="flow", not_applied=not_applied).replace("could not be applied to a flow mesh",
                                                                       "could not be applied on this mesher")


def facts_text(manifest: Mapping | None) -> str:
    """CFDREQ-6 for the reviewer: the user's requirements against what the mesh delivers, one plain line each, as
    the pipeline measured them ('' with none). A note is context: a missed one is delivered with notes, never a
    reason to fail the mesh."""
    q = (manifest or {}).get("quality") if isinstance(manifest, Mapping) else None
    rep = (q or {}).get("requirements") if isinstance(q, Mapping) else None
    lines = MR.report_words(rep) if isinstance(rep, Mapping) else []
    if not lines:
        return ""
    return ("WHAT THE USER'S NOTES ASKED OF THE MESH, AGAINST WHAT WAS DELIVERED (measured by the pipeline; a "
            "requirement not met is delivered with notes, never a reason to fail the mesh):\n"
            + "\n".join(f"- {line}" for line in lines))


def attach(workspace, quality: dict, engine: str) -> dict:
    """The report into a flow mesh's quality record (quality["requirements"], what the result's rows and the
    verdict read) and its one line among the result's notes. Unchanged with nothing asked; never raises."""
    if not workspace or "requirements" in (quality or {}) or not (Path(workspace) / MR.FILE).exists():
        return quality
    try:
        rep = report(workspace, quality, engine)
    except Exception:  # noqa: BLE001 - a report that cannot be made never stops a delivery
        logger.warning("cfd requirements: no report", exc_info=True)
        return quality
    # an attempt whose notes asked only things no setting matches still says so in its closing line (never silent)
    out = {**quality, "requirements": rep} if rep is not None else quality
    try:
        from meshpipeline.contracts.user_notes import record_remark
        from meshpipeline.engines import meshing_standard as MS
        sec = MR.read(workspace)
        # STD-2: the closing line says where the things asked came from ("From your settings: ..."); an ask the checks
        # set aside is a row of the report already, so it is not counted twice among the other things not applied
        others = max(0, len(MR.not_applied_words(sec)) - len(sec.get("dropped") or []))
        said = MS.remark(sec, summary(rep, not_applied=others))
        if said:
            record_remark(Path(workspace), said, by=CONSUMER)
    except Exception:  # noqa: BLE001
        logger.debug("cfd requirements: no remark", exc_info=True)
    return out


__all__ = ["Ask", "CONSUMER", "DEFINITIONS", "ENGINES", "NU", "PLAN_FILE", "ROUTE", "RULE_CELLS", "RULE_LAYERS",
           "RULE_LIMITS", "RULE_REPORT", "RULE_SAME", "RULE_SEAM", "RULE_SIZES", "SNAPPY_QUALITY", "attach",
           "cells_between_levels", "flow_run", "level_for", "netgen_built", "netgen_inputs", "netgen_planned",
           "read_for_attempt",
           "report", "requirements_for", "wrap_rows",
           "skin_friction", "snappy_dict", "summary", "y_plus_height"]
