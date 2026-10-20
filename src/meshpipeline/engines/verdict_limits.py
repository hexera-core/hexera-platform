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
    Limit("skew_fraction", "Share of skewed faces", "<=", 5e-4, ("cfmesh",),
          source="the snappy-family gating bar (engines/snappy/criteria.py); advisory for cfMesh, "
                 "which often does not report it"),
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
    return [{"key": c.key, "label": c.label, "op": c.op, "limit": c.threshold,
             "measured": measured.get(c.key)}
            for c in crit if c.gating and c.key not in BUILD_TIME_KEYS]


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
    return {"engine": engine, "topology": topology, "valid": bool(valid),
            "failed_gate": str(failed_gate or ""), "layers_planned": bool(layers_planned),
            "gating": gating_rows(engine, mf) if valid else [],
            "limits": limits,
            # what the AI reviewer wrote, for the record only: never read by the verdict
            "review_notes": len(list(notes or ()))}


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


__all__ = ["PRODUCTION_LIMITS", "Limit", "gating_rows", "inputs_for", "limits_for", "words"]
