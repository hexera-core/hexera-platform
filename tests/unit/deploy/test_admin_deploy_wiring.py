# Responsibility: Verify the workflow pins an admin service for dev and prod, and proves it is not public.
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


def test_dev_and_prod_each_pin_their_own_admin_service():
    text = WF.read_text(encoding="utf-8")
    assert 'echo "admin_service=dev-admin"' in text
    # Matched as the exact literal echo statement, not a bare substring of the value, following
    # the same idiom as the dev assertion above.
    assert 'echo "admin_service=prod-admin"' in text, (
        "prod must pin admin_service=prod-admin now that the prod-admin service account and its "
        "secret access exist in hexera-prod")


def test_the_provision_job_maps_the_admin_service():
    # provision is the job that runs deploy.sh, so its environment is what create-admin-service.sh
    # reads. (It was called `deploy` until the workflow was split into release/provision.)
    body = yaml.dump(_doc()["jobs"]["provision"])
    assert "CLOUDRUN_ADMIN_SERVICE" in body


def test_a_manual_run_can_select_the_admin_console():
    inputs = _doc()[True]["workflow_dispatch"]["inputs"]
    assert "admin" in inputs, (
        f"no checkbox input named 'admin', so a manual run cannot deploy it. Offered: {list(inputs)}")
    assert inputs["admin"]["type"] == "boolean"
    assert inputs["admin"]["default"] is False, (
        "the admin checkbox must default to false - nothing may be selected implicitly")


def _verify_admin_job() -> dict:
    """The job that proves the deployed admin console is not publicly reachable.

    Found by what it DOES - it reads the invoker policy - rather than by its name or by slicing
    3000 characters out of the file from a step title. The check used to be a step inside the
    deploy job and is now its own job running beside verify-console; locating it by behaviour is
    what stops this test from breaking again the next time it moves, while still failing loudly
    if it stops existing.
    """
    jobs = _doc()["jobs"]
    matches = {name: job for name, job in jobs.items()
               if "get-iam-policy" in yaml.dump(job)}
    assert len(matches) == 1, (
        f"expected exactly one job reading the invoker policy, found {sorted(matches)} - if the "
        f"admin check has been removed, the one failure that makes IAP pointless is unguarded")
    return next(iter(matches.values()))


def test_the_deployed_admin_is_proved_not_public():
    job = _verify_admin_job()
    body = yaml.dump(job)
    assert "admin_url" in yaml.dump(_doc()["jobs"]["provision"].get("outputs", {})), (
        "provision does not publish admin_url, so nothing downstream can verify the deployed "
        "admin console")
    assert "provision" in job["needs"], (
        "the admin check does not wait for the deploy it is checking")
    assert "admin_url" in body, "nothing verifies the deployed admin console"
    assert "allUsers" in body, (
        "the post-deploy check does not assert the absence of a public invoker binding - the one "
        "failure that makes IAP pointless")
    assert job.get("if"), (
        "the admin check runs unconditionally - a deploy that never reconciled the admin console "
        "would report a failure about a service it did not touch")


def _admin_check_step_text() -> str:
    return yaml.dump(_verify_admin_job())


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
