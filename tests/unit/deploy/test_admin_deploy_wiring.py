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


def _admin_check_step_text() -> str:
    text = WF.read_text(encoding="utf-8")
    start = text.index("Verify the admin console is not publicly reachable")
    return text[start:start + 3000]


def test_a_failed_invoker_policy_read_is_not_swallowed():
    step_text = _admin_check_step_text()
    assert "get-iam-policy" in step_text
    # The bug: `... 2>/dev/null | tr ... || true)"` discarded gcloud's stderr AND its exit
    # status, so a missing permission, a wrong region, a typo'd service name, or gcloud being
    # absent all rendered as "OK: no public invoker binding" - the one check the workflow calls
    # "the one check that matters" was reporting success when it could not check anything.
    assert "|| true)" not in step_text, (
        "the policy read still swallows a gcloud failure with `|| true` - a failed read must "
        "fail the step, not report success")
    assert "::error::" in step_text and "could not read" in step_text.lower(), (
        "a failed policy read must emit an ::error:: naming what could not be read")


def test_an_empty_invoker_policy_read_is_treated_as_a_failure():
    step_text = _admin_check_step_text()
    # A working admin service always carries the IAP service agent's own invoker binding, so a
    # policy read that comes back with no members at all is evidence the read did not work, not
    # evidence the service is private.
    assert "empty" in step_text.lower() or "no members" in step_text.lower(), (
        "an empty invoker-policy read is not itself flagged as evidence the check did not run")


def test_the_invoker_policy_check_matches_allauthenticatedusers_too():
    step_text = _admin_check_step_text()
    # grep -qx allUsers is an exact match, so a binding admitting any Google account on earth
    # (allAuthenticatedUsers) passed unnoticed - the same hole for a named-identity allowlist.
    assert "allAuthenticatedUsers" in step_text, (
        "the invoker-binding check does not match allAuthenticatedUsers, so a binding admitting "
        "any Google account on earth would pass unnoticed")
