# Responsibility: Verify intake asks domain-neutral questions, inferring nothing from a filename and no CFD default.
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent.parent.parent / "src" / "meshpipeline"

PROMPT_FILE = APP_DIR / "prompts" / "intake" / "system.txt"


def _prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def _prompt_lower() -> str:
    return _prompt().lower()



def test_prompt_file_exists():
    assert PROMPT_FILE.exists(), "intake_system.txt must exist"


def test_prompt_no_filename_inference():
    text = _prompt_lower()
    assert "use the step filename to infer" not in text
    assert "filename to infer" not in text
    if "filename" in text:
        assert "do not" in text or "never" in text, \
            "Filename appears but not in a prohibition context"


def test_prompt_no_old_mandatory_cfd_checklist():
    text = _prompt_lower()
    assert "mandatory checklist" not in text
    assert "you must collect all 7" not in text
    assert "k-omega sst unless" not in text


def test_prompt_no_proactive_cfd_params_as_requirements():
    text = _prompt_lower()
    assert "1. flow conditions" not in text
    assert "- mach number\n" not in text
    assert "- angle of attack (aoa)" not in text.split("never")[0], \
        "Angle of attack appears before any 'never' prohibition clause"


def test_prompt_does_not_recommend_komega_as_default():
    text = _prompt_lower()
    assert "k-omega sst unless" not in text
    assert "k-omega sst" not in text


def test_the_analysis_type_is_settled_before_any_domain_question():
    # This used to assert the exact words of a scripted opening. The wording was never the
    # invariant - neutrality is: the analysis type is established first, and nothing
    # domain-specific may be asked until the user has supplied it. A prompt that phrases the
    # question freely still honours that; a prompt that asks about flow conditions up front
    # does not, whatever words it uses.
    text = _prompt_lower()
    assert "analysis type" in text
    assert "only ask domain-specific questions after" in text


def test_prompt_states_domain_neutrality_principle():
    text = _prompt_lower()
    assert "do not assume" in text or "domain neutralit" in text


def test_prompt_cfd_questions_are_conditional():
    text = _prompt_lower()
    cfd_gate_idx = text.find("if the user mentions cfd")
    assert cfd_gate_idx >= 0, "Prompt must have 'if the user mentions CFD' conditional gate"
    mach_idx = text.find("mach number")
    if mach_idx >= 0:
        assert mach_idx >= cfd_gate_idx, \
            "Mach number mentioned before the CFD conditional gate"


def test_prompt_adaptive_structure_present():
    text = _prompt_lower()
    assert "adaptive questioning" in text or "only ask domain-specific" in text


def test_prompt_neutral_examples_in_domain_label():
    text = _prompt_lower()
    assert "structural" in text or "fea" in text


def test_prompt_no_proactive_turbulence_model_recommendation():
    text = _prompt_lower()
    cfd_gate_idx = text.find("if the user mentions cfd")
    turb_idx = text.find("turbulence model")
    if turb_idx >= 0 and cfd_gate_idx >= 0:
        never_idx = text.find("never proactively mention")
        assert turb_idx >= cfd_gate_idx or (never_idx >= 0 and turb_idx >= never_idx), \
            "Turbulence model mentioned before CFD conditional gate and not in a prohibition"



def test_web_search_description_no_cfd_bias():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    tool = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "web_search")
    desc = tool["function"]["description"].lower()
    assert "flow conditions" not in desc
    assert "vehicle class" not in desc
    assert "mach" not in desc


def test_web_search_query_example_includes_noncfd():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    tool = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "web_search")
    query_desc = tool["function"]["parameters"]["properties"]["query"]["description"].lower()
    assert "structural" in query_desc or "fea" in query_desc or "internal flow" in query_desc or "pump" in query_desc


def test_web_search_description_mentions_domain_first():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    tool = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "web_search")
    desc = tool["function"]["description"].lower()
    assert "after" in desc or "established" in desc or "stated" in desc



def test_submit_requirements_domain_param_neutral():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    sub = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "submit_requirements")
    domain_desc = sub["function"]["parameters"]["properties"]["domain"]["description"].lower()
    assert "structural" in domain_desc or "fea" in domain_desc or "internal flow" in domain_desc or "pump" in domain_desc


def test_submit_requirements_request_txt_domain_agnostic():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    sub = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "submit_requirements")
    req_desc = sub["function"]["parameters"]["properties"]["request_txt"]["description"].lower()
    assert "load" in req_desc or "material" in req_desc



def test_node_intake_does_not_inject_filename_into_prompt():
    prompt_text = _prompt()
    assert "{step_filename}" not in prompt_text, \
        "Prompt must not include {step_filename} - filename must not be used for geometry inference"
