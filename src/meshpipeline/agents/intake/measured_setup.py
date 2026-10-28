# Responsibility: Check a submitted external-flow setup against the part the geometry check measured,
# before it is put to the user for approval: a setup the measured part cannot carry is said in the
# chat, never found by the engine after the user has approved it.
# Boundaries: pure over the submission's args, the check's stored facts and the confirmed form. It
# reads no store and calls no model; the executor hands it what it read. Nothing measured: nothing said.
"""What this catches (real path on dev-areen, 2026-10-28, and job dce77409 before it):

- A SYMMETRY PLANE ON A WHOLE BODY. The user asked for symmetry, the chat agreed, and the run
  stopped before meshing: "the geometry is not a half-model - it has no coplanar cut face".
  A half model is cut on a plane through the origin, and snappy lays the symmetry plane there
  (engines/snappy/snappy_runner.detect_symmetry_plane): a part whose box lies on no such plane can
  only be refused.
- THE GROUND TAKEN FOR A CUT. A wind turbine standing at z = 0 with a ground and a symmetry plane:
  the only side on the origin is the base it stands on, so both landed on the box's floor and the
  run stopped ("one face can be only one boundary").
- A REFERENCE LENGTH A THOUSAND TIMES OFF. "79.862 mm, not 79.862 m" for an 80 m long body was
  taken as said: the far field is sized in reference lengths, so the domain shrank a thousandfold.
  A slip between millimetres and metres is the usual cause; it is asked about, never taken.
"""
from __future__ import annotations

#: A box side lies on the plane through the origin when it is this close to it, as a share of the
#: part's longest side - the engine's own tolerance (snappy_runner.detect_symmetry_plane tol_frac).
ON_PLANE = 0.005
#: The vertical axis the engine lays the ground on (engines/ground_plane.VERTICAL_AXIS).
VERTICAL = 2
#: A reference length this far from the part's longest side, either way, is asked about. A wing's
#: chord is a tenth to a fiftieth of its span; a slip between millimetres and metres is a thousand.
REFERENCE_TOO_FAR = 100.0


def half_model_sides(bbox_min, bbox_max) -> list[tuple[int, str]]:
    """The sides of the part's box that lie on a plane through the origin, as (axis, "min"|"max"),
    the side the part extends away from: where a half model's cut can be. Empty for a part that
    straddles every such plane or stands clear of it - a whole body."""
    try:
        lo = [float(v) for v in bbox_min]
        hi = [float(v) for v in bbox_max]
    except (TypeError, ValueError):
        return []
    if len(lo) != 3 or len(hi) != 3:
        return []
    longest = max(h - low for low, h in zip(lo, hi, strict=True))
    if longest <= 0:
        return []
    tol = ON_PLANE * longest
    out = []
    for k in range(3):
        if abs(lo[k]) < tol < hi[k]:
            out.append((k, "min"))
        elif abs(hi[k]) < tol and lo[k] < -tol:
            out.append((k, "max"))
    return out


def _patches(args: dict) -> list[dict]:
    return [p for p in (args.get("patches") or []) if isinstance(p, dict)]


def _symmetry_errors(args: dict, facts: dict) -> list[str]:
    sym = [p for p in _patches(args) if (p.get("type") or "").strip() == "symmetry"]
    # two symmetry patches are a swept slab (both end caps), which the engine reads on its own terms
    if len(sym) != 1 or str(args.get("dimensionality") or "3D").upper() != "3D":
        return []
    if facts.get("bbox_min_m") is None or facts.get("bbox_max_m") is None:
        return []
    sides = half_model_sides(facts["bbox_min_m"], facts["bbox_max_m"])
    name = (sym[0].get("name") or "symmetry").strip()
    if not sides:
        return [
            f"patches declare the symmetry plane {name!r}, but the measured part is a whole body: no "
            "side of it lies on a plane through the origin, where a half model is cut and the "
            "symmetry plane is laid. The engine would refuse this setup after the user approved it. "
            f"Tell the user, drop {name!r} and mesh the whole body (wall + farfield), unless they "
            "upload a half model cut on such a plane."]
    from meshpipeline.engines.ground_plane import ground_patch_name

    ground = ground_patch_name(_patches(args))
    if ground and all(k == VERTICAL and side == "min" for k, side in sides):
        return [
            f"patches declare both the ground {ground!r} and the symmetry plane {name!r}, but the "
            "only side of the measured part on a plane through the origin is the bottom it stands "
            "on - that is the ground, not a half model's cut, and one face of the domain can be "
            f"only one boundary. Tell the user, and keep {ground!r} without {name!r} (the whole "
            "body on the ground), unless they upload a half model."]
    return []


def _reference_errors(args: dict, confirmed: dict) -> list[str]:
    ref = args.get("reference_length_m")
    size = confirmed.get("size_mm")
    if isinstance(ref, bool) or not isinstance(ref, (int, float)) or ref <= 0 or not isinstance(size, list):
        return []
    try:
        longest_m = max(float(v) for v in size) / 1000.0
    except (TypeError, ValueError):
        return []
    if longest_m <= 0:
        return []
    ratio = float(ref) / longest_m
    if 1.0 / REFERENCE_TOO_FAR <= ratio <= REFERENCE_TOO_FAR:
        return []
    return [
        f"reference_length_m {float(ref):g} m is {ratio:.3g} times the part's measured longest side "
        f"({longest_m:g} m, as confirmed on the picture): the far field is sized in reference "
        "lengths, so a length this far off builds the domain the wrong size. It is usually a slip "
        "between millimetres and metres. Do not submit it: tell the user the measured length and "
        "ask which they mean."]


def setup_errors(args: dict, facts: dict | None, confirmed: dict | None) -> list[str]:
    """What the measured part says against this submission: sentences for the model, empty when it
    can carry it - or when nothing was measured or confirmed (then nothing is said)."""
    if str(args.get("purpose") or "") != "external_cfd":
        return []
    return _symmetry_errors(args, facts or {}) + _reference_errors(args, confirmed or {})


__all__ = ["half_model_sides", "setup_errors"]
