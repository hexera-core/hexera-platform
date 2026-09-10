# Console domains: prod hardening and the edge (Edge-0 + Edge-1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make prod's API refuse to start in its currently-broken auth configuration, and give the
deploy pipeline an `edge` component that provisions a load balancer, a static IP and a managed
certificate for the consoles' hostnames.

**Architecture:** A new `edge` deploy component runs after the service stages and reconciles a
global external Application Load Balancer per project: static IP → serverless NEG per service →
backend service → URL map with host rules → managed certificate → HTTPS proxy and forwarding rule,
plus a separate redirect URL map on `:80`. IAP is untouched; it already covers the load-balancer
path.

**Tech Stack:** Cloud Run, Compute Engine global load balancing, Google-managed SSL certificates,
bash + pytest for the deploy tooling, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-09-console-domains-design.md` (§4 and §5 are this plan's
scope; §6, Edge-2 / prod, is a later plan)

## Global Constraints

- **Nothing is deleted and recreated.** A recreated forwarding rule gets a new IP, which silently
  breaks DNS that was already correct and sends the certificate back to `PROVISIONING`. Every
  resource is validated and reused.
- **A `PROVISIONING` certificate exits 0.** It cannot validate until DNS points at an IP that
  cannot exist before the first run. A `FAILED` certificate is still a failure.
- **No credential value in a spec, a generated env file, or a log line.** `<SETTING>_SECRET` names
  a Secret Manager container. `devtools/quality/check_deploy_secrets.py` is the gate.
- **A tier a deployment does not have is a STATED skip** — exit 0 with a message, using the
  existing `want` / `skipped` helpers.
- **Scripts must parse and run on bash 3.2** (what macOS ships). Avoid `${var,,}`, `${var^^}`,
  `declare -A`, `mapfile`, `readarray`, `&>>`.
- **IAP is not touched.** It stays on Cloud Run. Enabling it on both the load balancer and the
  service is documented as wrong.
- Region is `us-central1`. `hexera-dev` is project number **224734058693**; `hexera-prod` is
  **688073002171**.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `deploy/gcp/scripts/create-edge.sh` | Reconcile the load balancer, IP and certificate for one project |
| `tests/unit/deploy/test_edge_stage.py` | Drive it against a fake gcloud |
| `tests/unit/deploy/test_prod_api_hardening.py` | Prove a prod config that would serve open is refused |
| `docs/deployment/console-domains.md` | The DNS runbook: what to add, and what "PROVISIONING" means |

**Modified**

| Path | Change |
|---|---|
| `deploy/gcp/generated.prod.env` | `APP_ENV=prod`, the two auth secret containers, `CORS_ORIGINS` |
| `deploy/gcp/scripts/validate-config.sh` | refuse an unhardened prod; validate the edge block |
| `deploy/gcp/scripts/bootstrap-env.sh` | declare the edge tier |
| `deploy/gcp/scripts/deploy.sh` | `edge` in `_known`; a stage after the admin console |
| `deploy/gcp/scripts/write-deployment-state.sh` | record the reserved IP and certificate state |
| `.github/workflows/deploy.yml` | dev hostnames, an `edge` dispatch option |

---

### Task 1: Prod cannot start unhardened (Edge-0)

**Files:**
- Modify: `deploy/gcp/generated.prod.env`
- Modify: `deploy/gcp/scripts/validate-config.sh`
- Test: `tests/unit/deploy/test_prod_api_hardening.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a prod deployment env whose `APP_ENV` is `prod`, and a validation refusal for any
  deployment whose `DEPLOYMENT_ID` is `prod` but whose `APP_ENV` is unset or in the dev allowlist.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify a prod deployment cannot be configured to accept self-asserted identities.
# Boundaries: read-only validation; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"
PROD_ENV = REPO / "deploy" / "gcp" / "generated.prod.env"

# The unconditionally-required config validate-config.sh checks before anything else. Copied from
# the working sibling fixture in test_console_config_validation.py.
_BASE = {
    "GCP_PROJECT_ID": "hexera-prod", "GCP_PROJECT_NUMBER": "688073002171",
    "GCP_REGION": "us-central1", "ARTIFACT_REGISTRY_REPOSITORY": "mesh",
    "CLOUDRUN_MESH_JOB": "prod-mesh", "MESH_SERVICE_ACCOUNT": "prod-mesh",
    "GCP_MESH_BUCKET": "prod-exchange-688073002171",
}


def _validate(over: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f'{k}="{v}"' for k, v in {**_BASE, **over}.items()) + "\n", encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_prod_with_no_app_env_is_refused(tmp_path):
    """create-api-service.sh defaults APP_ENV to dev, and settings/policy.py exempts dev from every
    hardening guard - so an unset APP_ENV on prod is an API that accepts any X-User-Id sent to it."""
    done = _validate({"DEPLOYMENT_ID": "prod"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_prod_with_a_dev_app_env_is_refused(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "dev"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_a_hardened_prod_passes(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "prod"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_dev_is_unaffected(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "dev"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_prod_env_file_is_hardened():
    text = PROD_ENV.read_text(encoding="utf-8")
    assert "\nAPP_ENV=prod" in text or text.startswith("APP_ENV=prod"), (
        "generated.prod.env does not set APP_ENV=prod, so create-api-service.sh's default of "
        "'dev' applies and every hardening guard is skipped in production")
    for name in ("MESH_API_KEY_SECRET", "USER_TOKEN_SECRET_SECRET"):
        line = [l for l in text.splitlines() if l.startswith(f"{name}=")]
        assert line and line[0] != f"{name}=", (
            f"{name} is empty in prod; settings/policy.py refuses to import for a hardened "
            f"environment missing it, so prod would not start - or worse, would start unhardened")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_prod_api_hardening.py -v`
Expected: FAIL — the refusal does not exist and the prod env is not hardened.

- [ ] **Step 3: Add the refusal**

In `validate-config.sh`, near the other unconditional checks:

```bash
# PRODUCTION CANNOT BE ALLOWED TO START UNHARDENED. create-api-service.sh defaults APP_ENV to
# `dev` when the deployment env does not state one, and settings/policy.py exempts `dev` from
# every hardening guard - so an unset APP_ENV here is not a missing label, it is an API that
# accepts whatever X-User-Id a caller sends. The code's own comment calls that IDOR.
#
# Checked on DEPLOYMENT_ID rather than on the presence of a service, because the failure is about
# which environment this is, not which tier it deploys.
case "${DEPLOYMENT_ID:-}" in
  prod|production)
    case "${APP_ENV:-}" in
      ""|dev|development|local|test|testing|ci)
        add "DEPLOYMENT_ID is '${DEPLOYMENT_ID}' but APP_ENV is '${APP_ENV:-<unset>}', which
   settings/policy.py treats as a development environment - MESH_API_KEY and USER_TOKEN_SECRET
   would not be enforced and the API would accept self-asserted identities. Set APP_ENV=prod." ;;
    esac ;;
esac
```

- [ ] **Step 4: Harden the prod env file**

In `deploy/gcp/generated.prod.env`: set `APP_ENV=prod`, point `MESH_API_KEY_SECRET=mesh-api-key`
and `USER_TOKEN_SECRET_SECRET=user-token-secret` at their containers, and set
`API_CORS_ORIGINS=https://console.hexera.ai` — a hardened environment refuses a wildcard, so
leaving the default would make the API refuse to start for a second reason.

Add a comment above them recording that these are load-bearing rather than cosmetic, and that the
containers still need real versions added by an owner (`create-secrets.sh` creates the container;
it deliberately never invents a value).

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_prod_api_hardening.py tests/unit/deploy/test_console_config_validation.py -v`
Run: `python devtools/quality/check_deploy_secrets.py`
Run: `shellcheck deploy/gcp/scripts/validate-config.sh`
Expected: tests pass, gate exits 0, only the pre-existing `SC1091`.

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/generated.prod.env deploy/gcp/scripts/validate-config.sh tests/unit/deploy/test_prod_api_hardening.py
git commit -m "deploy: refuse a production deployment that would accept self-asserted identities"
```

---

### Task 2: Declare the edge tier

**Files:**
- Modify: `deploy/gcp/scripts/bootstrap-env.sh`
- Modify: `deploy/gcp/scripts/validate-config.sh`
- Test: `tests/unit/deploy/test_edge_config_declaration.py`

**Interfaces:**
- Consumes: nothing.
- Produces, in the generated deployment env: `CONSOLE_DOMAIN`, `ADMIN_DOMAIN`, `EDGE_IP_NAME`,
  `EDGE_URL_MAP`, `EDGE_CERT`. Task 3 reads all five.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the deployment env declares the edge tier and that an absent one is legal.
# Boundaries: it reads the generator and the validator; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

DECLARED = ("CONSOLE_DOMAIN", "ADMIN_DOMAIN", "EDGE_IP_NAME", "EDGE_URL_MAP", "EDGE_CERT")

_BASE = {
    "DEPLOYMENT_ID": "dev", "GCP_PROJECT_ID": "hexera-dev",
    "GCP_PROJECT_NUMBER": "224734058693", "GCP_REGION": "us-central1",
    "ARTIFACT_REGISTRY_REPOSITORY": "mesh", "CLOUDRUN_MESH_JOB": "dev-mesh",
    "MESH_SERVICE_ACCOUNT": "dev-mesh", "GCP_MESH_BUCKET": "dev-exchange-224734058693",
}


def test_every_edge_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [n for n in DECLARED if f"{n}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def _validate(over: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f'{k}="{v}"' for k, v in {**_BASE, **over}.items()) + "\n", encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_a_domain_without_its_service_is_refused(tmp_path):
    """A hostname with nothing behind it produces a load balancer that 404s on a real name."""
    done = _validate({"CONSOLE_DOMAIN": "dev.console.hexera.ai"}, tmp_path)
    assert done.returncode != 0
    assert "CLOUDRUN_CONSOLE_SERVICE" in done.stdout + done.stderr


def test_a_domain_with_its_service_passes(tmp_path):
    done = _validate({"CONSOLE_DOMAIN": "dev.console.hexera.ai",
                      "CLOUDRUN_CONSOLE_SERVICE": "dev-console"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_no_domains_is_not_an_error(tmp_path):
    done = _validate({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_edge_config_declaration.py -v`
Expected: FAIL on the declaration test and the refusal test.

- [ ] **Step 3: Declare the tier**

In `bootstrap-env.sh`'s generated-env heredoc, after the admin block:

```bash
# THE EDGE. Empty domains mean this deployment serves no custom hostname and that stage is
# skipped - the same arrangement every other optional tier uses.
#
# Cloud Run hands out no IP, so a hostname is a load balancer: a reserved global address, a
# serverless NEG per service, a URL map that routes by host, and a Google-managed certificate.
# The names below are the resources create-edge.sh reconciles; they are stated rather than derived
# so an operator can find them in the console without reading a script.
CONSOLE_DOMAIN=${CONSOLE_DOMAIN:-}
ADMIN_DOMAIN=${ADMIN_DOMAIN:-}
EDGE_IP_NAME=${EDGE_IP_NAME:-${DEPLOY_ID}-edge-ip}
EDGE_URL_MAP=${EDGE_URL_MAP:-${DEPLOY_ID}-edge}
EDGE_CERT=${EDGE_CERT:-${DEPLOY_ID}-edge-cert}
```

- [ ] **Step 4: Validate it**

In `validate-config.sh`, after the admin block:

```bash
# A HOSTNAME WITH NOTHING BEHIND IT is a load balancer that answers a real name with a 404, and it
# is worse than no hostname because the name looks like it works. Each declared domain requires
# the service it fronts.
if [ -n "${CONSOLE_DOMAIN:-}" ] && [ -z "${CLOUDRUN_CONSOLE_SERVICE:-}" ]; then
  add "CONSOLE_DOMAIN is '${CONSOLE_DOMAIN}' but CLOUDRUN_CONSOLE_SERVICE is unset - there is no
   console for that hostname to reach"
fi
if [ -n "${ADMIN_DOMAIN:-}" ] && [ -z "${CLOUDRUN_ADMIN_SERVICE:-}" ]; then
  add "ADMIN_DOMAIN is '${ADMIN_DOMAIN}' but CLOUDRUN_ADMIN_SERVICE is unset - there is no admin
   console for that hostname to reach"
fi
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_edge_config_declaration.py tests/unit/deploy/test_discovery.py -v`
Run: `shellcheck deploy/gcp/scripts/bootstrap-env.sh deploy/gcp/scripts/validate-config.sh`
Run: `python devtools/quality/check_deploy_secrets.py`

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/ tests/unit/deploy/test_edge_config_declaration.py
git commit -m "deploy: declare the edge tier and refuse a hostname with no service behind it"
```

---

### Task 3: The edge

The largest task. Read `deploy/gcp/scripts/create-admin-service.sh` first — it is the shape to
follow for the skip, the refusals, the logging and the `$GITHUB_OUTPUT` publication.

**Files:**
- Create: `deploy/gcp/scripts/create-edge.sh`
- Test: `tests/unit/deploy/test_edge_stage.py`

**Interfaces:**
- Consumes: `CONSOLE_DOMAIN`, `ADMIN_DOMAIN`, `EDGE_IP_NAME`, `EDGE_URL_MAP`, `EDGE_CERT`,
  `CLOUDRUN_CONSOLE_SERVICE`, `CLOUDRUN_ADMIN_SERVICE`, `GCP_REGION` (Task 2).
- Produces: `EDGE_IP_ADDRESS` exported into the deployment env file, and `edge_ip=<address>` plus
  `edge_cert_state=<state>` appended to `$GITHUB_OUTPUT` when that variable is set. Task 4 records
  the address in the manifest.

**The gcloud commands, verified from Google's documentation.** Use these exact flags:

```bash
# global static IP
gcloud compute addresses create NAME --global --network-tier=PREMIUM --ip-version=IPV4
gcloud compute addresses describe NAME --global --format='value(address)'

# one serverless NEG per Cloud Run service
gcloud compute network-endpoint-groups create NEG \
  --region=us-central1 --network-endpoint-type=serverless --cloud-run-service=SERVICE

# one backend service per NEG
gcloud compute backend-services create BE --global --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend BE --global \
  --network-endpoint-group=NEG --network-endpoint-group-region=us-central1

# the URL map, then a host rule per hostname
gcloud compute url-maps create MAP --default-service BE
gcloud compute url-maps add-path-matcher MAP \
  --path-matcher-name=PM --default-service=BE --new-hosts=HOSTNAME

# the certificate, covering every declared hostname
gcloud compute ssl-certificates create CERT --domains=HOST1,HOST2 --global
gcloud compute ssl-certificates describe CERT --global --format='value(managed.status)'

# HTTPS
gcloud compute target-https-proxies create PROXY --ssl-certificates=CERT --url-map=MAP
gcloud compute forwarding-rules create RULE --global --load-balancing-scheme=EXTERNAL_MANAGED \
  --network-tier=PREMIUM --address=NAME --target-https-proxy=PROXY --ports=443
```

**The HTTP redirect needs its own URL map — this is the one place the documentation misleads.**
Pointing the HTTP proxy at the same URL map *serves the app over HTTP* rather than redirecting.
A redirect is a URL map with a `defaultUrlRedirect` action, which `gcloud` can only create by
import:

```bash
cat > /tmp/redirect.yaml <<'YAML'
name: REDIRECT_MAP
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
YAML
gcloud compute url-maps import REDIRECT_MAP --global --source=/tmp/redirect.yaml --quiet
gcloud compute target-http-proxies create HTTP_PROXY --url-map=REDIRECT_MAP
gcloud compute forwarding-rules create HTTP_RULE --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --network-tier=PREMIUM \
  --address=NAME --target-http-proxy=HTTP_PROXY --ports=80
```

- [ ] **Step 1: Write the failing test**

```python
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
  [ -n "${FAKE_IP_EXISTS:-}" ] || exit 1
  printf '%s\n' "${FAKE_IP:-203.0.113.10}"; exit 0
fi
if has "ssl-certificates describe"; then
  [ -n "${FAKE_CERT_EXISTS:-}" ] || exit 1
  printf '%s\n' "${FAKE_CERT_STATE:-PROVISIONING}"; exit 0
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_edge_stage.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the script**

Create `deploy/gcp/scripts/create-edge.sh`. Follow `create-admin-service.sh` for its shape: the
header comment explaining *why*, `load_env`, `require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID`,
the stated skip and `exit 0` when both domains are empty, and the `$GITHUB_OUTPUT` publication at
the end.

Structure it as one `_ensure` helper per resource kind — describe, and create only when the
describe fails — so "reuse, never recreate" is one rule expressed once rather than eight times.
Build the hostname/service/backend triples in a plain indexed array (bash 3.2 has no associative
arrays), skipping any whose domain is empty.

The certificate's `--domains` is the comma-joined list of declared hostnames, in the order
console-then-admin, so the test's expectation and the script agree.

Read the certificate's managed status after ensuring it and branch on it:

```bash
case "${CERT_STATE}" in
  ACTIVE)        log "  certificate    ACTIVE" ;;
  PROVISIONING|PROVISIONING_FAILED_TEMPORARILY)
    # NOT A FAILURE. A managed certificate cannot validate until DNS points at the address this
    # run may have just reserved, so the first run always lands here. Failing would make the
    # pipeline unusable on the day it is introduced.
    warn "certificate ${EDGE_CERT} is ${CERT_STATE} - it stays that way until the A records below
       exist and propagate, then becomes ACTIVE on its own within roughly 15-60 minutes." ;;
  *)             die "certificate ${EDGE_CERT} is ${CERT_STATE} - this does not resolve itself.
   Check the domains on the certificate against the A records that exist." ;;
esac
```

End with the operator instruction, which is the whole point of the run:

```bash
info "DNS - add these A records, then nothing else is required:"
for pair in ${DOMAIN_LIST[@]+"${DOMAIN_LIST[@]}"}; do
  log "  ${pair}  A  ${EDGE_IP_ADDRESS}"
done
```

- [ ] **Step 4: Make it executable and verify**

```bash
chmod +x deploy/gcp/scripts/create-edge.sh
python -m pytest tests/unit/deploy/test_edge_stage.py -v
shellcheck deploy/gcp/scripts/create-edge.sh
/bin/bash -n deploy/gcp/scripts/create-edge.sh
python devtools/quality/check_deploy_secrets.py
```

Expected: 11 tests pass; shellcheck shows only the pre-existing `SC1091`; `bash -n` silent under
bash 3.2; the secret gate exits 0.

Report which branches the fake gcloud does **not** exercise, so review knows what is untested.

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/create-edge.sh tests/unit/deploy/test_edge_stage.py
git commit -m "deploy: reconcile the load balancer, address and certificate for the consoles"
```

---

### Task 4: Select the edge stage and record its address

**Files:**
- Modify: `deploy/gcp/scripts/deploy.sh`
- Modify: `deploy/gcp/scripts/write-deployment-state.sh`
- Test: `tests/unit/deploy/test_edge_component_selection.py`

**Interfaces:**
- Consumes: `create-edge.sh` (Task 3).
- Produces: `DEPLOY_COMPONENTS=edge` selects it; `deployment.json` carries
  `resources.edge` with the reserved address and certificate state.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify 'edge' is selectable, ordered last, and its address is recorded.
# Boundaries: it reads the driver and the state writer; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_edge_is_a_known_component():
    known = [l for l in DEPLOY.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("_known=")]
    assert known and "edge" in known[0]


def test_the_edge_stage_runs_the_edge_script():
    assert "create-edge.sh" in DEPLOY.read_text(encoding="utf-8")


def test_an_unselected_edge_states_its_skip():
    assert "skipped edge" in DEPLOY.read_text(encoding="utf-8")


def test_the_edge_runs_after_the_services_it_fronts():
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("create-console-service.sh") < text.index("create-edge.sh")
    assert text.index("create-admin-service.sh") < text.index("create-edge.sh")


def test_the_reserved_address_is_recorded():
    text = WRITER.read_text(encoding="utf-8")
    assert '"edge"' in text, (
        "the manifest records no edge, so the address an operator put in DNS survives only in the "
        "log of the run that printed it")
    assert "EDGE_IP_ADDRESS" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_edge_component_selection.py -v`
Expected: FAIL on all five.

- [ ] **Step 3: Add the component and the stage**

In `deploy.sh`, extend `_known` with `edge`, and add a stage **after** the admin console stage:

```bash
stage "Edge (the address, the load balancer and the managed certificate)"
# LAST, and after both console stages: a serverless NEG cannot be created for a Cloud Run service
# that does not exist yet, and the certificate names hostnames that route to them.
if want edge; then
  bash "${S}/create-edge.sh"
else
  skipped edge "the load balancer keeps its current address, routes and certificate"
fi
```

`STAGE_TOTAL` is computed by `grep -c '^stage "'`, so it needs no edit — confirm that rather than
assume it.

- [ ] **Step 4: Record the address**

In `write-deployment-state.sh`, add an `edge` resource beside the others, carrying
`${EDGE_IP_ADDRESS:-}`, the declared hostnames and the certificate state. Match the surrounding
Python-heredoc-inside-bash quoting exactly; `${...}` is shell-expanded before Python sees it.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_edge_component_selection.py tests/unit/deploy/test_fresh_install_contract.py -v`
Run: `shellcheck deploy/gcp/scripts/deploy.sh deploy/gcp/scripts/write-deployment-state.sh`

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/ tests/unit/deploy/test_edge_component_selection.py
git commit -m "deploy: make edge a selectable component and record its address"
```

---

### Task 5: Wire the workflow, and document the DNS step

**Files:**
- Modify: `.github/workflows/deploy.yml`
- Create: `docs/deployment/console-domains.md`
- Test: `tests/unit/deploy/test_edge_deploy_wiring.py`

**Interfaces:**
- Consumes: the `edge` component (Task 4) and `edge_ip` (Task 3).
- Produces: dev deploys the edge; the run's summary names the IP and the certificate state.

- [ ] **Step 1: Write the failing test**

```python
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
    assert "console.hexera.ai" in text
    assert 'echo "console_domain="' in text or 'console_domain=' in text


def test_the_deploy_job_maps_both_domains():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "CONSOLE_DOMAIN" in body
    assert "ADMIN_DOMAIN" in body


def test_a_manual_run_can_select_the_edge():
    options = _doc()[True]["workflow_dispatch"]["inputs"]["components"]["options"]
    assert [o for o in options if "edge" in o], (
        f"no components option contains 'edge', so a manual run cannot provision it. Offered: {options}")


def test_the_run_surfaces_the_address():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "edge_ip" in body, (
        "the address an operator must put in DNS is not surfaced by the run that reserved it")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_edge_deploy_wiring.py -v`
Expected: FAIL on all five.

- [ ] **Step 3: Wire it**

Add `console_domain` and `admin_domain` outputs to the `target` job. Dev emits
`dev.console.hexera.ai` and `dev.admin.hexera.ai`; **prod emits both empty**, with a comment saying
Edge-2 turns them on in its own reviewed diff and that doing it here would provision billed
resources on the next tag. Map both into the `deploy` job's env as `CONSOLE_DOMAIN` and
`ADMIN_DOMAIN`.

Add `edge` and `images,migrate,console,admin,edge` to the `workflow_dispatch` options.

Add a step after the deploy that writes the address and certificate state into the run summary, so
the value is not buried in a 17-stage log:

```yaml
      - name: Surface the edge address for DNS
        if: steps.deploy.outputs.edge_ip != ''
        env:
          EDGE_IP: ${{ steps.deploy.outputs.edge_ip }}
          EDGE_CERT_STATE: ${{ steps.deploy.outputs.edge_cert_state }}
          CONSOLE_DOMAIN: ${{ needs.target.outputs.console_domain }}
          ADMIN_DOMAIN: ${{ needs.target.outputs.admin_domain }}
        run: |
          set -euo pipefail
          {
            echo "## Edge"
            echo
            echo "| Hostname | Record | Value |"
            echo "|---|---|---|"
            [ -n "${CONSOLE_DOMAIN}" ] && echo "| \`${CONSOLE_DOMAIN}\` | A | \`${EDGE_IP}\` |"
            [ -n "${ADMIN_DOMAIN}" ] && echo "| \`${ADMIN_DOMAIN}\` | A | \`${EDGE_IP}\` |"
            echo
            echo "Certificate: **${EDGE_CERT_STATE}**"
          } >> "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 4: Write the runbook**

Create `docs/deployment/console-domains.md`, in the register of the other deployment docs — the
observation is the claim, and it explains *why*. Cover: which hostname is which service in which
project; that Cloud Run has no IP so a hostname is a load balancer; that a first run reserves an
address and leaves the certificate `PROVISIONING` **by design**, with what the operator owes it;
that `PROVISIONING` becomes `ACTIVE` on its own in roughly 15–60 minutes once the A records exist;
that a recreated forwarding rule would change the address and break DNS, which is why the script
reuses; and that IAP stays on Cloud Run rather than the load balancer, so `admin` is protected on
both paths. Link it from `docs/README.md`'s deployment table.

- [ ] **Step 5: Verify**

Run: `python -m pytest tests/unit/deploy/test_edge_deploy_wiring.py tests/unit/deploy/test_admin_deploy_wiring.py -v`
Run: extract the new step's `run:` with PyYAML and `bash -n` it.
Run: `python devtools/quality/check_deploy_secrets.py`

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/deploy.yml docs/ tests/unit/deploy/test_edge_deploy_wiring.py
git commit -m "deploy: provision dev's edge and surface the address DNS needs"
```

---

## Final verification

- [ ] `python -m pytest tests/unit/deploy -q` — 7 pre-existing failures only (docker-compose
      flags, missing pyenv 3.12, the bash-3.2 parse error in `deploy-preflight.sh`)
- [ ] `shellcheck` and `/bin/bash -n` on every changed shell script
- [ ] `python devtools/quality/check_deploy_secrets.py`
- [ ] Deploy to dev with `components=edge`, read the address from the run summary
- [ ] Add the two dev A records, wait, re-run `components=edge`, confirm the certificate reports
      `ACTIVE`
- [ ] `curl -sI https://dev.console.hexera.ai/` redirects to `/sign-in`;
      `curl -sI https://dev.admin.hexera.ai/` does **not** return 200 to an anonymous request

## What this plan deliberately does not do

- Prod. `console_domain` and `admin_domain` are empty for prod, and `prod-console` / `prod-admin`
  stay unpinned. Edge-2 is a separate plan and a separate reviewed diff.
- A hostname for the product API.
- Cloud CDN, Cloud Armor, or path-based routing.
- Moving IAP to the load balancer.
