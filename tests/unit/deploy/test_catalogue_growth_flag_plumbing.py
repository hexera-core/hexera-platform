from pathlib import Path
ROOT = Path(__file__).parents[3]
NEW = {"CAD_REPAIR_FILL_NONPLANAR_HOLES": "cad-repair-fill-nonplanar-holes",
       "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "cad-repair-heal-self-intersections"}

def _text(rel): return (ROOT / rel).read_text(encoding="utf-8")

def test_each_new_flag_follows_the_planar_fill_path_through_all_four_files():
    wf, api = _text(".github/workflows/deploy.yml"), _text("deploy/gcp/scripts/create-api-service.sh")
    fleet, boot = _text("deploy/gcp/scripts/create-worker-fleet.sh"), _text("deploy/gcp/worker/startup.sh")
    for env, meta in NEW.items():
        assert f"{env}: ${{{{ inputs.cad_repair && 'true' || '' }}}}" in wf
        assert f'API_ENV_PAIRS+=("{env}=${{{env}}}")' in api
        assert f"OPT_IN_DROPPABLE+=({env})" in api
        assert f'WORKER_METADATA+=("{meta}=${{{env}}}")' in fleet
        assert f'"{meta}:{env}"' in boot
