# Responsibility: the model's PICK for `search_seq_ai` (B25) - one metered call that chooses ONE of the repairs
#                 this arm already offers, and nothing else.
# Owns: SeqPicker (an `seq.AIPick`), its tool and its prompt.
# Boundaries: the candidates, the parameters and the decision to ask at all are `seq.py`'s. The metering is
#             `model_ledger.WaveLedger` and `model_throttle`, used exactly as `proposer.AIProposer` uses them.
"""The AI picks; it never invents.

`search_seq` asks a model only where the rule table cannot decide: no entry for the defect's code, or a declared
tie at the top (`seq.TIED`). This is that call, and it is deliberately the smallest thing that can answer it.

THE MODEL CHOOSES AN INDEX. The tool takes an integer into the list of candidates this arm built, so "never
invent an op" and "never set parameters" are STRUCTURAL rather than policed: there is no field in which an op
name or a parameter could arrive. An index that is not in the list is ignored, loudly - counted, recorded with
its reason, and the table's own order stands (seq.pick_repair's rule, unchanged).

WHAT IT IS TOLD: the defect's code, its measurement, how many entities it is on, and the numbered candidates.
NO FILE NAME, NO SHA, NO SPLIT (B21b's rule: the model must not be able to recognise a dev file and recite its
answer). One turn, no tool loop, no images.

METERING is the proposer's, not a second scheme: estimate an upper bound, `admit` it against the file's ceiling
and the wave cap, take a throttle slot, call, then `settle` the real cost (or `release` when the call failed and
was never billed). A refusal to admit, a throttle slot that never comes, or a failed round all mean the same
thing here - no pick - and the arm falls back to the table's order.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.proposer import (
    _RATE_LIMITED_MARKER,
    _run_sync,
    estimate_call_usd,
)

#: what one pick may spend its output on: a choice and a sentence, never a plan
PICK_OUTPUT_TOKENS = 256
#: refuse a call whose estimated input is larger than this (the prompt is tiny; this is a guard, not a budget)
MAX_INPUT_TOKENS = 8000

PICK = "pick"
PICK_TOOL: list[dict] = [{
    "type": "function",
    "function": {
        "name": PICK,
        "description": ("Choose exactly ONE of the numbered repairs for this defect. You may only choose from "
                        "the list; you cannot propose an op or set a parameter."),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 0,
                          "description": "the 0-based number of the repair you choose"},
                "why": {"type": "string", "description": "one sentence: why this one, from the measurement"},
            },
            "required": ["index", "why"],
            "additionalProperties": False,
        },
    },
}]

SYSTEM = (
    "You choose repairs for one CAD defect at a time, in a sequential repair arm.\n"
    "You are given the defect's code, what was measured about it, and a numbered list of repairs that a table of "
    "measured outcomes offers for that code. The table could not separate them (it has no entry for this code, or "
    "its top entries are equally well evidenced), which is why you are being asked.\n"
    "Call the tool exactly once with the index of the ONE repair you choose. You cannot name an op that is not in "
    "the list and you cannot set parameters: the arm derives every parameter from the measurement.\n"
    "Prefer the repair whose mechanism matches what was measured. If nothing matches, choose the first."
)


class SeqPicker:
    """`seq.AIPick`: `(defect, candidates) -> Repair | None`. One metered model call per question."""

    def __init__(self, route: str, *, ledger: WaveLedger, usd_ceiling: float, file_id: str, job_id: str = "",
                 max_picks: int = 12, call: Any = None, throttle: Any = None,
                 prices: tuple[float, float, float] | None = None) -> None:
        if not isinstance(ledger, WaveLedger):
            raise TypeError("a WaveLedger is required: the cap is not optional")
        self.route = route
        self.file_id = file_id
        self.job_id = job_id
        self.usd_ceiling = usd_ceiling
        self.max_picks = max_picks
        self._ledger = ledger
        self._call = call
        self._throttle = throttle
        if prices is None:
            from meshpipeline.contracts.model_inference import repair_v4_route_prices
            prices = repair_v4_route_prices(route)
        self._prices = prices
        #: what the row reports
        self.asked = 0
        self.answered = 0
        self.failed = 0
        self.not_offered = 0
        self.spent_usd = 0.0
        self.stop_reason: str | None = None
        self.whys: list[dict] = []

    # --- the AIPick interface ---------------------------------------------------------------------------------

    def __call__(self, defect: Defect, candidates: Sequence[tuple[str, ...]]) -> tuple[str, ...] | None:
        if not candidates:
            # an EMPTY table is not a question this call can answer: the arm offers no candidate to choose from,
            # and the model may not invent one. The defect is skipped by the caller, which is the honest outcome.
            return None
        if self.stop_reason is not None or self.asked >= self.max_picks:
            self.stop_reason = self.stop_reason or "max_picks"
            return None
        return _run_sync(self._pick(defect, list(candidates)))

    async def _pick(self, defect: Defect, candidates: list[tuple[str, ...]]) -> tuple[str, ...] | None:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _question(defect, candidates)}]
        usd, tokens = estimate_call_usd(messages, PICK_TOOL, prices=self._prices,
                                        output_tokens=PICK_OUTPUT_TOKENS)
        if tokens > MAX_INPUT_TOKENS:
            self.stop_reason = "input_too_large"
            return None
        admission = self._ledger.admit(self.file_id, usd, file_ceiling_usd=self.usd_ceiling)
        if not admission.ok:
            self.stop_reason = admission.reason or "ledger_refused"
            return None
        assert admission.reservation is not None
        if self._throttle is not None and not self._throttle.acquire(tokens):
            self._ledger.release(admission.reservation)     # nothing was sent, so nothing was billed
            self.failed += 1
            return None
        self.asked += 1
        try:
            priced = await self._ask(messages)
        except BaseException:
            self._ledger.release(admission.reservation)
            if self._throttle is not None:
                self._throttle.release()
            raise
        reply = priced.reply
        if self._throttle is not None:
            self._throttle.release(rate_limited=reply.failure_marker == _RATE_LIMITED_MARKER)
        if reply.failure_marker:
            # the router has already retried: a failed round is not billed, so the reservation is RELEASED
            self._ledger.release(admission.reservation)
            self.failed += 1
            return None
        self._ledger.settle(admission.reservation, file_id=self.file_id, job_id=self.job_id,
                            provider=priced.provider, model=priced.model, input_tokens=reply.input_tokens,
                            cached_input_tokens=reply.cached_input_tokens, output_tokens=reply.output_tokens,
                            cost_usd=priced.cost_usd, cache_write_tokens=reply.cache_write_tokens,
                            cache_write_30m_tokens=reply.cache_write_30m_tokens)
        self.spent_usd += float(priced.cost_usd or 0.0)
        self.answered += 1
        return self._chosen(reply, defect, candidates)

    async def _ask(self, messages: list[dict]) -> Any:
        call = self._call
        if call is None:
            from meshpipeline.contracts.model_inference import call_repair_v4_propose
            call = call_repair_v4_propose
        return await call(messages, PICK_TOOL, route=self.route, job_id=self.job_id, user_id="",
                          tool_choice="auto", max_output_tokens=PICK_OUTPUT_TOKENS)

    def _chosen(self, reply: Any, defect: Defect, candidates: list[tuple[str, ...]]) -> tuple[str, ...] | None:
        """The candidate the model named, or None. An answer that is not one of the offered indices is IGNORED,
        loudly: counted, recorded with its reason, and the caller keeps the table's own order."""
        for call in getattr(reply, "tool_calls", None) or ():
            if getattr(call, "name", None) != PICK:
                continue
            try:
                args = json.loads(call.arguments or "{}")
            except ValueError:
                self.not_offered += 1
                self.whys.append({"defect": defect.code, "ignored": "arguments are not valid JSON"})
                continue
            index = args.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(candidates):
                self.not_offered += 1
                self.whys.append({"defect": defect.code, "ignored": f"index {index!r} is not one of the "
                                                                    f"{len(candidates)} candidates"})
                continue
            self.whys.append({"defect": defect.code, "chose": list(candidates[index]),
                              "why": str(args.get("why") or "")[:300]})
            return candidates[index]
        return None

    def result(self) -> dict[str, Any]:
        """What the row carries about the picking."""
        return {"asked": self.asked, "answered": self.answered, "failed": self.failed,
                "ignored_not_offered": self.not_offered, "spent_usd": round(self.spent_usd, 6),
                "stop_reason": self.stop_reason, "route": self.route, "picks": self.whys[:40]}


def _question(defect: Defect, candidates: list[tuple[str, ...]]) -> str:
    """The prompt. No file name, no sha, no split (B21b): the model must not be able to recognise a dev file."""
    body = {
        "defect": {"code": defect.code, "kind": defect.kind, "entities": len(defect.entities),
                   "overlap_pairs": len(defect.pairs), "measurement": _measurement(defect.measurement),
                   "failing_member": defect.member},
        "candidates": [{"index": i, "repair": list(r)} for i, r in enumerate(candidates)],
    }
    return ("Choose one repair for this defect. The table could not separate these candidates.\n"
            + json.dumps(body, sort_keys=True))


def _measurement(m: dict) -> dict:
    """The measurement, rounded and without anything that could name the file."""
    out: dict[str, Any] = {}
    for key in ("diagonal_mm", "free_loops", "coarse"):
        if m.get(key) is not None:
            out[key] = m[key]
    face = m.get("narrowest_face")
    if face:
        out["narrowest_face_mm"] = {"width": round(float(face["width_mm"]), 4),
                                    "length": round(float(face["length_mm"]), 4)}
    clash = m.get("clash")
    if clash:
        out["worst_clash_mm"] = round(float(clash["dist_mm"]), 4)
    if m.get("clash_by_pair"):
        out["clash_pairs_mm"] = sorted(round(float(v), 4) for v in m["clash_by_pair"].values())[:8]
    return out
