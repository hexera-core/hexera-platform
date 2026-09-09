# Responsibility: Turn measured thin-feature geometry into snappy's LOCAL layer policy, and
# escalate that policy deterministically when prism layers invert cells.
# Owns: the class thresholds, the per-class layer counts, the escalation ladder's state machine,
# and the honest policy record the manifest carries.
# Boundaries: the classifier measures (cad/thin_features), the renderer authors syntax
# (snappy_runner), the driver orchestrates (drivers). This module only decides.
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import meshpipeline.engines.snappy.settings as scfg
from meshpipeline.cad.thin_features import (
    CLASS_NORMAL,
    CLASS_RAZOR,
    CLASS_THIN,
    ThinFeatureField,
    class_area_fractions,
    classify_faces,
)

logger = logging.getLogger(__name__)

#: The honest record of what was locally traded, written beside the case and attached to the
#: manifest's quality dict by finalize. Versioned so a reader can tell the schema apart.
LAYER_POLICY_FACT = "layer_policy.json"

#: The ladder's durable state - which escalation stage this workspace has reached. Survives the
#: driver's pass loop and, via the sibling lookup, a pipeline-level retry in a fresh attempt_N
#: workspace (the geometry is the same geometry; starting the ladder over would replay the same
#: negative-volume failure the earlier attempt already measured).
ESCALATION_FACT = ".thin_escalation.json"

#: Terminal ladder stage. Stage 0 is the initial classification-derived policy; stages 1..MAX
#: are deterministic escalations. escalate() beyond MAX returns None and the driver falls back
#: to the planner's freeform re-plan, exactly as before this module existed.
MAX_ESCALATION_STAGE = 2

#: min layer-thickness (relative, matches addLayersControls relativeSizes true) per stage.
#: Stage 0 keeps snappy's historical 0.05 unless razor area exists - a razor region's single
#: layer must be allowed to thin locally rather than abort the whole patch's inflation.
_MIN_THICKNESS_BY_STAGE = {0: 0.05, 1: 0.01, 2: 0.005}
_MIN_THICKNESS_RAZOR_STAGE0 = 0.02

#: maxThicknessToMedialRatio override per stage (None = renderer's own quality-derived value).
#: From stage 1 the strict-profile 0.3 makes layers auto-thin approaching the medial axis - the
#: mechanism that lets opposing stacks in a thin core shrink instead of colliding.
_MEDIAL_BY_STAGE = {0: None, 1: 0.3, 2: 0.3}


def intended_surface_cell(rec: dict, strategy: dict | None) -> float:
    """The ABSOLUTE wall-cell size the recommender intends for this plan.

    Mirrors render_snappy_case's level choice (floor at the sealing level, ceiling at the
    afford level, strategy may move within) MINUS the clamp deficit - deliberately: the
    deficit exists to HOLD this absolute size when the background grid coarsens, so the
    intended size is the deficit-free one."""
    sl = rec["surface_level"]
    floor = int(sl[0]) if isinstance(sl, (list, tuple)) else int(sl)
    ceil_ = max(floor, int(rec.get("afford_level", floor)))
    so = (strategy or {}).get("surface_level")
    if so is not None and not isinstance(so, (list, tuple)):
        so = [so]
    lvl = floor if not so else max(floor, min(int(so[-1]), ceil_))
    return float(rec["base_cell"]) / (2 ** lvl)


def stack_thickness_m(surf_cell: float, n_layers: int, first_layer_rel: float,
                      expansion: float = 1.2) -> float:
    """Total prism-stack thickness. finalLayerThickness is the OUTERMOST layer as a fraction of
    the local cell (relativeSizes true); each deeper layer shrinks by 1/expansion."""
    n = int(n_layers)
    if n <= 0 or surf_cell <= 0:
        return 0.0
    r = 1.0 / float(expansion)
    return float(first_layer_rel) * float(surf_cell) * (1.0 - r ** n) / (1.0 - r)


def class_layer_counts(n_requested: int, stage: int) -> dict[str, int]:
    """Per-class layer counts, per ladder stage. Deterministic and total: any stage at or past
    the terminal one returns the terminal counts."""
    n = max(0, int(n_requested))
    thin0 = max(1, (n + 1) // 2) if n else 0
    if stage <= 0:
        return {"normal": n, "thin": thin0, "razor": min(1, n)}
    if stage == 1:
        return {"normal": n, "thin": max(1, thin0 - 1) if n else 0, "razor": min(1, n)}
    return {"normal": n, "thin": min(1, n), "razor": 0}


def escalate(stage: int) -> int | None:
    """The ladder's whole transition function: failure at stage s -> retry at s+1, exhausted
    past MAX_ESCALATION_STAGE (None hands the failure back to the planner's freeform re-plan)."""
    s = int(stage)
    return s + 1 if s < MAX_ESCALATION_STAGE else None


def is_layer_fatal(q: dict) -> bool:
    """Does this checkMesh verdict carry the LAYER-INVERSION signature? Mirrors the judge's own
    reading of the fatal classes: negative-volume / mis-oriented cells are what folding prism
    layers produce; open cells and carve defects are not a layer problem."""
    blob = " ".join(str(f) for f in (q or {}).get("fatal", [])).lower()
    return ("negative" in blob) or ("orient" in blob)


@dataclass(frozen=True)
class LayerPolicy:
    """The decided policy for ONE authoring pass. `policy` is the JSON-ready honest record;
    `labels` are the per-triangle class codes the surface splitter consumes (split mode only)."""

    policy: dict
    labels: np.ndarray | None = field(default=None, compare=False)

    @property
    def mode(self) -> str:
        return str(self.policy.get("mode", ""))


def _region_names(wall_name: str) -> dict[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", wall_name) or "body"
    return {"normal": safe, "thin": f"{safe}_thin", "razor": f"{safe}_razor"}


def plan_layer_policy(field_: ThinFeatureField | None, *, rec: dict, strategy: dict,
                      wall_name: str, stage: int = 0,
                      split_min_normal_frac: float = 0.05) -> LayerPolicy | None:
    """Classification -> policy, or None when today's global behaviour should stand unchanged.

    None whenever: the feature is off, the field could not be measured, no layers were
    requested, the thin+razor area is below the action floor - or ANY error occurs (a policy
    is an improvement, never a prerequisite; perception must not cost a build). The caller
    then authors exactly the historical dict (pinned byte-identical by regression test)."""
    try:
        return _plan_layer_policy(field_, rec=rec, strategy=strategy, wall_name=wall_name,
                                  stage=stage, split_min_normal_frac=split_min_normal_frac)
    except Exception:  # noqa: BLE001 - degrade to the historical global behaviour
        logger.warning("thin-feature layer policy could not be planned; authoring the "
                       "historical global layer configuration", exc_info=True)
        return None


def _plan_layer_policy(field_: ThinFeatureField | None, *, rec: dict, strategy: dict,
                       wall_name: str, stage: int,
                       split_min_normal_frac: float) -> LayerPolicy | None:
    if not scfg.SNAPPY_THIN_LAYER_POLICY:
        return None
    if field_ is None or not field_.measured or field_.n_triangles == 0:
        return None
    n_layers = max(0, int((strategy or {}).get("n_layers", 3)))
    if n_layers <= 0:
        return None
    first_rel = float((strategy or {}).get("first_layer_rel", 0.35))
    surf_cell = intended_surface_cell(rec, strategy)
    stack = stack_thickness_m(surf_cell, n_layers, first_rel)
    razor_below = scfg.SNAPPY_RAZOR_CELL_FACTOR * surf_cell
    thin_below = max(scfg.SNAPPY_THIN_STACK_FACTOR * 2.0 * stack, 2.0 * surf_cell)
    labels = classify_faces(field_, thin_below_m=thin_below, razor_below_m=razor_below)
    fracs = class_area_fractions(labels, field_.area_m2)
    if fracs["thin"] + fracs["razor"] < scfg.SNAPPY_THIN_AREA_FLOOR:
        return None

    stage = max(0, int(stage))
    counts = class_layer_counts(n_layers, stage)
    razor_present = fracs["razor"] > 0.0
    if stage <= 0:
        min_thickness = _MIN_THICKNESS_RAZOR_STAGE0 if razor_present else _MIN_THICKNESS_BY_STAGE[0]
    else:
        min_thickness = _MIN_THICKNESS_BY_STAGE.get(stage, _MIN_THICKNESS_BY_STAGE[MAX_ESCALATION_STAGE])
    medial = _MEDIAL_BY_STAGE.get(stage, _MEDIAL_BY_STAGE[MAX_ESCALATION_STAGE])

    # SPLIT only when a meaningful normal remainder exists to carry the declared wall patch and
    # the full layer request; an (almost) wholly thin body is a GLOBAL reduction, not a split.
    split = fracs["normal"] >= float(split_min_normal_frac)
    names = _region_names(wall_name)
    if split:
        mode = "split"
        region_patches = {names[c]: c for c in ("normal", "thin", "razor") if fracs[c] > 0.0}
    else:
        mode = "global"
        dominant = "razor" if fracs["razor"] >= fracs["thin"] else "thin"
        region_patches = {names["normal"]: dominant}
    policy = {
        "version": 1,
        "source": "thin_feature_classifier",
        "mode": mode,
        "requested_layers": n_layers,
        "escalation_stage": stage,
        "classes": {c: {"n_layers": counts[c], "area_frac": round(fracs[c], 6)}
                    for c in ("normal", "thin", "razor")},
        "thresholds_m": {"thin_below": thin_below, "razor_below": razor_below},
        "surface_cell_m": surf_cell,
        "stack_m": stack,
        "min_thickness_rel": min_thickness,
        "max_thickness_to_medial": medial,
        "sharp_area_frac": round(float(field_.area_m2[field_.sharp].sum()
                                       / max(field_.area_m2.sum(), 1e-30)), 6),
        "region_patches": region_patches,
    }
    return LayerPolicy(policy=policy, labels=labels if split else None)


def make_region_labeler(policy: LayerPolicy, wall_name: str) -> Callable[[list], list[str]] | None:
    """The per-triangle labeller prepare_surface applies when splitting a monolithic wall into
    class regions. Returns None outside split mode. Tolerates the mirrored (doubled) triangle
    list by tiling; any other length mismatch degrades to all-normal rather than mislabel."""
    if policy is None or policy.mode != "split" or policy.labels is None:
        return None
    names = _region_names(wall_name)
    by_code = {CLASS_NORMAL: names["normal"], CLASS_THIN: names["thin"],
               CLASS_RAZOR: names["razor"]}
    codes = np.asarray(policy.labels)

    def _label(tris: list) -> list[str]:
        n = len(tris)
        if n == len(codes):
            seq = codes
        elif n == 2 * len(codes):
            seq = np.concatenate([codes, codes])
        else:
            logger.warning("thin-feature labeller: %d triangles vs %d labels - surface changed "
                           "between measurement and staging; not splitting", n, len(codes))
            return [names["normal"]] * n
        return [by_code[int(c)] for c in seq]

    return _label


def reconcile_policy(policy: LayerPolicy | None, surface_regions: list,
                     wall_name: str) -> LayerPolicy | None:
    """Fit the intended policy to the surface prepare_surface ACTUALLY staged.

    Split mode survives only when the synthetic class regions really exist on the staged
    surface; when the input carried its own named CAD solids instead (prepare_surface never
    splits those - their names are the user's), the policy degrades to UNIFORM on the single
    declared wall entry: the full request at stage 0 (knob relaxation only), the thin-class
    count from stage 1 up. Split mode also drops class regions that received no triangles.
    The returned record is what the manifest reports - it must describe the dict that was
    authored, never the intention."""
    if policy is None:
        return None
    regions = [str(r) for r in (surface_regions or [])]
    names = _region_names(wall_name)
    p = dict(policy.policy)
    if policy.mode == "split":
        synthetic = {names["thin"], names["razor"]}
        if regions and (synthetic & set(regions)):
            # keep only the class regions that actually exist on the staged surface
            p["region_patches"] = {r: c for r, c in p["region_patches"].items() if r in regions}
            return LayerPolicy(policy=p, labels=policy.labels)
        # the split did not stage (real CAD regions, or a degraded labeller) - uniform fallback
        stage = int(p.get("escalation_stage", 0))
        n_req = int(p.get("requested_layers", 0))
        uniform = n_req if stage <= 0 else class_layer_counts(n_req, stage)["thin"]
        p["mode"] = "uniform"
        p["region_patches"] = {names["normal"]: "uniform"}
        p["classes"] = {c: {"n_layers": uniform, "area_frac": p["classes"][c]["area_frac"]}
                        for c in p["classes"]}
        p["uniform_n_layers"] = uniform
        return LayerPolicy(policy=p, labels=None)
    return LayerPolicy(policy=p, labels=policy.labels)


def layer_counts_for(policy: LayerPolicy | None) -> dict[str, int] | None:
    """The per-patch nSurfaceLayers dict the renderer consumes, from the reconciled policy."""
    if policy is None:
        return None
    p = policy.policy
    if p.get("mode") == "uniform":
        n = int(p.get("uniform_n_layers", p.get("requested_layers", 0)))
        return dict.fromkeys(p.get("region_patches", {}), n)
    counts = {c: int(v["n_layers"]) for c, v in p.get("classes", {}).items()}
    return {r: counts.get(c, int(p.get("requested_layers", 0)))
            for r, c in p.get("region_patches", {}).items()}


def overrides_for(policy: LayerPolicy | None) -> dict | None:
    """The addLayersControls knob overrides the renderer consumes. None keeps every default."""
    if policy is None:
        return None
    p = policy.policy
    out: dict = {"min_thickness_rel": float(p.get("min_thickness_rel", 0.05))}
    if p.get("max_thickness_to_medial") is not None:
        out["max_thickness_to_medial"] = float(p["max_thickness_to_medial"])
    return out


# -- durable facts ---------------------------------------------------------------------------

def write_layer_policy(workspace, policy: LayerPolicy | None) -> None:
    """Record the pass's policy beside the case (finalize attaches it to the manifest). A pass
    with NO policy removes a stale record - the delivered report must describe the delivered
    mesh, never a previous pass's trade."""
    ws = Path(workspace)
    target = ws / LAYER_POLICY_FACT
    try:
        if policy is None:
            target.unlink(missing_ok=True)
            return
        tmp = ws / (LAYER_POLICY_FACT + ".tmp")
        tmp.write_text(json.dumps(policy.policy))
        tmp.replace(target)
    except Exception:  # noqa: BLE001 - the honest record is best-effort, never costs a mesh
        logger.warning("layer-policy record could not be written", exc_info=True)


def read_escalation(workspace) -> int:
    """This workspace's ladder stage, else the newest sibling attempt's (a pipeline retry meshes
    the same geometry - restarting the ladder would replay a measured failure), else 0."""
    ws = Path(workspace)
    for cand in (ws, *_siblings(ws)):
        try:
            raw = json.loads((cand / ESCALATION_FACT).read_text())
            return max(0, int(raw.get("stage", 0)))
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001 - a corrupt fact is not a stage
            logger.warning("unreadable %s in %s - ignoring", ESCALATION_FACT, cand)
            continue
    return 0


def write_escalation(workspace, stage: int) -> None:
    ws = Path(workspace)
    try:
        tmp = ws / (ESCALATION_FACT + ".tmp")
        tmp.write_text(json.dumps({"stage": int(stage)}))
        tmp.replace(ws / ESCALATION_FACT)
    except Exception:  # noqa: BLE001
        logger.warning("escalation stage could not be persisted", exc_info=True)


def _siblings(ws: Path) -> list[Path]:
    m = re.fullmatch(r"attempt_(\d+)", ws.name)
    if m is None:
        return []
    return [ws.parent / f"attempt_{n}" for n in range(int(m.group(1)) - 1, 0, -1)]


def measure_field(surface) -> ThinFeatureField | None:
    """The driver's one-per-build measurement, degrading to None on any failure."""
    if not scfg.SNAPPY_THIN_LAYER_POLICY:
        return None
    try:
        from meshpipeline.cad.thin_features import measure_thin_features
        return measure_thin_features(surface)
    except Exception:  # noqa: BLE001 - perception must never cost a build
        logger.warning("thin-feature measurement failed; local layer policy disabled",
                       exc_info=True)
        return None


__all__ = [
    "ESCALATION_FACT", "LAYER_POLICY_FACT", "MAX_ESCALATION_STAGE", "LayerPolicy",
    "class_layer_counts", "escalate", "intended_surface_cell", "is_layer_fatal",
    "layer_counts_for", "make_region_labeler", "measure_field", "overrides_for",
    "plan_layer_policy", "read_escalation", "reconcile_policy", "stack_thickness_m",
    "write_escalation", "write_layer_policy",
]
