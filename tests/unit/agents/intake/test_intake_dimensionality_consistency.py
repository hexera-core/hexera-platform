# Responsibility: Verify validation rejects a 2D declaration with no empty patch, and a 3D one that carries one.
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent.parent.parent / "src" / "meshpipeline"


# The 2D/3D empty-patch rules are now enforced through EngineSpec.admit() (the single
# admission path) rather than an inline if-forest, so these assert the OUTCOME via
# validate_submission - behaviour, not the location of a string.
def _errs(**over) -> list[str]:
    from meshpipeline.agents.intake.validation import validate_submission
    base = {
        "domain": "naca airfoil aero",
        "request_txt": ("A complete requirements summary covering the geometry, the simulation "
                        "type, every confirmed parameter and the mesh requirements. " * 2),
        "review_brief_txt": ("Acceptance criteria: a valid mesh, the correct regions, no fatal "
                             "defects, sizing at the builder's discretion. " * 2),
        "mesh_engine": "snappy", "mesh_fidelity": "standard", "engine_source": "user_direct",
        "purpose": "external_cfd", "input_kind": "body-surface", "engine_params": {},
    }
    base.update(over)
    return validate_submission(base)


def test_intake_validation_rejects_2D_without_empty_patch():
    e = _errs(dimensionality="2D",
              patches=[{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"}])
    assert any("dimensionality is '2D' but no patch has type 'empty'" in x for x in e), e
    # the fix guidance must NOT offer a dimensionality workaround for a missing empty
    # patch (the retired "2.5D" escape hatch); it names the two honest options
    assert any("declare the case '3d'" in x.lower() for x in e), \
        "error must tell the LLM how to fix it"
    assert not any("2.5D" in x for x in e), "no 2.5D workaround may be suggested"


def test_intake_validation_rejects_3D_with_empty_patch():
    e = _errs(dimensionality="3D",
              patches=[{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"},
                       {"name": "frontAndBack", "type": "empty"}])
    assert any("dimensionality is '3D' but patches include a type 'empty'" in x for x in e), e
