from __future__ import annotations

from pathlib import Path

WORKFLOW = (Path(__file__).parents[3] / ".github" / "workflows" / "deploy.yml").read_text(
    encoding="utf-8"
)


def test_production_releases_enable_core_repair_and_review_by_default():
    prod_or_opt_in = "needs.target.outputs.deployment_id == 'prod' || inputs.cad_repair"

    assert f"CAD_REPAIR_ENABLED: ${{{{ ({prod_or_opt_in}) && 'true' || '' }}}}" in WORKFLOW
    assert (
        "CAD_REPAIR_REVIEW_ENABLED: "
        "${{ needs.target.outputs.deployment_id == 'prod' && 'true' || "
        "inputs.cad_repair && (inputs.cad_repair_review && 'true' || 'false') || '' }}"
        in WORKFLOW
    )


def test_production_default_does_not_enable_geometry_invention_flags():
    for name in (
        "CAD_REPAIR_FILL_PLANAR_HOLES",
        "CAD_REPAIR_FILL_NONPLANAR_HOLES",
        "CAD_REPAIR_HEAL_SELF_INTERSECTIONS",
    ):
        assert f"{name}: ${{{{ inputs.cad_repair && 'true' || '' }}}}" in WORKFLOW
