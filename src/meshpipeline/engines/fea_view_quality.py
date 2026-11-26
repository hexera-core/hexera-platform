# Responsibility: The per-element quality an FEA result's viewer colours its surface with - a solid's tetrahedra (aspect ratio, scaled Jacobian, angle between faces) and a shell's plate elements (aspect ratio, warp, skew, corner angles, Jacobian, taper) - packed in the viewer's one heatmap form (render/face_quality's `quality_fields`).
# Boundaries: measurement and packing only. It decides nothing about the mesh: the figures are engines/fea_quality's and engines/shell_quality's own, the limits the ones the result and the verdict read (the user's own limit where they typed one), and the counts the result's own. Nothing that goes wrong here may cost the user the mesh: every failure is logged and the view ships without a heatmap.
# Collaborates with: engines/gmsh/spec.py (the FEA viewer hook attaches it), engines/gmsh/viewer_surface.read_msh (the surface and the tetrahedra), ui/js/viewer/viewer.js (draws it - the same control as a flow mesh's).
"""THE FEA HEATMAP, ONE VALUE PER DRAWN ELEMENT.

* A SOLID: every surface triangle carries the figure of the tetrahedron behind it; where two
  tetrahedra share it (a face between two bonded solids) the worse of the two. Bad elements inside the
  volume, which no surface triangle shows, ship as `hotspots` (the worst MAX_HOTSPOTS_PER_METRIC per
  measure, by how far past the limit) and the viewer makes a problem area of each.
* A SHELL: every drawn polygon is one element (a quad is one cell), carrying its own figure. A figure an
  element shape does not have (a triangle's warp or taper) ships as NaN: the viewer leaves it calm.
* WHAT SHIPS: one float32 per drawn face per measure - never one per element. A 10-million-tetrahedron
  mesh draws about a million surface triangles: 3 measures x 4 bytes x 1M = 12 MB, a third of the
  surface's own coordinates. The whole-mesh pass that finds the interior hot spots runs only up to
  MAX_ELEMENTS_MEASURED; past it the surface's own elements are measured and the counts are the
  result's.
* THE LIMIT: the user's own (a typed or saved limit on that measure, as the result's row reads it),
  else the verdict's. `low` marks a measure where lower is worse (the viewer turns its scale over);
  `best` is a perfect element's value. `n_over` is the result's own count of elements past it.
"""
from __future__ import annotations

import base64
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: the whole-mesh pass (interior hot spots) runs up to this many elements: ~10 us an element in
#: fea_quality.element_measures, so about a minute of the worker at the cap
MAX_ELEMENTS_MEASURED = 5_000_000
MAX_HOTSPOTS_PER_METRIC = 60
_CHUNK = 50_000
#: a regular tetrahedron's dihedral angle, the best the smallest one can be
PERFECT_DIHEDRAL = math.degrees(math.acos(1.0 / 3.0))
#: a tetrahedron's faces by corner (engines/gmsh/abaqus_deck.TET_FACE_CORNERS)
_TET_FACES = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))
_TRI = np.dtype([("a", "<i8"), ("b", "<i8"), ("c", "<i8")])
#: who the limit is from, as the result says it
FROM_YOU = "your limit"
FROM_RESULT = "the result's limit"


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype="<f4").tobytes()).decode("ascii")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def user_limit(quality: Mapping | None, measure: str, op: str | None = None) -> Mapping | None:
    """The result's row of the user's own limit on `measure` (a `limit:<measure>` requirement row that
    was applied and measured), or None. `op`: only a row in that direction (a skew can be limited either
    way; a row that does not say its direction is taken only when the measure has one)."""
    rows = (((quality or {}).get("requirements") or {}).get("rows")) or []
    for r in rows:
        if not (isinstance(r, Mapping) and r.get("field") == f"limit:{measure}"
                and r.get("applied") is not False and _num(r.get("limit_target")) is not None):
            continue
        if op is not None and r.get("op") not in (None, op):
            continue
        return r
    return None


def _metric(label: str, unit: str, limit: float, values: np.ndarray, n_over: int, n: int, *,
            low: bool, best: float | None = None, floor: float | None = None, source: str) -> dict:
    v = values[np.isfinite(values)]
    out: dict = {"label": label, "unit": unit, "limit": float(limit), "n_over": int(n_over),
                 "n_faces": int(n), "from": source}
    if low:
        out.update(low=True, best=float(best if best is not None else 0.0),
                   min=float(v.min()) if len(v) else float(limit))
    else:
        out["max"] = float(v.max()) if len(v) else 0.0
        if floor is not None:
            out["floor"] = float(floor)
    return out


#: the value a degenerate element's endless figure (an aspect ratio of a flat tetrahedron) ships as: JSON holds
#: no infinity, and the element must still read as the worst there is
ENDLESS = 1.0e30


def _hot(metric: str, values: np.ndarray, centres: np.ndarray, limit: float, low: bool, best: float) -> list[dict]:
    values = np.nan_to_num(np.asarray(values, dtype=np.float64), nan=best if low else 0.0,
                           posinf=ENDLESS, neginf=-ENDLESS)
    past = np.nonzero(values < limit if low else values > limit)[0]
    if not len(past):
        return []
    excess = ((best - values[past]) / max(best - limit, 1e-12)) if low else values[past] / max(limit, 1e-12)
    pick = past[np.argsort(-excess, kind="stable")][:MAX_HOTSPOTS_PER_METRIC]
    return [{"metric": metric, "value": float(values[i]), "x": float(centres[i, 0]), "y": float(centres[i, 1]),
             "z": float(centres[i, 2]), "patch": None} for i in pick]


# ------------------------------------------------------------------------------- the solid ----

def _keys(rows: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.sort(np.asarray(rows, dtype=np.int64)[:, :3], axis=1)).view(_TRI).ravel()


def behind(tets: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(first, second): the tetrahedron behind every triangle of `tris` (point indices), and the one on its
    other side where it is shared (-1 where none; -1 in `first` too for a triangle no tetrahedron has).
    Only the tetrahedra with three corners on the drawn surface are looked at, so a big mesh's interior
    costs one boolean pass."""
    tets4 = np.asarray(tets, dtype=np.int64)[:, :4]
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    first = np.full(len(tris), -1, dtype=np.int64)
    second = first.copy()
    if not len(tris) or not len(tets4):
        return first, second
    on = np.zeros(int(max(tets4.max(), tris.max())) + 1, dtype=bool)
    on[tris.ravel()] = True
    cand = np.nonzero(on[tets4].sum(axis=1) >= 3)[0]
    fk = _keys(tets4[cand][:, list(_TET_FACES)].reshape(-1, 3))
    owner = np.repeat(cand, 4)
    order = np.lexsort((fk["c"], fk["b"], fk["a"]))
    fk, owner = fk[order], owner[order]
    q = _keys(tris)
    pos = np.searchsorted(fk, q, side="left")
    ok = pos < len(fk)
    ok[ok] = fk[pos[ok]] == q[ok]
    first[ok] = owner[pos[ok]]
    nxt = pos + 1
    two = ok & (nxt < len(fk))
    two[two] = fk[nxt[two]] == q[two]
    second[two] = owner[nxt[two]]
    return first, second


def _solid_specs(quality: Mapping, order: int) -> list[dict]:
    """The three measures the map offers on tetrahedra: the figure of fea_quality.element_measures each
    reads, its limit, and the result's count past it."""
    from meshpipeline.engines.fea_quality import limits_for

    lim = {k: v for k, _l, _o, v, _s in limits_for(order)}
    fea = quality.get("fea") if isinstance(quality.get("fea"), Mapping) else {}
    shape = (fea or {}).get("quality") if isinstance((fea or {}).get("quality"), Mapping) else {}
    over = (shape or {}).get("over_limit") or quality.get("over_limit") or {}
    specs = []
    asked = user_limit(quality, "aspect_ratio")
    # the user's aspect-ratio limit is measured as longest edge over shortest (engines/fea_requirements):
    # the map then reads that figure, so its count is their row's count
    specs.append({"key": "aspect_ratio", "label": "aspect ratio", "unit": "", "low": False, "floor": 1.0,
                  "figure": "edge_ratio" if asked else "aspect",
                  "limit": float(asked["limit_target"]) if asked else lim["fea_max_aspect_ratio"],
                  "n_over": (asked.get("outside") if asked else over.get("fea_max_aspect_ratio")),
                  "source": FROM_YOU if asked else FROM_RESULT})
    specs.append({"key": "scaled_jacobian", "label": "scaled Jacobian", "unit": "", "low": True, "best": 1.0,
                  "figure": "scaled_jacobian", "limit": lim["fea_min_scaled_jacobian"],
                  "n_over": over.get("fea_min_scaled_jacobian"), "source": FROM_RESULT})
    specs.append({"key": "min_dihedral", "label": "angle between faces", "unit": "°", "low": True,
                  "best": PERFECT_DIHEDRAL, "figure": "min_dihedral", "limit": lim["fea_min_dihedral_deg"],
                  "n_over": over.get("fea_min_dihedral_deg"), "source": FROM_RESULT})
    return specs


def solid_fields(points, tets: Sequence[np.ndarray] | np.ndarray, patches: Sequence[tuple[str, Sequence]],
                 quality: Mapping | None, patch_names: Sequence[str] | None = None) -> dict | None:
    """`quality_fields` for an FEA solid: `points` (P, 3), `tets` the tetrahedra (E, 4|10) in solver order
    (or a list of such blocks), `patches` the drawn surface [(name, triangles as point indices)] in drawn
    order. None when there is nothing to colour."""
    from meshpipeline.engines.fea_quality import element_measures

    blocks = [np.asarray(b, dtype=np.int64) for b in (tets if isinstance(tets, (list, tuple)) else [tets]) if len(b)]
    if not blocks:
        return None
    # one block of one order (a mixed-order mesh is measured at its corners)
    T = blocks[0] if len(blocks) == 1 else (
        np.concatenate(blocks) if len({b.shape[1] for b in blocks}) == 1 else np.concatenate([b[:, :4] for b in blocks]))
    P = np.asarray(points, dtype=np.float64)
    q = quality or {}
    specs = _solid_specs(q, 2 if T.shape[1] == 10 else 1)
    wanted = set(patch_names) if patch_names else None
    drawn = [(n, np.asarray(polys, dtype=np.int64).reshape(-1, 3)) for n, polys in patches
             if polys and (wanted is None or n in wanted) and all(len(p) == 3 for p in polys)]
    if not drawn:
        return None
    tris = np.concatenate([t for _n, t in drawn])
    first, second = behind(T, tris)
    used = np.unique(np.concatenate([first[first >= 0], second[second >= 0]]))
    if not len(used):
        return None
    m = element_measures(P[T[used]])
    at = np.full(len(T), -1, dtype=np.int64)
    at[used] = np.arange(len(used))
    per_face: dict[str, np.ndarray] = {}
    for s in specs:
        vals = np.asarray(m[s["figure"]], dtype=np.float64)
        a = np.where(first >= 0, vals[at[np.maximum(first, 0)]], np.nan)
        b = np.where(second >= 0, vals[at[np.maximum(second, 0)]], np.nan)
        # the worse of the two sides
        per_face[s["key"]] = np.fmin(a, b) if s["low"] else np.fmax(a, b)
    # THE WHOLE MESH: the interior's worst elements as hot spots, and the counts where the result has none
    hot: list[dict] = []
    counted: dict[str, int] = {}
    if len(T) <= MAX_ELEMENTS_MEASURED:
        cand: dict[str, list] = {s["key"]: [] for s in specs}
        for s0 in range(0, len(T), _CHUNK):
            X = P[T[s0:s0 + _CHUNK]]
            mm = element_measures(X)
            cen = X[:, :4].mean(axis=1)
            for s in specs:
                v = np.asarray(mm[s["figure"]], dtype=np.float64)
                past = v < s["limit"] if s["low"] else v > s["limit"]
                counted[s["key"]] = counted.get(s["key"], 0) + int(np.count_nonzero(past))
                cand[s["key"]].extend(_hot(s["key"], v, cen, s["limit"], s["low"], s.get("best", 0.0)))
        for s in specs:
            rows = cand[s["key"]]
            v = np.array([h["value"] for h in rows])
            if len(rows):
                c = np.array([[h["x"], h["y"], h["z"]] for h in rows])
                hot += _hot(s["key"], v, c, s["limit"], s["low"], s.get("best", 0.0))
    metrics = {}
    for s in specs:
        n_over = s["n_over"] if isinstance(s["n_over"], int) and not isinstance(s["n_over"], bool) else counted.get(
            s["key"], int(np.count_nonzero(per_face[s["key"]] < s["limit"] if s["low"] else per_face[s["key"]] > s["limit"])))
        whole = per_face[s["key"]]
        if s["low"]:
            worst_hot = [h["value"] for h in hot if h["metric"] == s["key"]]
            whole = np.concatenate([whole, np.asarray(worst_hot, dtype=np.float64)])
        else:
            whole = np.concatenate([whole, np.asarray([h["value"] for h in hot if h["metric"] == s["key"]])])
        metrics[s["key"]] = _metric(s["label"], s["unit"], s["limit"], whole, n_over, len(T), low=s["low"],
                                    best=s.get("best"), floor=s.get("floor"), source=s["source"])
    out_patches: dict[str, dict] = {}
    at0 = 0
    for name, t in drawn:
        n = len(t)
        out_patches[name] = {**{f"{s['key']}_b64": _b64(per_face[s["key"]][at0:at0 + n]) for s in specs}, "count": n}
        at0 += n
    logger.info("FEA viewer quality fields: %d tetrahedra, %d surface triangles, %d hot spot(s)",
                len(T), len(tris), len(hot))
    return {"basis": "element_behind", "noun": "element", "metrics": metrics, "patches": out_patches,
            "hotspots": hot}


# ------------------------------------------------------------------------------- the shell ----

#: (key, label, unit, quad figure, triangle figure, verdict figure of shell_quality.LIMITS, low, best, floor,
#: requirement measure, its op) - the shell's measures as the result names them (engines/shell_delivery.LABELS)
_SHELL: tuple[tuple[Any, ...], ...] = (
    ("aspect_ratio", "aspect ratio", "", "aspect", "aspect", "aspect", False, None, 1.0, "aspect_ratio", "<="),
    ("warp", "warp", "°", "warp_deg", None, "warp_deg", False, None, 0.0, "warpage", "<="),
    ("skew", "skew", "°", "skew_deg", "skew_deg", "skew_deg", True, 90.0, None, "skew", ">="),
    ("min_angle", "smallest corner angle", "°", "min_angle_quad", "min_angle_tria", "min_angle", True, None, None,
     "min_angle", ">="),
    ("max_angle", "largest corner angle", "°", "max_angle_quad", "max_angle_tria", "max_angle", False, None, None,
     "max_angle", "<="),
    ("jacobian", "Jacobian", "", "jacobian_ratio", None, "jacobian_ratio", True, 1.0, None, "jacobian", ">="),
    ("taper", "taper", "", "taper", None, "taper", False, None, 0.0, "taper", "<="),
)


def shell_fields(points, patches: Sequence[tuple[str, Sequence]], quality: Mapping | None,
                 patch_names: Sequence[str] | None = None, shell_quality: Mapping | None = None) -> dict | None:
    """`quality_fields` for a delivered shell: `patches` the drawn elements [(name, polygons of 3 or 4 point
    indices)] in drawn order - each polygon one element. `shell_quality`: the measured record the result
    reads (quality.json's `shell_quality` when not given)."""
    from meshpipeline.engines.shell_quality import LIMITS, quad_measures, tria_measures

    P = np.asarray(points, dtype=np.float64)
    q = quality or {}
    sq = shell_quality if isinstance(shell_quality, Mapping) else (q.get("shell_quality") or {})
    past = sq.get("past_limits") or {}
    verdict = {k: (op, lim) for k, op, lim, _s in LIMITS}
    wanted = set(patch_names) if patch_names else None
    drawn = [(n, list(polys)) for n, polys in patches if polys and (wanted is None or n in wanted)]
    if not drawn:
        return None
    polys = [p for _n, ps in drawn for p in ps]
    sizes = np.fromiter((len(p) for p in polys), dtype=np.int64, count=len(polys))
    is_q, is_t = sizes == 4, sizes == 3
    quads = np.asarray([p for p in polys if len(p) == 4], dtype=np.int64).reshape(-1, 4)
    trias = np.asarray([p for p in polys if len(p) == 3], dtype=np.int64).reshape(-1, 3)
    asks = {spec[0]: user_limit(q, spec[9], spec[10]) for spec in _SHELL}
    extra = any(asks.values())
    qm = quad_measures(P, quads, extra=extra) if len(quads) else {}
    tm = tria_measures(P, trias, extra=extra) if len(trias) else {}
    n = len(polys)
    metrics: dict[str, dict] = {}
    per_face: dict[str, np.ndarray] = {}
    for key, label, unit, qk, tk, vk, low, best, floor, measure, op in _SHELL:
        asked = asks[key]
        corner = vk in ("min_angle", "max_angle")
        # the corner angles: a quad and a triangle have their own limits; one map line holds one, so the map
        # reads the quads' (the triangles' figures are the result's own rows) - unless the user's limit holds
        # both shapes, or the shell has no quads
        use_t = tk is not None and (not corner or asked is not None or not len(quads))
        use_q = qk is not None and len(quads) > 0
        if corner:
            vk = (vk + "_quad") if len(quads) else (vk + "_tria")
        if asked is not None:
            from meshpipeline.engines.shell_requirements import figures_of
            fig = figures_of({"measure": measure, "op": op, "shape": asked.get("shape") or "any"})
            qk = fig.get("quad") if use_q else None
            tk = fig.get("tria") if use_t else None
            limit = float(asked["limit_target"])
            n_over = asked.get("outside")
            source = FROM_YOU
        else:
            if vk not in verdict:
                continue
            limit = verdict[vk][1]
            n_over = past.get(vk)
            source = FROM_RESULT
            if not use_t:
                tk = None
        vals = np.full(n, np.nan)
        if use_q and qk and qk in qm:
            vals[is_q] = qm[qk]
        if tk and tk in tm:
            vals[is_t] = tm[tk]
        if not np.isfinite(vals).any():
            continue
        if corner:
            ideal = 90.0 if len(quads) else 60.0
            best, floor = (ideal, None) if low else (None, ideal)
        if not isinstance(n_over, int) or isinstance(n_over, bool):
            n_over = int(np.count_nonzero(vals < limit if low else vals > limit))
        per_face[key] = vals
        metrics[key] = _metric(label, unit, limit, vals, n_over, n, low=low, best=best, floor=floor, source=source)
    if not metrics:
        return None
    out_patches: dict[str, dict] = {}
    at = 0
    for name, ps in drawn:
        k = len(ps)
        out_patches[name] = {**{f"{key}_b64": _b64(v[at:at + k]) for key, v in per_face.items()}, "count": k}
        at += k
    return {"basis": "element", "noun": "element", "metrics": metrics, "patches": out_patches, "hotspots": []}


# ------------------------------------------------------------------------------- the hook ----

def fields_for(mesh: Mapping | None, workspace, *, shell: bool, names: Sequence[str] | None = None) -> dict | None:
    """The fields of a mesh in read_msh's by-face form (a solid needs its `tets`), against the workspace's
    quality.json; None when there is nothing to colour."""
    if not mesh or not mesh.get("patches"):
        return None
    import json
    from pathlib import Path

    try:
        quality = json.loads((Path(workspace) / "quality.json").read_text())
    except (OSError, ValueError):
        quality = {}
    if shell:
        return shell_fields(mesh["points"], mesh["patches"], quality, names)
    if not mesh.get("tets"):
        return None
    return solid_fields(mesh["points"], mesh["tets"], mesh["patches"], quality, names)


def _centres(points: np.ndarray, polys: Sequence) -> np.ndarray:
    return np.asarray([points[np.asarray(p, dtype=np.int64)].mean(axis=0) for p in polys], dtype=np.float64)


def remap(fields: Mapping, full: Mapping, shown: Mapping) -> dict:
    """`fields` of the `full` surface carried onto a thinned copy of it (`shown`, engines/gmsh/viewer_surface
    .downsample): every drawn triangle takes the WORST value of the full surface's elements nearest it, so a
    bad element is never thinned out of sight - the stand-in view of a mesh kept in the bucket."""
    from scipy.spatial import cKDTree

    full_p = np.asarray(full["points"], dtype=np.float64)
    shown_p = np.asarray(shown["points"], dtype=np.float64)
    full_by = dict(full["patches"])
    metrics = fields.get("metrics") or {}
    out_patches: dict[str, dict] = {}
    for name, polys in shown["patches"]:
        src = (fields.get("patches") or {}).get(name)
        fpolys = full_by.get(name)
        if not src or not fpolys or not polys:
            continue
        near = cKDTree(_centres(shown_p, polys)).query(_centres(full_p, fpolys))[1]
        row: dict = {"count": len(polys)}
        for key, md in metrics.items():
            b = src.get(f"{key}_b64")
            if not b:
                continue
            vals = np.frombuffer(base64.b64decode(b), dtype="<f4").astype(np.float64)
            got = np.full(len(polys), np.nan)
            (np.fmin if md.get("low") else np.fmax).at(got, near, vals)
            row[f"{key}_b64"] = _b64(got)
        out_patches[name] = row
    return {**fields, "patches": out_patches, "thinned": True}


def attach(resp: dict, mesh: Mapping | None, workspace, *, shell: bool) -> None:
    """Add `quality_fields` to an FEA viewer response, or leave it untouched (a heatmap is never worth the
    mesh: every failure is logged and swallowed). A mesh kept in the bucket brings the fields its stand-in was
    written with (engines/bucket_files.write_stand_ins)."""
    try:
        if not resp or not mesh or not mesh.get("patches"):
            return
        fields = mesh.get("quality_fields")
        if not fields:
            names = [str(p["name"]) for p in (resp.get("patches") or []) if isinstance(p, dict) and p.get("name")]
            fields = fields_for(mesh, workspace, shell=shell, names=names)
        if fields:
            resp["quality_fields"] = fields
    except Exception:  # noqa: BLE001 - a heatmap is never worth a delivery
        logger.exception("FEA viewer quality fields unavailable - the mesh ships without a heatmap")


__all__ = ["MAX_ELEMENTS_MEASURED", "MAX_HOTSPOTS_PER_METRIC", "PERFECT_DIHEDRAL", "attach", "behind",
           "fields_for", "remap", "shell_fields", "solid_fields", "user_limit"]
