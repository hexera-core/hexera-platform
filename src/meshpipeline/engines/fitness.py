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

THE RULES (ENG-1..5; the user's own pick always wins and is never switched - ENG-6, enforced where a
run is moved, pipeline/engine_fallback.py):

ENG-1  CAN IT? Only engines the capability filter lets take this file and flow are ranked; an
       engine its own declared turf puts OUTSIDE stays on the list, last, never recommended.
ENG-2  HOME TURF FIRST. An engine that declares this shape class its home (EngineSpec.home_classes:
       VMTK for tubes and branching passages) goes first, unless its own turf rows mark this case
       a weak spot. Two home engines rank between themselves by ENG-4.
ENG-3  THE RECORD OVERRIDES HOME ONLY WHEN IT CLEARLY WINS: a challenger with at least MIN_RUNS
       runs on at least MIN_OVERRIDE_SHAPES different shapes of this class, no quality fault, and
       a 95% LOWER bound above the home engine's 95% UPPER bound. (Or the home engine's own record
       of MIN_RUNS runs here shows a quality fault.) A home engine never tried on this class can
       never be overridden there: "home turf unproven", and the lab must add cases.
ENG-4  EVERYONE ELSE ranks by the 95% LOWER bound of their pass rate on this shape class (Wilson's
       interval: the worst rate the runs still allow, 95% sure), with an engine that has fewer than
       MIN_RUNS runs here counted as if each missing run had failed - so 7 passes of 8 never
       outrank 14 of 19, and 3 of 3 never outrank 30 of 32. An engine with a QUALITY FAULT ranks
       after the sound ones. Bounds within TIE_STEP tie; then what the meshes were like: the
       share of runs whose mesh came out INVALID (broken cells), the share of delivered meshes
       past the 70-degree non-orthogonality limit (in fifths), the declared turf, measured layer
       coverage, and last the declared ladder_rank. Time never breaks a tie.
ENG-5  THE FALLBACK LADDER is this same ranked list (pipeline/engine_fallback.fallback_order),
       minus engines tried and engines that would refuse the case.

The record a rank reads is the engine's runs on THIS SHAPE CLASS, from any kind of file (CAD or
triangles): the same slice of the record for every engine on the list. An EXTERNAL body's class
includes how much fine detail it has (detailed or plain): a car with mirrors, wheels and 44
handles is not a smooth Ahmed body, and an engine's record on smooth bodies says little about it
(cfMesh passed 23 of 23 smooth thick bodies from surface meshes in the lab, then made broken
meshes on 7 of 8 DrivAer cars - e2e sweep, 2026-10-17). A record on OTHER shape classes is quoted
in the reason when there is nothing closer, but never ranks: how an engine does on a kind of shape
it was never tried on is unknown. Every number in a reason is a count of real runs; thin evidence
says so.

A QUALITY FAULT outweighs the pass rate, not only a tie. An engine whose own record on shapes like
this shows MOST of its delivered meshes past the cell-shape limit, MOST of its tries broken, or
near-wall layers on under half the wall, is not recommended over an engine with a measured record
and no such fault: "passed" there means the mesh cleared that engine's bars, not that it is the
mesh a solver wants. (cfMesh passed 38 of 38 thin bodies from CAD with 24 of the 38 past the
limit; snappyHexMesh passed 6 of 6 with none - the rate alone recommended cfMesh.) Both stay on
the list with their reasons, and the user still picks.

Engines also DECLARE where they are at home (EngineSpec.home_turf, rows of a fact, a test, a
severity and words). Those rows give the plain "why"; an OUTSIDE row keeps an engine from being
recommended - it stays on the list, marked, for the user to pick if they want it. Only a declared
home CLASS (EngineSpec.home_classes, read off the measured traits like every class) moves an
engine to the front, and only the measured record can take that place away (ENG-3).

THE TABLE is the committed fitness_table.json: dev-side runs of the frozen split only, runs where
a mesher ran, no hand relabels, versioned (devtools/fitness/build_engine_fitness.py,
docs/engines/fitness-changelog.md). No setting swaps it at run time.
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

#: An evidence level is QUOTED once it holds runs on at least this many distinct shapes.
MIN_SHAPES = 3
#: THE RUNS A RANK TRUSTS on a shape class (ENG-3, ENG-4): an engine with fewer is ranked as if each
#: missing run had failed, and never overrides a home engine.
MIN_RUNS = 20
#: ...and the distinct shapes of the class those runs must cover before they override home (ENG-3).
MIN_OVERRIDE_SHAPES = 8
#: The normal quantile of a two-sided 95% interval (Wilson's score interval).
Z95 = 1.959964
#: Lower bounds closer than this rank as a tie (then what the meshes were like decides).
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
#: An engine without a fault is put ahead of one with a fault only when the 95% lower bound of its
#: own record on this class is at least this: a sound engine that mostly fails is no better a pick
#: than a faulted one that passes.
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
    are kept too (`near_*`) so a reason can say both. The RANK reads `ranked_*`: the engine's runs
    on this shape class (see class_levels) - never its record on other classes."""

    runs: int = 0
    passed: int = 0
    shapes: int = 0
    level: int = -1             # 0 = the most specific level; -1 = no runs at all
    words: str = ""             # what "similar" meant at that level
    ranked_runs: int = 0        # the runs on this shape class the rank reads
    ranked_passed: int = 0
    ranked_shapes: int = 0
    ranked_words: str = ""
    layers_pct: float | None = None
    cells: int | None = None
    seconds: float | None = None
    cells_across: float | None = None
    non_ortho: float | None = None  # median max non-orthogonality (deg) of the passes
    invalid: int = 0            # runs at the quoted level whose mesh came out invalid
    shaped: int = 0             # passes at the quoted level whose cell shape was measured
    over_limit: int = 0         # ...and of those, how many went past NON_ORTHO_LIMIT
    layered: int = 0            # passes at the quoted level whose layer coverage was measured
    per_form: bool = False      # the level fixes the file's form: one run per shape
    near_runs: int = 0          # the few runs at a more specific level than `level`
    near_passed: int = 0
    near_shapes: int = 0
    near_words: str = ""

    @property
    def low(self) -> bool:
        return self.level != 0 or self.shapes < MIN_SHAPES

    @property
    def proven(self) -> bool:
        """At least MIN_RUNS runs on this shape class: enough for the rank to trust the record."""
        return self.ranked_runs >= MIN_RUNS

    @property
    def lower(self) -> float:
        """THE RANK (ENG-4): the 95% lower bound of the pass rate on this class, with each run short
        of MIN_RUNS counted as a failure."""
        return wilson_lower(self.ranked_passed, max(self.ranked_runs, MIN_RUNS))

    @property
    def upper(self) -> float:
        """The 95% upper bound of the pass rate on this class (1.0 with no runs): the best rate the
        record still allows - what a challenger must beat to override a home engine (ENG-3)."""
        return wilson_upper(self.ranked_passed, self.ranked_runs)

    @property
    def invalid_share(self) -> float:
        return self.invalid / self.runs if self.runs else 0.0

    @property
    def over_limit_share(self) -> float:
        return self.over_limit / self.shaped if self.shaped else 0.0

    def as_dict(self) -> dict:
        d = {"runs": self.runs, "passed": self.passed, "shapes": self.shapes,
             "level": self.level, "similar": self.words,
             "ranked": {"runs": self.ranked_runs, "passed": self.ranked_passed,
                        "shapes": self.ranked_shapes, "similar": self.ranked_words,
                        "lower_bound": round(self.lower, 3), "upper_bound": round(self.upper, 3),
                        "proven": self.proven},
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


def _wilson(passed: int, runs: int) -> tuple[float, float]:
    if runs <= 0:
        return 0.0, 1.0
    p = min(max(passed / runs, 0.0), 1.0)
    z2 = Z95 * Z95
    centre = (p + z2 / (2.0 * runs)) / (1.0 + z2 / runs)
    half = Z95 * math.sqrt(p * (1.0 - p) / runs + z2 / (4.0 * runs * runs)) / (1.0 + z2 / runs)
    return max(0.0, centre - half), min(1.0, centre + half)


def wilson_lower(passed: int, runs: int) -> float:
    """The worst pass rate `passed` of `runs` still allows, 95% sure (Wilson). 0 with no runs."""
    return _wilson(passed, runs)[0]


def wilson_upper(passed: int, runs: int) -> float:
    """The best pass rate `passed` of `runs` still allows, 95% sure (Wilson). 1 with no runs."""
    return _wilson(passed, runs)[1]


def class_levels(levels: Sequence[tuple[tuple[str, str, str, str], str]], cls: str,
                 detail: str) -> list[int]:
    """Which of `levels` (indices, most specific first) describe THIS shape class: the same class
    (and, for an external body, the same detail) from the same kind of file or from any file. A
    shape no class places reads its flow's levels."""
    return [i for i, ((_, c, _, d), _) in enumerate(levels)
            if c == cls and (d == detail or not cls)]


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
    levels = _levels(flow, cls, form, detail)
    stats = [(key, words, [r for r in mine if _match(r, key)]) for key, words in levels]
    with_runs = [i for i, st in enumerate(stats) if st[2]]
    if not with_runs:
        return Evidence()
    # THE RECORD THE RANK READS: this shape class from any kind of file - the same record for every
    # engine on the list, so no engine is judged on a narrower slice than another - and never
    # another class's record
    own = class_levels(levels, cls, detail)
    ranked_at = own[-1] if own else -1
    ranked_rows = stats[ranked_at][2] if ranked_at >= 0 else []
    ranked_words = stats[ranked_at][1] if ranked_at >= 0 else ""
    # THE RECORD QUOTED - and read for what the meshes were like (broken, out of square, layers) -
    # is the ranked one whenever the engine has runs on this class, so a reason and a quality
    # fault always speak of the runs the rank read; else the closest record there is
    # (named by the most specific of this class's levels that holds those very same runs)
    quoted = next(i for i in own if len(stats[i][2]) == len(ranked_rows)) if ranked_rows else next(
        (i for i in with_runs if len({r.shape for r in stats[i][2]}) >= MIN_SHAPES), with_runs[-1])
    key, words, rows = stats[quoted]
    ok = [r for r in rows if r.passed]
    near: dict[str, Any] = {}
    if with_runs[0] < quoted:
        _, nwords, nrows = stats[with_runs[0]]
        near = {"near_runs": len(nrows), "near_passed": sum(r.passed for r in nrows),
                "near_shapes": len({r.shape for r in nrows}), "near_words": nwords}
    return Evidence(
        runs=len(rows), passed=len(ok), shapes=len({r.shape for r in rows}), level=quoted,
        words=words, ranked_runs=len(ranked_rows), ranked_passed=sum(r.passed for r in ranked_rows),
        ranked_shapes=len({r.shape for r in ranked_rows}), ranked_words=ranked_words,
        layers_pct=_median([r.layers_pct for r in ok if r.layers_pct is not None]),
        cells=_int(_median([r.cells for r in ok if r.cells is not None])),
        seconds=_median([r.seconds for r in ok if r.seconds is not None]),
        cells_across=_median([r.cells_across for r in ok if r.cells_across is not None]),
        non_ortho=_median([r.non_ortho for r in ok if r.non_ortho is not None]),
        invalid=sum(r.invalid for r in rows),
        shaped=sum(r.non_ortho is not None for r in ok),
        over_limit=sum(r.non_ortho is not None and r.non_ortho > NON_ORTHO_LIMIT for r in ok),
        layered=sum(r.layers_pct is not None for r in ok),
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
    rule: str = ""                              # the rule that placed it (RULE_WORDS)
    home: bool = False                          # its declared home class, and first for it (ENG-2)

    @property
    def outside(self) -> bool:
        return self.turf == "outside"

    def as_dict(self) -> dict:
        return {"engine": self.engine, "label": self.label, "rank": self.rank,
                "score": round(self.score, 3), "recommended": self.recommended,
                "outside": self.outside, "turf": self.turf, "turf_words": list(self.turf_words),
                "layers": self.layers, "reason": self.reason, "heads_up": self.heads_up,
                "rule": self.rule, "home": self.home,
                "fit": fit_words(self), "evidence": self.evidence.as_dict()}


#: The rules that place an engine on the list (module doc), as a record names them.
RULE_WORDS = {
    "ENG-1": "its own turf rules this case out",
    "ENG-2": "the home engine for this shape",
    "ENG-3": "its record here clearly beats the home engine's",
    "ENG-4": "ranked by its measured record on shapes like this",
}


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
    if not fit.evidence.proven:
        return "little evidence"
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


def bound_words(ev: Evidence) -> str:
    """What the rank reads, plainly: the worst pass rate the record still allows, or that the
    record is too short to trust ('' when the engine has no runs on this class)."""
    if not ev.ranked_runs:
        return ""
    if ev.proven:
        return (f"from {ev.ranked_passed} of {ev.ranked_runs} on {ev.ranked_words} its pass rate "
                f"is at least {100 * wilson_lower(ev.ranked_passed, ev.ranked_runs):.0f}%, 95% sure")
    return (f"only {ev.ranked_runs} run{'s' if ev.ranked_runs != 1 else ''} on {ev.ranked_words}, "
            f"fewer than the {MIN_RUNS} the ranking trusts")


def _reason(ev: Evidence, *, layers: bool) -> str:
    """One plain line: the evidence, how sure the rank is of it, what the meshes were like, the
    layers and the time."""
    parts = [evidence_words(ev), bound_words(ev)]
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
    if ev.layered >= MIN_SHAPES and ev.layers_pct is not None and ev.layers_pct < FAULT_LAYERS_PCT:
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
    offer. When none does, every engine stays, and the run's own refusal says what to do.

    The order follows the rules in the module doc (ENG-1..4); each fit names the rule that placed
    it."""
    from meshpipeline.engines.capability import engines_covering
    from meshpipeline.engines.home_turf import rows_for, verdict

    engines = engines_covering(dict.fromkeys(engines), traits.flow, traits.passages) or list(engines)
    tab = committed_table() if table is None else table
    cls = shape_class(traits)
    detail = detail_of(traits)
    facts = facts_of(traits, brief)
    wants_layers = bool(facts.get("layers_requested"))
    scored: list[_Scored] = []
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
        score = ev.lower * (NO_LAYERS_VALUE if wants_layers and not layers else 1.0)
        ladder = getattr(spec, "ladder_rank", 100) if spec is not None else 100
        # ENG-2: a declared home class, unless the engine's own turf marks this case weak or outside
        home = bool(cls) and cls in tuple(getattr(spec, "home_classes", ()) or ()) \
            and turf not in ("weak", "outside")
        scored.append(_Scored(name, ev, turf, words, layers, score, ladder, home))

    # ENG-3: a home engine keeps first place unless the measured record clearly says otherwise
    overrides: dict[str, str] = {}
    for h in [s for s in scored if s.home]:
        beater = next((c for c in sorted(scored, key=_eng4_key(source_format))
                       if not c.home and c.turf != "outside" and _clearly_beats(c.ev, h.ev)), None)
        if beater is not None:
            overrides[h.name] = beater.name
        elif h.ev.proven and quality_fault(h.ev):
            overrides[h.name] = ""
    for s in scored:
        s.keeps_home = s.home and s.name not in overrides

    scored.sort(key=_eng4_key(source_format))
    best = next((s.name for s in scored if s.turf != "outside"), "")
    beaters = {b for b in overrides.values() if b}

    def rule(s: _Scored) -> str:
        if s.turf == "outside":
            return "ENG-1"
        if s.keeps_home:
            return "ENG-2"
        return "ENG-3" if s.name in beaters else "ENG-4"

    fits = tuple(EngineFit(engine=s.name, label=_label(s.name), rank=rank, score=s.score,
                           evidence=s.ev, reason=_reason(s.ev, layers=s.layers),
                           recommended=s.name == best, turf=s.turf, turf_words=s.words,
                           layers=s.layers, heads_up=_heads_up(s.turf, s.words), rule=rule(s),
                           home=s.keeps_home)
                 for rank, s in enumerate(scored, start=1))
    top = next((f for f in fits if f.recommended), None)
    reason = top.reason if top is not None else ""
    if top is not None:
        notes = []
        if top.home:
            # SAY WHY the home engine leads engines whose records read higher: it is home, and
            # their records do not clearly beat its own (ENG-3)
            notes += [f"first as the home engine for {_CLASS_WORDS.get(cls, cls)}"
                      + (f" ({top.turf_words[0].rstrip('.')})" if top.turf_words else "")
                      + ("; no lab results for it on this kind of shape yet, so its home turf is "
                         "unproven here" if not top.evidence.ranked_runs else "")]
            ahead = [f for f in fits if f is not top and not f.outside and f.score > top.score]
            if ahead:
                notes.append("; ".join(
                    f"{f.label}'s record here does not clearly beat it "
                    f"({_short(f.evidence)} against {_short(top.evidence)})" for f in ahead))
        # SAY WHY an engine that passed more tries is not the one recommended: its own fault
        passed_over = [f for f in fits if f is not top and not f.outside and not f.home
                       and f.score > top.score and quality_fault(f.evidence)]
        notes += [f"recommended over {f.label}, whose record scores higher on passes, because "
                  f"{quality_fault(f.evidence)}" for f in passed_over]
        if top.rule == "ENG-3":
            notes += [f"recommended over the home engine {_label(h)}: {_short(top.evidence)} "
                      f"clearly beats its {_short(next(s.ev for s in scored if s.name == h))}"
                      for h, b in overrides.items() if b == top.engine]
        if notes:
            reason += "; " + "; ".join(notes)
            fits = tuple(replace(f, reason=reason) if f is top else f for f in fits)
    return Recommendation(engine=best, reason=reason, fits=fits, shape=shape_words(traits),
                          traits=traits.as_dict(), table_version=tab.version)


@dataclass
class _Scored:
    name: str
    ev: Evidence
    turf: str
    words: tuple[str, ...]
    layers: bool
    score: float
    ladder: int
    home: bool
    keeps_home: bool = False


def _sound(s: _Scored) -> bool:
    """A record on this class with no quality fault whose own 95% lower bound (times the worth of
    a mesh without layers, when the brief asks for them) is at least SOUND_SCORE: an engine that
    mostly fails, or was never tried here, is not 'sound'."""
    ev = s.ev
    worth = s.score / ev.lower if ev.lower > 0 else 1.0
    return (ev.ranked_runs > 0 and not quality_fault(ev)
            and wilson_lower(ev.ranked_passed, ev.ranked_runs) * worth >= SOUND_SCORE)


def _clearly_beats(challenger: Evidence, home: Evidence) -> bool:
    """ENG-3: MIN_RUNS runs on MIN_OVERRIDE_SHAPES shapes of this class, no quality fault, and a 95%
    lower bound above the home engine's 95% upper bound."""
    return (challenger.proven and challenger.ranked_shapes >= MIN_OVERRIDE_SHAPES
            and not quality_fault(challenger) and challenger.lower > home.upper)


def _short(ev: Evidence) -> str:
    if not ev.ranked_runs:
        return "no runs"
    return f"{ev.ranked_passed} of {ev.ranked_runs}"


def _eng4_key(source_format: str):
    def key(s: _Scored):
        # outside last (ENG-1); an engine that reads the upload's own format first; a home engine
        # that keeps its place (ENG-2/3); then ENG-4: a sound record before a QUALITY FAULT
        # (most meshes past the cell-shape limit is not a pass a solver can use), the lower bound,
        # and within a tie what the meshes were like - how many came out broken, how many of the
        # delivered ones went past the cell-shape limit - then the declared turf, the layers and
        # the declared ladder order. Measured before declared: cfMesh declares thick bodies home
        # and made broken meshes on 7 of 8 DrivAer cars. Time never breaks a tie.
        ev = s.ev
        return (s.turf == "outside", not reads_own_format(s.name, source_format), not s.keeps_home,
                not _sound(s), -math.floor(s.score / TIE_STEP + 1e-9), validity_step(ev),
                quality_step(ev), _TURF_ORDER.get(s.turf, 1),
                -(ev.layers_pct if ev.layers_pct is not None else -1.0), s.ladder, s.name)
    return key


def ranked_engines(order: Sequence[str], ranking: Sequence[str]) -> list[str]:
    """`order` (the declared ladder) re-ordered by a recommendation's ranking: ranked engines first,
    in ranking order, then the rest in their declared order. Only engines in `order` appear."""
    pos = {e: i for i, e in enumerate(ranking)}
    known = [e for e in order if e in pos]
    rest = [e for e in order if e not in pos]
    return sorted(known, key=lambda e: pos[e]) + rest


def flow_ranking(flow: str) -> tuple[str, ...]:
    """ENG-5 WHEN NOTHING WAS MEASURED: every engine designed for the flow, ranked by the same rules
    on its record over that whole flow (no shape class, so no home engine). The fallback ladder
    walks this rather than a fixed order when a run's geometry could not be measured in time."""
    from meshpipeline.engines.capability import ladder_order
    order = ladder_order(flow)
    if not order:
        return ()
    return tuple(ranked_engines(order, recommend(ShapeTraits(flow=flow), order).ranking))


__all__ = ["BLUNT", "BRANCHED", "CHAMBER", "DEFAULT_CELL_BUDGET", "DETAILED", "MIN_OVERRIDE_SHAPES",
           "MIN_RUNS", "MIN_SHAPES", "MULTI_REGION", "NON_ORTHO_LIMIT", "NO_LAYERS_VALUE", "OBSTACLES",
           "PLAIN", "QUALITY_STEP", "RULE_WORDS", "SLENDER", "TABLE_PATH", "THIN_GAP", "TUBE",
           "EngineFit", "Evidence", "Recommendation", "Run", "Table", "bound_words", "brief_facts",
           "class_levels", "committed_table", "detail_of", "evidence_for", "evidence_words", "facts_of",
           "fit_words", "flow_ranking", "handles_through_fluid", "is_detailed", "quality_fault",
           "quality_step", "ranked_engines", "ranking_of", "recommend", "shape_class", "shape_words",
           "table_from", "validity_step", "wilson_lower", "wilson_upper"]
