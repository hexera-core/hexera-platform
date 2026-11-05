# Responsibility: Verify each engine declares its soft limitations, and intake shows them as a heads-up, not a gate.
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402


def test_every_engine_declares_at_least_one_soft_limitation():
    for name in engine_names():
        assert get_spec(name).intake_advisories, f"{name} declares no intake_advisories"


def test_snappy_declares_the_prism_layer_limitation_the_CRM_hit():
    adv = " ".join(get_spec("snappy").intake_advisories).lower()
    assert "prism" in adv or "layer" in adv
    assert "collapse" in adv or "coverage" in adv
    assert "junction" in adv or "trailing" in adv or "concave" in adv


def test_advisories_are_user_facing_mechanics_not_snake_case_or_physics_recipes():
    import re
    for name in engine_names():
        for a in get_spec(name).intake_advisories:
            assert a[0].isupper() and a.strip().endswith("."), f"{name}: not a sentence: {a!r}"
            assert "_" not in a, f"{name}: advisory leaks a struct key: {a!r}"
            # no turbulence-model / numeric-setpoint recipes (that's the removed RAG)
            assert not re.search(r"\bk-omega\b|\bk-epsilon\b|\bSST\b|Re\s*=|y\+\s*=", a), (
                f"{name}: advisory prescribes domain physics, not engine mechanics: {a!r}")


def test_intake_prompt_surfaces_advisories_as_heads_up_not_a_gate():
    from meshpipeline.agents.intake.agent import _block_engine_first
    block = _block_engine_first()
    assert "SOFT LIMITATIONS" in block
    # the actual snappy caveat is in the prompt
    assert any(a in block for a in get_spec("snappy").intake_advisories)
    # framed as non-blocking, explicitly distinct from the hard rejections
    low = block.lower()
    assert "never a gate" in low or "do not block" in low or "not a gate" in low
    assert "let them decide" in low or "then let them decide" in low


def test_the_hard_vs_soft_split_is_preserved():
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    ev = AdmissionEvidence(
        engine="snappy", purpose="external_cfd", input_kind="body-surface",
        dimensionality="3D",
        patches=(PatchSummary("aircraft", "wall"), PatchSummary("farfield", "farfield")))
    rej = get_spec("snappy").admit(ev)
    # a single-wall external aircraft request is ADMITTED - the prism-coverage risk is
    # advisory, surfaced by intake, never a rejection here
    assert not rej, [r.code for r in rej]
