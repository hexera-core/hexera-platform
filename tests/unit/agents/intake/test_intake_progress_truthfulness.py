# Responsibility: Verify a repeated identical preview is not progress, and the signature never carries the token value.
from __future__ import annotations

import asyncio
import json

import pytest

import meshpipeline.agents.intake.executor as ex_mod
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
from meshpipeline.contracts.agent_loop import LoopTally

PREVIEW = {"selected_engine": "snappy", "purpose": "external_aero", "input_kind": "solid"}


@pytest.fixture
def rig(monkeypatch):
    st = IntakeExecutionState(
        session_id="s", owner_id="u", revision="r1", user_msg_count=1, rec_authorized=False,
        selection={"id": "sel1", "confirmed": True, "engine": "snappy"})
    monkeypatch.setattr(ex_mod.es, "verify_confirmed", lambda *a, **k: (True, ""))
    monkeypatch.setattr(ex_mod, "preview_admission",
                        lambda *a, **k: {"verdict": "supported", "selected_engine": "snappy"})
    monkeypatch.setattr(ex_mod.at, "CONFIRM_REQUIREMENTS_ASK", "SUMMARY")
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy", "cfmesh"],
                            search_tool=lambda *a, **k: "")
    return st, IntakeLoopPolicy(exec_state=st, executor=ex)


def _preview(policy, round_index, **over):
    args = {**PREVIEW, **over}
    policy.before_round(LoopTally(rounds=round_index), [])
    asyncio.run(policy.execute(type("I", (), {"tool": "preview_selected_admission",
                                              "parsed": args, "call_index": 1})()))
    return policy.observe(LoopTally(rounds=round_index + 1))


# the churn, and its absence
def test_the_first_valid_preview_is_progress(rig):
    st, p = rig
    assert _preview(p, 0).made_progress is True
    assert st.pending is not None


def test_repeating_the_identical_preview_is_not_progress(rig):
    _st, p = rig
    assert _preview(p, 0).made_progress is True
    assert _preview(p, 1).made_progress is False, "a reissued nonce is not an advance"


def test_four_identical_previews_yield_exactly_one_progress(rig):
    _st, p = rig
    observed = [_preview(p, i).made_progress for i in range(4)]
    assert observed == [True, False, False, False]
    assert sum(observed) == 1, "one material advance, not one per reissued token"


def test_the_token_value_changes_while_the_signature_does_not(rig):
    st, p = rig
    _preview(p, 0)
    first_token, first_sig = st.pending["token"], st.authorization_signature()
    _preview(p, 1)
    assert st.pending["token"] != first_token, "the nonce is still freshly minted"
    assert st.authorization_signature() == first_sig, "the MEANING did not change"


def test_the_signature_contains_no_token_value(rig):
    st, p = rig
    _preview(p, 0)
    assert st.pending["token"] not in st.authorization_signature()
    assert st.pending["fingerprint"] in st.authorization_signature()


# material changes ARE progress
def test_a_changed_canonical_payload_is_progress(rig):
    _st, p = rig
    assert _preview(p, 0).made_progress is True
    assert _preview(p, 1, purpose="internal_flow").made_progress is True


def test_a_changed_revision_is_progress(rig):
    st, p = rig
    assert _preview(p, 0).made_progress is True
    st.revision = "r2"                      # the user sent another message
    assert _preview(p, 1).made_progress is True


def test_a_changed_selection_is_progress(rig, monkeypatch):
    st, p = rig
    assert _preview(p, 0).made_progress is True
    monkeypatch.setattr(ex_mod.es, "propose",
                        lambda e, **k: {"id": "sel2", "engine": e})
    monkeypatch.setattr(ex_mod.es, "render_selection_statement", lambda e: f"Selected: {e}")
    monkeypatch.setattr(ex_mod.ap, "invalidate", lambda a, why: a)
    p.before_round(LoopTally(rounds=1), [])
    asyncio.run(p.execute(type("I", (), {"tool": "propose_engine_selection",
                                         "parsed": {"engine": "cfmesh"}, "call_index": 1})()))
    assert p.observe(LoopTally(rounds=2)).made_progress is True


# refusals are never progress
def test_a_rejected_token_is_not_progress(rig, monkeypatch):
    st, p = rig
    monkeypatch.setattr(ex_mod, "validate_submission", lambda a: [])
    monkeypatch.setattr(ex_mod.at, "verify_for_submit", lambda *a, **k: (False, "wrong revision"))
    p.before_round(LoopTally(rounds=0), [])
    asyncio.run(p.execute(type("I", (), {"tool": "submit_requirements",
                                         "parsed": {"mesh_engine": "snappy"},
                                         "call_index": 1})()))
    assert p.observe(LoopTally(rounds=1)).made_progress is False
    assert st.submit_args is None


def test_a_same_turn_escalation_refusal_is_not_progress(rig):
    st, p = rig
    st.recommended_this_turn = True
    p.before_round(LoopTally(rounds=0), [])
    asyncio.run(p.execute(type("I", (), {"tool": "submit_requirements",
                                         "parsed": {"mesh_engine": "snappy"},
                                         "call_index": 1})()))
    assert p.observe(LoopTally(rounds=1)).made_progress is False


def test_a_malformed_preview_is_not_progress(rig):
    st, p = rig
    p.before_round(LoopTally(rounds=0), [])
    asyncio.run(p.execute(type("I", (), {"tool": "preview_selected_admission",
                                         "parsed": None, "call_index": 1})()))
    assert p.observe(LoopTally(rounds=1)).made_progress is False
    assert st.pending is None, "a malformed call issues no token"
    assert p.malformed_calls == 1


# bounds are unchanged
def test_no_no_progress_threshold_is_activated(rig):
    _st, p = rig
    assert p.limits().no_progress_threshold is None
    assert p.limits().total_timeout_s is None


def test_the_round_bound_is_still_twenty():
    import meshpipeline.agents.intake.settings as icfg
    assert icfg.INTAKE_MAX_ROUNDS == 20


def test_the_sanitized_extension_never_carries_a_token(rig):
    st, p = rig
    _preview(p, 0)
    flat = json.dumps(dict(p.extension().sanitized()))
    assert st.pending["token"] not in flat
    assert "preview_token" not in flat
