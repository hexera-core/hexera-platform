# Responsibility: Translate an engineer's free-text redirect into a catalogue directive, or honestly refuse.
# Owns: the interpreter prompt, the Interpretation result and its playback string.
# Boundaries: it NEVER executes; the directive is judged by the review session. Total: never raises.
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from meshpipeline.agents.repair_review import _wire
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)

NO_MODEL = ("review is running without a model, so i cannot read free-text redirects right "
            "now; pick one of the listed operations instead")

INTERPRETER_SYSTEM = (
    "you translate an engineer's note about how to repair one defect in a CAD part into a "
    "repair directive. the only operations that exist are:\n"
    f"{_wire.CATALOGUE_TEXT}\n"
    "reply with one JSON object and nothing else. if the note maps onto one operation: "
    '{"kind": "directive", "op": "<catalogue name>", "scope": "item" or "group", '
    '"playback": "<one lowercase sentence restating what you will try>"}. use scope "group" '
    "only if the engineer says all of them or every one. if the note asks for anything else, "
    'such as moving, redrawing or deleting geometry, or changing dimensions: {"kind": '
    '"refusal", "reason": "<lowercase sentence naming what cannot be done>"}.')


@dataclass(frozen=True)
class Interpretation:
    kind: str                       # "directive" | "refusal"
    playback: str = ""              # what the engineer confirms
    op: str = ""
    scope: str = ""                 # "item" | "group"
    targets: list = field(default_factory=list)
    defect_code: str = ""
    defect_fingerprint: str = ""
    item_id: str = ""
    source: str = "engineer"
    reason: str = ""

    @property
    def directive(self) -> dict:
        """The Task-4 wire dict."""
        return {"op": self.op, "targets": list(self.targets), "defect_code": self.defect_code,
                "defect_fingerprint": self.defect_fingerprint, "source": self.source,
                "item_id": self.item_id}


def _refusal(reason: str) -> Interpretation:
    return Interpretation(kind="refusal", reason=reason, playback=reason)


def _honest(reason: object) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        text = "i could not turn that into a repair"
    return f"{text}; {_wire.CATALOGUE_LIMITS}"


async def interpret(text: str, item: dict, group: list[dict], *, job_id: str,
                    stopped_because: str = "") -> Interpretation:
    try:
        return await _interpret(text, item, group, job_id, stopped_because)
    except Exception as exc:  # noqa: BLE001 - total
        logger.warning("repair review interpreter failed: %s", type(exc).__name__)
        return _refusal(_honest("i could not read that redirect"))


async def _interpret(text: str, item: dict, group: list[dict], job_id: str,
                     stopped_because: str) -> Interpretation:
    if not _wire.configured():
        return _refusal(NO_MODEL)
    user = json.dumps({
        "note": _wire.clip_text(text, 600), "defect_code": item.get("defect_code") or "",
        "entity": f"{item.get('entity_type')}#{item.get('entity_index')}",
        "measurements": _wire.clip_measurements(item.get("measurements")),
        "tried": _wire.tried_of([item]),
        "message": _wire.clip_text(item.get("message")),
        "current_proposal": item.get("proposal"), "group_size": len(group),
        "story": _wire.story_of(group or [item], stopped_because)}, default=str)
    reply = await _wire.ask(INTERPRETER_SYSTEM, user, job_id=job_id,
                            timeout_s=float(rcfg.CAD_REPAIR_REVIEW_LLM_TIMEOUT_S))
    if reply is None:
        return _refusal(_honest("i could not read a clear answer from the model"))
    if reply.get("kind") != "directive":
        return _refusal(_honest(reply.get("reason")))
    op = str(reply.get("op") or "")
    if op not in _wire.CATALOGUE:
        return _refusal(_honest(f"{op or 'that'} is not a repair operation i have"))
    scope = "group" if reply.get("scope") == "group" else "item"
    members = group if scope == "group" else [item]
    targets = [m["fingerprint"] for m in members if m.get("fingerprint")]
    if not targets:
        return _refusal(_honest("that defect has no identity i can target"))
    playback = " ".join(str(reply.get("playback") or "").split()) or (
        f"i will try {op} on {'every defect in this group' if scope == 'group' else 'this defect'}")
    return Interpretation(
        kind="directive", playback=playback, op=op, scope=scope, targets=targets,
        defect_code=str(item.get("defect_code") or ""),
        defect_fingerprint=str(item.get("fingerprint") or ""),
        item_id=str(item.get("id") or item.get("item_id") or ""))
