# Responsibility: Keep an unticked cad_simplify redeploy deployable - the live-setting check must read
#                 an unset opt-in as a stated intent to drop it - and keep the tick plumbed to the API,
#                 the worker fleet's metadata and the worker's env alike.
# Boundaries: runs the script's own droppable-list and check logic in bash; it calls no cloud.
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
API = ROOT / "deploy" / "gcp" / "scripts" / "create-api-service.sh"
FLEET = ROOT / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh"
BOOT = ROOT / "deploy" / "gcp" / "worker" / "startup.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
FLAGS = ("CAD_SIMPLIFY_ENABLED",)


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


def test_an_unticked_redeploy_may_drop_the_simplify_flag_a_ticked_one_left():
    assert _undeclared({}, list(FLAGS)) == []


def test_a_ticked_deploy_still_declares_it_and_unrelated_drift_is_still_refused():
    # declared by API_ENV_PAIRS in the real script; here nothing declares it, so it shows up
    assert _undeclared({"CAD_SIMPLIFY_ENABLED": "true"}, list(FLAGS)) == list(FLAGS)
    assert _undeclared({}, ["SOMETHING_ELSE"]) == ["SOMETHING_ELSE"]


def test_the_tick_reaches_the_api_the_fleet_and_the_worker_env():
    wf = WORKFLOW.read_text(encoding="utf-8")
    # a personal opt-in only: neither shared dev nor production turns it on by itself
    assert "CAD_SIMPLIFY_ENABLED: ${{ inputs.cad_simplify && 'true' || '' }}" in wf
    assert 'API_ENV_PAIRS+=("CAD_SIMPLIFY_ENABLED=${CAD_SIMPLIFY_ENABLED}")' in API.read_text(encoding="utf-8")
    assert 'WORKER_METADATA+=("cad-simplify-enabled=${CAD_SIMPLIFY_ENABLED}")' in FLEET.read_text(encoding="utf-8")
    assert '"cad-simplify-enabled:CAD_SIMPLIFY_ENABLED"' in BOOT.read_text(encoding="utf-8")
