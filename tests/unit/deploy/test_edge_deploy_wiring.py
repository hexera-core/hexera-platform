# Responsibility: Verify the workflow declares dev's hostnames and can deploy the edge.
# Boundaries: it reads the workflow document; it runs no deploy.
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
WF = REPO / ".github" / "workflows" / "deploy.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def test_dev_declares_both_hostnames():
    text = WF.read_text(encoding="utf-8")
    assert "dev.console.hexera.ai" in text
    assert "dev.admin.hexera.ai" in text


def test_prod_pins_the_production_hostnames_and_services():
    """Edge-2: the service accounts and secret access a console needs now exist in hexera-prod,
    so a release tag pins the real production services and hostnames, not empty placeholders."""
    text = WF.read_text(encoding="utf-8")
    # Matched as the full quoted `echo "key=value"` statement, not `value in text` alone -
    # "console.hexera.ai" is a substring of dev's "dev.console.hexera.ai" (and likewise
    # "admin.hexera.ai" of "dev.admin.hexera.ai"), so a bare substring check would pass even if
    # prod were still unpinned, or pinned to the wrong value.
    assert 'echo "console_service=prod-console"' in text, (
        "prod must pin console_service=prod-console now that the service account and its "
        "secret access exist")
    assert 'echo "admin_service=prod-admin"' in text, (
        "prod must pin admin_service=prod-admin now that the service account and its secret "
        "access exist")
    assert 'echo "console_domain=console.hexera.ai"' in text, (
        "prod must pin console_domain=console.hexera.ai now that the console's prerequisites "
        "exist")
    assert 'echo "admin_domain=admin.hexera.ai"' in text, (
        "prod must pin admin_domain=admin.hexera.ai now that the admin console's prerequisites "
        "exist")


def test_the_deploy_job_maps_both_domains():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "CONSOLE_DOMAIN" in body
    assert "ADMIN_DOMAIN" in body


def test_a_manual_run_can_select_the_edge():
    inputs = _doc()[True]["workflow_dispatch"]["inputs"]
    assert "edge" in inputs, (
        f"no checkbox input named 'edge', so a manual run cannot provision it. Offered: {list(inputs)}")
    assert inputs["edge"]["type"] == "boolean"
    assert inputs["edge"]["default"] is False, (
        "the edge checkbox must default to false - nothing may be selected implicitly")


def test_the_run_surfaces_the_address():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "edge_ip" in body, (
        "the address an operator must put in DNS is not surfaced by the run that reserved it")
