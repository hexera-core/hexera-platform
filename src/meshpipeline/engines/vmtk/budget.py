# Responsibility: Size a VMTK fill to its cell budget: predict the tetrahedra a staged lumen will
#                 fill to, and choose the volume element factor, the edge factor and (only to stay
#                 under the compute limit) the layer count that keep it inside the budget.
# Boundaries: pure arithmetic on facts measured at staging (lumen_staging.stage_lumen) and on the
#             remeshed wall; it runs nothing native and knows no file names.
from __future__ import annotations

import numpy as np

#: The edge factor the cost model is measured at; every other factor scales from it.
ELF_REF = 0.15

#: vmtk's surface remesh puts about this many triangles on the wall per (area / h^2), where h is
#: the target edge (edge factor x local radius). Measured on the remeshed walls of nine lab fills
#: (bends, tees, a manifold, an S-duct, reducers, a transition, a venturi, an aorta): 3.29-3.33.
WALL_TRIANGLES_PER_AREA = 3.3

#: Interior tetrahedra = V x mean(1 / h_in^3) x (VOLUME_FACTOR_DEFAULT / s)^3 / INTERIOR_FILL,
#: with h_in = edge factor x max(distance to the wall, local radius there) at points sampled
#: inside the staged fluid. Measured 0.039-0.050 on eight of those fills (the human aorta 0.046);
#: a manifold whose small branches meet a wide header read 0.082 (it fills COARSER than this
#: predicts), so the low end is taken: the prediction errs on the side of the budget.
INTERIOR_FILL = 0.042

#: vmtkmeshgenerator's own VolumeElementScaleFactor: the interior target is this times the square
#: root of the mean wall-triangle area around each wall point - about 0.44 of the wall edge, so the
#: core of every passage is about twice as fine as the wall (some 30 cells across).
VOLUME_FACTOR_DEFAULT = 0.8
#: The coarsest interior the budget may ask for: at 1.4 the interior edge is about 0.9 of the
#: wall's, some 14 cells across the core - above the 12 the passage floor requires.
VOLUME_FACTOR_MAX = 1.4

#: A layer stack splits every prism of every sublayer into three tetrahedra.
TETS_PER_LAYER_TRIANGLE = 3

#: The share of a budget a prediction may plan to use: the model is a model.
BUDGET_MARGIN = 0.85

#: Fewest layers a budget may cut a requested stack to before it gives the layers up entirely:
#: only the COMPUTE LIMIT ever cuts layers (a requested budget never does - see fit).
MIN_LAYERS = 2


def edge_factor_ceiling(min_cells_across: float) -> float:
    """The coarsest edge factor that keeps `min_cells_across` cells across every passage (about
    2 / factor cells across a diameter), with half a cell of headroom."""
    return 2.0 / (float(min_cells_across) + 0.5)


def cost_model(points: np.ndarray, faces: np.ndarray, radius: np.ndarray, closed_tris: np.ndarray,
               *, h_min: float, h_max: float, samples: int = 6000, seed: int = 7) -> dict:
    """The staged lumen's cost at ELF_REF: `wall` = sum of area / h^2 over the wall triangles, and
    `interior` = V x mean(1 / h_in^3) over points sampled inside the closed fluid boundary
    (`closed_tris`, n x 3 x 3: the wall plus its lids). Units cancel in predict()."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    r = np.asarray(radius, dtype=float)
    h = np.clip(ELF_REF * r, h_min, h_max)
    a = 0.5 * np.linalg.norm(np.cross(pts[f[:, 1]] - pts[f[:, 0]], pts[f[:, 2]] - pts[f[:, 0]]),
                             axis=1)
    ht = h[f].mean(axis=1)
    wall = float(np.sum(a / ht ** 2))
    inside, volume = _inside_samples(np.asarray(closed_tris, dtype=float), samples, seed)
    interior = 0.0
    if len(inside) and volume > 0.0:
        d, nn = cKDTree(pts).query(inside)
        h_in = np.clip(ELF_REF * np.maximum(d, r[nn]), h_min, h_max)
        interior = float(volume * np.mean(1.0 / h_in ** 3))
    return {"elf_ref": ELF_REF, "wall": wall, "interior": interior, "volume_m3": volume,
            "samples_inside": int(len(inside))}


def _inside_samples(tris: np.ndarray, want: int, seed: int) -> tuple[np.ndarray, float]:
    """Points spread uniformly inside a closed triangle surface, and the volume it encloses: the
    box times the share of uniform samples that land inside, so the lids may be wound either
    way. Seeded, so a run and its re-run plan the same mesh."""
    import pyvista as pv
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    flat = t.reshape(-1, 3)
    if len(t) < 4:
        return np.zeros((0, 3)), 0.0
    lo, hi = flat.min(axis=0), flat.max(axis=0)
    box = float(np.prod(np.maximum(hi - lo, 1e-12)))
    cells = np.hstack([np.full((len(t), 1), 3, dtype=np.int64),
                       np.arange(len(flat), dtype=np.int64).reshape(-1, 3)]).ravel()
    surf = pv.PolyData(flat, cells).clean()
    rng = np.random.default_rng(seed)
    got: list[np.ndarray] = []
    n_in = n_all = 0
    for _ in range(8):
        batch = 20_000 if not n_all else int(min(
            400_000, max(20_000, 1.5 * (want - n_in) * n_all / max(n_in, 1))))
        p = rng.uniform(lo, hi, size=(batch, 3))
        sel = pv.PolyData(p).select_enclosed_points(surf, tolerance=1e-9, check_surface=False)
        keep = p[np.asarray(sel.point_data["SelectedPoints"]).astype(bool)]
        got.append(keep)
        n_in += len(keep)
        n_all += batch
        if n_in >= want:
            break
    pts = np.concatenate(got) if got else np.zeros((0, 3))
    return pts[:want], (box * n_in / n_all if n_all else 0.0)


def predict(model: dict, *, edge_length_factor: float, volume_factor: float = VOLUME_FACTOR_DEFAULT,
            boundary_layers: int = 0, wall_triangles: float | None = None) -> dict:
    """Predicted tetrahedra: the layer stack (TETS_PER_LAYER_TRIANGLE per wall triangle per
    sublayer) and the interior fill. `wall_triangles`, when the remeshed wall is already known,
    replaces the model's own wall count and corrects the interior by the same ratio."""
    k = float(model.get("elf_ref") or ELF_REF) / float(edge_length_factor)
    wall_pred = WALL_TRIANGLES_PER_AREA * float(model["wall"]) * k ** 2
    interior = (float(model["interior"]) * k ** 3 / INTERIOR_FILL
                * (VOLUME_FACTOR_DEFAULT / float(volume_factor)) ** 3)
    wall = float(wall_pred)
    if wall_triangles and wall_pred > 0:
        wall = float(wall_triangles)
        interior *= (wall / wall_pred) ** 1.5
    layers = TETS_PER_LAYER_TRIANGLE * max(0, int(boundary_layers)) * wall
    return {"wall_triangles": int(round(wall)), "layer_tets": int(round(layers)),
            "interior_tets": int(round(interior)), "cells": int(round(layers + interior))}


def fit(strategy: dict, model: dict, *, requested: int, hard_limit: int, min_cells_across: float,
        wall_triangles: float | None = None, allow_edge_change: bool = True) -> tuple[dict, dict]:
    """The strategy sized into the budget, and the plan that says how. In order, and only as far
    as needed:
      1. the INTERIOR coarsens (volume factor up to VOLUME_FACTOR_MAX) - the user named no core
         resolution, and the wall, the layers and the passage floor are untouched;
      2. the EDGE FACTOR rises toward the passage floor (12 cells across), never past it;
      3. only past the COMPUTE LIMIT, the layer stack thins to MIN_LAYERS, then goes.
    A requested budget the floor and the requested layers cannot meet is NOT forced: the mesh is
    delivered at the floor, over the request but under the compute limit, and the plan says so."""
    s = dict(strategy)
    elf = float(s.get("edge_length_factor") or ELF_REF)
    vf = float(s.get("volume_element_factor") or VOLUME_FACTOR_DEFAULT)
    layers = int(s.get("boundary_layers") or 0)
    hard = int(hard_limit) * BUDGET_MARGIN
    target = min(int(requested or hard_limit), int(hard_limit)) * BUDGET_MARGIN

    def cost(e, v, n):
        return predict(model, edge_length_factor=e, volume_factor=v, boundary_layers=n,
                       wall_triangles=wall_triangles if e == elf else None)

    before = cost(elf, vf, layers)
    plan: dict = {"requested": int(requested or hard_limit), "hard_limit": int(hard_limit),
                  "predicted_before": before["cells"], "steps": []}
    if before["cells"] <= target:
        plan.update({"predicted": before["cells"], "volume_element_factor": vf,
                     "edge_length_factor": elf, "boundary_layers": layers, "within": "request"})
        return s, plan
    for goal, label in ((target, "request"), (hard, "compute limit")):
        # 1. the interior
        p = cost(elf, vf, layers)
        if p["cells"] > goal and vf < VOLUME_FACTOR_MAX:
            room = goal - p["layer_tets"]
            need = (VOLUME_FACTOR_MAX if room <= 0 else
                    vf * (p["interior_tets"] / room) ** (1.0 / 3.0))
            nv = min(VOLUME_FACTOR_MAX, max(vf, need))
            if nv > vf:
                plan["steps"].append(f"interior coarsened: volume factor {vf:g} -> {nv:.3g}")
                vf = nv
        # 2. the edge factor, to the floor
        ceiling = edge_factor_ceiling(min_cells_across)
        p = cost(elf, vf, layers)
        if p["cells"] > goal and allow_edge_change and elf < ceiling:
            lo, hi = elf, ceiling
            if cost(hi, vf, layers)["cells"] > goal:
                ne = hi
            else:
                for _ in range(30):
                    mid = 0.5 * (lo + hi)
                    if cost(mid, vf, layers)["cells"] > goal:
                        lo = mid
                    else:
                        hi = mid
                ne = hi
            plan["steps"].append(f"edge factor {elf:g} -> {ne:.4g} (floor: {min_cells_across:g} "
                                 "cells across)")
            elf = ne
        if label == "request":
            if cost(elf, vf, layers)["cells"] <= goal:
                break
            continue
        # 3. the compute limit alone may thin the layers
        while cost(elf, vf, layers)["cells"] > goal and layers > 0:
            nl = layers - 1 if layers > MIN_LAYERS else 0
            plan["steps"].append(f"layers {layers} -> {nl} to stay under the compute limit")
            layers = nl
    after = cost(elf, vf, layers)
    s["volume_element_factor"] = round(vf, 4)
    s["edge_length_factor"] = round(elf, 5)
    s["boundary_layers"] = layers
    # the margin is what the fit PLANS to; only a prediction past the limit itself is "over"
    # (the model errs high - the human aorta predicted 8.8M and filled 8.16M - so a fill planned
    # between the margin and the limit is still worth its run)
    within = ("request" if after["cells"] <= target else
              "compute limit" if after["cells"] <= int(hard_limit) else "over")
    plan.update({"predicted": after["cells"], "volume_element_factor": s["volume_element_factor"],
                 "edge_length_factor": s["edge_length_factor"], "boundary_layers": layers,
                 "within": within})
    return s, plan


def plan_words(plan: dict) -> str:
    """The budget plan in one sentence for the run's note."""
    if not plan.get("steps"):
        return ""
    head = (f"sized to the cell budget: about {plan['predicted_before']:,} cells predicted at the "
            f"requested settings, {plan['predicted']:,} after - " + "; ".join(plan["steps"]))
    if plan.get("within") == "compute limit":
        head += (f". The requested {plan['requested']:,} cells cannot hold the requested layers at "
                 "the passage floor, so the mesh is over the request but under the compute limit")
    elif plan.get("within") == "over":
        head += ". Even so the prediction is over the compute limit"
    return head
