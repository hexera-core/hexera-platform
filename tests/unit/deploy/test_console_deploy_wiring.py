# Responsibility: Verify the console service name is pinned in CI and the API origin is discovered.
# Boundaries: it reads the workflow's declared targets and drives discovery against a fake gcloud;
# it provisions nothing and calls no real cloud.
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy.yml"
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"


# ---------------------------------------------------------------------------
# Fix 1: deploy.yml pins console_service (dev and prod) and maps it into the deploy job.
# ---------------------------------------------------------------------------

def _pick_step_run() -> str:
    """The `run:` script of the target job's `pick` step, as literal text."""
    doc = yaml.safe_load(DEPLOY_WF.read_text(encoding="utf-8"))
    steps = doc["jobs"]["target"]["steps"]
    (pick,) = [s for s in steps if s.get("id") == "pick"]
    return pick["run"]


def test_dev_pick_resolves_console_service_to_dev_console():
    run = _pick_step_run()
    assert re.search(r'echo "console_service=dev-console"', run), (
        "the dev branch of the pick step does not emit console_service=dev-console - the "
        "console stage has no name to reconcile and will keep stating its own skip")


def test_prod_pick_resolves_console_service_to_prod_console():
    run = _pick_step_run()
    # Matched as the exact literal echo statement, not a bare substring: "prod-console" would
    # also match if it were merely a suffix of some other value, so anchor on the whole
    # `echo "console_service=prod-console"` line.
    assert 'echo "console_service=prod-console"' in run, (
        "the prod branch of the pick step must emit console_service=prod-console now that the "
        "prod-console service account and its secret access exist in hexera-prod")


def test_target_job_declares_a_console_service_output():
    doc = yaml.safe_load(DEPLOY_WF.read_text(encoding="utf-8"))
    outputs = doc["jobs"]["target"]["outputs"]
    assert "console_service" in outputs, (
        "the target job's outputs block has no console_service entry, so steps.pick's output "
        "never leaves the job")
    assert outputs["console_service"] == "${{ steps.pick.outputs.console_service }}"


def test_deploy_job_maps_cloudrun_console_service_from_the_target_output():
    doc = yaml.safe_load(DEPLOY_WF.read_text(encoding="utf-8"))
    env = doc["jobs"]["deploy"]["env"]
    assert env.get("CLOUDRUN_CONSOLE_SERVICE") == "${{ needs.target.outputs.console_service }}", (
        "the deploy job's env block does not map CLOUDRUN_CONSOLE_SERVICE from "
        "needs.target.outputs.console_service - bootstrap-env.sh's CLOUDRUN_CONSOLE_SERVICE "
        "would stay empty regardless of what pick resolved")


# ---------------------------------------------------------------------------
# Fix 2: bootstrap-env.sh discovers HEXERA_API_BASE_URL from the live API service.
# ---------------------------------------------------------------------------

# A fake gcloud whose answers come entirely from FAKE_* env vars, matching the shim in
# test_discovery.py plus a `run services describe` branch for the API's public URL.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "config get-value project"; then printf '%s\n' "${FAKE_PROJECT:-fake-proj}"; exit 0; fi
if has "config get-value run/region"; then printf '%s\n' "${FAKE_RUN_REGION:-us-fake1}"; exit 0; fi
if has "projects describe"; then [ -n "${FAKE_DENY_PROJECT:-}" ] && exit 1; printf '%s\n' "${FAKE_PROJECT_NUMBER:-123456}"; exit 0; fi
if has "run jobs list"; then
  if has "cloud.googleapis.com/location"; then printf '%s\n' "${FAKE_JOB_REGION:-us-fake1}"; exit 0; fi
  [ -n "${FAKE_DENY_JOBS_LIST:-}" ] && exit 1
  printf '%b' "${FAKE_MESH_JOBS:-}"; exit 0
fi
if has "run jobs describe"; then
  [ -n "${FAKE_DENY_DESCRIBE:-}" ] && exit 1
  printf '%s\n' "${FAKE_MESH_SA:-mesh-sa@fake-proj.iam.gserviceaccount.com}"; exit 0
fi
if has "storage buckets list"; then printf '%b' "${FAKE_BUCKETS:-}"; exit 0; fi
if has "storage buckets describe"; then [ -n "${FAKE_BUCKET_ABSENT:-}" ] && exit 1; exit 0; fi
if has "run services describe"; then
  # Record that discovery actually reached for the API service's URL, so a test can tell
  # "the read happened and degraded safely" apart from "the read was never attempted" - the
  # latter is what the pre-fix script does, and looks identical from the final env file alone.
  [ -n "${FAKE_CALL_LOG:-}" ] && printf 'run services describe %s\n' "${ARGS}" >> "${FAKE_CALL_LOG}"
  [ -n "${FAKE_DENY_API_DESCRIBE:-}" ] && exit 1
  printf '%s\n' "${FAKE_API_URL:-}"; exit 0
fi
exit 0
"""


def _gcloud_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(_FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    base = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "DEPLOY_ENV_FILE": str(tmp_path / "generated.env"),
        "FAKE_PROJECT": "fake-proj",
        "FAKE_RUN_REGION": "us-fake1",
    }
    return base, tmp_path


def _run(base, extra, *args):
    return subprocess.run(["bash", str(BOOTSTRAP), *args],
                           env={**base, **extra}, capture_output=True, text=True, timeout=60)


def _gen(tmp_path) -> dict[str, str]:
    f = tmp_path / "generated.env"
    out = {}
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def test_discovery_resolves_api_base_url_when_console_is_configured(tmp_path):
    base, tmp = _gcloud_env(tmp_path)
    p = _run(base, {
        "CLOUDRUN_CONSOLE_SERVICE": "dev-console",
        "CLOUDRUN_API_SERVICE": "dev-api",
        "FAKE_API_URL": "https://dev-api-abc123-uc.a.run.app",
    })
    assert p.returncode == 0, p.stderr
    env = _gen(tmp)
    assert env.get("HEXERA_API_BASE_URL") == "https://dev-api-abc123-uc.a.run.app", (
        "discovery did not resolve HEXERA_API_BASE_URL from the named CLOUDRUN_API_SERVICE "
        "even though a console is configured")
    # NEXT_PUBLIC_HEXERA_API_BASE_URL defaults to HEXERA_API_BASE_URL - confirm it follows.
    assert env.get("NEXT_PUBLIC_HEXERA_API_BASE_URL") == "https://dev-api-abc123-uc.a.run.app"


def test_discovery_leaves_api_base_url_empty_when_the_api_service_cannot_be_read(tmp_path):
    base, tmp = _gcloud_env(tmp_path)
    call_log = tmp / "calls.log"
    p = _run(base, {
        "CLOUDRUN_CONSOLE_SERVICE": "dev-console",
        "CLOUDRUN_API_SERVICE": "dev-api",
        "FAKE_DENY_API_DESCRIBE": "1",
        "FAKE_CALL_LOG": str(call_log),
    })
    assert p.returncode == 0, p.stderr
    # The read must actually have been ATTEMPTED (not just coincidentally absent, which is also
    # what the unfixed script produces) and must have degraded quietly rather than aborting.
    assert call_log.exists() and "dev-api" in call_log.read_text(), (
        "discovery never attempted to read the API service's URL at all")
    env = _gen(tmp)
    assert env.get("HEXERA_API_BASE_URL", "") == "", (
        "a failed read of the API service must leave HEXERA_API_BASE_URL empty, not invent a "
        "URL or fail the whole discovery run")


def test_discovery_leaves_api_base_url_empty_when_api_service_returns_no_url(tmp_path):
    base, tmp = _gcloud_env(tmp_path)
    call_log = tmp / "calls.log"
    p = _run(base, {
        "CLOUDRUN_CONSOLE_SERVICE": "dev-console",
        "CLOUDRUN_API_SERVICE": "dev-api",
        "FAKE_API_URL": "",
        "FAKE_CALL_LOG": str(call_log),
    })
    assert p.returncode == 0, p.stderr
    assert call_log.exists() and "dev-api" in call_log.read_text(), (
        "discovery never attempted to read the API service's URL at all")
    env = _gen(tmp)
    assert env.get("HEXERA_API_BASE_URL", "") == "", (
        "an API service with no status.url yet must leave HEXERA_API_BASE_URL empty")


def test_discovery_does_not_overwrite_an_already_set_api_base_url(tmp_path):
    base, tmp = _gcloud_env(tmp_path)
    p = _run(base, {
        "CLOUDRUN_CONSOLE_SERVICE": "dev-console",
        "CLOUDRUN_API_SERVICE": "dev-api",
        "HEXERA_API_BASE_URL": "https://operator-set-this.example.com",
        "FAKE_API_URL": "https://dev-api-abc123-uc.a.run.app",
    })
    assert p.returncode == 0, p.stderr
    env = _gen(tmp)
    assert env.get("HEXERA_API_BASE_URL") == "https://operator-set-this.example.com", (
        "an operator-supplied HEXERA_API_BASE_URL must win over discovery, exactly as an "
        "explicit MIGRATE_DB_HOST or REDIS_URL does")


def test_discovery_does_not_attempt_the_read_without_a_configured_console(tmp_path):
    # No CLOUDRUN_CONSOLE_SERVICE at all: even though the API service exists and resolves, this
    # is not a console deployment and there is no /api/v1 proxy for the URL to serve. An empty
    # final value alone would also be produced by a read that was ATTEMPTED and returned nothing
    # (see test_discovery_leaves_api_base_url_empty_when_api_service_returns_no_url), so this
    # asserts the read never happened at all, via the same FAKE_CALL_LOG the deny-path tests use.
    base, tmp = _gcloud_env(tmp_path)
    call_log = tmp / "calls.log"
    p = _run(base, {
        "CLOUDRUN_API_SERVICE": "dev-api",
        "FAKE_API_URL": "https://dev-api-abc123-uc.a.run.app",
        "FAKE_CALL_LOG": str(call_log),
    })
    assert p.returncode == 0, p.stderr
    assert not call_log.exists(), (
        "discovery attempted to read the API service's URL even though no console is "
        "configured - the console stage is the only consumer of HEXERA_API_BASE_URL")
    env = _gen(tmp)
    assert env.get("HEXERA_API_BASE_URL", "") == ""
