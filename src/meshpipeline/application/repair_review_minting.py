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

#: The most items one park may mint. A part with thousands of sliver faces would otherwise flood
#: the review queue; the dropped count travels beside the candidates so nothing is silent.
MINT_CAP = 500


def _key(defect: EntityDefect) -> str:
    return defect.fingerprint or f"{defect.entity_type}:{defect.entity_index}:{defect.code.value}"


def _tried(defect: EntityDefect, operations) -> list[str]:
    """Operations aimed at THIS defect: by fingerprint when the op names its targets, by code only
    for an op that names none (so two same-code defects are never credited with each other's work)."""
    names: list[str] = []
    for op in operations or ():
        if not isinstance(op, dict) or not op.get("name"):
            continue
        targets = [str(t) for t in op.get("targets") or ()]
        if op.get("defect"):
            targets.append(str(op["defect"]))
        if targets:
            hit = bool(defect.fingerprint) and defect.fingerprint in targets
        else:
            hit = op.get("defect_code") == defect.code.value
        if hit and op["name"] not in names:
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
          tried: list[str] | None = None, pairing_key: str = "", camera_shape=None) -> dict:
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
        "camera": camera_for(shape if camera_shape is None else camera_shape, defect),
    }


def _measure(item: dict) -> float:
    values = [abs(float(v)) for v in (item.get("measurements") or {}).values()
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(values, default=0.0)


def _cap(items: list[dict]) -> tuple[list[dict], int]:
    """Keep MINT_CAP items: largest measurement first within each group, groups interleaved so a
    flood in one group cannot crowd the rest out."""
    if len(items) <= MINT_CAP:
        return items, 0
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["group_key"], []).append(item)
    ranked: list[tuple[int, int, dict]] = []
    for order, members in enumerate(groups.values()):
        for rank, item in enumerate(sorted(members, key=lambda i: -_measure(i))):
            ranked.append((rank, order, item))
    ranked.sort(key=lambda r: (r[0], r[1]))
    kept = [item for _, _, item in ranked[:MINT_CAP]]
    return kept, len(items) - len(kept)


def _pairing_keys(shape, rims: list[EntityDefect]) -> dict[str, str]:
    """`{rim fingerprint: key}`, one key per PAIR of rims (the two ends of one missing tube).

    The planner's refusal names only its own rim, so the partner is recovered geometrically with the
    same discriminator the planner used. When that cannot be answered (no shape, unresolvable wire)
    every refused rim shares one key - coarser, never lost.
    """
    fingerprints = sorted(r.fingerprint for r in rims if r.fingerprint)
    everyone = {f: _digest(fingerprints) for f in fingerprints}
    if shape is None or len(rims) < 2:
        return everyone
    try:
        from meshpipeline.cad.repair.conservative import _paired_rims
        from meshpipeline.cad.repair.operations import free_wire_containing

        wires = [(r.fingerprint, free_wire_containing(shape, int(r.entity_index))) for r in rims
                 if r.fingerprint]
        if any(w is None for _, w in wires):
            return everyone
        parent = {f: f for f, _ in wires}

        def find(x: str) -> str:
            while parent[x] != x:
                x = parent[x]
            return x

        for i, (fa, wa) in enumerate(wires):
            for fb, wb in wires[i + 1:]:
                if _paired_rims([wa, wb]):
                    parent[find(fa)] = find(fb)
        members: dict[str, list[str]] = {}
        for f, _ in wires:
            members.setdefault(find(f), []).append(f)
        return {f: _digest(sorted(group)) for group in members.values() for f in group}
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
        # ImportError/AttributeError/TypeError are a SIGNATURE MISMATCH with plan/conservative/
        # operations; ValueError/RuntimeError are the kernel failing on a wire. Both degrade to the
        # coarse key, but loudly: tests/native/test_native_repair_minting.py pins the real signatures.
        logger.warning("repair review: rim pairing fell back to one key (%s: %s)",
                       type(exc).__name__, exc)
        return everyone


def _digest(fingerprints: list[str]) -> str:
    key = ",".join(fingerprints)
    if len(key) <= 120:
        return key
    import hashlib
    return "pair:" + hashlib.sha1(key.encode()).hexdigest()  # noqa: S324 - an id, not security


def mint_items(*, shape, report, repair_run_measured: dict, split: DefectSplit,
               original_shape=None) -> list[dict]:
    """The items only; see `mint_with_overflow` for the dropped count."""
    return mint_with_overflow(shape=shape, report=report, repair_run_measured=repair_run_measured,
                              split=split, original_shape=original_shape)[0]


def mint_with_overflow(*, shape, report, repair_run_measured: dict, split: DefectSplit,
                       original_shape=None) -> tuple[list[dict], int]:
    """`(items, dropped)`: one item per defect the run left for a person, capped at MINT_CAP.

    `repair_run_measured` is the loop's measurements (`stopped_because`, `operations`,
    `refusals`, `operations_kept`) plus, for a kept run, `survivors`: the entity dicts still
    present after the repair, indexed against `shape` (the repaired shape). Without `survivors`
    nothing changed, so `shape` is the original and the pool is the original repairable defects.
    A defect is minted once, by the first source that claims it.

    A judgement defect on a kept run is RE-LOCATED by fingerprint on the survivors, because the
    repair renumbers entities and the original index would frame the wrong one; with no fingerprint
    match its camera is computed on `original_shape`, where its index is still true.
    """
    measured = dict(repair_run_measured or {})
    operations = [dict(o) for o in measured.get("operations") or () if isinstance(o, dict)]
    refusals = [dict(r) for r in measured.get("refusals") or () if isinstance(r, dict)]
    stopped = str(measured.get("stopped_because", "") or "")
    kept = int(measured.get("operations_kept", 0) or 0)
    judgement_codes = {d.code for d, _ in split.judgement}

    relocated: dict[str, EntityDefect] = {}
    if "survivors" in measured:
        found = [defect_from_entity(e) for e in measured["survivors"] or ()]
        scanned = [d for d in found if d is not None]
        pool = [d for d in scanned if d.code not in judgement_codes]
        relocated = {d.fingerprint: d for d in scanned
                     if d.code in judgement_codes and d.fingerprint}
    else:
        pool = list(split.repairable)

    items: list[dict] = []
    seen: set[str] = set()

    for defect, reason in split.judgement:
        seen.add(_key(defect))
        again = relocated.get(defect.fingerprint) if defect.fingerprint else None
        if again is not None:
            items.append(_item(shape, again, origin=ORIGIN_TRIAGE, message=reason))
        elif "survivors" in measured and original_shape is not None:
            items.append(_item(shape, defect, origin=ORIGIN_TRIAGE, message=reason,
                               camera_shape=original_shape))
        else:
            items.append(_item(shape, defect, origin=ORIGIN_TRIAGE, message=reason))

    by_fingerprint = {d.fingerprint: d for d in pool if d.fingerprint}
    paired = [by_fingerprint[str(r["defect"])] for r in refusals
              if r.get("reason") == "paired_rim" and str(r.get("defect", "")) in by_fingerprint]
    pairing = _pairing_keys(shape, paired) if paired else {}
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
                           pairing_key=pairing.get(refused.fingerprint, "")
                           if reason == "paired_rim" else ""))

    stalled = stopped == _STALLED or (kept == 0 and bool(operations))
    for defect in pool:
        if _key(defect) in seen:
            continue
        seen.add(_key(defect))
        items.append(_item(shape, defect, origin=ORIGIN_STALL if stalled else ORIGIN_SURVIVOR,
                           extra={"stopped_because": stopped} if stopped else None,
                           tried=_tried(defect, operations)))
    return _cap(items)


# ---------------------------------------------------------------------------------------------
# DIRECTIVE OUTCOMES - what became of each thing an engineer told the repair to do.

OUTCOME_KEPT = "kept"
OUTCOME_ROLLED_BACK = "rolled_back"
OUTCOME_UNPLAYABLE = "unplayable"


def directive_outcomes(directives, operations, refusals) -> list[dict]:
    """`[{item_id, source, outcome, reason}]`, one per directive, in the order they were given.

    THE JOIN. An operation entry does not carry the directive that proposed it, so an entry is
    matched to a directive by what the directive named: the operation's name AND at least one
    target fingerprint in common. Each entry answers at most one directive (a repeated directive
    consumes entries in order), so two directives on the same defect are not both credited with
    one run. A directive the loop could not play is matched exactly: its refusal carries the
    directive dict itself. A directive in neither list was never reached.
    """
    ops = [o for o in operations or () if isinstance(o, dict)]
    skipped = [r for r in refusals or ()
               if isinstance(r, dict) and r.get("refusal") == "directive_unplayable"]
    used: set[int] = set()
    out: list[dict] = []
    for directive in directives or ():
        if not isinstance(directive, dict):
            continue
        row = {"item_id": directive.get("item_id"), "source": directive.get("source", "")}
        refusal = next((r for r in skipped if r.get("directive") == directive), None)
        if refusal is not None:
            skipped.remove(refusal)
            out.append({**row, "outcome": OUTCOME_UNPLAYABLE,
                        "reason": str(refusal.get("reason", ""))})
            continue
        wanted = directive.get("targets") or ()
        wanted = {wanted} if isinstance(wanted, str) else {str(t) for t in wanted}
        match = next((i for i, o in enumerate(ops)
                      if i not in used and o.get("name") == directive.get("op")
                      and wanted & {str(t) for t in o.get("targets") or ()}), None)
        if match is None:
            out.append({**row, "outcome": OUTCOME_UNPLAYABLE, "reason": "not_reached"})
            continue
        used.add(match)
        op = ops[match]
        if op.get("status") == "kept":
            out.append({**row, "outcome": OUTCOME_KEPT, "reason": ""})
        else:
            out.append({**row, "outcome": OUTCOME_ROLLED_BACK,
                        "reason": str(op.get("reason", "") or op.get("status", ""))})
    return out


def attach_history(items: list[dict], prior: dict[str, list[dict]]) -> list[dict]:
    """Items whose fingerprint was decided before carry `decision_history` (oldest first)."""
    out = []
    for item in items:
        history = prior.get(item.get("fingerprint") or "")
        out.append({**item, "decision_history": list(history)} if history else item)
    return out
