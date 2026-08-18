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
    chord = geom.get("chord") or geom.get("CHORD")
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
        diag = (
            "[DOMAIN_EXTENT_MISMATCH] Domain extents do not match the user's request "
            f"(tolerance {int(tol*100)}%): " + "; ".join(mismatches) + ". "
            "Recompute the far-field box corners (domain_min/domain_max passed to "
            "prepare_surface) so the box spans EXACTLY the requested chord multiples "
            "around the body - do NOT fall back to a default external-aero prior. "
            "Then re-run prepare_surface and run_mesh."
        )
        return False, diag
    return True, ""


def extent_gate_for_request(request_txt: str, manifest: dict) -> tuple[bool, str]:
    return check_domain_extents(parse_requested_extents(request_txt), manifest)
