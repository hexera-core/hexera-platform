# Responsibility: Verify what a turn hydrates, what it normalises, and what its two terminal patches may declare.
# Boundaries: the turn's data contract; the node is asserted to delegate normalisation rather than repeat it.
from __future__ import annotations

import pytest

from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.validation import normalise_submission


def _msgs(n_assistant=0, n_user=0, last_user=""):
    out = []
    for i in range(n_assistant):
        out.append({"role": "assistant", "content": f"a{i}"})
    for i in range(n_user):
        out.append({"role": "user", "content": last_user if i == n_user - 1 else f"u{i}"})
    return out


# turn budget

def test_a_fresh_conversation_is_not_nudged():
    b = turn.assess_budget(_msgs(), awaiting_confirmation=False)
    assert b.turn_count == 0 and not b.exhausted and b.turn_number == 1


def test_the_budget_fires_exactly_at_the_bound():
    assert not turn.assess_budget(_msgs(turn.MAX_TURNS - 1), awaiting_confirmation=False).exhausted
    assert turn.assess_budget(_msgs(turn.MAX_TURNS), awaiting_confirmation=False).exhausted


def test_a_confirmation_turn_is_never_nudged():
    assert not turn.assess_budget(_msgs(50), awaiting_confirmation=True).exhausted


def test_the_nudge_never_forces_a_submit():
    n = turn.BUDGET_NUDGE
    assert "Do NOT invent, assume, or guess any value" in n
    assert "unless every required field is genuinely established" in n
    assert "SINGLE most important piece of information still missing" in n
    for forced in ("Call submit_requirements() with all gathered",
                   "submit now with what you have", "make a best guess"):
        assert forced not in n


def test_the_nudge_is_recorded_as_synthetic_not_as_the_users_words():
    llm, state = turn.apply_budget_nudge([{"role": "system", "content": "s"}], [])
    assert llm[-1] == {"role": "user", "content": turn.BUDGET_NUDGE}
    assert state[-1]["_synthetic"] is True, (
        "the nudge was recorded as though the user said it")


def test_the_nudge_does_not_mutate_its_inputs():
    llm_in, state_in = [{"role": "system"}], []
    turn.apply_budget_nudge(llm_in, state_in)
    assert len(llm_in) == 1 and state_in == []


# hydration

def _state(**kw):
    base = {"job_id": "j1", "session_id": "s1", "user_id": "o1", "intake_gate": None}
    base.update(kw)
    return base


def test_hydration_reads_the_three_gate_sub_records():
    gate = {"admission": {"a": 1}, "selection": {"s": 2}, "approval": {"p": 3}}
    ctx = turn.hydrate(_state(intake_gate=gate), _msgs())
    assert ctx.pending == {"a": 1} and ctx.selection == {"s": 2} and ctx.approval == {"p": 3}


def test_hydration_copies_the_gate_rather_than_aliasing_it():
    gate = {"selection": {"engine": "cfmesh"}}
    ctx = turn.hydrate(_state(intake_gate=gate), _msgs())
    ctx.selection["engine"] = "gmsh"
    assert gate["selection"]["engine"] == "cfmesh", "the graph state's gate was mutated in place"


@pytest.mark.parametrize("gate", [None, {}, {"selection": None}, {"selection": {}}, "not-a-dict"])
def test_an_absent_or_malformed_gate_hydrates_to_none(gate):
    ctx = turn.hydrate(_state(intake_gate=gate), _msgs())
    assert ctx.selection is None


def test_the_latest_user_message_is_the_users_own_last_one():
    ctx = turn.hydrate(_state(), _msgs(n_assistant=2, n_user=3, last_user="use gmsh"))
    assert ctx.latest_user_msg == "use gmsh" and ctx.user_msg_count == 3


def test_recommendation_authority_comes_from_the_users_latest_message_only():
    asked = turn.hydrate(_state(), _msgs(n_user=1, last_user="which engine do you recommend?"))
    plain = turn.hydrate(_state(), _msgs(n_user=1, last_user="mesh the wing"))
    assert asked.rec_authorized is True
    assert plain.rec_authorized is False, "recommendation mode was authorized without being asked"


def test_hydration_is_not_a_copy_of_the_pipeline_state():
    fields = set(turn.TurnContext.__dataclass_fields__)
    for leaked in ("messages", "request_txt", "mesh_manifest", "openfoam_workspace",
                   "reviewer_verdict", "engine_params"):
        assert leaked not in fields, f"TurnContext carries {leaked} - it is a state copy"


# transcript

def test_the_system_message_is_never_serialised():
    out = turn.serialise_transcript(
        [{"role": "system", "content": "SECRET PROMPT"}, {"role": "user", "content": "hi"}], "ok")
    assert all(e["role"] != "system" for e in out)
    assert "SECRET PROMPT" not in str(out)


def test_tool_calls_and_ids_survive_serialisation():
    out = turn.serialise_transcript(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
         {"role": "tool", "tool_call_id": "c1", "content": "res"}], "")
    assert out[0]["tool_calls"] == [{"id": "c1"}] and out[1]["tool_call_id"] == "c1"


def test_the_synthetic_marker_survives_so_a_reader_can_tell_them_apart():
    _, state = turn.apply_budget_nudge([], [])
    out = turn.serialise_transcript(state, "")
    assert out[0]["_synthetic"] is True


def test_a_turn_with_no_assistant_text_serialises_no_assistant_entry():
    out = turn.serialise_transcript([{"role": "user", "content": "hi"}], "")
    assert [e["role"] for e in out] == ["user"]


# the three patches

def _ctx(**kw):
    base = {"job_id": "j", "session_id": "s", "owner_id": "o", "revision": 1,
            "latest_user_msg": "", "user_msg_count": 1, "source_ref": None,
            "pending": {"a": 1}, "selection": {"s": 2}, "approval": None, "rec_authorized": False}
    base.update(kw)
    return turn.TurnContext(**base)


def _record(**kw):
    base = {"finish_reason": "stop", "usage": {"total_tokens": 5}, "agent_run": {"r": 1},
            "search_events": ["e"], "public_trace": ["t"],
            "budget": turn.TurnBudget(2, False), "assistant_text": "hello",
            "transcript": [{"role": "user"}], "system_snapshot": "SYS"}
    base.update(kw)
    return turn.TurnRecord(**base)


def _requirements(**kw):
    args = {"domain": "wing", "request_txt": "r", "review_brief_txt": "b",
            "purpose": "external_cfd", "input_kind": "solid-body", "dimensionality": "3D",
            "mesh_engine": "cfmesh", "engine_source": "user", "engine_params": {},
            "patches": [{"name": "body", "type": "wall"}]}
    args.update(kw)
    return normalise_submission(args)


def test_both_terminal_patches_carry_the_same_common_keys():
    common = {"dispatch_confirmed", "messages", "intake_gate", "_intake_search_events",
              "_public_trace", "_intake_agent_run_event", "_intake_training_event"}
    cont = turn.continuing_patch(_ctx(), _record())
    done = turn.completed_patch(_ctx(), _record(), _requirements())
    assert common <= set(cont) and common <= set(done)


def test_the_gate_is_carried_through_both_patches():
    for patch in (turn.continuing_patch(_ctx(), _record()),
                  turn.completed_patch(_ctx(), _record(), _requirements())):
        assert patch["intake_gate"] == {"selection": {"s": 2}, "admission": {"a": 1},
                                        "approval": None}


def test_no_patch_ever_dispatches():
    for patch in (turn.continuing_patch(_ctx(), _record()),
                  turn.completed_patch(_ctx(), _record(), _requirements())):
        assert patch["dispatch_confirmed"] is False, "the intake node dispatched"


def test_a_continuing_turn_declares_no_requirements():
    patch = turn.continuing_patch(_ctx(), _record())
    for settled in ("mesh_engine", "purpose", "input_kind", "request_txt", "intake_patches"):
        assert settled not in patch, (
            f"a turn that asked a question declared {settled} - approval could fire early")
    assert patch["_intake_training_event"]["type"] == "intake_turn"


def test_a_completed_turn_declares_the_normalised_requirements():
    patch = turn.completed_patch(_ctx(), _record(), _requirements())
    assert patch["mesh_engine"] == "cfmesh" and patch["purpose"] == "external_cfd"
    assert patch["intake_patches"] == [{"name": "body", "type": "wall"}]
    assert patch["_intake_training_event"]["type"] == "intake_complete"
    payload = patch["_intake_training_event"]["payload"]
    assert payload["purpose"] == "external_cfd" and payload["input_kind"] == "solid-body"
    assert payload["intake_system_snapshot"] == "SYS"


def test_the_provider_failure_patch_is_the_canonical_envelope():
    patch = turn.provider_failure_patch("<<API_FAILURE:x>>", {"r": 1})
    assert patch["api_failure"] == "<<API_FAILURE:x>>"
    assert patch["_intake_agent_run_event"]["type"] == "agent_run"


def test_a_turn_with_no_reply_produces_no_assistant_message():
    assert turn.continuing_patch(_ctx(), _record(assistant_text=""))["messages"] == []


# normalisation

def test_the_user_selected_engine_survives_normalisation():
    assert _requirements(mesh_engine="GMSH").mesh_engine == "gmsh"


def test_an_engine_outside_the_catalog_is_blanked_never_substituted():
    r = _requirements(mesh_engine="notamesher")
    assert r.mesh_engine == "", "an unknown engine was passed through or replaced"


def test_purpose_does_not_select_an_engine():
    r = _requirements(mesh_engine="", purpose="external_cfd")
    assert r.mesh_engine == "", "the declared purpose selected an engine"


@pytest.mark.parametrize("purpose", ["structural", "external_cfd", "internal_cfd"])
def test_boundary_roles_are_purpose_owned(purpose):
    from meshpipeline.engines.purposes import PURPOSES

    allowed = set(PURPOSES[purpose].boundary_roles)
    foreign = next(r for p, spec in PURPOSES.items() if p != purpose
                   for r in spec.boundary_roles if r not in allowed)
    r = _requirements(purpose=purpose,
                      patches=[{"name": "keep", "type": next(iter(allowed))},
                               {"name": "drop", "type": foreign}])
    names = {p["name"] for p in r.intake_patches}
    assert "keep" in names and "drop" not in names, (
        f"a {foreign!r} boundary survived on a {purpose} run")


def test_an_omitted_fidelity_is_the_default_and_is_labelled_as_such():
    r = _requirements()
    assert r.requested_mesh_fidelity is None
    assert r.effective_mesh_fidelity == "standard" and r.mesh_fidelity_source == "default"


def test_a_chosen_fidelity_is_recorded_as_the_users_own():
    r = _requirements(mesh_fidelity="draft")
    assert r.requested_mesh_fidelity == "draft" and r.mesh_fidelity_source == "user"


def test_malformed_patches_and_params_are_dropped_not_guessed():
    r = _requirements(patches=["not-a-dict", {"name": "", "type": "wall"}], engine_params="nope")
    assert r.intake_patches == [] and r.engine_params == {}


# source context

def test_the_geometry_source_reference_survives_the_turn():
    from meshpipeline.pipeline.geometry_state import geometry_ref

    state = _state(geometry={"ref": {
        "source_id": "11111111-1111-4111-8111-111111111111", "owner_id": "o1",
        "object_key": "sources/11111111-1111-4111-8111-111111111111", "sha256": "a" * 64,
        "size_bytes": 10, "original_filename": "wing.step", "suffix_hint": ".step"}})
    expected = geometry_ref(state)
    ctx = turn.hydrate(state, _msgs())
    assert expected is not None, "the fixture no longer produces a source ref"
    assert ctx.source_ref is not None, "the geometry/source context was dropped from the turn"
    assert ctx.source_ref.source_id == expected.source_id


def test_a_session_with_no_geometry_hydrates_to_no_source():
    assert turn.hydrate(_state(), _msgs()).source_ref is None


# single authority

def test_the_node_delegates_normalisation_rather_than_repeating_it():
    import inspect

    from meshpipeline.agents.intake import agent

    src = inspect.getsource(agent.node_intake)
    assert "normalise_submission(" in src, "the node no longer delegates normalisation"
    assert "SubmittedRequirements(" not in src, (
        "the node constructs its own requirements - interpretation policy is duplicated")
    for owned_elsewhere in ("resolve_mesh_fidelity", "boundary_roles", "engine_names()",
                            "PURPOSES["):
        assert owned_elsewhere not in src, (
            f"the node re-implements {owned_elsewhere}, which validation owns")


def test_the_node_delegates_every_patch_rather_than_building_one():
    import inspect

    from meshpipeline.agents.intake import agent

    src = inspect.getsource(agent.node_intake)
    for builder in ("turn.completed_patch", "turn.continuing_patch", "turn.provider_failure_patch"):
        assert builder in src, f"the node no longer delegates {builder}"
    assert "_intake_training_event" not in src, "the node builds a training event itself again"
    assert "intake_gate\": {" not in src, "the node assembles the gate patch itself again"
