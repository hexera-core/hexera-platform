# Responsibility: Verify the edge reconciles rather than recreates, and never fails on a cert that cannot yet validate.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-edge.sh"

# A STATEFUL fake: it remembers what earlier calls created, because the property most worth
# testing here is what the SECOND run does. A fake that answers every describe the same way
# regardless of history can only ever exercise a first run, and a first run is the one case the
# real bugs in this script did not have.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
# the argument that FOLLOWS <word> - i.e. the resource name after `describe`/`create`/`import`
after(){ local want="$1" prev="" a; shift
  for a in "$@"; do
    if [ "${prev}" = "${want}" ]; then printf '%s' "${a}"; return 0; fi
    prev="${a}"
  done
  return 1; }
# the value of a --flag=value argument
flag(){ local want="$1" a; shift
  for a in "$@"; do
    case "${a}" in "${want}="*) printf '%s' "${a#*=}"; return 0;; esac
  done
  return 1; }

if has "addresses describe"; then
  if [ -n "${FAKE_IP_EXISTS:-}" ] || [ -f "${STATE}/address.created" ]; then
    printf '%s\n' "${FAKE_IP:-203.0.113.10}"; exit 0
  fi
  exit 1
fi
if has "addresses create"; then : > "${STATE}/address.created"; exit 0; fi

if has "ssl-certificates create"; then
  flag --domains "$@" > "${STATE}/cert.domains" || true
  : > "${STATE}/cert.created"; exit 0
fi
if has "ssl-certificates describe"; then
  if [ -z "${FAKE_CERT_EXISTS:-}" ] && [ ! -f "${STATE}/cert.created" ]; then exit 1; fi
  if has "managed.domains"; then
    if [ -n "${FAKE_CERT_DOMAINS:-}" ]; then d="${FAKE_CERT_DOMAINS}"
    elif [ -f "${STATE}/cert.domains" ]; then d="$(cat "${STATE}/cert.domains")"
    # A certificate that predates this state directory covers what the fixture declares, so a
    # scenario about something else (a PROVISIONING status, a reused address) is not derailed by
    # a drift refusal it never asked for. FAKE_CERT_DOMAINS is how a test asks for drift.
    else d="dev.console.hexera.ai,dev.admin.hexera.ai"; fi
    # real gcloud joins a repeated field with ';'
    printf '%s' "${d}" | tr ',' ';'; printf '\n'; exit 0
  fi
  printf '%s\n' "${FAKE_CERT_STATE:-PROVISIONING}"; exit 0
fi

if has "network-endpoint-groups create"; then
  n="$(after create "$@" || true)"; : > "${STATE}/neg.${n}"; exit 0
fi
if has "network-endpoint-groups describe"; then
  n="$(after describe "$@" || true)"
  if [ "${FAKE_NEG_RC:-1}" = 0 ] || [ -f "${STATE}/neg.${n}" ]; then exit 0; fi
  exit 1
fi

if has "backend-services create"; then
  b="$(after create "$@" || true)"; : > "${STATE}/be.${b}"; exit 0
fi
if has "backend-services add-backend"; then
  b="$(after add-backend "$@" || true)"
  g="$(flag --network-endpoint-group "$@" || true)"
  printf 'https://www.googleapis.com/compute/v1/projects/p/regions/r/networkEndpointGroups/%s\n' \
    "${g}" >> "${STATE}/be-backends.${b}"
  exit 0
fi
if has "backend-services describe"; then
  b="$(after describe "$@" || true)"
  if [ "${FAKE_BE_RC:-1}" = 0 ] || [ -f "${STATE}/be.${b}" ]; then
    # An existing backend service with NOTHING attached is a real and reachable state: a prior run
    # created it and died before attaching. `value(backends[].group)` is empty then, not absent.
    if has "backends[].group"; then cat "${STATE}/be-backends.${b}" 2>/dev/null || true; fi
    exit 0
  fi
  exit 1
fi

if has "url-maps import"; then
  m="$(after import "$@" || true)"
  s="$(flag --source "$@" || true)"
  [ -z "${s}" ] || cp "${s}" "${STATE}/import-${m}.yaml"
  : > "${STATE}/map.${m}"          # import INSERTS when absent and REPLACES when present
  exit 0
fi
if has "url-maps add-path-matcher"; then
  # THE REAL CONFLICT, modelled. gcloud's add_path_matcher surface refuses a host that already
  # belongs to a host rule:
  #   "Cannot create a new host rule with host [X] because the host is already part of a host rule
  #    that references the path matcher [Y]"
  # This is why a second run of the old script exited 1 before the certificate was ever read.
  m="$(after add-path-matcher "$@" || true)"
  h="$(flag --new-hosts "$@" || true)"
  f="${STATE}/hosts.${m}"
  if [ -f "${f}" ] && grep -q -x -- "${h}" "${f}"; then
    printf 'ERROR: Cannot create a new host rule with host [%s] because the host is already part of a host rule that references the path matcher [%s]\n' \
      "${h}" "$(flag --path-matcher-name "$@" || true)" >&2
    exit 1
  fi
  printf '%s\n' "${h}" >> "${f}"
  exit 0
fi
if has "url-maps describe"; then
  m="$(after describe "$@" || true)"
  if [ "${FAKE_MAP_RC:-1}" = 0 ] || [ -f "${STATE}/map.${m}" ]; then exit 0; fi
  exit 1
fi

case "${ARGS}" in
  *"target-https-proxies create"*|*"target-http-proxies create"*|*"forwarding-rules create"*)
    n="$(after create "$@" || true)"; : > "${STATE}/res.${n}"; exit 0 ;;
esac
if has "target-https-proxies describe" || has "target-http-proxies describe"; then
  n="$(after describe "$@" || true)"
  if [ -f "${STATE}/res.${n}" ]; then exit 0; fi
  exit 1
fi
if has "forwarding-rules describe"; then
  n="$(after describe "$@" || true)"
  if [ "${FAKE_RULE_RC:-1}" = 0 ] || [ -f "${STATE}/res.${n}" ]; then exit 0; fi
  exit 1
fi
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

    # The same state directory across every call, so a test can drive the stage twice and read
    # what the second run saw.
    _run.state = state
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
    done, _ = run()
    assert done.returncode == 0, done.stderr
    served = (run.state / "import-t-edge.yaml").read_text(encoding="utf-8")
    assert "name: t-edge" in served
    for host, backend in (("dev.console.hexera.ai", "t-console-be"),
                          ("dev.admin.hexera.ai", "t-admin-be")):
        assert f"  - {host}\n  pathMatcher: {backend}-pm\n" in served, served
        assert f"- name: {backend}-pm\n  defaultService: " in served, served
        assert f"/backendServices/{backend}\n" in served, served


def test_the_certificate_covers_exactly_the_declared_hostnames(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--domains=dev.console.hexera.ai,dev.admin.hexera.ai" in calls


def test_only_a_declared_hostname_is_served(run):
    done, calls = run({"ADMIN_DOMAIN": ""})
    assert done.returncode == 0, done.stderr
    assert "dev.console.hexera.ai" in calls
    assert "dev.admin.hexera.ai" not in calls
    served = (run.state / "import-t-edge.yaml").read_text(encoding="utf-8")
    assert "dev.admin.hexera.ai" not in served


def test_the_http_rule_redirects_rather_than_serving(run):
    """Pointing the HTTP proxy at the serving URL map would serve the app over plain HTTP."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "url-maps import" in calls, "no redirect URL map was created"
    assert "target-http-proxies create" in calls
    redirect = (run.state / "import-t-edge-redirect.yaml").read_text(encoding="utf-8")
    assert "httpsRedirect: true" in redirect


def test_running_twice_against_one_state_succeeds_both_times(run):
    """THE regression: `add-path-matcher` refuses a host that already has a host rule, so the old
    script exited 1 on every run after the first - dying before the certificate was read, before
    the DNS block printed and before $GITHUB_OUTPUT was written. deploy.sh is `set -euo pipefail`,
    so that aborted every deploy after the first at this stage."""
    first, _ = run()
    assert first.returncode == 0, first.stderr

    second, calls = run()
    assert second.returncode == 0, second.stderr
    assert "already part of a host rule" not in second.stderr

    # The second run reused everything the first reserved, and still printed what an operator
    # needs - the two things dying here used to cost.
    assert calls.count("addresses create t-edge-ip") == 1
    assert "delete" not in calls
    assert "203.0.113.10" in second.stdout
    assert "dev.console.hexera.ai  A  203.0.113.10" in second.stdout

    # The serving map is rewritten whole rather than appended to, so it still describes exactly
    # the two declared hostnames after two runs.
    served = (run.state / "import-t-edge.yaml").read_text(encoding="utf-8")
    assert served.count("pathMatcher:") == 2
    assert served.count("- name:") == 2

    # And the NEGs were attached once each, not once per run.
    assert calls.count("backend-services add-backend") == 2


def test_a_reused_backend_service_with_no_backends_still_gets_its_neg(run):
    """A run that created the backend service and then died - on the certificate, on a transient
    API error, on the old second-run failure above - leaves a backend service with nothing behind
    it. Gating the attach on 'did I just create this' left that load balancer serving 502 forever,
    because every later run reused the empty backend service and attached nothing."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_BE_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "backend-services create" not in calls, "the backend service was supposed to pre-exist"
    assert "backend-services add-backend t-console-be" in calls
    assert "--network-endpoint-group=t-console-neg" in calls


def test_an_already_attached_neg_is_not_attached_again(run):
    first, _ = run()
    assert first.returncode == 0, first.stderr
    before = (run.state / "calls.log").read_text(encoding="utf-8").count("add-backend")
    second, calls = run()
    assert second.returncode == 0, second.stderr
    assert calls.count("add-backend") == before, "the NEG was attached a second time"


def test_certificate_drift_is_refused_with_a_remediation(run):
    """Adding ADMIN_DOMAIN to a deployment that previously declared only CONSOLE_DOMAIN adds the
    admin host rule and prints an admin A record while the certificate still covers only the
    console name - that hostname then serves a mismatched certificate and browsers hard-fail."""
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_DOMAINS": "dev.console.hexera.ai"})
    assert done.returncode != 0, "certificate drift was accepted silently"
    assert "t-edge-cert" in done.stderr
    assert "dev.admin.hexera.ai" in done.stderr, "the refusal does not say what is missing"
    assert "ssl-certificates create t-edge-cert-v2" in done.stderr, "no remediation was offered"


def test_a_certificate_covering_the_declared_hostnames_is_not_drift(run):
    """The order gcloud returns is not the order this deployment declares, and neither is data."""
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_DOMAINS": "dev.admin.hexera.ai;dev.console.hexera.ai"})
    assert done.returncode == 0, done.stderr


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
