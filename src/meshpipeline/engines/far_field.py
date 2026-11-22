# Responsibility: Size an external flow's far-field box the way the domain-extent gate will measure it.
# Owns: the ruler (the stated reference length, else the body's extent along the flow), the flow-axis orientation of the
#       margins, and the reading of a request's typed extents as margins.
# Boundaries: engine-neutral arithmetic on a body box; it reads no file and builds no mesh.
# Collaborates with: engines/domain_extent_gate.py (the judge this agrees with by construction), the engines that build
#       a box around a body (cfMesh; gmsh's external fluid domain).
"""One box, sized in the unit it is judged in.

The domain-extent gate measures every margin in the user's reference length when they stated one,
else in the body's extent ALONG THE FLOW, with upstream/downstream on the declared flow axis and
lateral/vertical on the two remaining axes in (x, y, z) order (domain_extent_gate._axis_margins).
A box built in any other unit fails that gate on a correct request: cfMesh multiplied its margins by
the body's LARGEST extent along a fixed +x, and a rotor whose streamwise extent is a sixth of its
diameter came back with 47.8 lengths downstream where 8 were asked (2026-10-04, blade_row_rotor_002).
"""
from __future__ import annotations

from collections.abc import Mapping

_AXES = {"x": 0, "y": 1, "z": 2}

#: margins in rulers when neither the strategy nor the request names one: the cfMesh defaults
DEFAULT_MARGINS = {"up": 10.0, "down": 20.0, "side": 10.0, "vert": 10.0}

#: the request's typed directions (approved intent) -> the box's margin keys
_REQUEST_KEYS = {"upstream": "up", "downstream": "down", "lateral": "side", "vertical": "vert"}


def flow_axis_of(flow_axis: str | None) -> tuple[int, int]:
    """(axis index, sign) of a declared flow axis like '+x' / '-y' / 'z'; +x when undeclared,
    which is the gate's own legacy reading."""
    ax = str(flow_axis or "+x").strip().lower()
    if not ax:
        ax = "+x"
    sign, letter = (ax[0], ax[1:2]) if ax[0] in "+-" else ("+", ax[0])
    if letter not in _AXES:
        return 0, 1
    return _AXES[letter], (1 if sign == "+" else -1)


def extents_in_text(request_txt: str | None) -> dict:
    """The far-field margins a request states in words ('8 downstream', '5 lateral'), read by the
    SAME parser the case-level extent check reads them with (domain_extent_gate), so a box built
    from them is the box that check expects. That parser reads 'above'/'below' as lateral; the
    vertical room is taken as the same number when the text names no other."""
    from meshpipeline.engines.domain_extent_gate import parse_requested_extents
    out = dict(parse_requested_extents(request_txt or ""))
    if "lateral" in out and "vertical" not in out:
        out["vertical"] = out["lateral"]
    return out


def margins_from(strategy_margin: Mapping | None, requested_extents: Mapping | None,
                 request_txt: str | None = None) -> dict:
    """{up, down, side, vert} in rulers: what the strategy states, else the approved request's
    typed extents, else the margins its text states, else the defaults - key by key, so a request
    naming only two directions keeps the defaults for the others."""
    out = dict(DEFAULT_MARGINS)
    for k, v in extents_in_text(request_txt).items():
        key = _REQUEST_KEYS.get(str(k))
        if key and v is not None:
            out[key] = float(v)
    for k, v in (requested_extents or {}).items():
        key = _REQUEST_KEYS.get(str(k))
        if key and v is not None:
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                pass
    for k, v in (strategy_margin or {}).items():
        if k in out and v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def ruler_of(bbox_min, bbox_max, flow_axis: str | None, reference_length_m: float | None) -> float:
    """The unit every margin multiplies: the stated reference length, else the body's extent along
    the flow (the gate's body_streamwise_extent)."""
    if reference_length_m:
        try:
            r = float(reference_length_m)
            if r > 0.0:
                return r
        except (TypeError, ValueError):
            pass
    i, _ = flow_axis_of(flow_axis)
    ext = float(bbox_max[i]) - float(bbox_min[i])
    if ext > 0.0:
        return ext
    return max(float(bbox_max[k]) - float(bbox_min[k]) for k in range(3)) or 1.0


def far_field_box(bbox_min, bbox_max, margins: Mapping, *, flow_axis: str | None = None,
                  reference_length_m: float | None = None) -> tuple[list[float], list[float]]:
    """(domain_min, domain_max): the body box grown by `margins` (in rulers) - upstream against the
    flow, downstream with it, `side` on the first remaining axis and `vert` on the second, in
    (x, y, z) order, exactly as the gate reads them."""
    i, sign = flow_axis_of(flow_axis)
    r = ruler_of(bbox_min, bbox_max, flow_axis, reference_length_m)
    up, down = float(margins.get("up", 0.0)), float(margins.get("down", 0.0))
    side, vert = float(margins.get("side", 0.0)), float(margins.get("vert", 0.0))
    dmin = [float(v) for v in bbox_min]
    dmax = [float(v) for v in bbox_max]
    lo, hi = (up, down) if sign > 0 else (down, up)
    dmin[i] -= lo * r
    dmax[i] += hi * r
    rest = [j for j in range(3) if j != i]
    dmin[rest[0]] -= side * r
    dmax[rest[0]] += side * r
    dmin[rest[1]] -= vert * r
    dmax[rest[1]] += vert * r
    return dmin, dmax


#: PLAN-cfd-6: the most of the far field's cross-section a body may block (frontal area over the box's section across the
#: flow): COST Action 732's best-practice guideline (Franke et al. 2007) and the usual wind-tunnel correction limit
BLOCKAGE_MAX = 0.03
#: the rule the widening is recorded under
BLOCKAGE_RULE = "PLAN-cfd-6"
#: the share of BLOCKAGE_MAX the widened box aims at
BLOCKAGE_AIM = 0.95


def projected_areas(triangles) -> list[float]:
    """The body's frontal area seen along x, y and z: half the summed absolute projection of every wall triangle (a
    closed surface's front and back each project once; the CFD board's own measure). [0, 0, 0] for no triangles."""
    import numpy as np

    t = np.asarray(triangles, dtype=float).reshape(-1, 3, 3)
    if not len(t):
        return [0.0, 0.0, 0.0]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]) * 0.5
    return [float(v) for v in 0.5 * np.abs(n).sum(axis=0)]


def projected_areas_of(stl_path) -> list[float] | None:
    """projected_areas of a staged surface file (metres), or None when it cannot be read."""
    try:
        from pathlib import Path

        from meshpipeline.cad.stl_io import read_stl_triangles
        return projected_areas(read_stl_triangles(Path(stl_path)))
    except Exception:  # noqa: BLE001 - no reading, no growth: the box stays as planned
        return None


def grow_for_blockage(dmin: list, dmax: list, axis: int, frontal_m2: float | None, *,
                      fixed: frozenset | set | tuple = (), most: float = BLOCKAGE_MAX,
                      ruler_m: float | None = None) -> dict | None:
    """PLAN-cfd-6, THE CROSS-SECTION SIZED FOR BLOCKAGE: the box's two cross-flow axes moved out, every free face by
    the same distance, until the body's frontal area is at most `most` of the section across the flow - the planned
    margins stay as the minimum. A face in `fixed` ((axis, "min" | "max"): the floor under a grounded body, a
    symmetry plane) never moves. Changes `dmin`/`dmax` in place; returns the record ({frontal_m2, section_before_m2,
    blockage_before, blockage, grown_m, grown_rulers}) when it grew, else None (already within it, or nothing to
    grow)."""
    if not frontal_m2 or frontal_m2 <= 0.0:
        return None
    cross = [j for j in range(3) if j != int(axis)]
    span = [float(dmax[j]) - float(dmin[j]) for j in cross]
    free = [sum(1 for s in ("min", "max") if (j, s) not in set(fixed)) for j in cross]
    # aimed a little under the limit: the meshed wall's projection reads a few percent off the staged surface's
    need = float(frontal_m2) / (BLOCKAGE_AIM * float(most))

    def section(d: float) -> float:
        return (span[0] + free[0] * d) * (span[1] + free[1] * d)

    before = section(0.0)
    if before <= 0.0 or before >= need or not any(free):
        return None
    lo, hi = 0.0, max(span) + 1.0
    while section(hi) < need:
        hi *= 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if section(mid) < need else (lo, mid)
    d = hi
    for j, f in zip(cross, free, strict=True):
        if f == 0:
            continue
        if (j, "min") not in set(fixed):
            dmin[j] = float(dmin[j]) - d
        if (j, "max") not in set(fixed):
            dmax[j] = float(dmax[j]) + d
    r = float(ruler_m) if ruler_m else None
    return {"rule": BLOCKAGE_RULE, "frontal_m2": float(frontal_m2), "section_before_m2": before, "blockage_before": float(frontal_m2) / before,
            "blockage": float(frontal_m2) / section(d), "grown_m": d,
            **({"grown_rulers": d / r} if r else {})}


def blockage_words(rec: Mapping) -> str:
    """PLAN-cfd-6's plain line for the run note."""
    by = (f"{float(rec['grown_rulers']):.3g} body lengths" if rec.get("grown_rulers") is not None
          else f"{float(rec['grown_m']):.3g} m")
    return (f"the far field was widened by {by} on each open side so the body blocks "
            f"{100 * float(rec['blockage']):.2g}% of its cross-section (it was {100 * float(rec['blockage_before']):.2g}%; "
            f"at most {100 * BLOCKAGE_MAX:.0f}%)")


__all__ = ["BLOCKAGE_MAX", "DEFAULT_MARGINS", "blockage_words", "extents_in_text", "far_field_box", "flow_axis_of",
           "grow_for_blockage", "margins_from", "projected_areas", "projected_areas_of", "ruler_of"]
