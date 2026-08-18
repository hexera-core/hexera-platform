# Responsibility: Verify the agent-loop contract is frozen, self-contained, and decides nothing on the agent's behalf.
from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from meshpipeline.contracts import agent_loop as al

SRC = Path(al.__file__).resolve().parents[1]
MODULE = Path(al.__file__)


def _imported_subpackages(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("meshpipeline."):
            mods.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("meshpipeline."):
                    mods.add(a.name.split(".")[1])
    return mods


# neutral import direction
def test_the_contract_imports_nothing_above_itself():
    forbidden = {"adapters", "application", "api", "runtime", "persistence",
                 "agents", "engines", "pipeline", "cad", "render", "sandbox", "capture",
                 "agent_tools"}
    assert not (_imported_subpackages(MODULE) & forbidden)


def test_the_contract_imports_no_other_contract_module():
    assert _imported_subpackages(MODULE) <= {"contracts"}
    assert "from meshpipeline.contracts" not in MODULE.read_text()


# records are immutable
@pytest.mark.parametrize("cls", [al.CategoryCount, al.LoopLimits, al.LoopTally, al.RoundRecord,
                                 al.ToolCallRecord, al.ProgressObservation, al.AgentRunRecord])
def test_every_durable_record_is_frozen(cls):
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen, f"{cls.__name__} must be frozen"


# repository enum convention
@pytest.mark.parametrize("enum_cls", [al.AgentRole, al.LoopStage, al.LoopExit])
def test_enums_are_string_enums_with_lowercase_values(enum_cls):
    assert issubclass(enum_cls, str), f"{enum_cls.__name__} must be a str Enum (FailureClass style)"
    for m in enum_cls:
        assert m.value == m.value.lower() and " " not in m.value
        assert enum_cls(m.value) is m           # round-trips through its serialized form


def test_agent_role_covers_exactly_the_three_real_agents():
    assert {m.value for m in al.AgentRole} == {"intake", "builder", "reviewer"}


def test_loop_exit_never_carries_a_domain_verdict():
    for m in al.LoopExit:
        assert m.value not in ("pass", "fail", "passed", "failed")


# ToolCallRecord is sanitized by construction
FORBIDDEN_FIELD_HINTS = (
    "argument", "args", "reason", "prompt", "message", "content", "result", "output",
    "evidence", "axis", "text", "token_str", "credential", "secret", "url", "geometry",
    "payload", "response", "detail", "raw",
)


def test_tool_call_record_exposes_exactly_the_approved_accountability_fields():
    assert [f.name for f in dataclasses.fields(al.ToolCallRecord)] == [
        "round_index", "call_index", "tool", "category",
        "malformed", "accepted", "duration_ms"]


def test_tool_call_record_cannot_be_given_a_payload():
    with pytest.raises(TypeError):
        al.ToolCallRecord(round_index=0, call_index=0, tool="t", category="c",
                          raw_arguments='{"secret": 1}')      # type: ignore[call-arg]


# Protocol conventions
@pytest.mark.parametrize("proto", [al.RunExtension, al.AgentLoopPolicy])
def test_protocols_follow_the_repository_pattern(proto):
    assert getattr(proto, "_is_protocol", False)
    assert getattr(proto, "_is_runtime_protocol", False), "repo Protocols are @runtime_checkable"


def test_the_policy_protocol_asks_the_agent_and_decides_nothing_itself():
    methods = {n for n, _ in inspect.getmembers(al.AgentLoopPolicy, inspect.isfunction)
               if not n.startswith("_")}
    assert {"limits", "category_of", "observe", "extension"} <= methods
    for banned in ("is_valid", "passes_review", "covers_axis", "is_authorized", "is_complete"):
        assert banned not in methods


# unset limits enforce nothing
def test_every_limit_defaults_to_unset():
    lim = al.LoopLimits()
    for f in dataclasses.fields(lim):
        v = getattr(lim, f.name)
        assert v is None or v == (), f"{f.name} ships with a guessed value {v!r}"


def test_an_unset_limit_yields_no_remaining_budget_and_never_escalates():
    t = al.LoopTally(rounds=999, tool_calls=999, elapsed_s=1e6)
    unset = al.LoopLimits()
    assert t.remaining_rounds(unset) is None
    assert t.remaining_s(unset) is None
    assert t.stage(unset) is al.LoopStage.running


def test_stage_escalates_only_against_configured_thresholds():
    lim = al.LoopLimits(max_rounds=30, warn_at_remaining_rounds=8,
                        closing_at_remaining_rounds=3)
    assert al.LoopTally(rounds=10).stage(lim) is al.LoopStage.running
    assert al.LoopTally(rounds=22).stage(lim) is al.LoopStage.warned
    assert al.LoopTally(rounds=28).stage(lim) is al.LoopStage.closing


def test_category_counts_are_an_ordered_hashable_pair_list():
    counts = (al.CategoryCount("navigation", 12), al.CategoryCount("verdict", 9))
    assert hash(counts)
    assert al.count_for(counts, "navigation") == 12
    assert al.count_for(counts, "absent") is None
