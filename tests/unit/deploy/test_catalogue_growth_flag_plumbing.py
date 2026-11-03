from pathlib import Path

ROOT = Path(__file__).parents[3]
NEW = {"CAD_REPAIR_FILL_NONPLANAR_HOLES": "cad-repair-fill-nonplanar-holes",
       "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "cad-repair-heal-self-intersections",
       "CAD_REPAIR_FIX_SMALL_FACES": "cad-repair-fix-small-faces"}

DEV_OR_OPT_IN = "(needs.target.outputs.deployment_id == 'dev' || inputs.cad_repair)"

def _text(rel): return (ROOT / rel).read_text(encoding="utf-8")

def test_each_new_flag_follows_the_planar_fill_path_through_all_four_files():
    wf, api = _text(".github/workflows/deploy.yml"), _text("deploy/gcp/scripts/create-api-service.sh")
    fleet, boot = _text("deploy/gcp/scripts/create-worker-fleet.sh"), _text("deploy/gcp/worker/startup.sh")
    for env, meta in NEW.items():
        assert f"{env}: ${{{{ {DEV_OR_OPT_IN} && 'true' || '' }}}}" in wf
        assert f'API_ENV_PAIRS+=("{env}=${{{env}}}")' in api
        assert f"OPT_IN_DROPPABLE+=({env})" in api
        assert f'WORKER_METADATA+=("{meta}=${{{env}}}")' in fleet
        assert f'"{meta}:{env}"' in boot


def test_development_repair_deploys_get_the_mixed_defect_operation_budget():
    wf, api = _text(".github/workflows/deploy.yml"), _text("deploy/gcp/scripts/create-api-service.sh")
    fleet, boot = _text("deploy/gcp/scripts/create-worker-fleet.sh"), _text("deploy/gcp/worker/startup.sh")
    assert f"CAD_REPAIR_MAX_OPS: ${{{{ {DEV_OR_OPT_IN} && '12' || '' }}}}" in wf
    assert 'API_ENV_PAIRS+=("CAD_REPAIR_MAX_OPS=${CAD_REPAIR_MAX_OPS}")' in api
    assert "OPT_IN_DROPPABLE+=(CAD_REPAIR_MAX_OPS)" in api
    assert 'WORKER_METADATA+=("cad-repair-max-ops=${CAD_REPAIR_MAX_OPS}")' in fleet
    assert '"cad-repair-max-ops:CAD_REPAIR_MAX_OPS"' in boot
    assert f"CAD_REPAIR_TIMEOUT_S: ${{{{ {DEV_OR_OPT_IN} && '420' || '' }}}}" in wf
    assert 'API_ENV_PAIRS+=("CAD_REPAIR_TIMEOUT_S=${CAD_REPAIR_TIMEOUT_S}")' in api
    assert "OPT_IN_DROPPABLE+=(CAD_REPAIR_TIMEOUT_S)" in api
    assert 'WORKER_METADATA+=("cad-repair-timeout-s=${CAD_REPAIR_TIMEOUT_S}")' in fleet
    assert '"cad-repair-timeout-s:CAD_REPAIR_TIMEOUT_S"' in boot
