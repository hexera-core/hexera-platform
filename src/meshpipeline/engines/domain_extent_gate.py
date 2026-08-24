# Responsibility: Check a requested external domain against the geometry it must enclose.
# Boundaries: a bounded geometric check; it does not author the domain and does not modify the request.
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_KEYS = ("upstream", "downstream", "lateral")

_EXPLICIT_RE = {
    "upstream":   re.compile(r"D_UPSTREAM\s*[=:]\s*([\d.]+)", re.I),
    "downstream": re.compile(r"D_DOWNSTREAM\s*[=:]\s*([\d.]+)", re.I),
    "lateral":    re.compile(r"D_LATERAL\s*[=:]\s*([\d.]+)", re.I),
}
_PHRASE_RE = {
    "upstream":   re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:upstream|inlet|in front)", re.I),
    "downstream": re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:downstream|outlet|wake|behind)", re.I),
    "lateral":    re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:lateral|above/below|above|below|side)", re.I),
}


def parse_requested_extents(request_txt: str) -> dict:
    out: dict = {}
    if not request_txt:
        return out
    for key in _KEYS:
        m = _EXPLICIT_RE[key].search(request_txt) or _PHRASE_RE[key].search(request_txt)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def _applied_multipliers(geom: dict) -> dict | None:
    box  = geom.get("domain_box") or geom.get("box")
    body = geom.get("body_bbox") or geom.get("body_box")
    # The DIVISOR is the user's own reference length when they stated one, because their "Nc"
    # request is in that unit. Dividing by the body length judged a 20-MAC request as 3.2 and
    # rejected a correct mesh; the fallback (no stated reference) is the body length, which is
    # both the old behaviour and the unit the planner's margins are natively in.
    chord = geom.get("reference_length") or geom.get("chord") or geom.get("CHORD")
    if not (isinstance(box, dict) and isinstance(body, dict) and chord):
        return None
    try:
        c = float(chord)
        if c <= 0:
            return None
        up   = (float(body["xmin"]) - float(box["xmin"])) / c
        down = (float(box["xmax"])  - float(body["xmax"])) / c
        lat  = (float(body["ymin"]) - float(box["ymin"])) / c
        return {"upstream": up, "downstream": down, "lateral": lat}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def check_domain_extents(requested: dict | None, manifest: dict,
                         tol: float = 0.15) -> tuple[bool, str]:
    if not requested:
        return True, ""  # nothing to enforce
    geom = (manifest or {}).get("geometry", {}) or {}
    applied = _applied_multipliers(geom)
    if applied is None:
        logger.info("domain_extent_gate: manifest lacks box/body/chord - gate inconclusive, passing")
        return True, ""

    mismatches = []
    for k in _KEYS:
        rv = requested.get(k)
        av = applied.get(k)
        if rv is None or av is None:
            continue
        try:
            rv = float(rv)
        except (TypeError, ValueError):
            continue
        if rv == 0:
            continue
        if abs(av - rv) / abs(rv) > tol:
            mismatches.append(f"{k}: requested {rv:.3g}c, mesh has {av:.3g}c")

    if mismatches:
        ruler = float(geom.get("reference_length") or geom.get("chord") or 0.0)
        src = geom.get("reference_length_source") or "body_streamwise_extent"
        diag = (
            "[DOMAIN_EXTENT_MISMATCH] Domain extents do not match the user's request "
            f"(tolerance {int(tol*100)}%, measured in units of the {src} reference length "
            f"{ruler:.4g} m): " + "; ".join(mismatches) + ". "
            "Recompute the far-field box corners (domain_min/domain_max passed to "
            "prepare_surface) so the box spans EXACTLY the requested chord multiples "
            "around the body - do NOT fall back to a default external-aero prior. "
            "Then re-run prepare_surface and run_mesh."
        )
        return False, diag
    return True, ""


def extent_gate_for_request(request_txt: str, manifest: dict) -> tuple[bool, str]:
    return check_domain_extents(parse_requested_extents(request_txt), manifest)


# #
# TYPED evaluation (approved intent v5): the request arrives as numbers the user approved, the
# ruler is the approved reference length - never the manifest's, which a re-plan can lose or
# change (the heat-sink retries measured a correct box with a wrong-axis ruler and rejected two
# production-grade meshes). Tri-state: a request that CANNOT be measured is "unmeasured", never
# a silent pass. One-sided: over-delivering a margin is never a defect. Floored: under half of
# what was asked - or a box touching the body - is a category error and blocks outright.
# #

#: below this fraction of the requested margin, a miss is no longer a near-miss
FLOOR_FRACTION = 0.5

_ALL_DIRECTIONS = ("upstream", "downstream", "lateral", "vertical")


class ExtentVerdict:
    __slots__ = ("status", "caveats", "detail")

    def __init__(self, status: str, caveats: list, detail: str):
        self.status = status      # "na" | "unmeasured" | "pass" | "miss" | "block"
        self.caveats = caveats    # [{direction, requested, measured, units, ruler_m, ruler_source}]
        self.detail = detail


_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _axis_margins(box: dict, body: dict, r: float, flow_axis: str | None) -> dict:
    """Margins in ruler units, oriented by the DECLARED flow axis. No declaration keeps the
    legacy assume-X convention byte-for-byte (pre-v5 behaviour, documented). Sign matters:
    flow -y puts downstream at ymin. Lateral/vertical are the remaining axes in (x, y, z)
    order, each taken as the SMALLER of its two sides."""
    ax = (flow_axis or "+x").strip().lower()
    sign, letter = (ax[0], ax[1]) if ax[0] in "+-" else ("+", ax[0])
    i = _AXIS_INDEX[letter]
    names = "xyz"
    lo = (float(body[f"{names[i]}min"]) - float(box[f"{names[i]}min"])) / r
    hi = (float(box[f"{names[i]}max"]) - float(body[f"{names[i]}max"])) / r
    up, down = (lo, hi) if sign == "+" else (hi, lo)
    rest = [j for j in range(3) if j != i]
    def _min_side(j: int) -> float:
        n = names[j]
        return min((float(body[f"{n}min"]) - float(box[f"{n}min"])) / r,
                   (float(box[f"{n}max"]) - float(body[f"{n}max"])) / r)
    return {"upstream": up, "downstream": down,
            "lateral": _min_side(rest[0]), "vertical": _min_side(rest[1])}


def evaluate_domain_extents(requested: dict | None, reference_length_m: float | None,
                            manifest: dict, tol: float = 0.15,
                            flow_axis: str | None = None) -> ExtentVerdict:
    if (not isinstance(requested, dict)
            or not any(v is not None for v in requested.values())
            or not reference_length_m):
        return ExtentVerdict("na", [], "")
    geom = (manifest or {}).get("geometry") or {}
    box = geom.get("domain_box") or geom.get("box")
    body = geom.get("body_bbox") or geom.get("body_box")
    if not isinstance(box, dict) or not isinstance(body, dict):
        return ExtentVerdict(
            "unmeasured", [],
            "[DOMAIN_EXTENT_UNMEASURED] the request declares far-field extents but the mesh "
            "manifest records no measurable domain/body box - an unmeasured requirement can "
            "neither pass nor be delivered with a caveat")
    try:
        r = float(reference_length_m)
        if r <= 0:
            raise ValueError("nonpositive ruler")
        margins = _axis_margins(box, body, r, flow_axis)
    except Exception:  # noqa: BLE001 - no measurable box/body: honesty demands "unmeasured"
        return ExtentVerdict(
            "unmeasured", [],
            "[DOMAIN_EXTENT_UNMEASURED] the request declares far-field extents but the mesh "
            "manifest records no measurable domain/body box - an unmeasured requirement can "
            "neither pass nor be delivered with a caveat")

    caveats: list = []
    blocks: list = []
    for k in _ALL_DIRECTIONS:
        rv = requested.get(k)
        if rv is None:
            continue
        rv = float(rv)
        mv = margins[k]
        if mv <= 0:
            blocks.append(f"{k}: the domain box touches or clips the body")
            continue
        if mv >= rv * (1.0 - tol):
            continue                        # within tolerance, or over-delivered: never a defect
        if mv < rv * FLOOR_FRACTION:
            blocks.append(f"{k}: requested {rv:g}L, mesh has {mv:.3g}L - under half of what "
                          "was asked, which is a different domain, not a near-miss")
        else:
            caveats.append({"direction": k, "requested": rv, "measured": round(mv, 4),
                            "units": "reference_lengths", "ruler_m": r,
                            "ruler_source": "user_stated"})
    if blocks:
        return ExtentVerdict(
            "block", [],
            "[DOMAIN_EXTENT_BLOCK] " + "; ".join(blocks)
            + f" (measured in units of the approved reference length {r:.4g} m). Recompute the "
            "far-field box corners so every requested margin is met - do NOT shrink the "
            "request to fit the box.")
    if caveats:
        stated = "; ".join(f"{c['direction']}: requested {c['requested']:g}L, mesh has "
                           f"{c['measured']:g}L" for c in caveats)
        return ExtentVerdict(
            "miss", caveats,
            f"[DOMAIN_EXTENT_MISMATCH] {stated} (tolerance {int(tol * 100)}%, measured in "
            f"units of the approved reference length {r:.4g} m). Rebuild with the box spanning "
            "the requested multiples; if attempts run out, the best quality-passing mesh is "
            "delivered with this miss stated.")
    return ExtentVerdict("pass", [], "")
