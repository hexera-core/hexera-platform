# Responsibility: Keep an unticked cad_repair / llm_planner redeploy deployable - the live-setting
#                 check must read an unset opt-in as a stated intent to drop it, not as drift.
# Boundaries: runs the script's own droppable-list and check logic in bash; it calls no cloud.
from __future__ import annotations

import subprocess
from pathlib import Path

API = Path(__file__).parents[3] / "deploy" / "gcp" / "scripts" / "create-api-service.sh"
FLAGS = ("CAD_REPAIR_ENABLED", "CAD_REPAIR_REVIEW_ENABLED", "CAD_REPAIR_FILL_PLANAR_HOLES",
         "CAD_REPAIR_FILL_NONPLANAR_HOLES", "CAD_REPAIR_HEAL_SELF_INTERSECTIONS",
         "CAD_REPAIR_LLM_PLANNER")


def _segment(start: str, end: str) -> str:
    text = API.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


def _undeclared(env: dict, live: list[str]) -> list[str]:
    """What the script's check would refuse to drop, for a live service carrying `live`."""
    lists = _segment("# UNTICKED IS A STATED INTENT TOO", "# The database this deployment migrated")
    loop = _segment("  undeclared=()", "  if [ ${#undeclared[@]} -gt 0 ]; then")
    script = "\n".join([
        "set -u", "DECLARED_ENV_NAMES=(SOMETHING_DECLARED)", "BILLING_OFF_DROPPABLE=()",
        lists, f"live_env_names='{chr(10).join(live)}'", loop,
        'printf "%s\\n" ${undeclared[@]+"${undeclared[@]}"}'])
    out = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def test_an_unticked_redeploy_may_drop_the_repair_flags_a_ticked_one_left():
    assert _undeclared({}, list(FLAGS)) == []


def test_a_ticked_deploy_still_declares_them_and_unrelated_drift_is_still_refused():
    ticked = {"CAD_REPAIR_ENABLED": "true", "CAD_REPAIR_REVIEW_ENABLED": "true",
              "CAD_REPAIR_FILL_PLANAR_HOLES": "true",
              "CAD_REPAIR_FILL_NONPLANAR_HOLES": "true",
              "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "true",
              "CAD_REPAIR_LLM_PLANNER": "shadow"}
    # declared by API_ENV_PAIRS in the real script; here nothing declares them, so they show up
    assert _undeclared(ticked, list(FLAGS)) == list(FLAGS)
    assert _undeclared({}, ["SOMETHING_ELSE"]) == ["SOMETHING_ELSE"]
