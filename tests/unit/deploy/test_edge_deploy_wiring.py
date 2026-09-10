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


def test_the_provision_job_maps_both_domains():
    # provision is the job that runs deploy.sh, so its environment is the one create-edge.sh sees.
    body = yaml.dump(_doc()["jobs"]["provision"])
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
    """The address must leave the job that discovered it AND be printed somewhere a human reads.

    Two assertions rather than one, because the split separated them: create-edge.sh writes
    edge_ip to provision's $GITHUB_OUTPUT, and the `edge-dns` job is what turns it into the DNS
    table in the run summary. Either half missing means an operator never sees the address the
    run just reserved, which is the whole point of this test.
    """
    doc = _doc()
    assert "edge_ip" in yaml.dump(doc["jobs"]["provision"].get("outputs", {})), (
        "provision does not publish edge_ip as a job output, so the address cannot leave the job "
        "that reserved it")
    edge = doc["jobs"]["edge-dns"]
    assert "provision" in edge["needs"]
    body = yaml.dump(edge)
    assert "edge_ip" in body, (
        "the address an operator must put in DNS is not surfaced by the run that reserved it")
    assert "GITHUB_STEP_SUMMARY" in body, (
        "the address is never written to the run summary, so it is buried in a log nobody opens")
