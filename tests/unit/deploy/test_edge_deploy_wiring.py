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


def test_prod_hostnames_are_not_pinned_yet():
    """Edge-2 turns prod on deliberately, in its own reviewed diff."""
    text = WF.read_text(encoding="utf-8")
    # `"console.hexera.ai" in text` alone would also be satisfied by the substring inside
    # `dev.console.hexera.ai`, so it would pass even if prod WERE pinned - it proves nothing.
    # Assert instead, exactly, that prod's outputs are emitted empty - the same shape
    # `console_service=` / `admin_service=` are asserted in test_admin_deploy_wiring.py.
    assert 'echo "console_domain="' in text, (
        "prod must emit an empty console_domain, like console_service, so a release tag does "
        "not pin a hostname nobody reviewed")
    assert 'echo "admin_domain="' in text, (
        "prod must emit an empty admin_domain, like admin_service, so a release tag does not "
        "pin a hostname nobody reviewed")


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
