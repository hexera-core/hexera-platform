# Responsibility: Say what the FEA stage shows for a part - its size, its unit, how many bodies the
# file has and what the clean check found - and whether version 1 can take it (one part only).
# Also say which stage a session's check opens on, from what the user said the mesh is for.
# Boundaries: pure over the stored check's facts and the conversation; it stores nothing and
# meshes nothing. The unit box and its suggestion stay the generic check's (api/v1/geometry).
# Collaborates with: application/purpose_map.py (the route), api/v1/geometry.py (serves the view,
# refuses a multi-body confirmation), ui/js/render/fea_form.js (draws it).
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
            "single_part_only": blocked, "can_proceed": not blocked}


__all__ = ["MULTI_BODY", "QUESTION", "STAGE_FEA", "STAGE_FLOW", "STAGE_THERMAL", "body_count",
           "clean_words", "fea_view", "multi_body_message", "stage_route"]
