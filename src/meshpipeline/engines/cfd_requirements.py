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
    # STD-5: a saved standard written for other kinds of mesh, applied in nothing here: told before meshing and with
    # the result (the run is the run with no standard)
    off = str(sec.get("standard_not_applied") or "") if sec.get("status") == "none" else ""
    # CFDREQ-8: the flow conditions the user confirmed size the first layer, as a y+ in their notes would
    sec = with_conditions(sec, state)
    # NOTE-15: what the notes say that the product acted on outside the reading - the engine they name (this
    # attempt's engine says whether it was used), the flow conditions confirmed, the kind of mesh, the unit - is
    # never told or counted as not applied, whatever the reading made of it
    from meshpipeline.engines import notes_acted as NA
    sec = NA.apply(sec, MR.notes_text(state or {}), NA.facts_of(state, engine))
    MR.write(ws, sec)
    if MR.strictness_unnamed(sec):
        # NOTE-14: a note on how strict the limits are that names none the user set: one fixed plain line
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(ws, MR.NO_LIMIT_WORDS, by=MR.STRICTNESS_REMARK)
    (ws / PLAN_FILE).unlink(missing_ok=True)          # a carried plan never speaks for this attempt
    if off:
        MS.told_not_applied(ws, off)
        if publish is not None:
            await _N.asay(publish, off, op_id="cfd:notes:standard")
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
            await _N.asay(publish, line, op_id=f"cfd:notes:{i}", quoted=MR.live_quotes(line))
    return sec


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


def y_plus_height(y_plus: float, physics: Mapping | None, *, length_m: float | None = None,
                  length_from: str = "") -> tuple[float | None, list[str], dict]:
    """(the first layer height in metres for this y+, the numbers missing for it, how it was worked out). The
    reference length is the notes' own, else `length_m` (the case's: the user's ruler, else the body's length
    along the flow, said by `length_from`)."""
    ph = dict(physics or {})
    U, nu, Re = ph.get("velocity_ms"), ph.get("nu_m2s"), ph.get("reynolds")
    if nu is None and ph.get("fluid") in NU:
        nu = NU[str(ph["fluid"])]
    L, said_L = ph.get("length_m"), "the length in your notes"
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
    cf = skin_friction(re_n)
    if U is not None and nu is not None:
        h = float(y_plus) * float(nu) / (float(U) * math.sqrt(cf / 2.0))
    else:
        h = float(y_plus) * length / (re_n * math.sqrt(cf / 2.0))
    return h, [], {"reynolds": float(f"{re_n:.4g}"), "cf": float(f"{cf:.4g}"), "length_m": length,
                   "length_from": said_L, "first_m": float(f"{h:.4g}")}


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
    if not c.get("wall_said", True) and c["wall"] == "functions":
        # THE PROPOSAL ONLY ACCEPTED IS NO ASK (round 4, F): pressing OK on the proposed wall functions made an
        # absolute y+ 50 first layer about as thick as the wall cells, which snappyHexMesh grew on 4-6% of the wall
        # (jobs 07fd516d, 8eec5b7b, c0b45ad1); the mesher's own relative layers grow on all of it. A y+, first layer or
        # wall treatment the user gave themselves stays an ask (below, and CFDREQ-9 makes it one the mesher keeps).
        # Their speed and fluid still reach a y+ of their own notes.
        req0 = (sec.get("requirements") or {}) if sec.get("status") == "used" else {}
        lay0, ph0 = req0.get("layers") or {}, req0.get("physics") or {}
        if lay0.get("y_plus") is None or (ph0.get("velocity_ms") is not None and ph0.get("fluid") is not None):
            return sec
    before = MR.words(sec)
    used = sec.get("status") == "used"
    req = copy.deepcopy(dict(sec.get("requirements") or MR.empty())) if used else MR.empty()
    lay = req.setdefault("layers", {})
    ph = req.setdefault("physics", {})
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
        if ph.get("velocity_ms") is None and c["velocity_ms"] is not None:
            ph["velocity_ms"] = float(c["velocity_ms"])
        if ph.get("fluid") is None and c["fluid"] in FC.NU:
            ph["fluid"] = c["fluid"]
        asked = f"y+ of about {MR._g(c['y_plus'])} at the walls ({treatment}), for {c['fluid_words']} at {c['speed_words']}"
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
        s = self.req.get("sizes") or {}
        self.global_m = _m(s.get("global_mm"))
        self.min_m = _m(s.get("min_edge_mm"))
        # NOTE-14: a smallest cell the user said is HARD is held even where the body's shape would want finer cells
        self.min_hard = any(r.get("field") == "min_edge" and r.get("strictness") == "hard"
                            for r in self.req.get("rows") or [] if isinstance(r, Mapping))
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
        self.local = [dict(e) for e in self.req.get("local") or []]
        #: NOTE-17: each size typed with picked faces, re-checked on the face map by the engine (snappy/part_levels
        #: .picked_surfaces): entry id -> {"file", "where"} or {"gone"}; and the level each picked file is refined to
        self.picked: dict[str, dict] = {}
        self.picked_levels: dict[str, int] = {}
        self.limits = [dict(lim) for lim in self.req.get("limits") or []]
        #: the case's reference length for a y+ (the engine sets it: the user's ruler, else the body's length)
        self.length_m: float | None = None
        self.length_from = ""
        #: snappy: levels per named region the user sized; whether a budget of ours was raised for their sizes
        self.region_levels: dict = {}
        self.raised = False
        #: why the layers may cover less of the wall (a quality limit of the user's held while they are added)
        self.layer_note = ""

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
        self.done("layers:y_plus", f"first layer {_mm(h)} high (worked out at Re {MR._g(how['reynolds'])}, with "
                                   f"{how['length_from']} as the reference length)", **how)
        return h


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


#: words that mean the whole body on a model of one body ("the body", "the walls", "the surface")
_WHOLE_BODY = re.compile(r"^(?:(?:on|at|for|over|in)\s+)?(?:the\s+)?(?:(?:whole|entire|all(?:\s+the)?)\s+)?"
                         r"(?:body|bodies|walls?|surfaces?|model|part|object|geometry|skin|hull|shape)$"
                         r"|\beverywhere\b|\ball\s+over\b", re.I)


def _picked_level(ask: Ask, e: Mapping, base: float, *, floor: int, level: int, hard_max: int) -> None:
    """NOTE-17: a size typed with faces picked on the model, on snappyHexMesh: finer than the walls -> a distance
    refinement on those faces' own triangles at its level (ask.picked_levels, written by the dict); coarser or no
    finer than the walls, or the faces gone -> told, with the next best."""
    got = ask.picked.get(str(e["id"])) or {}
    where = str(e.get("where") or "the face you picked")
    if not got:
        ask.not_here(e["id"], f"{where} could not be re-checked on the part as meshed, so nothing was set there "
                              "(no other face was guessed) - next best: a cell size at the walls")
        return
    if not got.get("file"):
        ask.not_here(e["id"], f"{got.get('gone') or where} is no longer on the part as meshed, so nothing was set "
                              "there (no other face was guessed)")
        return
    if e.get("what") == "coarser":
        ask.not_here(e["id"], "coarser cells on one face need that face as its own surface; not available yet - next "
                              "best: a larger cell size for the whole mesh")
        return
    lv = (level_for(base, float(e["mm"]) * 1e-3, lo=floor, hi=hard_max) if e.get("what") == "size"
          else min(hard_max, int(level) + 1))
    if lv <= int(level):
        ask.not_here(e["id"], f"cells of {_mm(base / 2 ** lv)} on {where} is not finer than the cells the walls "
                              f"already have ({_mm(base / 2 ** int(level))}); coarser cells on one face are not "
                              "available yet - next best: a larger cell size for the whole mesh")
        return
    ask.picked_levels[str(got["file"])] = int(lv)
    tail = f" ({got['gone']} is no longer on the part: not set there)" if got.get("gone") else ""
    ask.done(e["id"], f"cells of {_mm(base / 2 ** lv)} on {where}{tail}", built_m=base / 2 ** lv, level=int(lv))


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
        if "wake" in where.casefold():
            continue                                   # snappy_wake
        if e.get("faces"):
            # NOTE-17: a size typed with picked faces is for those faces only - never spread to a part or every wall
            _picked_level(ask, e, base, floor=floor, level=lv, hard_max=hard_max)
            continue
        if part_levels:
            ask.done(e["id"], "set part by part (see the lines about your parts)")
            continue
        r = _region_of(where, regions)
        if r is None and len(list(regions)) <= 1 and not _WHOLE_BODY.search(" ".join(where.split())):
            # ON ONE BODY A PLACE THAT IS NOT THE BODY IS NOT LOCATED: "refine the leading edge" refined the whole
            # body; it is told, with the next best, and nothing is spread over the walls
            ask.not_here(e["id"], f"'{where}' was not found on this part, so nothing was set there - next best: "
                                  "click that face on the part and type the size with it")
            continue
        if r is None and len(list(regions)) > 1:
            ask.not_here(e["id"], f"no part named '{where}' in this model (its parts: {', '.join(map(str, regions))})")
            continue
        if e["what"] == "size":
            got = level_for(base, float(e["mm"]) * 1e-3, lo=floor, hi=hard_max)
        else:
            got = min(hard_max, lv + 1) if e["what"] == "finer" else max(floor, lv - 1)
        cell = base / 2 ** got
        if r is not None:
            ask.region_levels[r] = (got, got)
            ask.done(e["id"], f"cells of {_mm(cell)} on {r}")
        else:
            if ask.wall_m is None:
                lv = got
            ask.done(e["id"], f"read as the body's walls: cells of {_mm(cell)} there", built_m=cell)
    cap = hard_max
    if ask.min_m is not None:
        most = int(math.floor(math.log2(base / ask.min_m) + 1e-9))
        if most < floor:
            cap = floor
            # (NOTE-14: a HARD minimum too - the floor is the level that closes the body off from the flow, validity
            # before any limit; said so)
            ask.nearest("min_edge", f"the finest cells are {_mm(base / 2 ** cap)}: larger ones would not hold the "
                                    "body's shape" + (" (your minimum is hard, but cells that large would let the flow "
                                                      "leak into the body)" if ask.min_hard else ""),
                        built_m=base / 2 ** cap)
        else:
            cap = min(hard_max, most)
            ask.done("min_edge", f"the finest cells are {_mm(base / 2 ** cap)} (boundary layer cells are thinner, "
                                 "by design)", built_m=base / 2 ** cap)
        lv = min(lv, cap)
        ask.region_levels = {r: (min(a, cap), min(b, cap)) for r, (a, b) in ask.region_levels.items()}
    return cap, lv, lv, min(cap, max(lv, int(flevel)))


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
            continue                    # NOTE-17: sized on the picked faces' own surface (snappy_levels)
        if "wake" in where.casefold():
            ask.not_here(e["id"], "an internal flow has no wake behind a body")
            continue
        hits, how = openings_named(where, list(ports), roles)
        if not hits:
            if re.search(r"\b(wall|walls|surface|pipe|duct|passage|inside)\b", where, re.I):
                ask.not_here(e["id"], "the walls take the size at the walls; give it as a wall cell size")
            else:
                ask.not_here(e["id"], f"no opening named '{where}' in this model (its openings: "
                                      f"{_openings_words(list(ports), roles)})")
            continue
        made, held = [], []
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
        ask.done(e["id"], said, built_m=base_m / 2 ** lv)
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
                 roles: Mapping[str, str] | None = None) -> tuple[str, list[dict], float]:
    """(localRefinement entries for the patches the user named, objectRefinements for a wake box). A coarser cell
    than the wall's on a named wall is not a refinement cfMesh makes: said."""
    from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell

    names = [str(p.get("name")) for p in (patches or []) if p.get("name")]
    # CFDREQ-12: the roles confirmed at intake - the patches' own types, over the port declaration's
    roles = {**dict(roles or {}), **{str(p["name"]): str(p.get("type") or "").lower() for p in (patches or [])
                                     if p.get("name") and str(p.get("type") or "").lower() in OPENING_ROLES}}
    entries, boxes = "", []
    for e in ask.local:
        where = str(e.get("where") or "")
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
                              + (f" ({how})" if how else ""), built_m=octree_cell(max_cell, size))
            continue
        hit = hits[0] if hits else _region_of(where, names)
        if hit is None:
            if len([n for n in names if n == wall_patch]) and len(names) <= 3 and e["what"] != "coarser":
                hit = wall_patch          # one body: "around the wing" is its wall
            else:
                ask.not_here(e["id"], f"no part named '{where}' in this model (its parts: "
                                      f"{_openings_words(names, roles)})")
                continue
        if size is None:
            ask.not_here(e["id"], "this mesher refines a surface; a coarser cell than the wall's is not a refinement it "
                                  "makes")
            continue
        size = max(size, floor)
        got = octree_cell(max_cell, size)
        if hit == wall_patch:
            if ask.wall_m is None:
                wall_cell = size
            ask.done(e["id"], f"cells of {_mm(octree_cell(max_cell, wall_cell))} on the body's walls", built_m=got)
            continue
        entries += f"    {hit} {{ cellSize {size:.6g}; }}\n"
        ask.done(e["id"], f"cells of {_mm(got)} on {hit}", built_m=got)
    return entries, boxes, wall_cell


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

@guarded(lambda ask, s: dict(s))
def vmtk_spec(ask: Ask, s: Mapping) -> dict:
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
    for f in ("layers:first_height", "layers:total_thickness", "layers:y_plus"):
        if ask.rid(f):
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
                bridge: Mapping | None = None) -> str:
    """The snappyHexMeshDict text with the user's growth, layer sizes and quality limits applied (CFDREQ-2, 4, 5).
    The count of layers, the sizes and the budget are the plan's (the drivers set them before the dict is
    written). Unchanged with nothing asked. `bridge`: {"faces": wall faces, "room": cells the layers may add} on a
    case whose first layer may be far thinner than its wall cells (layer_bridge, CFDREQ-9); None leaves the count
    the plan's."""
    if ask is None:
        return text
    out = text
    if ask.growth is not None and "nCellsBetweenLevels" in out:
        n = cells_between_levels(float(ask.growth))
        out = _sub(out, "nCellsBetweenLevels", str(n))
        ask.done("growth", f"{n} cells between refinement levels, so sizes grow about {MR._g(2 ** (1 / n))} from one "
                           "cell to the next", cells_between_levels=n)
    if "addLayersControls" in out and (ask.first_m or ask.total_m or ask.layer_growth or ask.y_plus):
        out = _snappy_layers(ask, out, wall_cell_m, bridge)
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


#: CFDREQ-9: a first layer more than this many times thinner than the wall cells it grows into is not kept by
#: snappyHexMesh with the plan's few layers (measured: a 0.011 mm first layer under 2 mm wall cells kept layers on
#: 1.8% of a wing; a 0.022 mm one under 4.4 mm cells on 0% of a pipe reducer, job f4019933; 0.027 mm under 1.25 mm
#: cells, 46 times, kept 81.6% of a rotor, job 9c638f7a)
THIN_LAYER_RATIO = 50.0
#: CFDREQ-9: the most layers a stack bridged that way gets; past it the first layer is the thinnest it still fills
MAX_BRIDGE_LAYERS = 25


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


def _snappy_layers(ask: Ask, text: str, wall_cell_m: float | None, bridge: Mapping | None = None) -> str:
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
    bridged = None
    if (bridge is not None and first is not None and total is None and ask.n_layers is None and n > 0
            and wall_cell_m and wall_cell_m / first > THIN_LAYER_RATIO):
        last_rel = _num(out, "finalLayerThickness") or 0.3
        n_new, first_new, stack = layer_bridge(first, float(wall_cell_m), n_plan=n, growth=g, last_rel=last_rel,
                                               faces=bridge.get("faces"), room=bridge.get("room"))
        if n_new > n:
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_new};", out)
            bridged = (n, n_new, first, first_new, stack)
            n, first = n_new, first_new
    thick = None
    if (bridged is None and first is not None and total is None and ask.n_layers is None and n > 0
            and wall_cell_m):
        # CFDREQ-9, THE THICK SIDE: a first layer so thick that the plan's layers make a stack several wall cells
        # deep, which snappyHexMesh cannot shrink the mesh for (a y+ 50 layer, 1.35 mm under 1.72 mm cells: 5 layers
        # are 10 mm, and grew on 5.7% of the wall) - as few layers as fill the stack our own plan gives the wall
        last_rel = _num(out, "finalLayerThickness") or 0.3
        n_new, first_new, stack = layer_fit(first, float(wall_cell_m), n_plan=n, growth=g, last_rel=last_rel)
        if n_new < n or first_new < first:
            out = re.sub(r"\bnSurfaceLayers\s+[1-9][0-9]*\s*;", f"nSurfaceLayers {n_new};", out)
            thick = (n, n_new, first, first_new, stack)
            n, first = n_new, first_new
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
        if bridged is None and first is not None and wall_cell_m / first > THIN_LAYER_RATIO:
            # a stack far thinner than the cells it grows into is hard for the mesher to keep (measured: a 0.011 mm
            # first layer under 2 mm wall cells kept layers on 1.8% of a wing): said where it is set and where the
            # layers are reported, with what keeps more of them
            thin = (f"the first layer is {int(wall_cell_m / first):,} times thinner than the wall cells "
                    f"({_mm(wall_cell_m)}): layers that thin are hard to keep; finer cells at the walls keep more of them")
            ask.rows.get(ask.rid("layers:first_height") or ask.rid("layers:y_plus") or "", {}).update(note=thin)
            if not ask.layer_note:
                ask.layer_note = thin
    if bridged is not None:
        _say_bridge(ask, bridged, g, float(wall_cell_m or 0.0))
    if thick is not None:
        _say_thick(ask, thick, g, float(wall_cell_m or 0.0))
    return out


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


def _cannot(engine: str, field: str) -> bool:
    """Whether `engine` has no setting for a requirement row's field (the registry's NotHere for a control, the
    first-layer engines for a layer height or a y+)."""
    if field in ("layers:first_height", "layers:y_plus"):
        return engine not in FIRST_LAYER_ENGINES
    if field == "layers:total_thickness":
        return engine not in ("snappy", "snappy_multiregion", "netgen")
    if field.startswith("control:"):
        from meshpipeline.contracts import mesh_controls as MC
        return isinstance(MC.lane_of(LANES.get(engine, engine), field.split(":", 1)[1]), MC.NotHere)
    return False


def engine_that_can(sec: Mapping | None, engine: str, ladder, *, conditions_y_plus: bool = False) -> tuple[str, str]:
    """ENG-asks: (the engine to mesh with, the plain line that says why - "" when it is `engine`). When the notes'
    reading (and a wall treatment the user chose, `conditions_y_plus`) asks something `engine` has no setting for and
    another engine of the job's `ladder` can set all of those, that one - the first in the ladder's order. Nothing
    asked, or nothing `engine` cannot set: `engine` itself, as always."""
    req = MR.requirements_of(sec) or {}
    fields = [str(r.get("field") or "") for r in req.get("rows") or [] if isinstance(r, Mapping)]
    if conditions_y_plus and "layers:y_plus" not in fields and "layers:first_height" not in fields:
        fields.append("layers:y_plus")
    missing = [f for f in fields if _cannot(engine, f)]
    if not missing:
        return engine, ""
    # an absolute first layer (or a y+ made one) goes to the engine that DELIVERS it over the whole wall first, then
    # the ladder's order: on s_duct_004, y+ 1 on cfMesh had layers on 78% of the wall (the rest none, not met), on
    # snappyHexMesh on 100% (m483 round 9, 2e against 2c)
    order = list(ladder or ())
    if any(f in LAYER_HEIGHT_FIELDS for f in missing):
        order.sort(key=lambda e: LAYER_HEIGHT_DELIVERS.index(e) if e in LAYER_HEIGHT_DELIVERS
                   else len(LAYER_HEIGHT_DELIVERS))
    for other in order:
        if other != engine and not any(_cannot(str(other), f) for f in missing):
            from meshpipeline.engines.registry import engine_label
            try:
                name = engine_label(str(other))
            except Exception:  # noqa: BLE001 - the engine's own name still says which
                name = str(other)
            what = _ASK_NAMES.get(missing[0]) or next(
                (str(r.get("asked")) for r in req.get("rows") or [] if r.get("field") == missing[0]), "what")
            return str(other), f"Meshed with {name}, because it can set {what} you asked for."
    return engine, ""


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
            got = _layer_cells_said(limits[row["id"]], got, q, row)
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
        elif field in ("layers:y_plus", "layers:first_height") and status == "applied" and (
                fl := _first_layer_row(workspace, req, q, p)) is not None:
            got = fl
        elif field == "control:surface_level" and status in ("applied", "nearest") and (
                lv := _level_row(workspace, req)) is not None:
            got = lv
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
                     **({"from": row["from"]} if row.get("from") else {}), **MR.carried(row)})
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


#: CFDREQ-6: where each mesher writes the first layer height it was given (the dictionary in the case), and the key
_FIRST_LAYER_IN = (("system/snappyHexMeshDict", "firstLayerThickness"), ("system/meshDict", "maxFirstLayerThickness"))


def _first_layer_row(workspace, req: Mapping, q: Mapping, p: Mapping) -> dict | None:
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
    written = None
    for rel, key in _FIRST_LAYER_IN:
        try:
            written = _num((Path(workspace) / rel).read_text(errors="replace"), key)
        except OSError:
            continue
        if written is not None:
            break
    if written is None:
        return None
    as_asked = abs(written - float(planned)) <= 0.01 * float(planned)
    grown = float(cover) >= 80.0
    said = (f"first layer {_mm(written)} high, as asked; layers on {MR._g(float(cover))}% of the wall" if as_asked
            else f"first layer {_mm(written)} high, not {_mm(float(planned))}; layers on "
                 f"{MR._g(float(cover))}% of the wall")
    if as_asked and not grown:
        said += " - the rest of the wall has none, so the first cell there is not this height"
    return {"applied": True, "met": bool(as_asked and grown), "measured": float(f"{written:.4g}"), "delivered": said}


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


#: a limit written for even cells is not judged on prism-layer cells (docs/RULES.md)
LAYER_CELLS_RULE = "CFDREQ-11"
#: CFDREQ-11: the shape limits written for even (isotropic) cells - judged on a mesh with prism layers, the layers'
#: own thin cells count against them by design
ISOTROPIC_MEASURES = ("aspect_ratio",)


def _layer_cells_said(lim: Mapping, got: dict, q: Mapping, row: Mapping | None = None) -> dict:
    """CFDREQ-11: an aspect-ratio limit (written for even cells - a structural criteria list's "aspect ratio under 4")
    on a mesh with prism layers is not judged: the layer cells are thin by design, their aspect ratio set by the first
    layer height, and this mesh's figures do not tell them apart from the rest - so the share past it is told as
    information, never "not met" (a y+ 1 stack read "43.9% of cells over 4 - not met"). Met, or no layers: as it was."""
    cover = q.get("layer_coverage_pct")
    layered = isinstance(cover, (int, float)) and not isinstance(cover, bool) and float(cover) > 0.0
    if (str(lim.get("measure")) not in ISOTROPIC_MEASURES or not layered or got.get("met") is not False
            or (row or {}).get("strictness") == "hard"):
        return got                      # (said hard: judged on every cell, as the user asked)
    return {**got, "met": None, "layer_cells": True,
            "delivered": f"{got.get('delivered')} - prism layer cells are thin by design (set by the first layer height) "
                         "and are counted here; a limit for even cells is told, not judged, on a mesh with layers"}


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
        if not lever:
            # neither set by this mesher nor measured on its cells: never "set as asked" (an aspect ratio from a saved
            # criteria list on VMTK and cfMesh read "Set as asked" over "was not measured", m483 with a standard)
            return {"applied": False, "met": None,
                    "delivered": f"not applied - this mesher has no setting for {name}, and it was not measured on "
                                 "this mesh"}
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
