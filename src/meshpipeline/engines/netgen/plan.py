# Responsibility: Turn a staged internal-flow boundary into Netgen's plan: the local cell size at every wall point, the prism-layer stack, the cell estimate, and the ladder of rungs the native run walks.
# Owns: the sizing rules (NG-1..NG-5 below), the size file and netgen_spec.json.
# Boundaries: worker-side and deterministic - measured from the staged surface (engines/passage.py's field, the same one the resolution gate reads), never from the part's name; it runs no mesher.
# Collaborates with: engines/netgen/staging.py (the staged record), engines/netgen/driver.py (calls make_plan), engines/netgen/netgen_mesh.py (reads the spec).
"""THE NETGEN PLAN, in rules:

NG-1  CELLS ACROSS THE LOCAL PASSAGE. At every wall point the cell size is 2 r / CELLS_ACROSS, r the
      local passage radius measured on the staged surface (engines/passage.staged_passage_field: half
      the inward chord to the opposite wall, held to each declared port's half-width near it). The
      resolution gate holds PASSAGE_FLOOR_CELLS (12) at the narrowest wall; the plan aims above it so
      a measured reading's noise never puts a planned mesh under the floor. A wide chamber gets
      large cells and a narrow branch small ones - the sizing VMTK is built round, for any shape.
NG-2  CEILING AND FLOOR. No cell is larger than the widest opening over CEILING_DIVISIONS (the
      far end of a plenum still resolves its inlet jet) and none is smaller than the narrowest size
      the field asks for; Netgen grades between them at GRADING.
NG-3  LAYERS. The layers the request states (else DEFAULT_LAYERS when wall layers are on), growing
      by GROWTH, the whole stack LAYER_STACK of the cell size at the narrowest wall (5th percentile
      by area): thick enough to carry the near-wall gradient, thin enough never to fill a narrow
      branch. One stack for the whole wall - Netgen grows one thickness - so it is sized where the
      passage is narrowest.
NG-4  THE BUDGET. The estimate (tetrahedra from the volume at the planned sizes, prisms from the wall
      faces times the layers) is held under SOFT_BUDGET by coarsening the cells-across target - never
      below the floor (CELLS_ACROSS_MIN) for it: the floor is the bar, the budget is not. Only past
      the cell limit is everything coarsened together, below the floor; the plan says what it gave
      up.
NG-5  THE LADDER. The native run tries the planned stack, then a stack half as thick, then no
      layers - the last two only when the one before produced no valid mesh, and each is recorded:
      a layer-free mesh is delivered with that shortfall measured and stated, never silently.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SPEC_FILE = "netgen_spec.json"
SIZE_FILE = "netgen_size.txt"
#: NG-1: the cells the plan puts across the local passage (the gate's floor is 12)
CELLS_ACROSS = 16.0
#: Netgen's surface edges come out this much longer than the size asked at a point (measured on the
#: delivered walls of the lab cases: the carotid's 0.187 mm asked read 0.245 mm, 1.31x), so the size
#: handed to Netgen is the planned edge over it
EDGE_PER_H = 1.35
#: NG-4: the plan never coarsens below the gate's floor plus this margin
CELLS_ACROSS_MIN = 12.5
#: NG-4: the size the plan aims under before it gives up cells across (never below the floor for
#: it): a tetrahedral fill's memory and single-thread time grow with its cells (an 8M-cell plan
#: ran a 20 GB lab slot out of memory), and the floor is the bar, not the budget
SOFT_BUDGET = 4_000_000
#: NG-2
CEILING_DIVISIONS = 8.0
GRADING = 0.3
#: NG-3
DEFAULT_LAYERS = 5
GROWTH = 1.2
LAYER_STACK = 0.6
#: a size file of at most this many wall points (Netgen reads it point by point)
SIZE_POINTS_MAX = 40_000
#: cells per unit volume of a Netgen fill at edge length h, measured on the lab cases: ~ c / h^3
TET_DENSITY = 7.5
#: wall triangles per unit area at edge length h: ~ c / h^2 (equilateral: 2.31)
TRI_DENSITY = 2.6


def _area_percentile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    o = np.argsort(values)
    c = np.cumsum(weights[o])
    if not len(c) or c[-1] <= 0:
        return float(np.median(values)) if len(values) else 0.0
    return float(values[o][min(len(o) - 1, int(np.searchsorted(c, q / 100.0 * c[-1])))])


def _point_areas(pts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    a = 0.5 * np.linalg.norm(np.cross(pts[faces[:, 1]] - pts[faces[:, 0]],
                                      pts[faces[:, 2]] - pts[faces[:, 0]]), axis=1)
    out = np.zeros(len(pts))
    for k in range(3):
        np.add.at(out, faces[:, k], a / 3.0)
    return out


def closed_volume(tris: np.ndarray) -> float:
    """The volume a closed triangle surface bounds (divergence theorem), its sign dropped."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)


def estimate_cells(volume: float, wall_area: float, h_typ: float, layers: int) -> int:
    tets = TET_DENSITY * volume / max(h_typ, 1e-12) ** 3
    prisms = layers * TRI_DENSITY * wall_area / max(h_typ, 1e-12) ** 2
    return int(tets + prisms)


def make_plan(*, wall_points: np.ndarray, wall_faces: np.ndarray, radius: np.ndarray,
              port_diameters: list[float], volume: float, wall_area: float, walls: list[str],
              layers: int, cell_limit: int) -> dict:
    """The plan (NG-1..NG-5) from the measured field; pure, so tests pin every rule."""
    pts = np.asarray(wall_points, float)
    r = np.asarray(radius, float)
    areas = _point_areas(pts, np.asarray(wall_faces, np.int64))
    d_max = max(port_diameters) if port_diameters else 2.0 * float(np.max(r))
    ceiling = d_max / CEILING_DIVISIONS
    across = CELLS_ACROSS
    notes: list[str] = []

    def sizes(n_across: float, scale: float = 1.0) -> np.ndarray:
        return np.minimum(2.0 * r / (n_across * EDGE_PER_H), ceiling) * scale

    h = sizes(across)
    h_typ = _area_percentile(h, areas, 50.0)
    est = estimate_cells(volume, wall_area, h_typ, layers)
    scale = 1.0
    soft = min(cell_limit, SOFT_BUDGET)
    if est > soft:
        # NG-4: first toward the floor across the passage (to the soft budget), then - only past the
        # cell limit - everything together
        for _ in range(8):
            if est <= soft or across <= CELLS_ACROSS_MIN:
                break
            across = max(CELLS_ACROSS_MIN, across * (est / soft) ** (-1.0 / 3.0) / 1.01)
            h = sizes(across)
            h_typ = _area_percentile(h, areas, 50.0)
            est = estimate_cells(volume, wall_area, h_typ, layers)
        if est > cell_limit:
            # the estimate is part volume (h^-3), part wall (h^-2): a few steps settle it
            for _ in range(8):
                if est <= cell_limit:
                    break
                scale *= (est / cell_limit) ** (1.0 / 3.0) * 1.01
                h = sizes(across, scale)
                h_typ = _area_percentile(h, areas, 50.0)
                est = estimate_cells(volume, wall_area, h_typ, layers)
            notes.append(f"the cell limit ({cell_limit:,}) coarsens the narrowest passages to "
                         f"about {across / scale:.1f} cells across, under the {CELLS_ACROSS_MIN:g} "
                         "the plan aims for")
        else:
            notes.append(f"the plan's budget ({soft:,} cells) lowers it to {across:.1f} cells "
                         "across the local passage")
    h_narrow = _area_percentile(h, areas, 5.0)
    h_min = float(np.min(h))
    stack = LAYER_STACK * h_narrow
    first = stack / sum(GROWTH ** k for k in range(max(layers, 1))) if layers > 0 else 0.0
    maxh = float(min(ceiling * scale, np.max(h)))
    rungs = []
    if layers > 0:
        rungs.append({"name": "planned layers", "maxh": maxh, "layers": layers, "first_layer": first,
                      "growth": GROWTH})
        # Netgen's growth limiter stops a stack it finds running into another part of the wall; the
        # second rung is thinner and lets the layers grow unlimited (the checkMesh after it judges)
        rungs.append({"name": "layers half as thick", "maxh": maxh, "layers": layers,
                      "first_layer": first / 2.0, "growth": GROWTH, "limit_growth": False})
    rungs.append({"name": "no layers", "maxh": maxh, "layers": 0})
    return {
        "rule": "NG-1..NG-5", "cells_across": round(across, 2), "scale": round(scale, 4),
        "ceiling_m": ceiling, "h_typ_m": h_typ, "h_narrow_m": h_narrow, "h_min_m": h_min,
        "maxh_m": maxh, "min_edge": h_min * 0.05, "grading": GRADING,
        "layers": layers, "first_layer_m": first, "growth": GROWTH, "layer_stack_m": stack,
        "walls": walls, "estimated_cells": est, "cell_limit": cell_limit, "notes": notes,
        "volume_m3": volume, "wall_area_m2": wall_area, "rungs": rungs,
        "sizes": h,
    }


def write_plan(ws: Path, plan: dict, wall_points: np.ndarray) -> dict:
    """netgen_spec.json (everything but the per-point sizes) and the size file (a bounded, evenly
    thinned sample of the wall points and their sizes)."""
    h = np.asarray(plan.pop("sizes"), float)
    pts = np.asarray(wall_points, float)
    if len(pts) > SIZE_POINTS_MAX:
        keep = np.linspace(0, len(pts) - 1, SIZE_POINTS_MAX).astype(np.int64)
        pts, h = pts[keep], h[keep]
    with open(ws / SIZE_FILE, "w", encoding="utf-8") as fh:
        fh.write(f"{len(pts)}\n")
        for p, s in zip(pts, h):
            fh.write(f"{p[0]:.10g} {p[1]:.10g} {p[2]:.10g} {s:.10g}\n")
        fh.write("0\n")
    plan["size_points"] = int(len(pts))
    (ws / SPEC_FILE).write_text(json.dumps(plan, indent=1, default=float))
    return plan


def plan_words(plan: dict) -> str:
    """The plan in one plain sentence for the run's record."""
    n = int(plan.get("layers") or 0)
    lay = (f"{n} prism layers, {plan['layer_stack_m'] * 1e3:.3g} mm thick" if n else "no prism layers")
    s = (f"Netgen: about {plan['cells_across']:g} cells across every passage (cells "
         f"{plan['h_narrow_m'] * 1e3:.3g}-{plan['maxh_m'] * 1e3:.3g} mm), {lay}, "
         f"about {int(plan['estimated_cells']):,} cells")
    if plan.get("notes"):
        s += "; " + "; ".join(plan["notes"])
    return s + "."


def requested_layers(ws: Path, engine_params: dict | None) -> int:
    """NG-3: the count the request states, else DEFAULT_LAYERS unless wall layers are off."""
    from meshpipeline.engines.layer_census import stated_layers
    from meshpipeline.engines.workspace_facts import read_request_txt
    if str((engine_params or {}).get("wall_layers") or "").lower() == "off":
        return 0
    stated = stated_layers(read_request_txt(ws))
    return int(stated) if stated else DEFAULT_LAYERS


def is_finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
