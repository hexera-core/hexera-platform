# Responsibility: THE USER'S MESH REQUIREMENTS ON A SHELL MESH (SHMESH-24..26): turn the checked set read from their notes (engines/mesh_requirements) into what the shell mesher works with - the sizes of the plan (a global size, the shortest and longest edge, growth), the limits in force the tidy-up steps aim at (engines/shell_quality.InForce), the element kind - and, on the built mesh, say for every requirement what was asked and what was delivered: how many elements and what percent lie outside it.
# Boundaries: pure over plain data and numpy; no engine, no file, no model. Nothing here can fail a mesh: a requirement that cannot be met is aimed at, the mesh is delivered, and the row says by how much it was missed. With no requirement every function returns today's values (limits_in_force(None) is shell_quality.IN_FORCE; size_plan(rows, None) changes nothing).
# Collaborates with: engines/gmsh/shell_adapter.py (applies the sizing and the limits in force, measures the report), engines/shell_quality.py (the figures and the steps), engines/shell_run.py and engines/shell_delivery.py (the record and the result's rows), engines/mixed_run.py (the shell part of a mixed mesh).
"""HOW EACH FIGURE OF A USER'S LIMIT IS MEASURED when they name no tool - the common pre-processor definitions:

* warpage (degrees): the quad split along each diagonal, the angle between the two triangles' normals, the larger of
  the two (0 for a flat quad; a triangle has none).
* Jacobian: the smallest over the largest determinant of the Jacobian at the 2 x 2 Gauss points, in the element's
  mean plane (1 for a parallelogram; 1 for a flat 3-node triangle).
* aspect ratio: longest edge over shortest edge.
* corner angles (degrees): the interior angles at the corners, the smallest and the largest.
* skew (degrees): as a "most", 90 minus the angle between the lines joining opposite mid-sides (a triangle: a median
  and the mid-line across it) - 0 is ideal; as a "least", that angle itself (Nastran's reading, 90 is ideal).
* taper: the largest share by which a corner triangle departs from their mean.
* edge lengths (mm): each element's shortest and longest edge.
Percentages are of all the elements of the mesh.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines.mesh_requirements import MEASURES, _g

RULE_SIZES = "SHMESH-24"
RULE_LIMITS = "SHMESH-25"
RULE_REPORT = "SHMESH-26"

DEFINITIONS = ("Warpage: each quad split along each diagonal, the angle between the two triangles' normals, the "
               "larger of the two. Jacobian: the smallest over the largest determinant at the 2 x 2 Gauss points, in "
               "the element's mean plane. Aspect ratio: longest edge over shortest edge. Angles: the interior corner "
               "angles. Percentages are of all the elements of the mesh.")

#: SHMESH-24: with a global size G and no shortest / longest edge given, elements are "about G": curves and narrow
#: faces may take sizes down to BAND[0] x G and stretched strips up to BAND[1] x G (the usual reading of a global
#: size, and the band the typical criteria sheet states itself)
BAND = (0.5, 1.5)
#: SHMESH-24: gmsh's edges come out round the size it is given, not at it. With NO global size, the finest size
#: any curve or face is given is the user's shortest edge times MIN_MARGIN, and the coarsest their longest edge over
#: MAX_MARGIN. With a global size every face takes that size and the margins play no part (measured: the 20 lab
#: parts at 10 mm give byte-identical meshes at 1.0, 1.3 and 1.6). The two values are working values, not tuned on
#: a corpus yet
MIN_MARGIN = 1.3
MAX_MARGIN = 1.3
#: SHMESH-24: under the user's own sizes a curve keeps its shape, no more: a full turn (a hole's rim, a tube) gets at
#: least this many elements, whatever the shortest edge asked for - a hole is never closed to a slit; a face that
#: turns through no more than SHAPE_ONE_ACROSS radians (100 degrees: a bend) may have one element across it
SHAPE_PER_TURN = 4
SHAPE_ONE_ACROSS = 1.75
#: SHMESH-24: a mapped strip narrower than the user's size is stretched to their aspect ratio of its own width
STRIP_ASPECT = True
#: SHMESH-24: a mapped strip's elements are stretched along it to at most this share of the user's aspect target
ASPECT_SHARE = 0.9

#: requirement measure -> {shape: figure key of shell_quality.quad_measures / tria_measures (extra=True)}; a shape
#: left out has no such figure (a triangle has no warpage): it is inside the limit by nature
FIGURES: dict[tuple[str, str], dict[str, str]] = {
    ("aspect_ratio", "<="): {"quad": "aspect", "tria": "aspect"},
    ("warpage", "<="): {"quad": "warp_deg"},
    ("jacobian", ">="): {"quad": "jacobian"},
    ("skew", "<="): {"quad": "skew_pre", "tria": "skew_pre"},
    ("skew", ">="): {"quad": "skew_deg", "tria": "skew_deg"},
    ("taper", "<="): {"quad": "taper"},
    ("min_angle", ">="): {"quad": "min_angle_quad", "tria": "min_angle_tria"},
    ("max_angle", "<="): {"quad": "max_angle_quad", "tria": "max_angle_tria"},
}


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def _shapes(lim: Mapping) -> tuple[str, ...]:
    shape = str(lim.get("shape") or "any")
    return ("quad", "tria") if shape == "any" else ((shape,) if shape in ("quad", "tria") else ())


def figures_of(lim: Mapping) -> dict[str, str]:
    """{shape: figure key} a limit reads on a shell mesh; {} when it is not a measure of shell elements."""
    table = FIGURES.get((str(lim.get("measure")), str(lim.get("op"))), {})
    return {s: table[s] for s in _shapes(lim) if s in table}


def limits_in_force(req: Mapping | None) -> SQ.InForce:
    """SHMESH-25: the limits the tidy-up steps hold and aim at. The solver's own stay as they are; each limit of the
    user's becomes an aim (its target, its outer limit, its allowed share). No requirement: shell_quality.IN_FORCE,
    today's constants exactly."""
    aims = []
    for lim in (req or {}).get("limits") or []:
        for shape, key in sorted(figures_of(lim).items()):
            outer = lim.get("outer")
            aims.append((shape, key, str(lim["op"]), float(lim["target"]),
                         float(outer) if outer is not None else None, float(lim.get("share") or 0.0)))
    return SQ.InForce(aims=tuple(aims)) if aims else SQ.IN_FORCE


def kind_of(req: Mapping | None) -> str | None:
    """"triangles" (no quads are made), "quads" (no quad is cut to meet a user limit) or None."""
    k = (req or {}).get("kind") or {}
    if k.get("shell") in ("triangles", "quads"):
        return str(k["shell"])
    return "quads" if k.get("tria_percent_max") is not None else None


def curved_size(radius: float, width: float, *, floor: float, rule: float) -> float:
    """SHMESH-24: the element size on a face (or round an arc) of `radius` under the user's sizes (mm): the rules'
    own size `rule` (CURVE_PER_QUARTER a quarter-turn) but no finer than `floor` (the user's shortest edge, with
    its margin) - and never so coarse that the shape is lost: SHAPE_PER_TURN elements a full turn, or one element
    across a face that turns through no more than SHAPE_ONE_ACROSS (a bend, `width` its narrow way)."""
    if 0 < width <= SHAPE_ONE_ACROSS * radius:
        return max(rule, floor)          # a bend: narrower than the size, it gets one element across by itself
    return min(max(rule, floor), 2.0 * math.pi * radius / SHAPE_PER_TURN)


def size_plan(rows: list[dict], req: Mapping | None, areas_mm2: list[float], *, planned_max: float,
              curved: list[tuple[float | None, float]] | None = None) -> dict | None:
    """SHMESH-24: the plan's rows sized in place to the user's sizes; None (rows untouched) when the notes ask for no
    size and no aspect ratio. Returns {"global_mm", "low_mm", "high_mm", "floor_mm" (the finest size any curve is
    given), "growth", "stretch" (the most a mapped strip stretches), "planned", "rules_planned", "held" (the size
    asked for was over what a shell mesh is made of in the time allowed: stepped to the nearest that is),
    "coarser" / "finer" (than our accuracy rules; under half a wall)}. `curved`: per face, (its own radius of
    curvature or None, its narrow width), mm - a curved face keeps its shape (curved_size).

    The user's size beats our minimums - finer than half the wall, coarser than the rules - and is said; only what
    cannot be built (a plan past `planned_max` elements) is stepped to the nearest size that can."""
    s = (req or {}).get("sizes") or {}
    G, lo, hi, growth = (s.get(k) for k in ("global_mm", "min_edge_mm", "max_edge_mm", "growth"))
    aspect = min([float(lim["target"]) for lim in (req or {}).get("limits") or []
                  if lim.get("measure") == "aspect_ratio" and "quad" in _shapes(lim)], default=None)
    if G is None and lo is None and hi is None and growth is None and aspect is None:
        return None
    if G is not None:
        lo = BAND[0] * G if lo is None else lo
        hi = BAND[1] * G if hi is None else hi
    floor = MIN_MARGIN * lo if lo is not None else None
    if floor is not None and G is not None:
        floor = min(floor, G)
    rules_planned = sum(a / r["size_mm"] ** 2 for a, r in zip(areas_mm2, rows, strict=True))
    out: dict = {"global_mm": G, "low_mm": lo, "high_mm": hi, "floor_mm": _r(floor) if floor is not None else None,
                 "growth": growth, "stretch": ASPECT_SHARE * aspect if aspect is not None else None,
                 "rules_planned": int(round(rules_planned)), "held": False}
    if G is None and lo is None and hi is None:
        out["planned"] = out["rules_planned"]
        return out

    def sized(scale: float) -> tuple[float, bool, bool]:
        planned, coarser, finer = 0.0, False, False
        for k, (a, r) in enumerate(zip(areas_mm2, rows, strict=True)):
            rule = float(r["_rule_size"])
            size = G if G is not None else rule
            if G is None and hi is not None:
                size = min(size, hi / MAX_MARGIN)
            if G is None and floor is not None:
                size = max(size, floor)
            bend, width = curved[k] if curved else (None, 0.0)
            if bend is not None and bend > 0 and floor is not None:
                # with a global size the face takes it (the user's size beats our 6 a quarter-turn); without
                # one the rules' own size stands, held over the user's shortest edge. Either way the shape is kept
                size = min(size, curved_size(float(bend), float(width), floor=floor,
                                             rule=size if G is not None else min(rule, size)))
            size *= scale
            r["size_mm"] = _r(size)
            curve = r.get("_rule_curve")
            r.pop("curve_mm", None)
            if curve is not None:
                c = min(max(float(curve), floor * scale) if floor is not None else float(curve), size)
                if c < size:
                    r["curve_mm"] = _r(c)
            r["floored"] = False
            coarser = coarser or size > rule * (1 + 1e-6)
            finer = finer or min(size, r.get("curve_mm", size)) < 0.5 * r["wall_mm"] * (1 - 1e-6)
            planned += a / size ** 2
        return planned, coarser, finer

    for r in rows:
        r["_rule_size"], r["_rule_curve"] = r["size_mm"], r.get("curve_mm")
    planned, coarser, finer = sized(1.0)
    if planned > planned_max:
        # the nearest size that can be built in the time allowed: every size up by one factor, and said
        scale = math.sqrt(planned / (0.95 * planned_max))
        planned, coarser, finer = sized(scale)
        out.update(held=True, scale=round(scale, 4))
        if floor is not None:
            out["floor_mm"] = _r(floor * scale)
    for r in rows:
        del r["_rule_size"], r["_rule_curve"]
        r["rule"] = f"{r['rule']}+{RULE_SIZES}"
    out.update(planned=int(round(planned)), coarser=coarser, finer=finer)
    return out


# #
# asked against delivered (SHMESH-26)
# #

def _edge_stats(mesh) -> tuple[np.ndarray, np.ndarray]:
    """(each element's shortest edge, its longest), quads then triangles, mm."""
    q = SQ.quad_measures(mesh.xyz, mesh.quads, extra=True) if len(mesh.quads) else {}
    t = SQ.tria_measures(mesh.xyz, mesh.trias, extra=True) if len(mesh.trias) else {}
    lo = np.concatenate([d["min_edge"] for d in (q, t) if d]) if (q or t) else np.zeros(0)
    hi = np.concatenate([d["max_edge"] for d in (q, t) if d]) if (q or t) else np.zeros(0)
    return lo, hi


_pct = MR.pct
_n = MR.share_words


def _size_row(row: Mapping, field: str, mesh, sizing: Mapping | None, req: Mapping) -> dict:
    s = req.get("sizes") or {}
    sizing = sizing or {}
    n = mesh.n_elements
    lo, hi = _edge_stats(mesh)
    out: dict = {"applied": True}
    if field == "global":
        typical = float(np.median(0.5 * (lo + hi))) if n else 0.0
        out.update(measured=_r(typical), met=bool(n) and BAND[0] * s["global_mm"] <= typical <= BAND[1] * s["global_mm"])
        said = f"elements are about {_g(typical)} mm"
        if sizing.get("held"):
            out["met"] = False
            said += (f"; {_g(s['global_mm'])} mm would make more elements than a shell mesh is made of in the time "
                     f"allowed, so the nearest size that builds was used ({_g(s['global_mm'] * sizing['scale'])} mm)")
        elif sizing.get("coarser"):
            said += "; this is coarser than our accuracy rules ask for on some faces, so results may be less exact"
        if sizing.get("finer"):
            said += "; some elements are smaller than half the wall, finer than a shell usually needs"
        out["delivered"] = said
    elif field in ("min_edge", "max_edge"):
        limit = float(s[f"{field}_mm"])
        v = lo if field == "min_edge" else hi
        outside = int((v < limit).sum() if field == "min_edge" else (v > limit).sum())
        worst = float(v.min() if field == "min_edge" else v.max()) if n else 0.0
        # AN EDGE LENGTH IS A TARGET, reported with the percent outside: met when every element is inside it,
        # otherwise told - never a missed limit (CAD edges and strips shorter than the size are the part's own)
        out.update(outside=outside, outside_pct=_pct(outside, n), worst=_r(worst), met=True if outside == 0 else None,
                   target=True)
        if outside:
            out["delivered"] = MR.edge_words(field, outside, n, limit, worst)
        else:
            out["delivered"] = "every element is inside it"
    elif field == "growth":
        g = float(s["growth"])
        over, pairs = _growth(mesh, g)
        out.update(outside=over, outside_pct=_pct(over, pairs), met=None)
        out["delivered"] = (f"the sizes were graded at {_g(g)}; {_pct(over, pairs):g}% of neighbouring elements differ "
                            "by more" if pairs else f"the sizes were graded at {_g(g)}")
    else:  # thickness
        walls = sorted({round(float(w), 3) for w in mesh.wall}) if n else []
        shown = (f"{_g(walls[0])} mm" if len(walls) == 1 else f"{_g(min(walls))} to {_g(max(walls))} mm") if walls else ""
        out.update(applied=False, met=None,
                   delivered=f"not applied - each wall's thickness is measured from your CAD ({shown})" if shown
                   else "not applied - each wall's thickness is measured from your CAD")
    return out


def _growth(mesh, g: float) -> tuple[int, int]:
    """(pairs of elements across an edge whose sizes differ by more than `g` times, with gmsh's own scatter set
    aside at 1.25 times it; all pairs). An element's size is the mean of its edges."""
    sizes: list[float] = []
    users: dict[tuple[int, int], list[int]] = {}
    for e, conn in enumerate(mesh.elements()):
        p = mesh.xyz[np.asarray(conn)]
        k = len(conn)
        sizes.append(float(np.mean(np.linalg.norm(p[[(i + 1) % k for i in range(k)]] - p, axis=1))))
        for i in range(k):
            a, b = int(conn[i]), int(conn[(i + 1) % k])
            users.setdefault((min(a, b), max(a, b)), []).append(e)
    over = pairs = 0
    for u in users.values():
        if len(u) == 2:
            pairs += 1
            one, other = sizes[u[0]], sizes[u[1]]
            over += int(max(one, other) > 1.25 * g * min(one, other))
    return over, pairs


def _limit_row(lim: Mapping, mesh, q: Mapping, t: Mapping) -> dict:
    figs = figures_of(lim)
    if not figs:
        name = MEASURES[str(lim["measure"])][3]
        return {"applied": False, "met": None, "delivered": f"not applied - {name} of this kind is not a measure of "
                                                             "shell elements"}
    vals = [d[figs[k]] for k, d in (("quad", q), ("tria", t)) if k in figs and d]
    return MR.limit_row(lim, np.concatenate(vals) if vals else np.zeros(0), mesh.n_elements)


def report(mesh, req: Mapping | None, sizing: Mapping | None = None) -> dict | None:
    """SHMESH-26: for every requirement read from the notes, what was asked and what the built mesh delivers:
    {"rows": [{"id", "field", "asked", "delivered", "met": True | False | None, "applied", "outside", "outside_pct",
    "beyond", "worst"}], "met", "missed", "definitions"}. None with no requirement. `met` None: applied as a
    setting, or not applied, with nothing to measure it against - never told as met."""
    if not req or not req.get("rows"):
        return None
    q = SQ.quad_measures(mesh.xyz, mesh.quads, extra=True) if len(mesh.quads) else {}
    t = SQ.tria_measures(mesh.xyz, mesh.trias, extra=True) if len(mesh.trias) else {}
    limits = {lim["id"]: lim for lim in req.get("limits") or []}
    n, nq, nt = mesh.n_elements, len(mesh.quads), len(mesh.trias)
    rows = []
    for row in req["rows"]:
        field = str(row["field"])
        if field.startswith("limit:"):
            got = _limit_row(limits[row["id"]], mesh, q, t)
        elif field in ("global", "min_edge", "max_edge", "growth", "thickness"):
            got = _size_row(row, field, mesh, sizing, req)
        elif field == "quads_only":
            got = {"applied": True, "outside": nt, "outside_pct": _pct(nt, n), "met": nt == 0,
                   "delivered": "every element is a quad" if not nt else
                   f"{_n(nt, n)} are triangles: where four-sided elements cannot fill a face inside the solver's own "
                   "limits, a triangle closes it"}
        elif field == "triangles_only":
            got = {"applied": True, "outside": nq, "outside_pct": _pct(nq, n), "met": nq == 0,
                   "delivered": "every element is a triangle" if not nq else f"{_n(nq, n)} are quads"}
        elif field == "max_triangle_percent":
            most = float((req.get("kind") or {}).get("tria_percent_max") or 0.0)
            got = {"applied": True, "outside": nt, "outside_pct": _pct(nt, n), "met": _pct(nt, n) <= most + 1e-9,
                   "delivered": f"{_pct(nt, n):g}% of elements ({nt:,}) are triangles"}
        elif field == "linear":
            got = {"applied": True, "met": True, "delivered": "shell elements are 4-node quads and 3-node triangles"}
        else:  # quadratic
            got = {"applied": False, "met": None, "delivered": "not applied - shell elements are made as 4-node quads "
                                                               "and 3-node triangles"}
        rows.append({"id": row["id"], "field": field, "asked": row["asked"], **got})
    return MR.finish_report(rows, n, DEFINITIONS)


def line(row: Mapping) -> str:
    """One requirement as the user reads it after the mesh: what was asked, then what was delivered."""
    return MR.line(row)


def words(rep: Mapping | None) -> list[str]:
    """The report in plain words, one line per requirement ([] with none)."""
    return MR.report_words(rep)


def summary(rep: Mapping | None, not_applied: int = 0) -> str:
    """The one line the closing message carries ("From your notes: ...")."""
    return MR.summary(rep, kind="shell", not_applied=not_applied)


__all__ = ["BAND", "DEFINITIONS", "FIGURES", "MIN_MARGIN", "RULE_LIMITS", "RULE_REPORT", "RULE_SIZES",
           "SHAPE_PER_TURN", "curved_size", "figures_of",
           "kind_of", "limits_in_force", "line", "report", "size_plan", "summary", "words"]
