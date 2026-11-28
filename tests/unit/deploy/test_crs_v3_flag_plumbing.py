# Responsibility: Pin the crs_v3 switch: one dispatch input, one target output, and every CRS v3 repair posture derived from it along the four-file deploy path.
# Boundaries: static reads of the deploy workflow, the two provisioning scripts, the worker startup script and the settings inventory, plus RUNNING the target picker; nothing in the cloud is touched.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
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
# A COUNT, not a posture: the tournament's K. Shared dev (crs_v3=live) states 3 - the K the eval
# measures - because K=1 rehearses one candidate and is a tournament in name only. Unstated
# anywhere else, so shadow slugs keep the code default (1).
K_LIVE = "needs.target.outputs.crs_v3 == 'live' && '3' || ''"
TOURNAMENT_K_LIVE = "3"

# THE TABLE every later flag-adding task appends to: env name -> (metadata name, on-value expression).
FLAGS: dict[str, tuple[str, str]] = {
    "CAD_REPAIR_REGION_LOOP": ("cad-repair-region-loop", ON),
    "CAD_REPAIR_MESH_PROBE": ("cad-repair-mesh-probe", ON),
    "CAD_REPAIR_PACKAGE_REVIEW": ("cad-repair-package-review", ON),
    "CAD_REPAIR_SPLIT_CLOSED_FACES": ("cad-repair-split-closed-faces", LIVE_ONLY),
    "CAD_REPAIR_FRAGMENT_BODIES": ("cad-repair-fragment-bodies", LIVE_ONLY),
    "CAD_REPAIR_REBUILD_FACE": ("cad-repair-rebuild-face", LIVE_ONLY),
    "CAD_REPAIR_CLOSE_WIRE_GAPS": ("cad-repair-close-wire-gaps", LIVE_ONLY),
    "CAD_REPAIR_RESOLVE_SUB_MESH_GAP": ("cad-repair-resolve-sub-mesh-gap", LIVE_ONLY),
    # THE MESHABILITY PASS reads the exact `live`: live on shared dev, unstated elsewhere.
    "CAD_REPAIR_MESHABILITY_PASS": ("cad-repair-meshability-pass", LIVE_POSTURE),
    "CAD_REPAIR_TOLERANT_PROBE": ("cad-repair-tolerant-probe", LIVE_POSTURE),
    "CAD_REPAIR_INVESTIGATOR": ("cad-repair-investigator", CARRIED),
    # A TIER OF the review, so it is on wherever the review is - and inert until a run carries a
    # customer deviation threshold. It never applies to the vertex-moving ops, which is asserted
    # in the repair tests rather than here, because that rule lives in the tier, not the deploy.
    "CAD_REPAIR_AUTO_ACCEPT": ("cad-repair-auto-accept", ON),
    # THE PER-PART ATTEMPT ALLOWANCE (Task 22): on wherever the region-scoped driver is, because
    # it is a budget for that driver and inert without it, and because every outer bound - the
    # run's operation allowance, its deadline, the per-package budget and the two-rejections
    # escalation - is untouched by it.
    "CAD_REPAIR_BUDGET_ATTEMPTS": ("cad-repair-budget-attempts", ON),
    # THE TOURNAMENT has a real shadow (the rules package returned unchanged, the round recorded),
    # so the posture is carried through like the investigator's.
    "CAD_REPAIR_TOURNAMENT": ("cad-repair-tournament", CARRIED),
    # ... and its K, so the shipped live tournament is the one the eval measured (K=3), not K=1.
    "CAD_REPAIR_TOURNAMENT_K": ("cad-repair-tournament-k", K_LIVE),
    # LIVE-ONLY: each reads the exact `live` and nothing else, so it has no shadow meaning.
    "CAD_REPAIR_MEMORY": ("cad-repair-memory", LIVE_POSTURE),
    "CAD_REPAIR_REVIEW_OBJECTION": ("cad-repair-review-objection", LIVE_POSTURE),
    # The investigator may not escalate a region before it has tried an edit there. A live-only
    # posture (live/off), inert wherever the investigator is off.
    "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS": ("cad-repair-escalate-requires-attempts",
                                              LIVE_POSTURE),
    "CAD_REPAIR_MESH_HANDBACK": ("cad-repair-mesh-handback", LIVE_POSTURE),
    "CAD_REPAIR_STANDALONE_JOBS": ("cad-repair-standalone-jobs", LIVE_POSTURE),
    # DEV DIAGNOSTICS (lane D): the investigation trace - per-turn JSONL beside the job's
    # artifacts and a per-region "why no package" table in the report. A boolean, on only where
    # crs_v3 is live (shared dev, production, crsv3).
    "CAD_REPAIR_INVESTIGATION_TRACE": ("cad-repair-investigation-trace", LIVE_ONLY),
}

# PLUMBED AHEAD OF THEIR SETTINGS ROW. The code reads each of these with `getattr(rcfg, NAME,
# "off")` from meshpipeline.settings.cad_repair, which does not declare them yet, so until a row is
# added there (and in settings/inventory.py) the exported value is inert and the in-code default
# stands. When a row lands, delete the name from this set - the test below then holds it to the
# inventory like every other flag.
AWAITING_SETTINGS_ROW: frozenset[str] = frozenset()

# DELIBERATELY NOT PLUMBED from crs_v3: the router's image acceptance is unmeasured
# (INVESTIGATOR_IMAGES), and the one-shot author and the LLM planner are older postures with their
# own inputs. crs_v3 must not grow them by accident.
NOT_FROM_CRS_V3 = ("CAD_REPAIR_INVESTIGATOR_IMAGES", "CAD_REPAIR_LLM_AUTHOR", "CAD_REPAIR_LLM_PLANNER")

# What each expression evaluates to for each crs_v3 output, as GitHub's `&&`/`||` would.
_VALUE = {
    ON: lambda c: "live" if c != "" else "",
    LIVE_ONLY: lambda c: "true" if c == "live" else "",
    LIVE_POSTURE: lambda c: "live" if c == "live" else "",
    CARRIED: lambda c: c if c != "" else "",
    K_LIVE: lambda c: TOURNAMENT_K_LIVE if c == "live" else "",
}

VERTEX_MOVING = ("CAD_REPAIR_REBUILD_FACE", "CAD_REPAIR_CLOSE_WIRE_GAPS")
#: Ops that move no vertex but are never auto-accepted (review_tier.REVIEWER_ONLY_OPS): they ride
#: with the reviewer for the same reason.
REVIEWER_ONLY = ("CAD_REPAIR_RESOLVE_SUB_MESH_GAP",)


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


def test_crs_v3_is_one_switch_and_live_on_prod(tmp_path):
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
    # prod: `live` as committed config too, whatever the dispatch asked for
    prod = _pick(tmp_path, GITHUB_REF="refs/tags/v9.9.9", GITHUB_EVENT_NAME="push",
                 DISPATCH_CRS_V3="off")
    assert prod["deployment_id"] == "prod"
    assert prod.get("crs_v3") == "live"
    # no target states it empty any more: personal slugs leave it to the input
    assert len(re.findall(r'echo "crs_v3="', wf)) == 0

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
        if env in AWAITING_SETTINGS_ROW:
            continue
        assert f'EnvVar("{env}"' in inventory, env
    assert AWAITING_SETTINGS_ROW <= FLAGS.keys()


def test_the_postures_the_set_takes_on_each_target(tmp_path):
    """Production and shared dev's committed `live` turn the whole set on together; shadow turns
    on only the flags that have a shadow meaning."""
    prod = _pick(tmp_path, GITHUB_REF="refs/tags/v9.9.9", GITHUB_EVENT_NAME="push",
                 DISPATCH_CRS_V3="off")["crs_v3"]
    dev = _pick(tmp_path, DISPATCH_SLUG="", DISPATCH_CRS_V3="off")["crs_v3"]
    for env, (_, expr) in FLAGS.items():
        assert _VALUE[expr](prod) in ("live", "true", TOURNAMENT_K_LIVE), \
            f"{env} is not on in production"
        assert _VALUE[expr](dev) in ("live", "true", TOURNAMENT_K_LIVE), \
            f"{env} is not on in shared dev"
    shadow_on = {env for env, (_, expr) in FLAGS.items() if _VALUE[expr]("shadow")}
    assert "CAD_REPAIR_TOURNAMENT" in shadow_on
    assert not shadow_on & {"CAD_REPAIR_MEMORY", "CAD_REPAIR_REVIEW_OBJECTION",
                            "CAD_REPAIR_MESH_HANDBACK", "CAD_REPAIR_STANDALONE_JOBS",
                            "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS"}
    assert not shadow_on & set(VERTEX_MOVING)
    # wherever anything is on, the reviewer is on
    for crs in ("shadow", "live"):
        assert _VALUE[FLAGS["CAD_REPAIR_PACKAGE_REVIEW"][1]](crs) == "live", crs


def test_parked_postures_are_not_derived_from_crs_v3():
    wf = _text(".github/workflows/deploy.yml")
    boot = _text("deploy/gcp/worker/startup.sh")
    for env in NOT_FROM_CRS_V3:
        assert env not in FLAGS, env
        for line in wf.splitlines():
            if line.strip().startswith(f"{env}:"):
                assert "crs_v3" not in line, f"{env} is parked and must not derive from crs_v3"
    assert "CAD_REPAIR_INVESTIGATOR_IMAGES" not in boot


def test_vertex_moving_ops_ride_with_the_reviewer():
    review = FLAGS["CAD_REPAIR_PACKAGE_REVIEW"][1]
    for env in (*VERTEX_MOVING, *REVIEWER_ONLY):
        expr = FLAGS[env][1]
        # on only when crs_v3 is exactly live ...
        assert expr.startswith("needs.target.outputs.crs_v3 == 'live' &&"), env
        # ... and live is non-empty, so the reviewer is live too
        assert review.startswith("needs.target.outputs.crs_v3 != '' && 'live'"), env


def test_input_budget_still_holds():
    assert len(_inputs()) <= 25


# --- Task 28: the posture. Shared dev and production run the v3 set as one unit. --------------


def test_the_v3_flags_derive_only_from_the_one_switch():
    """No v3 flag may read the deployment id or name production: one switch commands the set."""
    for line in _text(".github/workflows/deploy.yml").splitlines():
        stripped = line.strip()
        if not stripped.startswith("CAD_REPAIR_") or "crs_v3" not in stripped:
            continue
        name = stripped.split(":", 1)[0]
        assert "deployment_id" not in stripped, f"{name} must derive from crs_v3 alone"
        assert "'prod'" not in stripped, f"{name} must not name production"


V3_RAW_ROWS = ("CAD_REPAIR_TOURNAMENT", "CAD_REPAIR_MEMORY", "CAD_REPAIR_REVIEW_OBJECTION",
               "CAD_REPAIR_MESH_HANDBACK", "CAD_REPAIR_STANDALONE_JOBS",
               "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS")


@pytest.mark.parametrize("name", V3_RAW_ROWS)
def test_an_exported_posture_actually_reaches_the_settings_module(monkeypatch, name):
    """The deploy path writing NAME=live is worthless unless the settings module reads it: every
    consumer asks `getattr(rcfg, NAME, "off")`, so a missing row reads off whatever the env says.
    That is exactly how five v3 postures shipped inert on a live personal deploy."""
    import importlib

    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setenv(name, "live")
    try:
        assert getattr(importlib.reload(rcfg), name) == "live"
    finally:
        monkeypatch.delenv(name)
        importlib.reload(rcfg)
    assert getattr(rcfg, name) == "off"


def test_shared_dev_runs_the_tournament_the_eval_measured(tmp_path):
    """K=1 rehearses one candidate: a live tournament with an unstated K is no tournament. Shared
    dev and production state the eval's K; shadow slugs state nothing (the code default, 1)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "evalspec_under_test_k", ROOT / "devtools" / "validation" / "v3_eval" / "evalspec.py")
    evalspec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evalspec)
    assert evalspec.arm("meshability_pass_ai")["env"]["CAD_REPAIR_TOURNAMENT_K"] == TOURNAMENT_K_LIVE

    expr = FLAGS["CAD_REPAIR_TOURNAMENT_K"][1]
    dev = _pick(tmp_path, DISPATCH_SLUG="", DISPATCH_CRS_V3="off")["crs_v3"]
    prod = _pick(tmp_path, GITHUB_REF="refs/tags/v9.9.9", GITHUB_EVENT_NAME="push",
                 DISPATCH_CRS_V3="off")["crs_v3"]
    assert _VALUE[expr](dev) == TOURNAMENT_K_LIVE
    assert _VALUE[expr](prod) == TOURNAMENT_K_LIVE
    assert _VALUE[expr]("shadow") == ""


def test_the_exported_k_reaches_the_settings_module(monkeypatch):
    import importlib

    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setenv("CAD_REPAIR_TOURNAMENT_K", TOURNAMENT_K_LIVE)
    try:
        assert importlib.reload(rcfg).CAD_REPAIR_TOURNAMENT_K == TOURNAMENT_K_LIVE
    finally:
        monkeypatch.delenv("CAD_REPAIR_TOURNAMENT_K")
        importlib.reload(rcfg)
    assert rcfg.CAD_REPAIR_TOURNAMENT_K == "1"
