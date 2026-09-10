# Responsibility: Verify deploy.yml implements the design spec's post-deploy console check
# (docs/superpowers/specs/2026-09-07-saas-console-design.md §4, "Verification") - and only when
# the console was actually reconciled, using the URL create-console-service.sh already resolves.
# Boundaries: it reads the workflow document; it runs no deploy and reaches no network. The
# script-level half of this fix (create-console-service.sh writing $GITHUB_OUTPUT) is covered by
# tests/unit/deploy/test_console_service_stage.py.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy.yml"


def _deploy_job() -> dict:
    doc = yaml.safe_load(DEPLOY_WF.read_text(encoding="utf-8"))
    return doc["jobs"]["deploy"]


def _step(name_fragment: str) -> dict:
    steps = _deploy_job()["steps"]
    matches = [s for s in steps if s.get("name") and name_fragment in s["name"]]
    assert len(matches) == 1, (
        f"expected exactly one step whose name contains {name_fragment!r}, found {len(matches)}")
    return matches[0]


def test_the_provisioning_step_publishes_an_id_for_its_output():
    provision = _step("Provision the deployment")
    assert provision.get("id") == "deploy", (
        "the provisioning step has no id: deploy - a later step cannot reference "
        "steps.deploy.outputs.console_url without one")


def test_a_verification_step_exists_gated_on_the_console_actually_being_reconciled():
    verify = _step("Verify the deployed console")
    assert verify.get("if") == "steps.deploy.outputs.console_url != ''", (
        "the verification step must run only when create-console-service.sh actually reported a "
        "URL - i.e. the console was reconciled - not on every deploy regardless of whether the "
        "console was in DEPLOY_COMPONENTS or configured for this target at all")
    assert verify.get("env", {}).get("CONSOLE_URL") == "${{ steps.deploy.outputs.console_url }}", (
        "the verification step does not read CONSOLE_URL from steps.deploy.outputs.console_url - "
        "it would have to re-derive the URL instead of using the one the script already resolved")


def test_the_verification_step_runs_after_provisioning_and_before_always_on_steps():
    steps = _deploy_job()["steps"]
    names = [s.get("name") or "" for s in steps]
    provision_at = next(i for i, n in enumerate(names) if "Provision the deployment" in n)
    verify_at = next(i for i, n in enumerate(names) if "Verify the deployed console" in n)
    assert provision_at < verify_at, "verification must run after provisioning, not before"
    # The verify step must NOT be if:always() - a real deploy failure should still stop it from
    # being (mis)reported as a console problem when the console was never reached.
    assert _step("Verify the deployed console").get("if") != "always()"


def test_the_verification_step_checks_all_four_spec_assertions():
    verify = _step("Verify the deployed console")
    run = verify["run"]
    # 1) unauthenticated / redirects to /sign-in.
    assert "/sign-in" in run and "redirect_url" in run
    # 2) the sign-in page renders, strongly enough to notice a broken asset path: it must fetch
    #    the referenced /_next/static assets for real, not just check the page's own status code.
    assert "/_next/static/" in run, (
        "the verification step never fetches a /_next/static asset - a 200 on /sign-in alone "
        "would not notice a broken .next/static or public/ path")
    # 3) /api/internal/health returns 200 - the same unauthenticated route Gate C's in-container
    #    smoke already curls, so post-deploy and build-time verification agree on one endpoint.
    assert "/api/internal/health" in run
    # 4) /readyz returns 401, not 200: /readyz is deliberately session-gated (it proxies to the
    #    product API presenting MESH_API_KEY), so an unauthenticated 200 there would mean any
    #    caller of this allUsers-invokable service could spend the console's own API key against
    #    the product API. 401 is the passing case - it proves the session gate is actually live on
    #    the deployed revision.
    assert "/readyz" in run and '"401"' in run

    # It must not follow redirects with -L when checking step 1 (that would hide a redirect to
    # somewhere other than /sign-in behind a final 200), and every curl call must be bounded so a
    # hung console cannot burn the job to its timeout ceiling the way the pre-fix Gate C smoke did.
    assert "-L " not in run and not run.strip().startswith("-L")
    assert run.count("--max-time") >= 4, (
        "each HTTP check (/, /sign-in, its assets, /api/internal/health, /readyz) should be "
        "individually time-bounded")


def test_readyz_route_exists_for_the_check_to_target():
    assert (REPO / "apps" / "console" / "src" / "app" / "readyz" / "route.ts").exists()


def _asset_extraction_pattern() -> str:
    """The character class handed to `grep -oE` in the /_next/static asset-extraction line, as
    the literal bytes bash sees (not a Python re-escaped string)."""
    run = _step("Verify the deployed console")["run"]
    m = re.search(r"grep -oE '(/_next/static/[^']+)'", run)
    assert m, (
        "could not find the asset-extraction `grep -oE '/_next/static/...'` call in the verify "
        "step - has it been rewritten to use a different tool or quoting style?")
    return m.group(1)


def test_the_asset_extraction_pattern_excludes_a_backslash():
    """Next.js embeds asset paths JSON-escaped inside <script> payloads
    (`\\"/_next/static/chunks/foo.js\\"`), so a character class that excludes only `"` also
    captures the backslash sitting immediately before the closing quote - producing a path like
    `/_next/static/chunks/foo.js\\` that a real server answers with a redirect, not 200, because
    it was never a real asset. A real deploy hit exactly this:
    `::error::asset /_next/static/chunks/0dmr_rldukvyq.js\\ returned 308, not 200`. Reproduced
    here against a fixture containing both an escaped-JSON occurrence and an ordinary quoted
    attribute occurrence, using the pattern actually embedded in the workflow (not a
    reimplementation of it) so this test tracks the real fix."""
    pattern = _asset_extraction_pattern()
    fixture = (
        r'<script>self.__next_f.push([1,"...\"/_next/static/chunks/escaped.js\"..."])</script>'
        '\n'
        '<link rel="stylesheet" href="/_next/static/css/plain.css">'
    )
    result = subprocess.run(
        ["grep", "-oE", pattern],
        input=fixture, capture_output=True, text=True, timeout=10)
    assets = sorted(set(result.stdout.splitlines()))
    assert not any(a.endswith("\\") for a in assets), (
        f"the extraction pattern still captures a trailing backslash from a JSON-escaped "
        f"occurrence: {assets!r} - this is the exact bug that turned a healthy console into a "
        f"failed deploy (a 308 on a path that was never real)")
    # A regex tight enough to exclude the backslash but too tight to still find real assets would
    # make this check silently vacuous - confirm both occurrences (escaped and plain) are found.
    assert "/_next/static/chunks/escaped.js" in assets
    assert "/_next/static/css/plain.css" in assets
