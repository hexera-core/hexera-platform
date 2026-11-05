# Responsibility: Verify the submit schema declares patches and dimensionality, and never renames a user's patch.
from __future__ import annotations

from pathlib import Path

from meshpipeline.agents.intake.agent import INTAKE_TOOLS

APP_DIR = Path(__file__).parent.parent.parent.parent.parent / "src" / "meshpipeline"


def _submit_schema() -> dict:
    for t in INTAKE_TOOLS:
        if t["function"]["name"] == "submit_requirements":
            return t["function"]
    raise AssertionError("submit_requirements tool not found in INTAKE_TOOLS")



def test_submit_requirements_declares_patches_array():
    schema = _submit_schema()
    props = schema["parameters"]["properties"]
    assert "patches" in props
    assert props["patches"]["type"] == "array"
    item = props["patches"]["items"]
    assert item["type"] == "object"
    assert "name" in item["properties"]
    assert "type" in item["properties"]
    assert set(item["required"]) == {"name", "type"}
    # the schema admits the UNION of implemented engines' role vocabularies;
    # the handler validates against the chosen engine
    assert set(item["properties"]["type"]["enum"]) == {
        "wall", "inlet", "outlet", "farfield", "symmetry", "empty",
        "fixed", "load", "contact", "free",
        "external",   # conjugate_heat_transfer: the solid's external thermal-BC faces
    }


def test_submit_requirements_declares_dimensionality_enum():
    schema = _submit_schema()
    props = schema["parameters"]["properties"]
    assert "dimensionality" in props
    # two values only - "2.5D" was removed (a thin slab meshed in full 3D with
    # symmetry patches IS a 3D case; the extra value only invited pseudo-2D workarounds)
    assert set(props["dimensionality"]["enum"]) == {"2D", "3D"}


def test_submit_requirements_marks_new_fields_required():
    schema = _submit_schema()
    req = set(schema["parameters"]["required"])
    assert "patches" in req
    assert "dimensionality" in req



def test_intake_prompt_explains_patches_field():
    p = (APP_DIR / "prompts" / "intake" / "system.txt").read_text(encoding="utf-8")
    assert "patches:" in p.lower() or "patches " in p
    assert "names the user actually said" in p.lower() or "do not translate" in p.lower()


def test_intake_prompt_explains_dimensionality():
    p = (APP_DIR / "prompts" / "intake" / "system.txt").read_text(encoding="utf-8")
    assert "dimensionality" in p.lower()
    assert "empty" in p.lower()
    assert "2D" in p


def test_intake_prompt_forbids_renaming_user_patches():
    p = (APP_DIR / "prompts" / "intake" / "system.txt").read_text(encoding="utf-8")
    assert "single airfoil wall patch" in p.lower(), (
        "Prompt should show the worked example: 'single airfoil wall patch' "
        "→ {name: airfoil, type: wall}, NOT {name: wall, ...}"
    )
