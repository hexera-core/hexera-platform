# Responsibility: Plan ahead - turn an inspection report into an ordered list of repair directives.
# Owns: the planner prompt, per-proposal validation, directive stamping and the decline record.
# Boundaries: it NEVER executes; a directive is a suggestion the repair session judges under its
#             caps, rollback and progress rule, exactly as it judges the rules planner's.
# Total: no router, a timeout, an unparseable reply or an invalid proposal leave fewer directives.
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from meshpipeline.agents.repair_review import _wire
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)

SOURCE = "llm-planner"

PLANNER_SYSTEM = (
    "you help an engineer repair a CAD part. you are shown every located defect the inspection "
    "found (each with a fingerprint, its kind and measurements), what was already tried, and "
    "the part's story. plan the order in which the repair catalogue should be tried, most "
    "promising first.\n"
    f"{_wire.CATALOGUE_TEXT}\n"
    "reply with one JSON object and nothing else: "
    '{"proposals": [{"op": "<catalogue name>", "defect_fingerprint": "<fingerprint from the '
    'defects>", "targets": ["<fingerprints, all of one kind>"], "confidence": <number from 0 '
    'to 1>, "rationale": "<one lowercase sentence, under 200 characters>"}]}. never invent an '
    "operation outside the catalogue; fill_planar_hole takes exactly one target; reply with an "
    'empty "proposals" list when nothing fits.')


@dataclass
class PlanResult:
    directives: list[dict] = field(default_factory=list)
    declines: list[dict] = field(default_factory=list)
    called: int = 0
    model_reply_cached: bool = False


def _context(report: dict, story: dict) -> str:
    defects = _wire.clip_defects(report.get("entities"))
    return json.dumps({"defects": defects, "tried": _wire.tried_of(list(report.get("entities") or [])[:_wire.DEFECTS_MAX]),
                       "story": story}, default=str)


def _log_decline(job_id: str, reason: str, proposal: object) -> dict:
    from meshpipeline.capture.logger import TrainingLogger
    row = {"decision": "decline", "reason": reason,
           "proposal": _wire.clip_text(json.dumps(proposal, default=str), 200)}
    try:
        TrainingLogger(job_id).log("repair_review", row, op_id="review:plan")
    except Exception as exc:  # noqa: BLE001 - total
        logger.warning("repair review plan decline not logged: %s", type(exc).__name__)
    return row


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _validate(proposal: object, code_of: dict[str, str]) -> tuple[dict | None, str]:
    if not isinstance(proposal, dict):
        return None, "proposal is not an object"
    op = str(proposal.get("op") or "")
    if op not in _wire.CATALOGUE:
        return None, f"unknown operation {op!r}"
    fp = str(proposal.get("defect_fingerprint") or "")
    if fp not in code_of:
        return None, "defect fingerprint is not in the scan"
    raw = proposal.get("targets")
    targets = [str(t) for t in raw] if isinstance(raw, list) and raw else [fp]
    unknown = [t for t in targets if t not in code_of]
    if unknown:
        return None, "a target fingerprint is not in the scan"
    if len({code_of[t] for t in targets}) > 1:
        return None, "targets span different defect codes"
    confidence = _confidence(proposal.get("confidence"))
    if confidence is None:
        return None, "confidence missing or not a number"
    if confidence < float(rcfg.CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE):
        return None, f"confidence {confidence:g} is below the minimum"
    if op == "fill_planar_hole" and len(targets) != 1:
        return None, "fill_planar_hole takes exactly one target"
    return {"op": op, "targets": targets, "defect_code": code_of[fp], "defect_fingerprint": fp,
            "source": SOURCE, "item_id": "", "planner_confidence": confidence}, ""


async def _ask(user: str, job_id: str, result: PlanResult) -> list | None:
    calls = max(1, int(rcfg.CAD_REPAIR_LLM_PLANNER_CALLS_MAX))
    for _ in range(calls):
        result.called += 1
        reply = await _wire.ask(PLANNER_SYSTEM, user, job_id=job_id,
                                timeout_s=float(rcfg.CAD_REPAIR_LLM_PLANNER_TIMEOUT_S))
        if reply is not None and isinstance(reply.get("proposals"), list):
            return reply["proposals"]
        result.declines.append(_log_decline(job_id, "no usable reply", reply))
    return None


async def llm_plan(report: dict, *, story: dict, job_id: str = "") -> PlanResult:
    result = PlanResult()
    try:
        if not _wire.configured():
            return result
        user = _context(report or {}, story or {})
        code_of = {str(e["fingerprint"]): str(e.get("code") or "")
                   for e in (report or {}).get("entities") or ()
                   if isinstance(e, dict) and e.get("fingerprint")}
        proposals = await _ask(user, job_id, result)
        for proposal in proposals or []:
            directive, reason = _validate(proposal, code_of)
            if directive is None:
                result.declines.append(_log_decline(job_id, reason, proposal))
                continue
            result.directives.append({**directive, "directive_id": f"llm:{len(result.directives)}"})
    except Exception as exc:  # noqa: BLE001 - total
        logger.warning("repair review planner failed: %s", type(exc).__name__)
    return result
