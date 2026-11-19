# Responsibility: Pin the crs_v3 switch: one dispatch input, one target output, and every CRS v3 repair posture derived from it along the four-file deploy path.
# Boundaries: static reads of the deploy workflow, the two provisioning scripts, the worker startup script and the settings inventory, plus RUNNING the target picker; nothing in the cloud is touched.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]
WF = ROOT / ".github" / "workflows" / "deploy.yml"

ON = "needs.target.outputs.crs_v3 != '' && 'live' || ''"
LIVE_ONLY = "needs.target.outputs.crs_v3 == 'live' && 'true' || ''"
# A POSTURE, not a boolean: the tolerant probe (tier 2b) reads `live`/`off`, and only the
# exact crs_v3=live turns it on - a shadow run records the ladder, it does not run fTetWild.
LIVE_POSTURE = "needs.target.outputs.crs_v3 == 'live' && 'live' || ''"
# THE POSTURE CARRIED THROUGH, not mapped: the investigator reads `shadow`/`live`/`off`, so a
# shadow slug gets a shadow investigation (recorded, executing nothing) and a live slug a live one.
CARRIED = "needs.target.outputs.crs_v3 != '' && needs.target.outputs.crs_v3 || ''"

# THE TABLE every later flag-adding task appends to: env name -> (metadata name, on-value expression).
FLAGS: dict[str, tuple[str, str]] = {
    "CAD_REPAIR_REGION_LOOP": ("cad-repair-region-loop", ON),
    "CAD_REPAIR_MESH_PROBE": ("cad-repair-mesh-probe", ON),
    "CAD_REPAIR_PACKAGE_REVIEW": ("cad-repair-package-review", ON),
    "CAD_REPAIR_SPLIT_CLOSED_FACES": ("cad-repair-split-closed-faces", LIVE_ONLY),
    "CAD_REPAIR_FRAGMENT_BODIES": ("cad-repair-fragment-bodies", LIVE_ONLY),
    "CAD_REPAIR_REBUILD_FACE": ("cad-repair-rebuild-face", LIVE_ONLY),
    "CAD_REPAIR_CLOSE_WIRE_GAPS": ("cad-repair-close-wire-gaps", LIVE_ONLY),
    "CAD_REPAIR_TOLERANT_PROBE": ("cad-repair-tolerant-probe", LIVE_POSTURE),
    "CAD_REPAIR_INVESTIGATOR": ("cad-repair-investigator", CARRIED),
    # A TIER OF the review, so it is on wherever the review is - and inert until a run carries a
    # customer deviation threshold. It never applies to the vertex-moving ops, which is asserted
    # in the repair tests rather than here, because that rule lives in the tier, not the deploy.
    "CAD_REPAIR_AUTO_ACCEPT": ("cad-repair-auto-accept", ON),
}

VERTEX_MOVING = ("CAD_REPAIR_REBUILD_FACE", "CAD_REPAIR_CLOSE_WIRE_GAPS")


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _inputs() -> dict:
    return _doc()[True]["workflow_dispatch"]["inputs"]


def _pick(tmp_path: Path, **env: str) -> dict[str, str]:
    """Run the target picker as GitHub would and return its outputs."""
    script = tmp_path / "picker.sh"
    script.write_text(_doc()["jobs"]["target"]["steps"][0]["run"], encoding="utf-8")
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    full = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_REF": "refs/heads/some-branch",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        **{f"DISPATCH_{c.upper()}": "false"
           for c in ("app", "data", "fleet", "console", "admin", "outreach", "edge")},
        "DISPATCH_APP": "true",
        **env,
    }
    p = subprocess.run(["bash", str(script)], env=full, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    parsed: dict[str, str] = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            parsed[k] = v
    return parsed


def test_crs_v3_is_one_switch_and_empty_on_prod(tmp_path):
    wf = _text(".github/workflows/deploy.yml")
    crs = _inputs()["crs_v3"]
    assert crs["type"] == "choice"
    assert crs["default"] == "off"
    assert crs["options"] == ["off", "shadow", "live"]
    # the target output, carried from the one dispatch input and from nothing else
    assert "crs_v3: ${{ steps.pick.outputs.crs_v3 }}" in wf
    assert "DISPATCH_CRS_V3: ${{ inputs.crs_v3 != 'off' && inputs.crs_v3 || '' }}" in wf

    # personal: whatever the input chose, and fail-closed on anything that is not a posture
    for given, expected in (("shadow", "shadow"), ("live", "live"), ("", ""), ("on", "")):
        out = _pick(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_CRS_V3=given)
        assert out["deployment_id"] == "dev-pranav"
        assert out.get("crs_v3") == expected, (given, out.get("crs_v3"))
    # shared dev: `live` as COMMITTED CONFIG - it does not matter what the dispatch asked for,
    # and a merge to main gets the same answer, because the repo states the posture, not the caller
    dev = _pick(tmp_path, DISPATCH_SLUG="", DISPATCH_CRS_V3="off")
    assert dev["deployment_id"] == "dev"
    assert dev.get("crs_v3") == "live"
    merge = _pick(tmp_path, GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="push")
    assert merge["deployment_id"] == "dev"
    assert merge.get("crs_v3") == "live"
    # prod: stated empty, permanently
    prod = _pick(tmp_path, GITHUB_REF="refs/tags/v9.9.9", GITHUB_EVENT_NAME="push",
                 DISPATCH_CRS_V3="live")
    assert prod["deployment_id"] == "prod"
    assert prod.get("crs_v3") == ""
    # exactly one target still states it empty: production
    assert len(re.findall(r'echo "crs_v3="', wf)) == 1

    for env, (_, expr) in FLAGS.items():
        assert "prod" not in expr, env
        assert "deployment_id" not in expr, env


def test_each_flag_follows_the_four_file_path():
    wf = _text(".github/workflows/deploy.yml")
    api = _text("deploy/gcp/scripts/create-api-service.sh")
    fleet = _text("deploy/gcp/scripts/create-worker-fleet.sh")
    boot = _text("deploy/gcp/worker/startup.sh")
    for env, (meta, expr) in FLAGS.items():
        lines = [ln.strip() for ln in wf.splitlines() if ln.strip().startswith(f"{env}:")]
        assert lines == [f"{env}: ${{{{ {expr} }}}}"], (env, lines)
        assert f'API_ENV_PAIRS+=("{env}=${{{env}}}")' in api, env
        assert f"OPT_IN_DROPPABLE+=({env})" in api, env
        assert f'WORKER_METADATA+=("{meta}=${{{env}}}")' in fleet, env
        assert f'"{meta}:{env}"' in boot, env


def test_every_flag_is_a_registered_setting():
    inventory = _text("src/meshpipeline/settings/inventory.py")
    for env in FLAGS:
        assert f'EnvVar("{env}"' in inventory, env


def test_vertex_moving_ops_ride_with_the_reviewer():
    review = FLAGS["CAD_REPAIR_PACKAGE_REVIEW"][1]
    for env in VERTEX_MOVING:
        expr = FLAGS[env][1]
        # on only when crs_v3 is exactly live ...
        assert expr.startswith("needs.target.outputs.crs_v3 == 'live' &&"), env
        # ... and live is non-empty, so the reviewer is live too
        assert review.startswith("needs.target.outputs.crs_v3 != '' && 'live'"), env


def test_input_budget_still_holds():
    assert len(_inputs()) <= 25


# --- Task 28: the posture. Shared dev runs the v3 set as one unit; production stays off. -------


def test_the_v3_flags_derive_only_from_the_one_switch():
    """No v3 flag may read the deployment id or name production: one switch commands the set."""
    for line in _text(".github/workflows/deploy.yml").splitlines():
        stripped = line.strip()
        if not stripped.startswith("CAD_REPAIR_") or "crs_v3" not in stripped:
            continue
        name = stripped.split(":", 1)[0]
        assert "deployment_id" not in stripped, f"{name} must derive from crs_v3 alone"
        assert "'prod'" not in stripped, f"{name} must not name production"
