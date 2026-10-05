# Responsibility: Name the facts an engine's home-turf rows may test, say where each comes from, and read one engine's rows into a verdict.
# Owns: the fact vocabulary of EngineSpec.home_turf and the one reading of a spec's rows against a case's facts.
# Boundaries: it measures nothing and decides no run; the facts arrive measured (the recommender's pre-mesh census, the intake's brief).
# Collaborates with: engines/base.py (TurfRow, the severities), engines/registry.py, the recommender and the intake proposal.
"""WHERE EACH ENGINE IS AT HOME - the facts its rows test, and one engine's verdict on one case.

An engine's spec declares `home_turf`: rows of `fact op value -> severity`, per flow. This module
is the vocabulary those rows may use - every fact a row names is one the system measures from the
upload or carries from the brief, listed below with where it comes from - and the one reading of
the rows: a case is OUTSIDE an engine's turf when an outside row holds, on a WEAK spot when a weak
row holds, and at HOME otherwise; the home rows that hold are its strengths there.

A fact the case does not carry makes its rows say nothing, so an engine is never marked weak or
outside on a fact nobody measured.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from meshpipeline.engines.base import (
    TURF_HOME,
    TURF_OPS,
    TURF_OUTSIDE,
    TURF_SEVERITIES,
    TURF_WEAK,
    TurfRow,
)


@dataclass(frozen=True)
class TurfFact:
    kind: str        # "number" | "bool" | "str" | "count"
    source: str      # where the value comes from
    words: str       # what it is, in plain words


#: Pre-mesh facts the recommender measures from the uploaded file (its geometry census).
_CENSUS = "recommender geometry census of the upload (ov/recommend)"
#: Facts carried by the request rather than measured from the file.
_BRIEF = "the approved brief / intake declaration"

TURF_FACTS: dict[str, TurfFact] = {
    "flow": TurfFact("str", "purpose -> capability.flow_of", "external, internal, multi-region or structural"),
    "form": TurfFact("str", "capability.geometry_form", "cad (B-rep solid) or surface (triangles)"),
    "input_kind": TurfFact("str", _BRIEF, "fluid-domain, body-surface, solid-body or solid-assembly"),
    "ports": TurfFact("count", _BRIEF, "declared inlets + outlets"),
    "n_solids": TurfFact("count", _CENSUS, "connected bodies in the file"),
    "region_count": TurfFact("count", "cad analysis surface_analysis.region_count", "named regions the file distinguishes"),
    "closed": TurfFact("bool", _CENSUS, "watertight after welding"),
    "genus": TurfFact("count", _CENSUS, "through-holes / obstacles crossing the fluid (a tube bank)"),
    "sharp_edges": TurfFact("number", _CENSUS, "feature-edge length (dihedral > 40 deg) per sqrt(wetted area)"),
    "thin_wall_fraction": TurfFact("number", _CENSUS, "share of wetted area whose solid is thinner than 1% of the part"),
    "scale_ratio": TurfFact("number", _CENSUS, "part size / 5th-percentile fluid chord"),
    "passage": TurfFact("number", _CENSUS, "median fluid gap, metres"),
    "neck": TurfFact("number", _CENSUS, "5th-percentile gap / median gap"),
    "slenderness": TurfFact("number", _CENSUS, "wetted area / (pi * median gap^2), about L/D"),
    "gap_vs_port": TurfFact("number", _CENSUS, "median gap / port hydraulic diameter (1 round tube, 0.5 annulus, > 1.4 chamber)"),
    "thickness_ratio": TurfFact("number", _CENSUS, "body thickness / length (external bluffness)"),
    "cells_across_at_budget": TurfFact(
        "number", _CENSUS + ", from the passage and the cell budget",
        "cells across the narrowest passage a uniform mesh within the budget would give"),
    "layers_requested": TurfFact("bool", "pipeline.engine_fallback.layer_request(brief).requested",
                                 "the brief asks for near-wall prism layers"),
    "ground": TurfFact("bool", "engines.ground_plane.ground_patch_name(declared patches)",
                       "an external body standing on a ground wall"),
    "cell_budget": TurfFact("number", _BRIEF, "the cell budget the brief allows"),
}


@dataclass(frozen=True)
class TurfVerdict:
    engine: str
    flow: str
    severity: str                                   # one of TURF_SEVERITIES
    strengths: tuple[TurfRow, ...] = ()             # home rows that hold
    weak_spots: tuple[TurfRow, ...] = ()            # weak rows that hold
    outside: tuple[TurfRow, ...] = ()               # outside rows that hold
    unmeasured: tuple[str, ...] = field(default=())  # facts some applicable row needed, not carried

    @property
    def reasons(self) -> tuple[str, ...]:
        rows = self.outside or self.weak_spots or self.strengths
        return tuple(r.words for r in rows)


def rows_for(spec, flow: str) -> tuple[TurfRow, ...]:
    return tuple(r for r in getattr(spec, "home_turf", ()) or () if r.applies_to(flow))


def verdict(spec, flow: str, facts: Mapping) -> TurfVerdict:
    """This engine on this case: outside when an outside row holds, weak when a weak row holds,
    home otherwise (nothing is claimed about a flow the engine does not declare; see accepts)."""
    held: dict[str, list[TurfRow]] = {s: [] for s in TURF_SEVERITIES}
    missing: list[str] = []
    for row in rows_for(spec, flow):
        h = row.holds(facts)
        if h is None:
            if row.fact not in missing:
                missing.append(row.fact)
        elif h:
            held[row.severity].append(row)
    sev = (TURF_OUTSIDE if held[TURF_OUTSIDE] else TURF_WEAK if held[TURF_WEAK] else TURF_HOME)
    return TurfVerdict(engine=str(getattr(spec, "name", "")), flow=flow, severity=sev,
                       strengths=tuple(held[TURF_HOME]), weak_spots=tuple(held[TURF_WEAK]),
                       outside=tuple(held[TURF_OUTSIDE]), unmeasured=tuple(missing))


def row_problems(spec) -> list[str]:
    """What is wrong with a spec's rows (unknown fact, op, severity or flow; a value the op cannot
    compare) - empty for a well-formed declaration. Tests run it over every registered engine."""
    from meshpipeline.engines.capability import FLOW_KINDS
    out = []
    for i, r in enumerate(getattr(spec, "home_turf", ()) or ()):
        where = f"{getattr(spec, 'name', '?')}.home_turf[{i}] ({r.fact})"
        if r.fact not in TURF_FACTS:
            out.append(f"{where}: unknown fact")
        if r.op not in TURF_OPS:
            out.append(f"{where}: unknown op {r.op!r}")
        if r.severity not in TURF_SEVERITIES:
            out.append(f"{where}: unknown severity {r.severity!r}")
        if r.flow and r.flow not in FLOW_KINDS:
            out.append(f"{where}: unknown flow {r.flow!r}")
        if r.op in ("<", "<=", ">", ">=") and not isinstance(r.value, (int, float)):
            out.append(f"{where}: {r.op} needs a number")
        if r.op in ("in", "not in") and not isinstance(r.value, tuple):
            out.append(f"{where}: {r.op} needs a tuple")
        if r.op == "is" and not isinstance(r.value, bool):
            out.append(f"{where}: 'is' needs a bool")
        if not r.words.strip():
            out.append(f"{where}: no words")
    return out


__all__ = ["TURF_FACTS", "TurfFact", "TurfVerdict", "row_problems", "rows_for", "verdict"]
