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


def margins_from(strategy_margin: Mapping | None, requested_extents: Mapping | None) -> dict:
    """{up, down, side, vert} in rulers: what the strategy states, else the approved request's
    typed extents, else the defaults - key by key, so a request naming only two directions keeps
    the defaults for the others."""
    out = dict(DEFAULT_MARGINS)
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


__all__ = ["DEFAULT_MARGINS", "far_field_box", "flow_axis_of", "margins_from", "ruler_of"]
