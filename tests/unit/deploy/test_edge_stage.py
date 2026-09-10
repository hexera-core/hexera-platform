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

# CERTIFICATES ARE KEYED BY NAME, because there is one per hostname now and a test's whole point
# is often that they differ. `<base>-console` / `<base>-admin`, so the trailing word is the label a
# per-host override names: FAKE_CERT_STATE_admin=FAILED_NOT_VISIBLE leaves the console's alone.
if has "ssl-certificates create"; then
  n="$(after create "$@" || true)"
  flag --domains "$@" > "${STATE}/cert.domains.${n}" || true
  : > "${STATE}/cert.created.${n}"; exit 0
fi
if has "ssl-certificates describe"; then
  n="$(after describe "$@" || true)"
  lbl="${n##*-}"
  if [ -z "${FAKE_CERT_EXISTS:-}" ] && [ ! -f "${STATE}/cert.created.${n}" ]; then exit 1; fi
  if has "managed.domains"; then
    v="FAKE_CERT_DOMAINS_${lbl}"
    if [ -n "${!v:-}" ]; then d="${!v}"
    elif [ -n "${FAKE_CERT_DOMAINS:-}" ]; then d="${FAKE_CERT_DOMAINS}"
    elif [ -f "${STATE}/cert.domains.${n}" ]; then d="$(cat "${STATE}/cert.domains.${n}")"
    # A certificate that predates this state directory covers the one hostname its name is for, so
    # a scenario about something else (a PROVISIONING status, a reused address) is not derailed by
    # a drift refusal it never asked for. FAKE_CERT_DOMAINS[_<label>] is how a test asks for drift.
    else d="dev.${lbl}.hexera.ai"; fi
    # real gcloud joins a repeated field with ';'
    printf '%s' "${d}" | tr ',' ';'; printf '\n'; exit 0
  fi
  v="FAKE_CERT_STATE_${lbl}"
  printf '%s\n' "${!v:-${FAKE_CERT_STATE:-PROVISIONING}}"; exit 0
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

# THE PROXY REMEMBERS ITS CERTIFICATE LIST, which is the only way to see whether a MIGRATION
# happened: a proxy that already exists is never created, so `create` says nothing about what it
# ends up serving. FAKE_PROXY_CERTS seeds a proxy that predates this state directory - e.g. the one
# shared certificate dev really has attached today.
if has "target-https-proxies update"; then
  n="$(after update "$@" || true)"
  flag --ssl-certificates "$@" > "${STATE}/proxy-certs.${n}" || true
  exit 0
fi
case "${ARGS}" in
  *"target-https-proxies create"*)
    n="$(after create "$@" || true)"; : > "${STATE}/res.${n}"
    flag --ssl-certificates "$@" > "${STATE}/proxy-certs.${n}" || true
    exit 0 ;;
  *"target-http-proxies create"*|*"forwarding-rules create"*)
    n="$(after create "$@" || true)"; : > "${STATE}/res.${n}"; exit 0 ;;
esac
if has "target-https-proxies describe"; then
  n="$(after describe "$@" || true)"
  if [ ! -f "${STATE}/res.${n}" ] && [ -n "${FAKE_PROXY_CERTS:-}" ]; then
    printf '%s' "${FAKE_PROXY_CERTS}" > "${STATE}/proxy-certs.${n}"
    : > "${STATE}/res.${n}"
  fi
  [ -f "${STATE}/res.${n}" ] || exit 1
  if has "sslCertificates"; then
    # real gcloud returns FULL RESOURCE URLs, joined with ';'
    tr ',' '\n' < "${STATE}/proxy-certs.${n}" 2>/dev/null | sed -e '/^[[:space:]]*$/d' \
      -e 's#^#https://www.googleapis.com/compute/v1/projects/p/global/sslCertificates/#' \
      | tr '\n' ';'
    printf '\n'
  fi
  exit 0
fi
if has "target-http-proxies describe"; then
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


def test_every_hostname_gets_a_certificate_of_its_own(run):
    """THE change. One certificate covering both names couples them: a managed certificate serves
    only when it is WHOLLY ACTIVE, so dev ran with `dev.console.hexera.ai` ACTIVE and unreachable
    over HTTPS behind a `dev.admin.hexera.ai` at FAILED_NOT_VISIBLE. In prod that shape lets admin
    take console - the customer-facing name - down with it."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "ssl-certificates create t-edge-cert-console --domains=dev.console.hexera.ai" in calls
    assert "ssl-certificates create t-edge-cert-admin --domains=dev.admin.hexera.ai" in calls
    # No certificate names two hostnames - that is the coupling, and it is what is being removed.
    assert "--domains=dev.console.hexera.ai,dev.admin.hexera.ai" not in calls
    for line in calls.splitlines():
        if "ssl-certificates create" not in line:
            continue
        domains = [a[len("--domains="):] for a in line.split() if a.startswith("--domains=")]
        assert len(domains) == 1, line
        assert "," not in domains[0], f"{domains[0]} couples two hostnames onto one certificate"


def test_the_https_proxy_is_given_every_hostname_certificate(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--ssl-certificates=t-edge-cert-console,t-edge-cert-admin" in calls, calls


def test_the_https_proxy_is_given_only_the_declared_hostname_certificate(run):
    done, calls = run({"ADMIN_DOMAIN": ""})
    assert done.returncode == 0, done.stderr
    assert "--ssl-certificates=t-edge-cert-console" in calls
    assert "t-edge-cert-admin" not in calls


def test_the_migration_keeps_the_serving_certificate_until_ours_can_serve(run):
    """THE OUTAGE THIS EXISTS TO PREVENT, and it was a real one. dev had the shared `dev-edge-cert`
    attached and ACTIVE. Narrowing the proxy to the two per-hostname certificates while both were
    still PROVISIONING took dev.console and dev.admin off HTTPS entirely - TCP connected and the
    handshake was aborted - until the old certificate was reattached by hand.

    Withholding ours until they are ACTIVE is not the fix and cannot be: a Google-managed
    certificate does not begin validating until it is attached to a target proxy with a forwarding
    rule, so a certificate withheld until ACTIVE never becomes ACTIVE. Attachment is a
    precondition of validation. The union is the only shape that both serves and validates."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1", "FAKE_RULE_RC": "0",
                            "FAKE_PROXY_CERTS": "t-edge-cert"})
    assert done.returncode == 0, done.stderr
    assert "target-https-proxies create" not in calls, "the proxy was supposed to pre-exist"
    # The retained certificate leads: the load balancer serves the FIRST attached certificate
    # matching the SNI name, so a PROVISIONING one ahead of it would make the union pointless.
    assert ("target-https-proxies update t-edge-https-proxy --global "
            "--ssl-certificates=t-edge-cert,t-edge-cert-console,t-edge-cert-admin") in calls, calls
    assert "delete" not in calls
    assert "addresses create" not in calls
    assert "forwarding-rules create" not in calls
    attached = (run.state / "proxy-certs.t-edge-https-proxy").read_text(encoding="utf-8")
    assert attached == "t-edge-cert,t-edge-cert-console,t-edge-cert-admin"


def test_the_old_certificate_is_detached_once_every_one_of_ours_is_active(run):
    """The other half of the cutover. Retaining forever would leave the shared certificate serving
    both names for good, which is the coupling the split exists to remove - so once every
    per-hostname certificate is ACTIVE the list narrows to exactly ours."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1", "FAKE_RULE_RC": "0",
                            "FAKE_PROXY_CERTS": "t-edge-cert", "FAKE_CERT_STATE": "ACTIVE"})
    assert done.returncode == 0, done.stderr
    assert ("target-https-proxies update t-edge-https-proxy --global "
            "--ssl-certificates=t-edge-cert-console,t-edge-cert-admin") in calls, calls
    # Detaching is not deleting. The old certificate survives for an operator to remove.
    assert "delete" not in calls
    attached = (run.state / "proxy-certs.t-edge-https-proxy").read_text(encoding="utf-8")
    assert attached == "t-edge-cert-console,t-edge-cert-admin"


def test_one_hostname_still_provisioning_holds_the_whole_cutover(run):
    """ALL of ours, not SOME. Narrowing while admin is still PROVISIONING would take the admin
    hostname off HTTPS even though the console's certificate was ready - the certificate is per
    hostname, but the proxy's list is shared, so the cutover is only safe when every one is ready."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1", "FAKE_RULE_RC": "0",
                            "FAKE_PROXY_CERTS": "t-edge-cert",
                            "FAKE_CERT_STATE_console": "ACTIVE",
                            "FAKE_CERT_STATE_admin": "PROVISIONING"})
    assert done.returncode == 0, done.stderr
    attached = (run.state / "proxy-certs.t-edge-https-proxy").read_text(encoding="utf-8")
    assert attached == "t-edge-cert,t-edge-cert-console,t-edge-cert-admin", attached


def test_a_first_run_attaches_only_its_own_certificates(run):
    """A project with no edge yet has nothing to retain, so the union degenerates to ours alone -
    prod's first run must not invent a certificate to keep."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--ssl-certificates=t-edge-cert-console,t-edge-cert-admin" in calls, calls


def test_a_proxy_already_carrying_the_right_certificates_is_left_alone(run):
    """Reconciling compares SETS, so a proxy that already serves both - in either order - is not
    rewritten on every run."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                            "FAKE_PROXY_CERTS": "t-edge-cert-admin,t-edge-cert-console"})
    assert done.returncode == 0, done.stderr
    assert "target-https-proxies update" not in calls
    assert "target-https-proxies create" not in calls


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
    """A certificate named for one hostname that covers a DIFFERENT one - an older shared
    certificate left under a name this scheme now claims, a hand-made one - makes that hostname
    serve a mismatched certificate, and browsers hard-fail on it. A managed certificate's domains
    cannot be edited and this script never deletes, so it says so instead of repairing it."""
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_DOMAINS_admin": "dev.console.hexera.ai"})
    assert done.returncode != 0, "certificate drift was accepted silently"
    assert "t-edge-cert-admin" in done.stderr
    assert "dev.admin.hexera.ai" in done.stderr, "the refusal does not say what is missing"
    assert "ssl-certificates create t-edge-cert-admin-v2" in done.stderr, "no remediation offered"
    # The remediation keeps the OTHER hostname attached: the proxy takes the whole list on every
    # update, so a remediation naming only the replacement would take console off HTTPS - the very
    # coupling one certificate per hostname exists to remove.
    assert "--ssl-certificates=t-edge-cert-console,t-edge-cert-admin-v2" in done.stderr


def test_a_certificate_covering_exactly_its_own_hostname_is_not_drift(run):
    """gcloud returns a repeated field ';'-joined, not in the form this deployment declares it in;
    only the set is data. And an existing certificate is reused, never recreated."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                            "FAKE_CERT_DOMAINS_console": "dev.console.hexera.ai",
                            "FAKE_CERT_DOMAINS_admin": "dev.admin.hexera.ai"})
    assert done.returncode == 0, done.stderr
    assert "ssl-certificates create" not in calls
    assert "ssl-certificates delete" not in calls


def test_one_failed_hostname_fails_the_run_and_the_other_is_still_reported(run):
    """The FAILED rule is unchanged - a state that does not resolve itself is a failure. What
    changes is that the ACTIVE hostname is still read, still reported and still attached: its
    certificate is a separate resource now, so its neighbour's failure is not its own."""
    done, calls = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                            "FAKE_CERT_STATE_console": "ACTIVE",
                            "FAKE_CERT_STATE_admin": "FAILED_NOT_VISIBLE"})
    assert done.returncode != 0
    assert "dev.console.hexera.ai" in done.stdout
    assert "t-edge-cert-console  ACTIVE" in done.stdout, done.stdout
    assert "FAILED_NOT_VISIBLE" in done.stderr
    assert "dev.admin.hexera.ai" in done.stderr
    # The console's certificate was attached BEFORE the admin's state could stop the run. Refusing
    # first would leave a console that validated with no certificate on the proxy, and no re-run
    # would ever attach one - the same deadlock, moved into this script.
    assert "--ssl-certificates=t-edge-cert-console,t-edge-cert-admin" in calls


def test_one_provisioning_hostname_beside_an_active_one_is_not_a_failure(run):
    """A certificate cannot validate before DNS resolves, so PROVISIONING is a passing state - and
    it stays one whatever the other hostname is doing."""
    done, _ = run(fake={"FAKE_IP_EXISTS": "1", "FAKE_CERT_EXISTS": "1",
                        "FAKE_CERT_STATE_console": "ACTIVE",
                        "FAKE_CERT_STATE_admin": "PROVISIONING"})
    assert done.returncode == 0, done.stderr
    assert "t-edge-cert-console  ACTIVE" in done.stdout, done.stdout
    assert "t-edge-cert-admin  PROVISIONING" in done.stdout, done.stdout


def test_a_converged_edge_creates_and_deletes_nothing(run):
    """Everything already in place, per hostname: the second run must be a read."""
    first, first_calls = run()
    assert first.returncode == 0, first.stderr
    second, all_calls = run()
    assert second.returncode == 0, second.stderr
    # Only the SECOND run's calls; everything before them is the run that built the state.
    converged = all_calls[len(first_calls):]
    assert "create" not in converged, converged
    assert "delete" not in converged, converged
    assert "target-https-proxies update" not in converged, converged


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
