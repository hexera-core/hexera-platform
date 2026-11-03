from __future__ import annotations

from pathlib import Path

WORKFLOW = (Path(__file__).parents[3] / ".github" / "workflows" / "deploy.yml").read_text(
    encoding="utf-8"
)

PROD = "needs.target.outputs.deployment_id == 'prod'"
SHARED_DEV = "needs.target.outputs.deployment_id == 'dev'"
GEOMETRY_FLAGS = (
    "CAD_REPAIR_FILL_PLANAR_HOLES",
    "CAD_REPAIR_FILL_NONPLANAR_HOLES",
    "CAD_REPAIR_HEAL_SELF_INTERSECTIONS",
    "CAD_REPAIR_FIX_SMALL_FACES",
)


def _env_line(name: str) -> str:
    return next(line.strip() for line in WORKFLOW.splitlines() if line.strip().startswith(f"{name}: ${{{{"))


def test_production_releases_enable_core_repair_and_review_by_default():
    assert (
        f"CAD_REPAIR_ENABLED: ${{{{ ({PROD} || {SHARED_DEV} || inputs.cad_repair) && 'true' || '' }}}}"
        in WORKFLOW
    )
    assert (
        "CAD_REPAIR_REVIEW_ENABLED: "
        f"${{{{ {PROD} && 'true' || "
        "inputs.cad_repair && (inputs.cad_repair_review && 'true' || 'false') || "
        f"{SHARED_DEV} && 'true' || '' }}}}"
        in WORKFLOW
    )


def test_production_default_does_not_enable_geometry_invention_flags():
    for name in GEOMETRY_FLAGS + ("CAD_REPAIR_MAX_OPS", "CAD_REPAIR_TIMEOUT_S", "CAD_REPAIR_LLM_PLANNER"):
        assert "'prod'" not in _env_line(name), name


def test_shared_dev_enables_every_crs_capability_without_a_dispatch_input():
    # A merge to main carries no inputs, so shared dev must key on the target, not the tick.
    for name in GEOMETRY_FLAGS:
        assert _env_line(name) == f"{name}: ${{{{ ({SHARED_DEV} || inputs.cad_repair) && 'true' || '' }}}}"
    assert _env_line("CAD_REPAIR_LLM_PLANNER") == (
        "CAD_REPAIR_LLM_PLANNER: ${{ inputs.llm_planner != 'off' && inputs.llm_planner || "
        f"{SHARED_DEV} && 'shadow' || '' }}}}"
    )
