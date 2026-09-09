# Responsibility: Verify the edge reconciles rather than recreates, and never fails on a cert that cannot yet validate.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-edge.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "addresses describe"; then
  [ -n "${FAKE_IP_EXISTS:-}" ] || [ -f "${STATE}/address.created" ] || exit 1
  printf '%s\n' "${FAKE_IP:-203.0.113.10}"; exit 0
fi
if has "addresses create"; then
  : > "${STATE}/address.created"
  exit 0
fi
if has "ssl-certificates describe"; then
  [ -n "${FAKE_CERT_EXISTS:-}" ] || [ -f "${STATE}/cert.created" ] || exit 1
  printf '%s\n' "${FAKE_CERT_STATE:-PROVISIONING}"; exit 0
fi
if has "ssl-certificates create"; then
  : > "${STATE}/cert.created"
  exit 0
fi
if has "network-endpoint-groups describe"; then exit "${FAKE_NEG_RC:-1}"; fi
if has "backend-services describe";       then exit "${FAKE_BE_RC:-1}"; fi
if has "url-maps describe";               then exit "${FAKE_MAP_RC:-1}"; fi
if has "target-https-proxies describe";   then exit 1; fi
if has "target-http-proxies describe";    then exit 1; fi
if has "forwarding-rules describe";       then exit "${FAKE_RULE_RC:-1}"; fi
exit 0
"""

_ENV = {
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693", "GCP_REGION": "us-central1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_CONSOLE_SERVICE": "t-console", "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "CONSOLE_DOMAIN": "dev.console.hexera.ai", "ADMIN_DOMAIN": "dev.admin.hexera.ai",
    "EDGE_IP_NAME": "t-edge-ip", "EDGE_URL_MAP": "t-edge", "EDGE_CERT": "t-edge-cert",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over=None, fake=None):
        vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        env_file.write_text(
            "\n".join(f'{k}="{v}"' for k, v in vals.items() if v != "") + "\n", encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file),
                 **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def test_no_domains_is_a_stated_skip(run):
    done, calls = run({"CONSOLE_DOMAIN": "", "ADMIN_DOMAIN": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "addresses create" not in calls


def test_the_first_run_reserves_an_ip_and_prints_it(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "addresses create t-edge-ip" in calls
    assert "203.0.113.10" in done.stdout, "the IP an operator must put in DNS was not printed"


def test_a_provisioning_certificate_is_not_a_failure(run):
    """It cannot validate until DNS points at an IP that could not exist before this run."""
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_STATE": "PROVISIONING"})
    assert done.returncode == 0, done.stderr
    assert "PROVISIONING" in done.stdout


def test_a_failed_certificate_is_a_failure(run):
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_STATE": "FAILED_NOT_VISIBLE"})
    assert done.returncode != 0


def test_an_existing_ip_is_reused_never_recreated(run):
    """Recreating the address changes it, silently breaking DNS that was already correct."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1"})
    assert done.returncode == 0, done.stderr
    assert "addresses create" not in calls
    assert "addresses delete" not in calls


def test_an_existing_forwarding_rule_is_reused_never_recreated(run):
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_RULE_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "forwarding-rules create" not in calls
    assert "forwarding-rules delete" not in calls


def test_each_hostname_gets_a_host_rule_to_its_own_backend(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--new-hosts=dev.console.hexera.ai" in calls
    assert "--new-hosts=dev.admin.hexera.ai" in calls


def test_the_certificate_covers_exactly_the_declared_hostnames(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--domains=dev.console.hexera.ai,dev.admin.hexera.ai" in calls


def test_only_a_declared_hostname_is_served(run):
    done, calls = run({"ADMIN_DOMAIN": ""})
    assert done.returncode == 0, done.stderr
    assert "dev.console.hexera.ai" in calls
    assert "dev.admin.hexera.ai" not in calls


def test_the_http_rule_redirects_rather_than_serving(run):
    """Pointing the HTTP proxy at the serving URL map would serve the app over plain HTTP."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "url-maps import" in calls, "no redirect URL map was created"
    assert "target-http-proxies create" in calls


def test_the_ip_and_cert_state_are_published(run, tmp_path):
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f'{k}="{v}"' for k, v in _ENV.items()) + "\n", encoding="utf-8")
    subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "FAKE_GCP_STATE": str(tmp_path / "state"),
                        "DEPLOY_ENV_FILE": str(env_file), "GITHUB_OUTPUT": str(out)})
    written = out.read_text(encoding="utf-8")
    assert "edge_ip=" in written
    assert "edge_cert_state=" in written
