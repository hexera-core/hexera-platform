# Responsibility: Verify the prompt's dimensionality, input-kind and engine wording match the enums and the catalogue.
from __future__ import annotations

import re

import meshpipeline.agents.intake.agent as intake
from meshpipeline.engines.purposes import INPUT_KINDS
from meshpipeline.pipeline.enums import Dimensionality

SYSTEM = intake.compose_intake_system()
SUBMIT = next(t["function"] for t in intake.INTAKE_TOOLS if t["function"]["name"] == "submit_requirements")
PROPS = SUBMIT["parameters"]["properties"]


# section 5: dimensionality prompt <-> enum parity

def test_dimensionality_prompt_matches_the_enum_exactly():
    enum_values = {d.value for d in Dimensionality}
    assert enum_values == {"2D", "3D"}
    # every value the prompt presents is accepted by the enum, and every enum value is presented
    assert "2.5D" not in SYSTEM, "stale 2.5D dimensionality is back in the prompt"
    for v in enum_values:
        assert v in SYSTEM, f"the prompt no longer presents the supported dimensionality {v!r}"
    # the tool schema enum is exactly the supported set
    assert set(PROPS["dimensionality"]["enum"]) == enum_values


def test_no_2p5d_in_the_dimensionality_schema_or_description():
    assert "2.5D" not in PROPS["dimensionality"]["description"]
    assert "2.5D" not in PROPS["dimensionality"]["enum"]


# section 8: input_kind prompt <-> catalog parity

def test_input_kind_prompt_matches_the_supported_catalog():
    from meshpipeline.agents.intake import vocabulary as _vocab

    choices = _vocab.input_kind_choices()
    assert set(PROPS["input_kind"]["enum"]) == set(choices)
    # every choice round-trips to a supported key, so the catalog cannot drift from what is offered
    assert {_vocab.to_key(_vocab.INPUT_KIND, c) for c in choices} == set(INPUT_KINDS)
    # The names reach the model through the tool schema, not prose - so the prompt is asserted to
    # be FREE of the routing keys rather than to repeat the names.
    for key in INPUT_KINDS:
        assert not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(key)}(?![A-Za-z0-9_-])", SYSTEM), \
            f"the internal key {key!r} is visible to intake"


# section 6: one authoritative engine-question policy

def test_engine_question_policy_is_stated_once_and_coherently():
    assert "ENGINE FIRST" in SYSTEM
    # the single authority declares itself
    assert "single authority" in SYSTEM
    # if already selected, do not re-ask
    assert "ALREADY named an engine" in SYSTEM and "to choose or confirm it again" in SYSTEM
    # never infer purpose from engine name
    assert "read the engineering purpose off the engine name" in SYSTEM
    # one question per turn
    assert "ONE substantive question per turn" in SYSTEM or "ONE question per turn" in SYSTEM


# section 7: no unsolicited engine recommendation

def test_prompt_recommends_an_engine_only_on_request():
    # soft-limit + ill-suited wording must not instruct offering a specific alternative unprompted
    assert "recommend a SPECIFIC alternative engine only if they ask" in SYSTEM \
        or "recommend a specific alternative only if they ask" in SYSTEM
    assert "do not name a replacement engine unless they ask" in SYSTEM.lower() \
        or "do NOT name a replacement engine unless they ask" in SYSTEM


def test_incompatibility_message_names_no_replacement_engine():
    from meshpipeline.agents.intake.validation import validate_submission
    from meshpipeline.engines.registry import engine_names
    R = ("A complete requirements summary covering geometry, simulation type, confirmed parameters "
         "and mesh requirements for the case. " * 2)
    B = ("Acceptance criteria: valid mesh, correct regions, sound quality at the builder's "
         "discretion for this case. " * 2)
    errs = validate_submission({
        "domain": "bracket fea", "request_txt": R, "review_brief_txt": B, "dimensionality": "3D",
        "mesh_fidelity": "standard", "engine_source": "user_direct", "mesh_engine": "cfmesh", "purpose": "structural",
        "input_kind": "solid-body", "engine_params": {},
        "patches": [{"name": "base", "type": "fixed"}],
    })
    msg = " ".join(errs)
    assert "cannot produce a structural mesh" in msg
    assert not any(n in msg for n in engine_names() if n != "cfmesh"), \
        f"the rejection named a replacement engine unsolicited: {msg!r}"
    assert "ask me to recommend" in msg      # recommendation offered only on request


def test_supported_unusual_gmsh_external_cfd_is_still_accepted():
    from meshpipeline.agents.intake.validation import validate_submission
    R = ("External CFD on a pre-built fluid domain around a body, meshed with gmsh. Confirmed "
         "freestream and near-wall targets, resolve the body surface and wake. " * 2)
    B = ("Fluid volume meshed around the body. Wall and farfield patches present. Near-wall "
         "resolution adequate for the target. " * 2)
    errs = validate_submission({
        "domain": "body external cfd", "request_txt": R, "review_brief_txt": B, "dimensionality": "3D",
        "mesh_fidelity": "standard", "engine_source": "user_direct", "mesh_engine": "gmsh", "purpose": "external_cfd",
        "input_kind": "fluid-domain", "engine_params": {"element_order": "2"},
        "patches": [{"name": "body", "type": "wall"}, {"name": "ff", "type": "farfield"}],
    })
    assert errs == [], errs      # unusual-but-supported combination accepted


# section 9: fail-safe turn cap (never forces a fabricated submit)

def test_turn_cap_message_is_fail_safe_not_a_forced_submit():
    # reproduce the turn-cap branch's message deterministically from the source
    import inspect

    # the fail-safe nudge is turn.BUDGET_NUDGE, beside the budget that fires it
    from meshpipeline.agents.intake import turn as _turn
    src = _turn.BUDGET_NUDGE + inspect.getsource(_turn.assess_budget)
    assert "Call submit_requirements() with all gathered parameters" not in src, \
        "the old unconditional forced-submit instruction is back"
    assert "Do NOT invent, assume, or guess any value" in src
    assert "unless every required field is genuinely established" in src
    assert "SINGLE most important piece of information still missing" in src
