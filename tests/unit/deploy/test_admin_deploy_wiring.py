# Responsibility: Verify the workflow pins an admin service for dev, not prod, and proves it is not public.
# Boundaries: it reads the workflow document; it runs no deploy.
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
WF = REPO / ".github" / "workflows" / "deploy.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def test_the_target_publishes_an_admin_service_output():
    assert "admin_service" in _doc()["jobs"]["target"]["outputs"]


def test_dev_pins_an_admin_service_and_prod_does_not():
    text = WF.read_text(encoding="utf-8")
    assert 'echo "admin_service=dev-admin"' in text
    assert 'echo "admin_service="' in text, (
        "prod must be left deliberately empty, like the console, so a release tag does not "
        "provision an unasked-for billed service")


def test_the_deploy_job_maps_the_admin_service():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "CLOUDRUN_ADMIN_SERVICE" in body


def test_a_manual_run_can_select_the_admin_console():
    options = _doc()[True]["workflow_dispatch"]["inputs"]["components"]["options"]
    assert [o for o in options if "admin" in o], (
        f"no components option contains 'admin', so a manual run cannot deploy it. Offered: {options}")


def test_the_deployed_admin_is_proved_not_public():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "admin_url" in body, "nothing verifies the deployed admin console"
    assert "allUsers" in body, (
        "the post-deploy check does not assert the absence of a public invoker binding - the one "
        "failure that makes IAP pointless")
