# Responsibility: Propose a catalogue fix for review items the rules planner had nothing for.
# Owns: the proposer prompt, the batching per defect-code group, and the decline record.
# Boundaries: it NEVER executes; a proposal is a suggestion the review session judges under its caps.
# Total: no router, a timeout, an unparseable reply or an unknown operation leave proposal None.
from __future__ import annotations

import json
import logging

from meshpipeline.agents.repair_review import _wire
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)

RATIONALE_MAX = 200
SAMPLE_PER_GROUP = 5

PROPOSER_SYSTEM = (
    "you help an engineer decide how to repair a CAD part that automatic repair could not finish. "
    "you are shown one kind of defect, its measurements, what was already tried, and the part's "
    "story. pick the single operation from this catalogue most likely to help, or decline.\n"
    f"{_wire.CATALOGUE_TEXT}\n"
    "reply with one JSON object and nothing else: "
    '{"op": "<catalogue name or decline>", "rationale": "<one lowercase sentence, under 200 '
    'characters, saying why>"}. never invent an operation outside the catalogue; decline '
    "when none fits.")


def _context(code: str, members: list[dict]) -> str:
    sample = [{"entity": f"{m.get('entity_type')}#{m.get('entity_index')}",
               "severity": m.get("severity"), "message": m.get("message"),
               "measurements": m.get("measurements") or {}} for m in members[:SAMPLE_PER_GROUP]]
    tried = sorted({str(t) for m in members
                    for t in ((m.get("measurements") or {}).get("tried") or [])})
    origins = sorted({str(m.get("origin")) for m in members})
    return json.dumps({"defect_code": code, "count": len(members), "tried": tried,
                       "story": {"origins": origins}, "samples": sample}, default=str)


def _rationale(text: object) -> str:
    out = " ".join(str(text or "").split()).lower()
    return out[:RATIONALE_MAX]


def _decline(job_id: str, code: str, reason: str, count: int) -> None:
    from meshpipeline.capture.logger import TrainingLogger
    TrainingLogger(job_id).log(
        "repair_review", {"decision": "decline", "defect_code": code, "reason": reason,
                          "items": count}, op_id="review:propose")


async def propose_for_groups(items: list[dict], *, job_id: str) -> list[dict]:
    try:
        return await _propose(items, job_id)
    except Exception as exc:  # noqa: BLE001 - total
        logger.warning("repair review proposer failed: %s", type(exc).__name__)
        return items


async def _propose(items: list[dict], job_id: str) -> list[dict]:
    if not _wire.configured():
        return items
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item.get("proposal") is None:
            groups.setdefault(str(item.get("defect_code")), []).append(item)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ranked = ranked[:max(0, int(rcfg.CAD_REPAIR_REVIEW_LLM_CALLS_MAX))]
    filled: dict[int, dict] = {}
    for code, members in ranked:
        reply = await _wire.ask(PROPOSER_SYSTEM, _context(code, members), job_id=job_id,
                                timeout_s=float(rcfg.CAD_REPAIR_REVIEW_LLM_TIMEOUT_S))
        if reply is None:
            _decline(job_id, code, "no usable reply", len(members))
            continue
        op = str(reply.get("op") or "")
        if op not in _wire.CATALOGUE:
            _decline(job_id, code, "declined" if op == "decline" else f"unknown operation {op!r}",
                     len(members))
            continue
        rationale = _rationale(reply.get("rationale"))
        for member in members:
            filled[id(member)] = {
                "source": "llm", "provenance": "llm", "op": op, "defect_code": code,
                "targets": [member["fingerprint"]] if member.get("fingerprint") else [],
                "rationale": rationale}
    return [{**item, "proposal": filled[id(item)]} if id(item) in filled else item
            for item in items]
