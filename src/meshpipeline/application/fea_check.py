# Responsibility: Say what the FEA stage shows for a part - its size, its unit, how many bodies the
# file has and what the clean check found - and whether version 1 can take it (one part only).
# Also say which stage a session's check opens on, from what the user said the mesh is for.
# Boundaries: pure over the stored check's facts and the conversation; it stores nothing and
# meshes nothing. The unit box and its suggestion stay the generic check's (api/v1/geometry).
# It also serves what the measurement found (cad/fea_measure, FEA build step 3) - the thinnest wall
# and the stress hot spots - for the stage to show, and re-reads that record in a confirmed unit.
# Collaborates with: application/purpose_map.py (the route), api/v1/geometry.py (serves the view,
# refuses a multi-body confirmation), ui/js/render/fea_form.js (draws it), cad/fea_measure.py (the
# record), application/geometry_check.py (measures and stores it).
from __future__ import annotations

#: What a file of several bodies is told, in plain words (design 3.6). {n} is the body count.
MULTI_BODY = ("This file has {n} separate bodies. Version 1 meshes one part at a time: export each "
              "part on its own and upload it, or wait for assembly support.")
#: The question the stage asks (design 3.1, Areen's words).
QUESTION = "Is this your part and are the units right?"
#: The check modes a stage opens in. "thermal" is the ECXML model's own (contracts/intake_formats).
STAGE_FLOW, STAGE_FEA, STAGE_THERMAL = "flow", "fea", "thermal"


def stage_route(stored: dict | None, messages) -> str:
    """Which stage the check opens on: "thermal" for a whole thermal model (its file decides),
    "fea" or "flow" once the user's words settle the purpose (application/purpose_map), and ""
    while they do not - no answer yet, or the one purpose question still open. A stage opened
    before the purpose is known would show an FEA part the flow form and its stickers."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    if (stored or {}).get("check_mode") == THERMAL_CHECK:
        return STAGE_THERMAL
    reading = pm.reading_for(messages)
    if not reading.settled:
        return ""
    return STAGE_FEA if reading.route == pm.ROUTE_FEA else STAGE_FLOW


def body_count(facts: dict | None) -> int | None:
    """How many solid bodies the scout found (separate shells, for a triangle file); None unknown."""
    try:
        n = (facts or {}).get("solids")
        return int(n) if n is not None else None
    except (TypeError, ValueError):
        return None


def clean_words(facts: dict | None) -> tuple[bool | None, str]:
    """The clean check (cad/validity, #224) in plain words: (clean yes/no/unknown, one sentence)."""
    v = (facts or {}).get("validity") or {}
    clean = v.get("clean")
    summary = str(v.get("summary") or "").strip()
    if v.get("kind") in ("unchecked", "check_failed") or clean not in (True, False):
        return None, summary or "The clean check did not run on this file."
    if clean is True:
        return True, summary or "The geometry is clean."
    return False, summary or "The geometry is not clean."


def multi_body_message(facts: dict | None) -> str:
    """The plain message for a file of several bodies, "" for one (or an unknown count)."""
    n = body_count(facts)
    return MULTI_BODY.format(n=n) if n is not None and n > 1 else ""


def fea_view(facts: dict | None) -> dict:
    """What the FEA stage shows beside the part (the size and unit come with the proposal)."""
    clean, said = clean_words(facts)
    blocked = multi_body_message(facts)
    return {"question": QUESTION, "bodies": body_count(facts), "clean": clean, "clean_summary": said,
            "single_part_only": blocked, "can_proceed": not blocked, "measure": measure_view(facts)}


#: The stage is sent EVERY hot spot the record holds (all of them, up to cad/fea_features.
#: MAX_HOT_SPOTS): a hot spot the stage leaves out looks the same as one never found (Areen's F1
#: car, 21 Oct: a rear-wing corner had no tag while its mirror twin had one). The stage draws a
#: long list as small marks and the panel groups it by type.
SHOWN_HOT_SPOTS = None
#: What the stage needs of a hot spot: what it is, how big, where, and whether it is capped.
_SHOWN_KEYS = ("id", "type", "shape", "radius_mm", "angle_deg", "length_mm", "depth_mm", "faces",
               "at_mm", "capped")
#: Hot-spot words, one and many, in the order they are said.
_NOUNS = {"hole": ("hole", "holes"), "fillet": ("fillet", "fillets"),
          "notch": ("notch or groove", "notches or grooves"),
          "sharp_corner": ("sharp inside corner", "sharp inside corners")}


def measure_view(facts: dict | None) -> dict | None:
    """The measurement as the stage shows it (V1: shown only, nothing to edit): the thinnest wall
    and where, and the hot spots the plan will refine. None before anything was measured."""
    from meshpipeline.cad.fea_measure import FACT_KEY

    rec = (facts or {}).get(FACT_KEY)
    if not isinstance(rec, dict):
        return None
    if not rec.get("measured"):
        return {"measured": False, "reason": str(rec.get("reason") or "the part was not measured")}
    spots = [{k: h[k] for k in _SHOWN_KEYS if k in h} for h in rec.get("hot_spots") or []]
    thick = rec.get("thickness") or {}
    counts = {str(k): int(v) for k, v in (rec.get("counts") or {}).items()}
    shown = spots if SHOWN_HOT_SPOTS is None else spots[:SHOWN_HOT_SPOTS]
    return {"measured": True, "basis": rec.get("basis"), "thickness_min_mm": thick.get("min_mm"),
            "thickness_face": thick.get("face"), "thickness_at_mm": thick.get("at_mm"),
            "hot_spots": shown, "counts": counts, "more": max(0, sum(counts.values()) - len(shown)),
            "summary": hot_spot_words(counts), "notes": list(rec.get("notes") or [])}


def hot_spot_words(counts: dict[str, int]) -> str:
    """"1 hole, 2 fillets and 1 sharp inside corner", or "no hot spots"."""
    parts = [f"{counts[k]} {_NOUNS[k][0 if counts[k] == 1 else 1]}" for k in _NOUNS if counts.get(k)]
    if not parts:
        return "no hot spots"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def in_unit(record: dict | None, scale_to_m: float | None) -> dict | None:
    """The measurement re-read in the unit the user confirmed: every length scales by the ratio of
    the confirmed unit to the one it was measured under (a file read as millimetres and confirmed
    as inches has every length 25.4 times longer). For FEA build step 4, which plans in the
    confirmed unit."""
    if not isinstance(record, dict):
        return record
    measured_under = float(record.get("scale_to_m") or 0.0)
    if not scale_to_m or measured_under <= 0:
        return dict(record)
    k = float(scale_to_m) / measured_under
    if abs(k - 1.0) <= 1e-12:
        return dict(record)
    from meshpipeline.application.geometry_check import rescaled_lengths

    out = rescaled_lengths(record, k)
    out["scale_to_m"] = float(scale_to_m)
    return out


__all__ = ["MULTI_BODY", "QUESTION", "SHOWN_HOT_SPOTS", "STAGE_FEA", "STAGE_FLOW", "STAGE_THERMAL",
           "body_count", "clean_words", "fea_view", "hot_spot_words", "in_unit", "measure_view",
           "multi_body_message", "stage_route"]
