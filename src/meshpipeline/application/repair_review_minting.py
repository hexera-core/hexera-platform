# Responsibility: Turn what a repair run could not settle into review-item dicts a person can open.
# Owns: which defects are "unsure", the item shape, and the group key that lets one decision cover many.
# Boundaries: PURE MINTING. It reads a shape and a report and returns dicts; it opens no session,
#             writes no row and calls no model. The terminal path persists what the node mints.
# Collaborates with: cad/repair/triage.py (split), cad/repair/spotlight.py (the camera),
#                    cad/repair/plan.py (what the rules planner would try), pipeline/repair_attempt.py.
from __future__ import annotations

import logging

from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.spotlight import camera_for
from meshpipeline.cad.repair.triage import DefectSplit, defect_from_entity

logger = logging.getLogger(__name__)

ORIGIN_TRIAGE = "triage"
ORIGIN_STALL = "stall"
ORIGIN_REFUSAL = "refusal"
ORIGIN_SURVIVOR = "survivor"
_STALLED = "stalled"


def _key(defect: EntityDefect) -> str:
    return defect.fingerprint or f"{defect.entity_type}:{defect.entity_index}:{defect.code.value}"


def _tried(defect: EntityDefect, operations) -> list[str]:
    names: list[str] = []
    for op in operations or ():
        if not isinstance(op, dict):
            continue
        hit = (op.get("defect_code") == defect.code.value
               or (defect.fingerprint and any(str(t).endswith(defect.fingerprint)
                                              or t == defect.fingerprint
                                              for t in op.get("targets") or ())))
        if hit and op.get("name") and op["name"] not in names:
            names.append(str(op["name"]))
    return names


def _proposal(shape, defect: EntityDefect, tried: list[str]) -> dict | None:
    """What the RULES planner would try next on this defect alone, or None. Provenance only."""
    if not defect.fingerprint:
        return None
    try:
        history = RunHistory()
        for name in tried:
            history.mark_ineffective(defect.fingerprint, name)
        candidate = RulesPlanner().propose([defect], history, shape=shape)
    except Exception:  # noqa: BLE001 - a proposal is a convenience, never a reason to lose an item
        return None
    if candidate is None:
        return None
    return {"source": "rules", "planner": RulesPlanner.label, "op": candidate.op.name,
            "defect_code": candidate.defect_code.value,
            "targets": [t.fingerprint for t in candidate.targets]}


def _item(shape, defect: EntityDefect, *, origin: str, message: str = "", extra: dict | None = None,
          tried: list[str] | None = None, pairing_key: str = "") -> dict:
    tried = tried or []
    proposal = _proposal(shape, defect, tried)
    op = proposal["op"] if proposal else None
    measurements = dict(defect.measurements or {})
    measurements.update(extra or {})
    if tried:
        measurements["tried"] = list(tried)
    return {
        "group_key": f"{defect.code.value}:{op or 'none'}",
        "defect_code": defect.code.value,
        "severity": defect.severity.value,
        "entity_type": defect.entity_type,
        "entity_index": int(defect.entity_index),
        "fingerprint": defect.fingerprint,
        "measurements": measurements,
        "message": message or defect.message,
        "origin": origin,
        "pairing_key": pairing_key,
        "proposal": proposal,
        "camera": camera_for(shape, defect),
    }


def mint_items(*, shape, report, repair_run_measured: dict, split: DefectSplit) -> list[dict]:
    """One item per defect the run left for a person.

    `repair_run_measured` is the loop's measurements (`stopped_because`, `operations`,
    `refusals`, `operations_kept`) plus, for a kept run, `survivors`: the entity dicts still
    present after the repair. Without `survivors` nothing changed, so the pool is the original
    repairable defects. A defect is minted once, by the first source that claims it.
    """
    measured = dict(repair_run_measured or {})
    operations = [dict(o) for o in measured.get("operations") or () if isinstance(o, dict)]
    refusals = [dict(r) for r in measured.get("refusals") or () if isinstance(r, dict)]
    stopped = str(measured.get("stopped_because", "") or "")
    kept = int(measured.get("operations_kept", 0) or 0)
    judgement_codes = {d.code for d, _ in split.judgement}

    if "survivors" in measured:
        found = [defect_from_entity(e) for e in measured["survivors"] or ()]
        pool = [d for d in found if d is not None and d.code not in judgement_codes]
    else:
        pool = list(split.repairable)

    items: list[dict] = []
    seen: set[str] = set()

    for defect, reason in split.judgement:
        seen.add(_key(defect))
        items.append(_item(shape, defect, origin=ORIGIN_TRIAGE, message=reason))

    rims = sorted({str(r["defect"]) for r in refusals if r.get("reason") == "paired_rim"
                   and r.get("defect")})
    pairing = ",".join(rims)
    by_fingerprint = {d.fingerprint: d for d in pool if d.fingerprint}
    for refusal in refusals:
        refused = by_fingerprint.get(str(refusal.get("defect", "")))
        if refused is None or _key(refused) in seen:
            continue
        seen.add(_key(refused))
        reason = str(refusal.get("reason", ""))
        extra = {"refused_because": reason}
        if refusal.get("detail"):
            extra["refusal_detail"] = refusal["detail"]
        items.append(_item(shape, refused, origin=ORIGIN_REFUSAL, extra=extra,
                           tried=_tried(refused, operations),
                           pairing_key=pairing if reason == "paired_rim" else ""))

    stalled = stopped == _STALLED or (kept == 0 and bool(operations))
    for defect in pool:
        if _key(defect) in seen:
            continue
        seen.add(_key(defect))
        items.append(_item(shape, defect, origin=ORIGIN_STALL if stalled else ORIGIN_SURVIVOR,
                           extra={"stopped_because": stopped} if stopped else None,
                           tried=_tried(defect, operations)))
    return items
