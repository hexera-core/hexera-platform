# Responsibility: v5's narrow AI role for `unknown` defects: a short DESCRIPTION of each, for the weekly review that
#                 turns recurring unknowns into new classes. It never picks a class. One metered call per file.
# Owns: `describe_unknown`, `DESCRIBE_TOOL`, `AI_FILE_CAP_USD`, `FileDescriber` (one file's describer).
# Boundaries: no geometry and no search. The call goes through v4's route and is admitted and settled on the file's
#             `ModelLedger` (`model_route.metered_call`), so it shares the file's $1 cap with the search_ai fallback.
#             Defects are the loop's frozen `local_verify.Defect`s: answers come back via `dataclasses.replace`.
#             It never raises: a failed call records its error in `Defect.ai`.
"""AI descriptions of the defects nobody could locate (spec "AI role (narrow)")."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect

#: the spec's cap: $1 per file, for every model call v5 makes on it (this one and the search_ai fallback)
AI_FILE_CAP_USD = 1.00
MAX_DEFECTS = 12
EVIDENCE_CHARS = 240
DESCRIPTION_CHARS = 300

_SYSTEM = """You describe CAD geometry failures that block tetrahedral meshing (gmsh, Netgen) or B-rep validity and
that no rule could locate. For each defect, say in one or two sentences what is most likely wrong, from the evidence
given only. Answer by calling describe_defects once, one entry per defect id."""

DESCRIBE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "describe_defects",
        "description": "Describe each defect.",
        "parameters": {
            "type": "object",
            "properties": {"defects": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "description": {"type": "string"}},
                "required": ["id", "description"], "additionalProperties": False}}},
            "required": ["defects"],
            "additionalProperties": False,
        },
    },
}


def _key(i: int) -> str:
    """The id the model sees for the i-th defect. Positional, not `Defect.id`: the loop's ids are site ids, and one
    site with two failing codes is two defects with ONE id."""
    return f"D{i + 1}"


def messages_for(defects: Sequence[Defect]) -> list[dict]:
    brief = [{"id": _key(i), "code": d.code, "rule": d.rule, "priority": d.kind,
              "evidence": [{**e, "message": str(e.get("message", ""))[:EVIDENCE_CHARS]} for e in d.evidence][:6],
              "cause": json.loads(json.dumps(d.cause, default=str))} for i, d in enumerate(defects)]
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "Defects nobody could locate:\n"
                                        + json.dumps(brief, default=str, separators=(",", ":"))}]


def _answers(reply: Any) -> dict[str, str] | str:
    """{model id: description}, or the error in words."""
    if reply.failure_marker:
        return f"provider failure: {reply.failure_marker}"
    calls = [c for c in reply.tool_calls if c.name == "describe_defects"]
    if not calls:
        return "the model did not call describe_defects"
    try:
        rows = json.loads(calls[0].arguments).get("defects", [])
    except (ValueError, AttributeError):
        return "the model's arguments were not a JSON object"
    return {str(r.get("id")): str(r.get("description", ""))[:DESCRIPTION_CHARS]
            for r in rows if isinstance(r, dict)} if isinstance(rows, list) else "no defects list"


def describe_unknown(defects: Sequence[Defect], *, ledger: Any, file_id: str, route: str,
                     cap_usd: float = AI_FILE_CAP_USD, call: Any = None) -> list[Defect]:
    """`defects` in their order, each `unknown` one (up to MAX_DEFECTS) with `ai` = `{description, route}` or
    `{error, route}`. Classes are never changed. `call` is for tests; the default is the metered real route."""
    asked = [i for i, d in enumerate(defects) if d.cls == "unknown"][:MAX_DEFECTS]
    out = list(defects)
    if not asked:
        return out
    if call is None:
        from meshpipeline.cad.repair_v4.model_route import metered_call

        call = metered_call(ledger, file_id, cap_usd, kind="v5_describe")
    try:
        priced = asyncio.run(call(messages_for([defects[i] for i in asked]), [DESCRIBE_TOOL], route=route,
                                  job_id=file_id, tool_choice="required", cache_key="v5_describe"))
        got: dict[str, str] | str = _answers(priced.reply)
    except Exception as exc:  # noqa: BLE001 - a refused or failed call is recorded, it never raises
        got = f"{type(exc).__name__}: {exc}"[:DESCRIPTION_CHARS]
    for n, i in enumerate(asked):
        ai = ({"error": got} if isinstance(got, str)
              else {"description": got[_key(n)]} if _key(n) in got else {"error": "no answer for this defect"})
        out[i] = dataclasses.replace(out[i], ai={**ai, "route": route})
    return out


class FileDescriber:
    """`repair_v5`'s `ai_describe`: `describe_ai.describe_unknown` on the file's ledger, each unknown (id, code)
    described ONCE per file (the loop re-diagnoses every round), its $ counted for the row."""

    def __init__(self, ledger: Any, file_id: str, route: str, cap_usd: float) -> None:
        self.ledger, self.file_id, self.route, self.cap_usd = ledger, file_id, route, cap_usd
        self.spent_usd, self.calls = 0.0, 0
        self.described: dict[tuple[str, str], dict] = {}

    def __call__(self, defects: list) -> list:
        from meshpipeline.cad.repair_v4.model_route import metered_call

        known = [dataclasses.replace(d, ai=self.described[(d.id, d.code)])
                 if d.cls == "unknown" and (d.id, d.code) in self.described else d for d in defects]
        if not any(d.cls == "unknown" and d.ai is None for d in known):
            return known
        inner = metered_call(self.ledger, self.file_id, self.cap_usd, kind="v5_describe")

        async def call(*a: Any, **kw: Any) -> Any:
            self.calls += 1
            priced = await inner(*a, **kw)
            self.spent_usd += float(getattr(priced, "cost_usd", 0.0) or 0.0)
            return priced

        fresh = [d for d in known if not (d.cls == "unknown" and d.ai is not None)]
        got = iter(describe_unknown(fresh, ledger=self.ledger, file_id=self.file_id, route=self.route,
                                    cap_usd=self.cap_usd, call=call))
        out = [d if d.cls == "unknown" and d.ai is not None else next(got) for d in known]
        for d in out:
            if d.cls == "unknown" and d.ai is not None:
                self.described[(d.id, d.code)] = d.ai
        return out

    def summary(self) -> dict[str, Any]:
        return {"calls": self.calls, "spent_usd": round(self.spent_usd, 5),
                "described": [{"defect": f"{k[0]}|{k[1]}", **v} for k, v in list(self.described.items())[:20]]}
