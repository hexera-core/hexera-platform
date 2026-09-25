# Responsibility: Verify vmtk declares no intake parameter its own mesher cannot honour.
# Boundaries: the declaration and what the customer is asked; the layer count itself is the builder's.
from __future__ import annotations

from meshpipeline.engines.registry import get_spec, resolve_engine_params
from meshpipeline.engines.vmtk.vmtk_runner import _DEFAULTS, resolve_strategy

SPEC = get_spec("vmtk")


def test_every_param_vmtk_declares_is_a_key_its_own_strategy_honours():
    """THE DEFECT, as the rule that would have caught it. vmtk declared `wall_layers` with values
    ("on", "off") and its mesher has no such knob: the count lives in `boundary_layers`, and
    `resolve_strategy` fills that from `_DEFAULTS` when nothing names it. `_DEFAULTS` is the whole
    set of keys that reach `build_pype`, so a declared param outside it is an answer with nowhere
    to go."""
    for p in SPEC.intake_params:
        assert p.key in _DEFAULTS, (
            f"vmtk asks the customer for {p.key!r} and its mesher reads no such key - "
            f"the answer is discarded at {sorted(_DEFAULTS)}")


def test_an_off_answer_would_have_been_built_as_three_prism_layers():
    """WHY THE ANSWER COULD NOT SIMPLY BE WIRED UP, pinned so it cannot be quietly relied on. A
    strategy that does not name `boundary_layers` gets three, whatever else it carries - so the
    "off" the customer chose arrived at the mesher as three prism layers inside the lumen wall, and
    an `on`/`off` switch on an integer the builder and the repair ladder both move would be a second
    authority on the same number."""
    assert resolve_strategy({"wall_layers": "off"})["boundary_layers"] == 3
    assert "wall_layers" not in resolve_strategy({"wall_layers": "off"})
    # the real knob does honour a zero, which is why the capability is not what was missing
    assert resolve_strategy({"boundary_layers": 0})["boundary_layers"] == 0


def test_the_customer_is_not_asked_for_a_layer_switch_at_intake():
    """The question reached the customer through the declared-params block of the intake prompt, and
    `admission_token.canonical_payload` fingerprinted the answer into what they approved - so the
    approval attested to a setting the mesher never consulted."""
    import meshpipeline.agents.intake.agent as intake

    system = intake.compose_intake_system()
    assert "wall_layers" not in system
    assert "is a plain tetrahedral fill enough" not in system
    # nothing reaches the canonical payload either, because nothing is declared
    assert resolve_engine_params("vmtk", {"wall_layers": "off"}) == {}
    assert resolve_engine_params("vmtk", None) == {}


def test_the_layer_question_is_still_asked_where_the_answer_travels():
    """NOT FIXED BY DELETING THE QUESTION. `intake_guidance` asks it in the engine's own vocabulary,
    including the "how many" half that never had a declared param, and the answer travels as a
    requirement in the customer's words into request.txt - which the builder is told to read first
    and which the review judges against."""
    import meshpipeline.agents.intake.agent as intake

    guidance = SPEC.intake_guidance.lower()
    assert "boundary layers" in guidance and "how many" in guidance
    assert SPEC.intake_guidance in intake.compose_intake_system()


def test_no_layer_obligation_is_invented_from_params_that_cannot_carry_one():
    """The resolver used to raise a LAYER_REGION obligation when `engine_params["boundary_layers"]`
    was positive. `resolve_engine_params` keeps only the keys the spec declares, so that key could
    never be present and the branch was a check with the blind spot of the thing it checked. An
    obligation comes from trusted job data or it does not exist."""
    from meshpipeline.contracts.review_evidence import TargetKind

    for given in ({}, {"wall_layers": "on"}, {"boundary_layers": 5}):
        params = resolve_engine_params("vmtk", given)
        obligations = SPEC.expected_target_obligations(
            {"n_open_profiles": 2}, params, "internal_cfd")
        kinds = {o.kind for o in obligations}
        assert TargetKind.OPENING in kinds
        assert TargetKind.LAYER_REGION not in kinds, (
            f"a layer obligation was raised from {params!r}, which no job can produce")
