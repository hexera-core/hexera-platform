# Responsibility: Rank the engines that can take a geometry by how they did, in the lab, on shapes measured to be like it.
# Owns: the shape classes (from measured traits), the committed fitness table's reading, the ranking and its plain-words reasons.
# Boundaries: advice only - it never selects, refuses or switches an engine; the capability filter decides who CAN, the user picks.
# Collaborates with: cad/shape_traits.py (the measurements), engines/fitness_table.json (the evidence, written by
#                    devtools/fitness/build_engine_fitness.py), engines/capability.py, agents/intake/, pipeline/engine_fallback.py.
"""WHICH ENGINE IS BEST FOR THIS SHAPE - measured, explained, and never imposed.

The capability filter (engines/capability.py) answers which engines CAN mesh a file for a flow.
This module answers which of those is likely BEST for this particular geometry, and why, from
evidence: every lab run of every engine on the production mesh image, each geometry measured with
the same shape traits (cad/shape_traits.py) a user's upload is measured with. The user always
picks; this is the recommendation they pick from.

THE MODEL is a table, not a black box. Each lab run is one row: the measured traits of its
geometry, the engine, the file's form and what happened (passed or not, layer coverage, cells,
time). A new geometry is put in a SHAPE CLASS by its traits:

    internal  tube        one passage, about as wide as its openings, long against its width
              branched    the same, with three or more openings
              chamber     much wider than its openings, or short and stubby
              thin gap    a gap or annulus: the passage spans half its openings' hydraulic diameter
              obstacles   a passage round many internal obstacles - a tube bank (three or more
                          handles through the fluid)
    external  thin body   under a quarter as thick across the flow as it is long (wings, blades)
              thick body  thicker (cars, nacelles, cylinders, discs facing the flow)
              + detailed  several bodies, an open or non-manifold surface, or dense sharp edges
    multi-region          fluid and solid regions together

For each engine the evidence is read at four levels, most specific first: the same class from the
same kind of file, the same class from any file, the same flow from the same kind of file, the same
flow. An EXTERNAL body is first read against bodies with as much fine detail as it has (detailed or
plain, from the same kind of file, then from any file): a car with mirrors, wheels and 44 handles
is not a smooth Ahmed body, and an engine's record on smooth bodies says little about it (cfMesh
passed 23 of 23 smooth thick bodies from surface meshes in the lab, then made broken meshes on 7
of 8 DrivAer cars - e2e sweep, 2026-10-17).
Its pass rate is read from the most specific level that has runs, plus two pseudo runs at the
rate of the next broader group that has more, so a few runs on shapes exactly like this count, but
only as much as a few runs can.
Engines rank by a CAUTIOUS score - that rate less one standard error of it - so an engine with a
long record on shapes like this outranks one that was never tried on them (whose record on other
kinds of shape is halved toward even odds and weighs no more than the pseudo runs), and three
passes out of three never outrank thirty out of thirty-two. Scores within 5% tie; then, BEFORE
speed, what the meshes were like: the share of runs whose mesh came out INVALID (broken cells),
then the share of delivered meshes whose cells went past the 70-degree non-orthogonality limit
(in fifths), then measured layer coverage, and only then time. A fast engine whose meshes come
out broken or badly shaped never wins a tie on speed. Every number in a reason is a count of
real runs; thin evidence says so.

A QUALITY FAULT outweighs the pass rate, not only a tie. An engine whose own record on shapes like
this shows MOST of its delivered meshes past the cell-shape limit, MOST of its tries broken, or
near-wall layers on under half the wall, is not recommended over an engine with a measured record
and no such fault: "passed" there means the mesh cleared that engine's bars, not that it is the
mesh a solver wants. (cfMesh passed 38 of 38 thin bodies from CAD with 24 of the 38 past the
limit; snappyHexMesh passed 6 of 6 with none - the rate alone recommended cfMesh.) Both stay on
the list with their reasons, and the user still picks.

Engines may also DECLARE where they are at home (EngineSpec.home_turf, rows of a fact, a test, a
severity and words). Those rows give the plain "why"; an OUTSIDE row keeps an engine from being
recommended - it stays on the list, marked, for the user to pick if they want it. The ranking
itself comes only from measured runs.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from meshpipeline.cad.shape_traits import TRAITS_VERSION, ShapeTraits

logger = logging.getLogger(__name__)

#: The committed evidence. Regenerated by devtools/fitness/build_engine_fitness.py.
TABLE_PATH = Path(__file__).with_name("fitness_table.json")

#: An evidence level is used once it holds runs on at least this many distinct shapes.
MIN_SHAPES = 3
#: Pass rates are shrunk toward the next broader group's rate by this many pseudo runs.
PRIOR_RUNS = 2.0
#: The score is the rate less this many standard errors of it (a cautious estimate).
CAUTION = 1.0
#: Rates closer than this rank as a tie (then layers, then time decide).
TIE_STEP = 0.05
#: What a mesh WITHOUT reliable near-wall layers is worth, against one with them, when the brief
#: asks for layers: an engine that declares none (EngineSpec.delivered_mesh.prism_layers) has its
#: measured rate scaled by this. Its mesh is not quite the one asked for - the fallback ladder
#: offers such an engine and never swaps a run onto it (pipeline/engine_fallback.py).
NO_LAYERS_VALUE = 0.7

# the shape classes
TUBE = "tube"
BRANCHED = "branched"
CHAMBER = "chamber"
THIN_GAP = "thin gap"
OBSTACLES = "obstacles"
SLENDER = "thin body"
BLUNT = "thick body"
MULTI_REGION = "multi-region"

#: gap_vs_port bands (shared with VMTK's declared turf): a round passage reads ~1, a gap ~0.5.
GAP_THIN_BELOW = 0.6
CHAMBER_ABOVE = 1.4
#: wetted area / (pi * chord^2) under this is short and stubby
STUBBY_BELOW = 3.0
#: handles through the fluid at or above this read as a tube bank
OBSTACLES_FROM = 3
#: smallest cross-flow extent / streamwise extent at or above this is a thick body
BLUNT_FROM = 0.25
#: feature-edge length per sqrt(area) above this is a detailed body
DETAILED_EDGES = 12.0
#: THE CELL-SHAPE LIMIT a tie reads (degrees of non-orthogonality): checkMesh's own "severely
#: non-orthogonal" threshold, past which OpenFOAM solvers need non-orthogonal correctors to stay
#: accurate, and the review flags the mesh. A tie reads the SHARE of an engine's delivered meshes
#: on shapes like this that went past it, in steps of QUALITY_STEP: a few meshes over the line in a
#: long record (cfMesh: 3 of 37 tubes) is no step, a third of them (cfMesh: 10 of 33 smooth bodies
#: from CAD, where snappyHexMesh had none) is.
NON_ORTHO_LIMIT = 70.0
QUALITY_STEP = 0.2
#: A QUALITY FAULT (see quality_fault): more than this share of an engine's delivered meshes past
#: NON_ORTHO_LIMIT, or of its tries broken, on shapes like this - read only once MIN_SHAPES meshes
#: (or tries) back it, so one bad mesh in two is not a record.
FAULT_SHARE = 0.5
#: ...or its delivered meshes carried near-wall layers on under this share of the wall (median).
FAULT_LAYERS_PCT = 50.0
#: An engine without a fault is put ahead of one with a fault only when its own cautious score is at
#: least this: a sound engine that mostly fails is no better a pick than a faulted one that passes.
SOUND_SCORE = 0.5

# how much fine detail an external body carries (its first evidence level)
DETAILED = "detailed"
PLAIN = "plain"

_CLASS_WORDS: dict[str, str] = {
    TUBE: "long passages about as wide as their openings",
    BRANCHED: "branching passages about as wide as their openings",
    CHAMBER: "chambers much wider than their openings",
    THIN_GAP: "thin gaps or annuli",
    OBSTACLES: "passages around many internal obstacles, like a tube bank",
    SLENDER: "thin bodies in external flow",
    BLUNT: "thick bodies in external flow",
    MULTI_REGION: "multi-region fluid and solid assemblies",
}
_ONE_SHAPE_WORDS: dict[str, str] = {
    TUBE: "a long passage about as wide as its openings",
    BRANCHED: "branching passages about as wide as their openings",
    CHAMBER: "a chamber much wider than its openings",
    THIN_GAP: "a thin gap or annulus",
    OBSTACLES: "a passage around many internal obstacles, like a tube bank",
    SLENDER: "a thin body in external flow",
    BLUNT: "a thick body in external flow",
    MULTI_REGION: "a multi-region fluid and solid assembly",
}
_FLOW_WORDS = {"internal": "internal-flow shapes", "external": "external bodies",
               "multi-region": "multi-region assemblies", "structural": "structural parts"}
_FORM_WORDS = {"cad": "from CAD", "surface": "from surface meshes"}


# #
# the shape class, from measured traits
# #

def handles_through_fluid(t: ShapeTraits) -> int | None:
    """Handles of the surface that cross the FLUID: all of them for a fluid-domain solid; for a
    wall around the fluid, those beyond the ones its own openings make (a tee's wall has two)."""
    if t.genus is None:
        return None
    if t.flow == "internal" and t.input_kind not in ("", "fluid-domain"):
        return max(0, t.genus - max((t.ports or 0) - 1, 0))
    return t.genus


def is_detailed(t: ShapeTraits) -> bool:
    return bool(t.n_solids > 1 or t.closed is False
                or (t.sharp_edges is not None and t.sharp_edges > DETAILED_EDGES))


def detail_of(t: ShapeTraits) -> str:
    """DETAILED or PLAIN for a measured external body ('' for any other flow, or a body not
    measured): the first level its evidence is read at."""
    if t.flow != "external" or t.thickness_ratio is None:
        return ""
    return DETAILED if is_detailed(t) else PLAIN


def shape_class(t: ShapeTraits) -> str:
    """The class of a measured geometry ('' when the traits cannot place it)."""
    if t.flow == "multi-region":
        return MULTI_REGION
    if t.flow == "external":
        if t.thickness_ratio is None:
            return ""
        return BLUNT if t.thickness_ratio >= BLUNT_FROM else SLENDER
    if t.flow != "internal":
        return ""
    handles = handles_through_fluid(t)
    if handles is not None and handles >= OBSTACLES_FROM:
        return OBSTACLES
    g, s = t.gap_vs_port, t.slenderness
    if g is None and s is None:
        return ""
    if g is not None and g < GAP_THIN_BELOW:
        return THIN_GAP
    if (g is not None and g > CHAMBER_ABOVE) or (s is not None and s < STUBBY_BELOW):
        return CHAMBER
    return BRANCHED if (t.ports or 0) >= 3 else TUBE


def shape_words(t: ShapeTraits) -> str:
    """The shape in a few plain words, e.g. 'a thin gap or annulus' ('' when unplaced)."""
    c = shape_class(t)
    words = _ONE_SHAPE_WORDS.get(c, "")
    if words and t.flow == "external" and is_detailed(t):
        words += ", with fine detail"
    return words


_DETAIL_WORDS = {DETAILED: "with fine detail", PLAIN: "without fine detail"}


def _levels(flow: str, cls: str, form: str,
            detail: str = "") -> list[tuple[tuple[str, str, str, str], str]]:
    """The evidence levels, most specific first: (key, plain words for 'similar shapes'). A key is
    (flow, class, form, detail); '' matches anything."""
    out = []
    if cls and detail:
        words = f"{_CLASS_WORDS.get(cls, cls)} {_DETAIL_WORDS.get(detail, detail)}"
        if form:
            out.append(((flow, cls, form, detail), f"{words}, {_FORM_WORDS.get(form, form)}"))
        out.append(((flow, cls, "", detail), words))
    if cls and form:
        out.append(((flow, cls, form, ""), f"{_CLASS_WORDS.get(cls, cls)}, {_FORM_WORDS.get(form, form)}"))
    if cls:
        out.append(((flow, cls, "", ""), _CLASS_WORDS.get(cls, cls)))
    if form:
        out.append(((flow, "", form, ""), f"{_FLOW_WORDS.get(flow, flow)} {_FORM_WORDS.get(form, form)}"))
    out.append(((flow, "", "", ""), _FLOW_WORDS.get(flow, flow)))
    return out


# #
# the table
# #

@dataclass(frozen=True)
class Run:
    """One lab run, as the table keeps it."""

    shape: str
    engine: str
    flow: str
    form: str
    cls: str
    passed: bool
    layers_pct: float | None = None
    cells: int | None = None
    seconds: float | None = None
    cells_across: float | None = None
    non_ortho: float | None = None
    detail: str = ""            # DETAILED | PLAIN for an external body
    #: the run's mesh came out INVALID - checkMesh found broken cells (negative volumes, faces
    #: pointing the wrong way): not a mesh that missed a bar, a mesh no solver can use
    invalid: bool = False

    @classmethod
    def from_row(cls, row: Mapping) -> Run | None:
        try:
            traits = ShapeTraits.from_dict(row.get("traits"))
            quality = row.get("quality") or {}
            passed = row.get("status") == "pass"
            return cls(shape=str(row["shape"]), engine=str(row["engine"]),
                       flow=str(traits.flow or row.get("flow") or ""),
                       form=str(traits.form or row.get("form") or ""),
                       cls=shape_class(traits), passed=passed,
                       layers_pct=_num(row.get("layers_pct")), cells=_int(row.get("cells")),
                       seconds=_num(row.get("seconds")),
                       cells_across=_num(row.get("cells_across_p05")),
                       non_ortho=_num(quality.get("max_non_ortho")),
                       detail=detail_of(traits),
                       invalid=not passed and bool(row.get("invalid") or quality.get("fatal")))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


@dataclass(frozen=True)
class Table:
    runs: tuple[Run, ...] = ()
    version: str = ""
    provenance: Mapping = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.runs


def table_from(data: Mapping | None, *, exclude_shapes: Iterable[str] = ()) -> Table:
    """A table from its committed JSON (or any dict of the same shape). `exclude_shapes` drops
    every run on those shapes - how the held-out check builds a table that never saw them."""
    if not isinstance(data, Mapping):
        return Table()
    skip = set(exclude_shapes)
    runs = []
    if int(data.get("traits_version") or 0) not in (0, TRAITS_VERSION):
        logger.warning("fitness table: measured with traits v%s, this code measures v%s - "
                       "rebuild it (devtools/fitness/build_engine_fitness.py)",
                       data.get("traits_version"), TRAITS_VERSION)
    for row in data.get("rows") or ():
        if not isinstance(row, Mapping) or row.get("shape") in skip:
            continue
        r = Run.from_row(row)
        if r is not None:
            runs.append(r)
    return Table(runs=tuple(runs), version=str(data.get("version") or ""),
                 provenance=dict(data.get("provenance") or {}))


@lru_cache(maxsize=1)
def committed_table() -> Table:
    """The committed table, read once. A missing or unreadable file is an empty table: the
    recommendation then says it has no measured evidence, and nothing else changes."""
    try:
        return table_from(json.loads(TABLE_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.warning("fitness table unavailable (%s): engines are ranked without evidence", exc)
        return Table()


# #
# the evidence for one engine
# #

@dataclass(frozen=True)
class Evidence:
    """One engine's lab record for shapes like this one.

    The counts are QUOTED from the most specific level with runs on MIN_SHAPES distinct shapes
    (`level`, `words`); when a more specific level has a few runs but not that many shapes, those
    are kept too (`near_*`) so a reason can say both. The RATE the ranking reads comes from the
    most specific level with any runs, shrunk toward the next broader group: two failures on the
    only two thin gaps seen count, but only as much as two runs can."""

    runs: int = 0
    passed: int = 0
    shapes: int = 0
    level: int = -1             # 0 = the most specific level; -1 = no runs at all
    words: str = ""             # what "similar" meant at that level
    rate: float = 0.5           # the shrunk pass rate
    weight: float = PRIOR_RUNS  # runs behind that rate: the most specific level's, plus the prior
    layers_pct: float | None = None
    cells: int | None = None
    seconds: float | None = None
    cells_across: float | None = None
    non_ortho: float | None = None  # median max non-orthogonality (deg) of the passes
    invalid: int = 0            # runs at the quoted level whose mesh came out invalid
    shaped: int = 0             # passes at the quoted level whose cell shape was measured
    over_limit: int = 0         # ...and of those, how many went past NON_ORTHO_LIMIT
    per_form: bool = False      # the level fixes the file's form: one run per shape
    near_runs: int = 0          # the few runs at a more specific level than `level`
    near_passed: int = 0
    near_shapes: int = 0
    near_words: str = ""

    @property
    def low(self) -> bool:
        return self.level != 0 or self.shapes < MIN_SHAPES

    @property
    def invalid_share(self) -> float:
        return self.invalid / self.runs if self.runs else 0.0

    @property
    def over_limit_share(self) -> float:
        return self.over_limit / self.shaped if self.shaped else 0.0

    def as_dict(self) -> dict:
        d = {"runs": self.runs, "passed": self.passed, "shapes": self.shapes,
             "level": self.level, "similar": self.words, "rate": round(self.rate, 3),
             "layers_pct": None if self.layers_pct is None else round(self.layers_pct, 1),
             "cells": self.cells,
             "seconds": None if self.seconds is None else round(self.seconds, 1),
             "cells_across": None if self.cells_across is None else round(self.cells_across, 1),
             "max_non_ortho": None if self.non_ortho is None else round(self.non_ortho, 1),
             "invalid": self.invalid, "over_limit": self.over_limit, "shaped": self.shaped,
             "low_evidence": self.low}
        if self.near_runs:
            d["near"] = {"runs": self.near_runs, "passed": self.near_passed,
                         "shapes": self.near_shapes, "similar": self.near_words}
        return d


def cautious(ev: Evidence) -> float:
    """The rate less CAUTION standard errors of it: what the ranking reads."""
    p = min(max(ev.rate, 0.0), 1.0)
    return max(0.0, p - CAUTION * math.sqrt(p * (1.0 - p) / max(ev.weight, 1.0)))


def _median(xs: Sequence[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _match(r: Run, key: tuple[str, str, str, str]) -> bool:
    flow, cls, form, detail = key
    return (r.flow == flow and (not cls or r.cls == cls) and (not form or r.form == form)
            and (not detail or r.detail == detail))


def evidence_for(table: Table, engine: str, *, flow: str, cls: str, form: str,
                 detail: str = "") -> Evidence:
    """The engine's evidence for a shape of this class, form and (external) detail (see
    Evidence)."""
    mine = [r for r in table.runs if r.engine == engine]
    stats = [(key, words, [r for r in mine if _match(r, key)])
             for key, words in _levels(flow, cls, form, detail)]
    with_runs = [i for i, st in enumerate(stats) if st[2]]
    if not with_runs:
        return Evidence()
    # THE RATE: the most specific runs, plus PRIOR_RUNS pseudo runs at the (Laplace) rate of the
    # next broader group that holds more runs - each run counted once, never shrunk toward itself
    spec_rows = stats[with_runs[0]][2]
    # AN ENGINE NEVER TRIED ON THIS CLASS is read off its record on other shapes; how it does on
    # this kind is unknown, so that record is halved toward even odds and weighs no more than the
    # pseudo runs
    extrapolated = bool(cls) and not stats[with_runs[0]][0][1]
    weight = PRIOR_RUNS if extrapolated else len(spec_rows) + PRIOR_RUNS
    broader = next((st[2] for st in stats[with_runs[0] + 1:] if len(st[2]) > len(spec_rows)), [])
    prior = (sum(r.passed for r in broader) + 1.0) / (len(broader) + 2.0) if broader else 0.5
    rate = (sum(r.passed for r in spec_rows) + PRIOR_RUNS * prior) / (len(spec_rows) + PRIOR_RUNS)
    if extrapolated:
        # its record elsewhere counts as PRIOR_RUNS pseudo runs, and not knowing how this kind of
        # shape goes as PRIOR_RUNS more at even odds
        rate = (rate + 0.5) / 2.0
    quoted = next((i for i in with_runs if len({r.shape for r in stats[i][2]}) >= MIN_SHAPES),
                  with_runs[-1])
    key, words, rows = stats[quoted]
    ok = [r for r in rows if r.passed]
    near: dict[str, Any] = {}
    if with_runs[0] < quoted:
        _, nwords, nrows = stats[with_runs[0]]
        near = {"near_runs": len(nrows), "near_passed": sum(r.passed for r in nrows),
                "near_shapes": len({r.shape for r in nrows}), "near_words": nwords}
    return Evidence(
        runs=len(rows), passed=len(ok), shapes=len({r.shape for r in rows}), level=quoted,
        words=words, rate=rate, weight=weight,
        layers_pct=_median([r.layers_pct for r in ok if r.layers_pct is not None]),
        cells=_int(_median([r.cells for r in ok if r.cells is not None])),
        seconds=_median([r.seconds for r in ok if r.seconds is not None]),
        cells_across=_median([r.cells_across for r in ok if r.cells_across is not None]),
        non_ortho=_median([r.non_ortho for r in ok if r.non_ortho is not None]),
        invalid=sum(r.invalid for r in rows),
        shaped=sum(r.non_ortho is not None for r in ok),
        over_limit=sum(r.non_ortho is not None and r.non_ortho > NON_ORTHO_LIMIT for r in ok),
        per_form=bool(key[2]), **near)


# #
# the facts declared turf rows test (engines/home_turf.TURF_FACTS names every one)
# #

#: The cell budget assumed when the brief names none: the planner's usual ceiling.
DEFAULT_CELL_BUDGET = 2_000_000


def facts_of(traits: ShapeTraits, brief: Mapping | None = None) -> dict:
    """The facts an engine's home-turf rows test: the measured traits, the shape class, the cells
    a uniform mesh of the budget would put across the narrowest passage, and the brief's own facts
    (layers_requested, ground, cell_budget, region_count) when known. Unknown facts are left out,
    so a row about them says nothing."""
    d = {k: v for k, v in traits.as_dict().items() if v is not None and k != "notes"}
    d["shape_class"] = shape_class(traits)
    handles = handles_through_fluid(traits)
    if handles is not None:
        d["handles"] = handles
    extra = {k: v for k, v in dict(brief or {}).items() if v is not None}
    budget = extra.get("cell_budget") or DEFAULT_CELL_BUDGET
    if traits.narrow_vs_volume:
        try:
            d["cells_across_at_budget"] = round(traits.narrow_vs_volume * float(budget) ** (1.0 / 3.0), 2)
        except (TypeError, ValueError):
            pass
    d.update(extra)
    return d


def brief_facts(*, request_txt: str = "", review_brief_txt: str = "", patches=None,
                cell_budget=None, region_count=None) -> dict:
    """The turf facts a brief carries: whether it asks for near-wall layers (the fallback ladder's
    own reading), whether an external body stands on a declared ground wall, the cell budget and
    the region count. Only what can be read is returned."""
    out: dict = {}
    if (request_txt or "").strip() or (review_brief_txt or "").strip():
        from meshpipeline.pipeline.engine_fallback import layer_request
        out["layers_requested"] = bool(layer_request(request_txt or "", review_brief_txt or "").requested)
    if patches:
        try:
            from meshpipeline.engines.ground_plane import ground_patch_name
            out["ground"] = ground_patch_name(list(patches)) is not None
        except Exception:  # noqa: BLE001 - a fact we cannot read says nothing
            pass
    if isinstance(cell_budget, (int, float)) and not isinstance(cell_budget, bool) and cell_budget > 0:
        out["cell_budget"] = float(cell_budget)
    if isinstance(region_count, int) and not isinstance(region_count, bool):
        out["region_count"] = region_count
    return out


# #
# the recommendation
# #

@dataclass(frozen=True)
class EngineFit:
    engine: str
    label: str
    rank: int
    score: float
    evidence: Evidence
    reason: str
    recommended: bool = False
    turf: str = ""                            # the declared verdict: home | weak | outside | ''
    turf_words: tuple[str, ...] = ()
    layers: bool = True
    heads_up: str = ""                          # the declared weak spot (or limit) that holds here

    @property
    def outside(self) -> bool:
        return self.turf == "outside"

    def as_dict(self) -> dict:
        return {"engine": self.engine, "label": self.label, "rank": self.rank,
                "score": round(self.score, 3), "recommended": self.recommended,
                "outside": self.outside, "turf": self.turf, "turf_words": list(self.turf_words),
                "layers": self.layers, "reason": self.reason, "heads_up": self.heads_up,
                "fit": fit_words(self), "evidence": self.evidence.as_dict()}


@dataclass(frozen=True)
class Recommendation:
    engine: str
    reason: str
    fits: tuple[EngineFit, ...]
    shape: str
    traits: Mapping = field(default_factory=dict)
    table_version: str = ""

    @property
    def ranking(self) -> list[str]:
        return [f.engine for f in self.fits]

    def fit_of(self, engine: str) -> EngineFit | None:
        return next((f for f in self.fits if f.engine == engine), None)

    def as_dict(self) -> dict:
        return {"engine": self.engine, "reason": self.reason, "shape": self.shape,
                "fits": [f.as_dict() for f in self.fits], "traits": dict(self.traits),
                "table_version": self.table_version}


def ranking_of(data: Mapping | None) -> list[str]:
    """The ranking kept in a recommendation's dict (as a run's state carries it)."""
    if not isinstance(data, Mapping):
        return []
    return [str(f.get("engine")) for f in (data.get("fits") or ())
            if isinstance(f, Mapping) and f.get("engine")]


def fit_words(fit: EngineFit) -> str:
    """A couple of words for the fit, as the console shows them beside the engine."""
    if fit.outside:
        return "not suited"
    if fit.recommended:
        return "best fit"
    if fit.evidence.level < 0:
        return "untested"
    if quality_fault(fit.evidence):
        return "quality concerns"
    return "good fit" if fit.score >= 0.75 else "fair fit" if fit.score >= 0.45 else "poor fit"


def _label(engine: str) -> str:
    try:
        from meshpipeline.engines.registry import engine_label
        return engine_label(engine) or engine
    except Exception:  # noqa: BLE001 - a label never breaks advice
        return engine


def _spec(engine: str):
    try:
        from meshpipeline.engines.registry import get_spec
        return get_spec(engine)
    except Exception:  # noqa: BLE001
        return None


def _delivers_layers(spec) -> bool:
    dm = getattr(spec, "delivered_mesh", None)
    return bool(dm is None or dm.prism_layers)


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 90:
        return "about a minute a mesh"
    return f"about {int(round(seconds / 60.0))} minutes a mesh"


def _counted(passed: int, runs: int, shapes: int, words: str, *, per_form: bool) -> str:
    if per_form and runs == shapes:
        return f"passed {passed} of {shapes} similar shapes ({words})"
    return f"passed {passed} of {runs} tries on {shapes} similar shape{'s' if shapes != 1 else ''} ({words})"


def evidence_words(ev: Evidence) -> str:
    """The counts behind a fit, plainly: 'passed 9 of 10 similar shapes (...)', or what little
    there is on shapes exactly like this and the broader record the rest comes from."""
    if ev.level < 0:
        return "no lab results yet for shapes like this"
    broad = _counted(ev.passed, ev.runs, ev.shapes, ev.words, per_form=ev.per_form)
    if not ev.low:
        return broad
    if ev.near_runs:
        near = (f"passed {ev.near_passed} of {ev.near_runs} tries on {ev.near_shapes} "
                f"shape{'s' if ev.near_shapes != 1 else ''} like this ({ev.near_words})")
        return f"little evidence on shapes like this: {near}; {broad}"
    return f"no lab results on shapes exactly like this; {broad}"


def _reason(ev: Evidence, *, layers: bool) -> str:
    """One plain line: the evidence, what the meshes were like, the layers and the time."""
    parts = [evidence_words(ev)]
    if ev.invalid:
        parts.append(f"{ev.invalid} of those {ev.runs} tries made a broken mesh a solver cannot use")
    if quality_step(ev):
        parts.append(f"{ev.over_limit} of its {ev.shaped} meshes had cells more than "
                     f"{NON_ORTHO_LIMIT:.0f} degrees out of square, past the usual limit")
    if not layers:
        parts.append("no reliable near-wall layers")
    elif ev.layers_pct is not None and ev.passed:
        parts.append(f"near-wall layers on {ev.layers_pct:.0f}% of the wall")
    took = _minutes(ev.seconds)
    if took and ev.passed:
        parts.append(took)
    return "; ".join(p for p in parts if p)


def _heads_up(turf: str, words: Sequence[str]) -> str:
    """The engine's own declared words when its turf marks this case weak or outside."""
    if turf not in ("weak", "outside") or not words:
        return ""
    return str(words[0]).strip()


#: Within a tie on measured rate, a declared strength ranks first and a weak spot last.
_TURF_ORDER = {"home": 0, "": 1, "weak": 2, "outside": 3}


def validity_step(ev: Evidence) -> int:
    """The share of an engine's runs on shapes like this whose mesh came out INVALID, in TIE_STEP
    steps (0 when none did). A tie reads it before cell shape, layers and time."""
    return int(math.floor(ev.invalid_share / TIE_STEP + 1e-9)) + 1 if ev.invalid else 0


def quality_step(ev: Evidence) -> int:
    """The share of the engine's delivered meshes on shapes like this whose cells went past the
    cell-shape limit (NON_ORTHO_LIMIT), in QUALITY_STEP steps: 0 when few or none did, or when
    none was measured."""
    return int(math.floor(ev.over_limit_share / QUALITY_STEP + 1e-9)) if ev.shaped else 0


def quality_fault(ev: Evidence) -> str:
    """What makes an engine's record on shapes like this a QUALITY FAULT, in plain words, or "":
    most of its tries made a broken mesh, most of its delivered meshes went past the cell-shape
    limit, or its delivered meshes had near-wall layers on under half the wall. Each is read only
    when MIN_SHAPES runs back it. A fault outweighs the pass rate in the ranking (recommend)."""
    if ev.runs >= MIN_SHAPES and ev.invalid_share > FAULT_SHARE:
        return f"{ev.invalid} of its {ev.runs} tries made a broken mesh"
    if ev.shaped >= MIN_SHAPES and ev.over_limit_share > FAULT_SHARE:
        return (f"{ev.over_limit} of its {ev.shaped} meshes had cells more than "
                f"{NON_ORTHO_LIMIT:.0f} degrees out of square")
    if ev.passed >= MIN_SHAPES and ev.layers_pct is not None and ev.layers_pct < FAULT_LAYERS_PCT:
        return f"its meshes had near-wall layers on only {ev.layers_pct:.0f}% of the wall"
    return ""


def reads_own_format(engine: str, source_format: str) -> bool:
    """Whether `engine` reads files of `source_format` ITSELF (EngineSpec.reads_source_formats):
    it meshes the model the file describes, not a conversion of it."""
    spec = _spec(engine)
    return bool(source_format) and spec is not None and source_format in spec.reads_source_formats


def recommend(traits: ShapeTraits, engines: Sequence[str], *, table: Table | None = None,
              brief: Mapping | None = None, source_format: str = "") -> Recommendation:
    """Rank `engines` - those that CAN take this file for this flow - for this geometry. The best
    measured engine that its own declared turf does not put OUTSIDE is recommended; every engine
    keeps its place on the list, with its reason, for the user to pick.

    `source_format` is the upload's own format. An engine that reads that format ITSELF ranks
    ahead of engines that would mesh a conversion of it, measured or not: the conversion is what
    loses the model (an ECXML's parts, materials and powers), and lab runs of other engines on
    the converted geometry say nothing about that. Declared by the engine, not named here.

    SEPARATE PASSAGES: when the openings lie on several passages that do not connect
    (traits.passages), only the engines whose declaration meshes that many in one job are ranked
    (capability.covers_passages) - an engine that will certainly refuse the case is no choice to
    offer. When none does, every engine stays, and the run's own refusal says what to do."""
    from meshpipeline.engines.capability import engines_covering
    from meshpipeline.engines.home_turf import rows_for, verdict

    engines = engines_covering(dict.fromkeys(engines), traits.flow, traits.passages) or list(engines)
    tab = committed_table() if table is None else table
    cls = shape_class(traits)
    detail = detail_of(traits)
    facts = facts_of(traits, brief)
    wants_layers = bool(facts.get("layers_requested"))
    scored = []
    for name in dict.fromkeys(engines):
        ev = evidence_for(tab, name, flow=traits.flow, cls=cls, form=traits.form, detail=detail)
        spec = _spec(name)
        turf: str = ""
        words: tuple[str, ...] = ()
        if spec is not None and rows_for(spec, traits.flow):
            v = verdict(spec, traits.flow, facts)
            held = v.outside or v.weak_spots or v.strengths
            turf = v.severity if held else ""
            words = tuple(r.words for r in held)
        layers = _delivers_layers(spec)
        score = cautious(ev) * (NO_LAYERS_VALUE if wants_layers and not layers else 1.0)
        ladder = getattr(spec, "ladder_rank", 100) if spec is not None else 100
        scored.append((name, ev, turf, words, layers, score, ladder))

    def key(item):
        # an engine with a measured record and no QUALITY FAULT ranks ahead of one with a fault
        # (or with no record at all), whatever their pass rates: most meshes past the cell-shape
        # limit is not a pass a solver can use. Then, within a tie on the measured rate, what the
        # meshes were like - how many came out broken, how many of the delivered ones went past
        # the cell-shape limit - and only then the declared turf, the layers and the time.
        # Measured before declared: cfMesh declares thick bodies home and made broken meshes on
        # 7 of 8 DrivAer cars.
        name, ev, turf, _, _, score, ladder = item
        sound = ev.level >= 0 and score >= SOUND_SCORE and not quality_fault(ev)
        return (turf == "outside", not reads_own_format(name, source_format), not sound,
                -math.floor(score / TIE_STEP + 1e-9), validity_step(ev), quality_step(ev),
                _TURF_ORDER.get(turf, 1),
                -(ev.layers_pct if ev.layers_pct is not None else -1.0),
                ev.seconds if ev.seconds is not None else math.inf, ladder, name)

    scored.sort(key=key)
    best = next((s[0] for s in scored if s[2] != "outside"), "")
    fits = tuple(EngineFit(engine=name, label=_label(name), rank=rank, score=score, evidence=ev,
                           reason=_reason(ev, layers=layers), recommended=name == best, turf=turf,
                           turf_words=words, layers=layers, heads_up=_heads_up(turf, words))
                 for rank, (name, ev, turf, words, layers, score, _) in enumerate(scored, start=1))
    top = next((f for f in fits if f.recommended), None)
    reason = top.reason if top is not None else ""
    if top is not None:
        # SAY WHY an engine that passed more tries is not the one recommended: its own fault
        passed_over = [f for f in fits if f is not top and not f.outside
                       and f.score > top.score and quality_fault(f.evidence)]
        if passed_over:
            reason += "; " + "; ".join(
                f"recommended over {f.label}, which passes more often, because "
                f"{quality_fault(f.evidence)}" for f in passed_over)
            fits = tuple(replace(f, reason=reason) if f is top else f
                         for f in fits)
    return Recommendation(engine=best, reason=reason, fits=fits, shape=shape_words(traits),
                          traits=traits.as_dict(), table_version=tab.version)


def ranked_engines(order: Sequence[str], ranking: Sequence[str]) -> list[str]:
    """`order` (the declared ladder) re-ordered by a recommendation's ranking: ranked engines first,
    in ranking order, then the rest in their declared order. Only engines in `order` appear."""
    pos = {e: i for i, e in enumerate(ranking)}
    known = [e for e in order if e in pos]
    rest = [e for e in order if e not in pos]
    return sorted(known, key=lambda e: pos[e]) + rest


__all__ = ["BLUNT", "BRANCHED", "CAUTION", "CHAMBER", "DEFAULT_CELL_BUDGET", "DETAILED", "MIN_SHAPES",
           "MULTI_REGION", "NON_ORTHO_LIMIT", "NO_LAYERS_VALUE", "OBSTACLES", "PLAIN", "QUALITY_STEP",
           "SLENDER", "TABLE_PATH", "THIN_GAP", "TUBE", "EngineFit", "Evidence", "Recommendation", "Run",
           "Table", "brief_facts", "cautious", "committed_table", "detail_of", "evidence_for",
           "evidence_words", "facts_of", "fit_words", "handles_through_fluid", "is_detailed",
           "quality_fault", "quality_step", "ranked_engines", "ranking_of", "recommend", "shape_class", "shape_words",
           "table_from", "validity_step"]
