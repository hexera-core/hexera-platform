"""CRS v5 beta, Decision 1 and "Bypass permission": the three-way repair choice (meshing |
standalone | off), the review bypass bit, and where each is honoured - the dispatch contract,
the graph, the mesher handback and the approval that builds the payload."""
from __future__ import annotations

import pytest

from meshpipeline.agents.intake.approval import repair_goal_for
from meshpipeline.application import dispatch_contract
from meshpipeline.cad.repair.contracts import RepairGoal
from meshpipeline.pipeline import repair_goal as rg
from meshpipeline.pipeline.graph import route_after_engine_select, route_after_repair_attempt


@pytest.mark.parametrize("raw, choice", [
    ("meshing", "meshing"), ("standalone", "standalone"), ("off", "off"),
    ({"goal": "meshing"}, "meshing"), ({"goal": "standalone"}, "standalone"),
    ({"goal": "off"}, "off"),
])
def test_every_choice_parses_and_round_trips(raw, choice):
    asked = rg.parse(raw)
    assert asked.choice == choice
    again = rg.from_state({"repair_goal": asked.to_state()})
    assert again is not None and again.choice == choice


def test_off_is_a_meshing_goal_with_no_repair_and_drops_a_bypass():
    asked = rg.parse({"goal": "off", "auto_accept_repairs": True,
                      "auto_accept_deviation_mm": 0.1})
    assert asked.repair_off and asked.goal is RepairGoal.meshing
    assert not asked.bypass_review and asked.auto_accept_mm is None


def test_the_bypass_bit_must_be_a_real_boolean():
    assert rg.parse({"goal": "meshing", "auto_accept_repairs": True}).bypass_review
    for junk in ("true", 1, "yes", [True]):
        with pytest.raises(rg.GoalRequestError):
            rg.parse({"goal": "meshing", "auto_accept_repairs": junk})


def test_no_choice_is_repair_plus_mesh():
    assert rg.choice_of({}) == "meshing"
    assert not rg.repair_off({}) and not rg.bypass_review({})


def _payload(**extra) -> dict:
    return {"schema_version": dispatch_contract.DISPATCH_SCHEMA_VERSION, **extra}


def test_dispatch_contract_refuses_a_choice_the_run_cannot_read():
    with pytest.raises(dispatch_contract.DispatchContractError, match="repair_goal"):
        dispatch_contract._check_repair_goal(_payload(repair_goal={"goal": "benchmark"}), "t")
    with pytest.raises(dispatch_contract.DispatchContractError, match="repair_goal"):
        dispatch_contract._check_repair_goal(_payload(repair_goal="sideways"), "t")
    for ok in ({}, None, "off", {"goal": "standalone", "auto_accept_repairs": True}):
        dispatch_contract._check_repair_goal(_payload(repair_goal=ok), "t")


def test_mesh_only_skips_the_repair_check():
    assert route_after_engine_select({"repair_goal": {"goal": "off"}}) == "node_geometry_admission"
    assert route_after_engine_select({}) == "node_repair_inspect"
    assert route_after_engine_select({"repair_goal": {"goal": "standalone"}}) == \
        "node_repair_inspect"


def test_a_repair_waiting_for_review_ends_the_run():
    assert route_after_repair_attempt({"repair_review_wait": True}) == "__end__"
    assert route_after_repair_attempt(
        {"repair_review_wait": True, "repair_goal": {"goal": "standalone"}}) == "__end__"


def test_mesh_only_gets_no_mesher_handback(monkeypatch):
    from meshpipeline.pipeline import repair_handback as hb
    monkeypatch.setattr(hb, "posture", lambda: hb.LIVE)
    assert hb.wanted({"repair_goal": {"goal": "off"}}) == (False, hb.REPAIR_OFF)


def test_the_graph_wires_the_new_edges():
    from unittest.mock import patch

    from meshpipeline.pipeline import graph as G

    class _Recording:
        def __init__(self):
            self.cond: dict = {}

        def add_node(self, *a, **k):
            pass

        def add_edge(self, *a, **k):
            pass

        def add_conditional_edges(self, src, fn, mapping):
            self.cond[src] = dict(mapping)

        def compile(self, **k):
            return self

    rec = _Recording()
    with patch.object(G, "StateGraph", return_value=rec):
        G.build_graph(checkpointer=object())
    assert rec.cond["node_engine_select"]["node_geometry_admission"] == "node_geometry_admission"
    assert G.END in rec.cond["node_repair_attempt"]


@pytest.mark.parametrize("approved, confirmed, allowed, expected", [
    ({}, {}, False, {}),                                             # the default: no field
    ({}, {"goal": "standalone"}, False, {"goal": "standalone"}),     # the form's choice
    ({"repair_goal": "off"}, {"goal": "standalone"}, False, {"goal": "off"}),   # chat wins
    ({"repair_goal": "standalone"}, {}, False, {"goal": "standalone"}),  # "just fix my STEP"
    ({}, {"goal": "meshing", "auto_accept_repairs": True}, True,
     {"goal": "meshing", "auto_accept_repairs": True}),
    # NOT ALLOWED BY THE ORGANISATION: the bypass never reaches the payload
    ({}, {"goal": "meshing", "auto_accept_repairs": True}, False, {}),
    ({"repair_goal": "off"}, {"goal": "meshing", "auto_accept_repairs": True}, True,
     {"goal": "off"}),
    # the bypass is the FORM's only: prose never grants it
    ({"repair_goal": "standalone", "auto_accept_repairs": True}, {}, True,
     {"goal": "standalone"}),
])
def test_the_approval_builds_the_choice(approved, confirmed, allowed, expected):
    got = repair_goal_for(approved, confirmed, bypass_allowed=allowed)
    assert got == expected
    if got:
        dispatch_contract._check_repair_goal(_payload(repair_goal=got), "t")
