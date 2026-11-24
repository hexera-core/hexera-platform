# Responsibility: B25's pick - the model chooses ONE of the repairs the arm offers, by index, metered on the
#                 file's ledger, and an answer it was not offered is ignored loudly.
# Boundaries: no model and no pool. The call is a fake returning a priced reply; the ledger is a real WaveLedger.
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.seq_ai import PICK, SeqPicker


@dataclass
class _ToolCall:
    name: str
    arguments: str


@dataclass
class _Reply:
    tool_calls: list[_ToolCall] = field(default_factory=list)
    input_tokens: int = 500
    cached_input_tokens: int = 0
    output_tokens: int = 40
    cache_write_tokens: int = 0
    cache_write_30m_tokens: int = 0
    failure_marker: str | None = None
    finish_reason: str = "tool_calls"


@dataclass
class _Priced:
    reply: _Reply
    provider: str = "openai"
    model: str = "gpt-6-astra"
    cost_usd: float = 0.004


def _call_returning(*replies: _Reply) -> Any:
    seen: list[dict] = []

    async def call(messages: list[dict], tools: list[dict], **kw: Any) -> _Priced:
        seen.append({"messages": messages, "tools": tools, **kw})
        return _Priced(replies[min(len(seen) - 1, len(replies) - 1)])

    call.seen = seen  # type: ignore[attr-defined]
    return call


def _pick_reply(index: Any, why: str = "the measured clash is tiny") -> _Reply:
    return _Reply([_ToolCall(PICK, json.dumps({"index": index, "why": why}))])


def _picker(call: Any, **kw: Any) -> SeqPicker:
    return SeqPicker("openai:gpt-6-astra", ledger=WaveLedger.for_file(1.0), usd_ceiling=1.0,
                     file_id="f" * 64, call=call, prices=(1.0, 2.0, 0.5), **kw)


def _defect(code: str = "body_overlap") -> Defect:
    return Defect(id="S1", kind="validity", code=code, entities=("B1", "B2"),
                  measurement={"clash": {"a": "B1", "b": "B2", "dist_mm": 0.25}, "diagonal_mm": 250.0})


CANDIDATES = [("conformalise",), ("fuse",), ("fuse", "conformalise")]


def test_the_model_chooses_by_INDEX_so_it_cannot_name_an_op_or_set_a_parameter() -> None:
    call = _call_returning(_pick_reply(1))
    picker = _picker(call)
    assert picker(_defect(), CANDIDATES) == ("fuse",)
    # the tool it was given has exactly two fields, and neither can carry an op or a parameter
    (sent,) = call.seen
    schema = sent["tools"][0]["function"]["parameters"]
    assert sorted(schema["properties"]) == ["index", "why"] and schema["additionalProperties"] is False
    assert picker.answered == 1 and picker.spent_usd == pytest.approx(0.004)


def test_an_index_it_was_not_offered_is_IGNORED_loudly() -> None:
    picker = _picker(_call_returning(_pick_reply(7)))
    assert picker(_defect(), CANDIDATES) is None
    assert picker.not_offered == 1
    assert "not one of the 3 candidates" in picker.result()["picks"][0]["ignored"]


def test_a_non_integer_or_malformed_answer_is_ignored_too() -> None:
    for bad in (_pick_reply("fuse"), _Reply([_ToolCall(PICK, "{not json")]), _Reply([])):
        picker = _picker(_call_returning(bad))
        assert picker(_defect(), CANDIDATES) is None


def test_the_prompt_names_NO_file_and_carries_the_measurement() -> None:
    """B21b's rule: the model must not be able to recognise a dev file and recite its answer."""
    call = _call_returning(_pick_reply(0))
    _picker(call)(_defect(), CANDIDATES)
    user = call.seen[0]["messages"][-1]["content"]
    assert "f" * 64 not in user and ".step" not in user and "sha" not in user.lower()
    body = json.loads(user[user.index("{"):])
    assert body["defect"]["code"] == "body_overlap"
    assert body["defect"]["measurement"]["worst_clash_mm"] == 0.25
    assert [c["index"] for c in body["candidates"]] == [0, 1, 2]   # every candidate offered, numbered
    assert body["defect"]["entities"] == 2                         # a COUNT, never the ids' provenance


def test_a_failed_round_spends_NOTHING_and_returns_no_pick() -> None:
    picker = _picker(_call_returning(_Reply(failure_marker="repair_v4_propose:rate_limit")))
    assert picker(_defect(), CANDIDATES) is None
    assert picker.failed == 1 and picker.spent_usd == 0.0


def test_an_EMPTY_candidate_list_is_never_asked_because_the_model_may_not_invent_one() -> None:
    call = _call_returning(_pick_reply(0))
    picker = _picker(call)
    assert picker(_defect("a_kind_nobody_has_measured"), []) is None
    assert call.seen == [] and picker.asked == 0


def test_the_file_ceiling_stops_the_picking_and_the_arm_falls_back_to_the_table() -> None:
    picker = _picker(_call_returning(_pick_reply(0)), max_picks=2)
    for _ in range(3):
        picker(_defect(), CANDIDATES)
    assert picker.asked == 2 and picker.stop_reason == "max_picks"


def test_seq_asks_the_picker_only_where_the_table_cannot_decide() -> None:
    """The wiring B25 adds: `pick_repair` reaches the model on a declared tie, and never where the table leads."""
    asked: list[str] = []

    def ai(defect: Any, candidates: Any) -> Any:
        asked.append(defect.code)
        return candidates[1] if candidates else None

    tie = seq.pick_repair(_defect("body_overlap"), ai_pick=ai)          # TIED: conformalise vs fuse
    assert tie.by == "ai" and asked == ["body_overlap"]
    led = seq.pick_repair(_defect("edge_through_face"), ai_pick=ai)     # the table leads: no call
    assert led.by == "table" and asked == ["body_overlap"]
