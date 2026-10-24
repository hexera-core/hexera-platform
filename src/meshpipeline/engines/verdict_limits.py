# Responsibility: Hold the measured quality limits a delivered mesh is judged against, per engine and purpose, and turn a mesh's measured numbers into the inputs of the verdict rule (docs/RULES.md VER-1..4).
# Owns: the production-limit table (VER-2), the gating rows read from each engine's criteria (VER-1), and the one function that reads a manifest's quality record into verdict inputs.
# Boundaries: pure over the manifest and plain facts. It measures nothing and decides nothing itself: contracts/decision_record.verdict decides from the inputs built here, and nothing a review said is ever an input.
# Collaborates with: engines/<engine>/criteria.py (the gating rows), engines/quality_criteria (evaluates them), engines/passage_flow (the resolution floor), contracts/decision_record (the verdict rule), application/final_result (calls inputs_for at the end of a run).
"""THE MEASURED LIMITS A DELIVERED MESH IS JUDGED AGAINST.

Two kinds of row:

* GATING rows (VER-1) are each engine's own criteria (engines/<engine>/criteria.py) marked gating.
  The executor already refuses a mesh that fails one; they are read again here so the verdict can
  be re-derived from the numbers alone.
* PRODUCTION limits (VER-2) are the table below. A valid mesh that misses one is delivered with
  concerns, and each miss is listed as measured vs limit. Every row names its source.

A row applies to the engines it lists and, when it names topologies, only to those flows. A row
whose number was not measured is listed as unmeasured and decides nothing. No row may name a
file, a part or a model: limits are general, or they are not limits.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: the OpenFOAM-family engines that report checkMesh's non-orthogonality and skew on a hex mesh
_OPENFOAM_HEX = ("snappy", "cfmesh", "snappy_multiregion")
#: the engines that build one connected fluid region (a second region is a disconnected one)
_ONE_REGION = ("snappy", "cfmesh", "gmsh", "vmtk")
#: the engines whose internal meshes the resolution floor measures (engines/passage_flow)
_PASSAGE_MEASURED = ("snappy", "cfmesh", "gmsh", "vmtk")
#: every flow engine
_ALL = ("snappy", "cfmesh", "snappy_multiregion", "snapgrid", "gmsh", "vmtk")
_CFD = ("external", "internal")

_CHECKMESH = "https://doc.cfd.direct/openfoam/user-guide-v12/mesh-description (checkMesh warns above 70 degrees)"


@dataclass(frozen=True)
class Limit:
    """One production limit (VER-2): `key` measured `op` `limit`, on these engines and flows."""
    key: str
    label: str
    op: str
    limit: Any
    engines: tuple[str, ...]
    #: the flow topologies it applies to; () = every one
    topologies: tuple[str, ...] = ()
    #: applies only when the approved plan builds prism layers
    needs_layers: bool = False
    source: str = ""

    def applies(self, engine: str, topology: str, layers_planned: bool) -> bool:
        if engine not in self.engines:
            return False
        if self.topologies and topology not in self.topologies:
            return False
        return layers_planned or not self.needs_layers


#: THE PRODUCTION LIMITS (VER-2), per engine and purpose.
PRODUCTION_LIMITS: tuple[Limit, ...] = (
    Limit("max_non_ortho", "Worst face non-orthogonality", "<=", 70.0, _OPENFOAM_HEX,
          source=_CHECKMESH),
    Limit("max_non_ortho", "Worst face non-orthogonality a CFD solver can correct", "<=", 85.0,
          ("gmsh",), topologies=_CFD,
          source="engines/gmsh/criteria.py (also gmsh's gating bar, so a mesh past it never "
                 "validates)"),
    Limit("skew_fraction", "Share of skewed faces", "<=", 5e-4, ("snappy", "cfmesh"),
          source="checkMesh's skewness bars (maxInternalSkewness 4, maxBoundarySkewness 20) as a "
                 "share of all faces (engines/snappy/criteria.py): a production limit on a sound "
                 "mesh, never a validity bar (LAD-cfd-3); cfMesh often does not report it"),
    Limit("layer_coverage_pct", "Wall covered by prism layers (percent)", ">=", 80.0,
          ("snappy", "cfmesh", "snappy_multiregion", "vmtk"), needs_layers=True,
          source="Hexera lab history: under 80% coverage, wall shear and y+ are unreliable on a "
                 "material share of the wall"),
    Limit("layers_short", "Prism layers built short of the layers asked for", "<=", 0,
          ("cfmesh",), needs_layers=True,
          source="engines/layer_census.py (layers in the meshDict vs the layers the request asked)"),
    Limit("regions", "Connected cell regions", "<=", 1, _ONE_REGION,
          source="checkMesh's region count: a second region is cut off from the flow"),
    Limit("passage_cells_across", "Cells across the narrowest passage (5th percentile)", ">=",
          None, _PASSAGE_MEASURED, topologies=("internal",),
          source="engines/passage_flow.judge_resolution (the floor the gate uses; narrow side "
                 "passages it set aside are listed here)"),
    Limit("requirement_near_misses", "Stated requirements met", "<=", 0, _ALL,
          source="the executor's measured requirement checks (requirement_caveats)"),
    Limit("size_concerns", "A believable size for what it is", "<=", 0, _ALL,
          source="contracts/unit_plausibility (the delivered body's size against what it is)"),
)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _layers_short(quality: Mapping) -> float | None:
    asked, built = _num(quality.get("layers_requested")), _num(quality.get("layers_in_meshdict"))
    if asked is None or built is None:
        return None
    return max(0.0, asked - built)


def _passage(quality: Mapping) -> tuple[float | None, float | None]:
    """(cells across the narrowest passage the flow must use, the floor) - the resolution floor's
    own reading. Where it set narrow side passages aside, their figure is the one measured here."""
    from meshpipeline.engines.passage_flow import judge_resolution
    v = judge_resolution(dict(quality))
    if v.scope == "unmeasured":
        return None, v.floor
    if v.scope == "main" and v.side:
        raw = v.side.get("cells_across")
        side = _num(raw.get("p05")) if isinstance(raw, Mapping) else None
        return (side if side is not None else v.cells_across), v.floor
    return v.cells_across, v.floor


def _measured(key: str, quality: Mapping, facts: Mapping) -> Any:
    if key == "layers_short":
        return _layers_short(quality)
    if key in ("requirement_near_misses", "size_concerns"):
        return facts.get(key)
    return quality.get(key)


def limits_for(engine: str, topology: str, *, layers_planned: bool) -> tuple[Limit, ...]:
    """The production limits that apply to this engine and flow."""
    rows = [lim for lim in PRODUCTION_LIMITS if lim.applies(engine, topology, layers_planned)]
    return tuple(rows)


def _declared_advisory(engine: str) -> list[dict]:
    """The advisory rows an engine's own criteria declare as lists that must be empty (the
    snap-grid engine's thin-layer and resolution shortfalls): production limits as declared. The
    numeric advisory rows are not read: their limits are the table's, stated once."""
    from meshpipeline.engines.quality_criteria import criteria_for
    try:
        crit = criteria_for(engine)
    except Exception:  # noqa: BLE001 - an unknown engine declares nothing
        return []
    return [{"key": c.key, "label": c.label, "op": c.op, "limit": c.threshold,
             "source": f"engines/{engine}/criteria.py"}
            for c in crit if not c.gating and c.op == "empty"]


def gating_rows(engine: str, manifest: Mapping) -> list[dict]:
    """The engine's own gating criteria, measured on the manifest (VER-1). The build-time rows
    (exit code, time-out) describe a process that has exited; the executor judged them."""
    from meshpipeline.engines.quality_criteria import (
        BUILD_TIME_KEYS,
        criteria_for,
        measurements_from_manifest,
    )
    try:
        crit = criteria_for(engine)
        measured = measurements_from_manifest(dict(manifest))
    except Exception:  # noqa: BLE001 - an engine that cannot be read leaves validity to the gates
        return []
    # AN FEA MESH is held to validity only (PLAN-fea-8): its quality floor is the NOTE floor, a row
    # of fea_limits, never a gate - the gmsh floor row gating it refused a valid assembly whose 7
    # curved slivers sat under SICN 0.01 (dev-areen, 24 Oct). "No degenerate elements" still gates.
    raw = manifest.get("quality")
    q: Mapping = raw if isinstance(raw, Mapping) else {}
    fea = isinstance(q.get("fea"), Mapping) and bool(q["fea"].get("plan"))
    skip = FEA_NOTE_ONLY if fea else ()
    return [{"key": c.key, "label": c.label, "op": c.op, "limit": c.threshold,
             "measured": measured.get(c.key)}
            for c in crit if c.gating and c.key not in BUILD_TIME_KEYS and c.key not in skip]


#: Gating rows an FEA mesh reads as notes instead (they are fea_limits rows).
FEA_NOTE_ONLY = ("elements_under_floor",)


def inputs_for(*, engine: str, topology: str, manifest: Mapping | None, valid: bool,
               failed_gate: str = "", layers_planned: bool = False,
               requirement_near_misses: int = 0, size_concerns: int = 0,
               notes: Sequence[Any] = ()) -> dict:
    """The verdict rule's inputs (contracts/decision_record.verdict) for one delivered mesh: every
    row with its measured number, its limit and its source. Same numbers in, same inputs out."""
    mf = manifest if isinstance(manifest, Mapping) else {}
    raw = mf.get("quality")
    quality: Mapping = raw if isinstance(raw, Mapping) else {}
    facts = {"requirement_near_misses": int(requirement_near_misses or 0),
             "size_concerns": int(size_concerns or 0)}
    limits: list[dict] = []
    for lim in limits_for(engine, topology, layers_planned=layers_planned):
        if lim.key == "passage_cells_across":
            measured, floor = _passage(quality)
            limit: Any = floor
        else:
            measured, limit = _measured(lim.key, quality, facts), lim.limit
        limits.append({"key": lim.key, "label": lim.label, "op": lim.op, "limit": limit,
                       "measured": _plain(measured), "source": lim.source})
    for row in _declared_advisory(engine):
        limits.append({**row, "measured": _plain(quality.get(row["key"]))})
    limits.extend(fea_limits(quality))
    return {"engine": engine, "topology": topology, "valid": bool(valid),
            "failed_gate": str(failed_gate or ""), "layers_planned": bool(layers_planned),
            "gating": gating_rows(engine, mf) if valid else [],
            "limits": limits,
            # what the AI reviewer wrote, for the record only: never read by the verdict
            "review_notes": len(list(notes or ()))}


#: The FEA plan's own rule row (VER-2): every hot spot met its count on the mesh (the walls are
#: gating, engines/gmsh/gates.py).
FEA_RULES_ROW = ("fea_rules_short", "Mesh zones short of the FEA plan's rules", "<=", 0,
                 "engines/fea_checks.py (elements per quarter-turn and through the wall at each hot spot)")


def fea_limits(quality: Mapping) -> list[dict]:
    """THE FEA ROWS (VER-2), for a mesh built from an FEA plan only (quality.fea.plan): the element
    quality limits - the strictest of the version-1 solvers (engines/fea_quality.LIMITS) - and the
    plan's rules as met on the mesh. Engine-neutral: any engine's FEA adapter reports the same keys."""
    fea = quality.get("fea") if isinstance(quality.get("fea"), Mapping) else {}
    if not (fea or {}).get("plan"):
        return []
    from meshpipeline.engines.fea_quality import limits_for
    order = quality.get("element_order")
    fea_rows = limits_for(int(order) if isinstance(order, (int, float)) and not isinstance(order, bool) else 1)
    fea_m: Mapping = fea if isinstance(fea, Mapping) else {}
    # ONE LIST OF NOTES (22 Oct): the rows the result screen shows are exactly the rows the job's
    # verdict reads - the quality note floor and the elements through the thinnest wall first, as
    # the screen lists them (a job said "Review: passed" while its screen said "with notes")
    from meshpipeline.engines.gmsh.gates import FEA_WALL_FLOOR
    rows = [{"key": "elements_under_floor", "op": "==", "limit": 0,
             "label": f"Elements under the quality note floor (SICN {float(quality.get('sicn_floor') or 0.1):g})",
             "measured": _plain(quality.get("elements_under_floor")),
             "source": "engines/gmsh/fea_adapter.py (the note floor: SICN 0.1 straight, 0.01 curved)"},
            {"key": "through_wall_min", "op": ">=", "limit": FEA_WALL_FLOOR,
             "label": "Elements through the thinnest wall (2 planned; read to within 10%)",
             "measured": _plain(fea_m.get("through_wall_min")),
             "source": "docs/RULES.md PLAN-fea-3 (engines/fea_checks.py)"}]
    rows += [{"key": k, "label": lab, "op": op, "limit": lim, "measured": _plain(quality.get(k)),
              "source": src} for k, lab, op, lim, src in (*fea_rows, FEA_RULES_ROW)]
    coarse = fea_m.get("coarsened") if isinstance(fea_m.get("coarsened"), Mapping) else None
    if coarse:
        # PLAN-fea-8: the rules needed more than the element limit, so the finest zones were made
        # coarser to fit; what that leaves short is named, as a note (Areen, 21 Oct)
        rows.append({"key": "fea_resolution_short", "op": "empty", "limit": [],
                     "measured": [str(x) for x in fea_m.get("resolution_short") or []],
                     "label": (f"The rules need {coarse.get('needed_words') or 'more elements'}, more than the element "
                               "limit allows - the finest zones were made coarser to fit; short of the rules where named"),
                     "source": "docs/RULES.md PLAN-fea-8 (engines/fea_driver.choose_plan)"})
    raw_aside = quality.get("fea_set_aside")
    aside: Mapping = raw_aside if isinstance(raw_aside, Mapping) else {}
    if aside.get("elements") and aside.get("places_mm") and aside.get("cusp_elements", 1):
        # PLAN-fea-10: the elements set aside at a cusp are delivered with a CONCERN that names each
        # place (Areen's decision, 21 Oct): never silently, never as a pass
        places = [f"({', '.join(f'{float(v):g}' for v in p)}) mm" for p in aside.get("places_mm") or []]
        rows.append({"key": "fea_cusps", "op": "empty", "limit": [], "measured": places,
                     "label": (f"{int(aside.get('cusp_elements') or aside['elements'])} element(s) at a cusp in the CAD, set aside from the quality "
                               "floor - the material runs to nothing here, so stress at this point is not meaningful; "
                               "round it off in CAD for a clean mesh"),
                     "source": "docs/RULES.md PLAN-fea-10 (cad/fea_measure.cad_cusps)"})
    micro = [m for m in aside.get("micro") or [] if isinstance(m, Mapping)]
    if aside.get("micro_elements") and micro:
        # PLAN-fea-12: each micro detail named, and that stress right at it is not resolved (Areen,
        # 21 Oct: on a gear that is the contact stress at the tooth tips, and the note says so)
        places = [f"({', '.join(f'{float(v):g}' for v in m.get('at_mm') or [])}) mm, a {m.get('kind')} "
                  f"{float(m.get('size_mm') or 0.0):.3g} mm across" for m in micro]
        rows.append({"key": "fea_micro_details", "op": "empty", "limit": [], "measured": places,
                     "label": (f"{int(aside['micro_elements'])} element(s) on CAD details far smaller than the mesh, set "
                               "aside from the quality floor - the mesh cannot follow these details, so stress right at "
                               "them is not resolved (on a gear, the contact stress at the tooth tips is not resolved); "
                               "remove or enlarge them in CAD for a clean mesh"),
                     "source": "docs/RULES.md PLAN-fea-12 (cad/fea_measure.cad_micro)"})
    return rows


def _plain(v: Any) -> Any:
    """A measured value as plain JSON (lists of strings, numbers, None)."""
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def words(row: Mapping) -> str:
    """One missed limit in plain words: 'Worst face non-orthogonality: 74.2, limit at most 70'."""
    op = {"<=": "at most", ">=": "at least", "<": "under", ">": "over", "==": "exactly",
          "empty": "none"}.get(str(row.get("op") or ""), str(row.get("op") or ""))
    measured = row.get("measured")
    if isinstance(measured, float):
        measured = f"{measured:.4g}"
    elif isinstance(measured, list):
        measured = ", ".join(measured[:6]) or "none"
    limit = row.get("limit")
    limit_text = "" if row.get("op") == "empty" else f" {limit:g}" if isinstance(
        limit, (int, float)) and not isinstance(limit, bool) else f" {limit}"
    return f"{row.get('label')}: measured {measured}, limit {op}{limit_text}"


__all__ = ["FEA_RULES_ROW", "PRODUCTION_LIMITS", "Limit", "fea_limits", "gating_rows", "inputs_for",
           "limits_for", "words"]
