# Responsibility: Verify the admin rollout is IAP-gated, never public, and digest-pinned.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"

# The fake understands two families of Cloud Run call:
#   - existence/description of the SERVICE itself (create vs. update path), parameterised by
#     FAKE_SVC_EXISTS_RC / FAKE_LIVE_IMAGE / FAKE_LIVE_ENV_NAMES
#   - the invoker policy, parameterised by FAKE_POLICY_MEMBERS / FAKE_POLICY_RC
# Everything else (service-account create, run deploy, the IAP grant, the invoker removal) always
# "succeeds" via the trailing exit 0, exactly as before.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "run services describe"; then
  if has "containers[0].image"; then printf '%s\n' "${FAKE_LIVE_IMAGE:-}"; exit 0; fi
  if has "containers[0].env";   then printf '%s\n' "${FAKE_LIVE_ENV_NAMES:-}"; exit 0; fi
  if has "status.url";          then printf '%s\n' "https://t-admin.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"          # 0 => the service already exists
fi
if has "run services get-iam-policy"; then
  fake_rc="${FAKE_POLICY_RC:-0}"
  if [ "${fake_rc}" != "0" ]; then exit "${fake_rc}"; fi
  printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0
fi
exit 0
"""

_ADMIN_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee"

# A subset of the names create-admin-service.sh's own ADMIN_ENV_PAIRS declares (step 2). The drift
# check refuses names the deployment does NOT declare, so a subset is a valid "no drift" fixture and
# stays valid as the declared set grows. test_admin_fleet_access.py asserts the full list.
_DECLARED_ENV_NAMES = "ENV;DEPLOYMENT_ID;NODE_ENV"

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "ADMIN_IMAGE": _ADMIN_DIGEST,
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None, fake: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
        # `over` is deployment CONFIG - it goes into generated.env, exactly like a real deploy.
        # `fake` is gcloud-RESPONSE control (FAKE_*) - it goes straight into the subprocess
        # environment, never through generated.env. Routing FAKE_* knobs through generated.env
        # (which load_env sources with `set -a`, under `set -e`) is the confusion this split
        # exists to prevent: a value containing a shell metacharacter (";", the same join
        # character gcloud itself uses for `value(bindings.members)`) would be parsed as a second
        # statement rather than carried as part of an assignment.
        env_vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_vals.items() if v != "") + "\n",
            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ,
                 "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file),
                 **(fake or {})})
        log_path = state / "calls.log"
        return done, (log_path.read_text(encoding="utf-8") if log_path.exists() else "")

    return _run


def test_no_admin_service_is_a_stated_skip(run):
    # GCP_PROJECT_NUMBER is dropped too (M-1): the skip must fire before the require_vars call
    # that needs it, so a deployment with no admin tier and no project number still exits 0.
    done, calls = run({"CLOUDRUN_ADMIN_SERVICE": "", "GCP_PROJECT_NUMBER": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"ADMIN_IMAGE": "us-central1-docker.pkg.dev/p/r/admin:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned_and_iap_enabled(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_ADMIN_DIGEST}" in calls
    assert "--iap" in calls, "IAP was not enabled on the service"


def test_the_service_is_never_publicly_invokable(run):
    """The single most important assertion in this file."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--no-allow-unauthenticated" in calls
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls, (
        "the admin service was granted a public invoker binding; IAP in front of that protects "
        "nothing, because the two are separate gates")


def test_an_existing_public_binding_is_removed(run):
    done, calls = run(fake={"FAKE_POLICY_MEMBERS": "allUsers;serviceAccount:x@y"})
    assert done.returncode == 0, done.stderr
    assert "remove-iam-policy-binding" in calls and "allUsers" in calls


def test_the_iap_service_agent_is_granted_invoker(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "service-224734058693@gcp-sa-iap.iam.gserviceaccount.com" in calls, (
        "IAP terminates the request and calls Cloud Run itself; without this grant every request "
        "403s in a way that looks like the IAP policy is wrong when it is not")
    assert "roles/run.invoker" in calls


def test_the_url_is_published_for_the_post_deploy_check(run, tmp_path):
    output_file = tmp_path / "gh_output"
    output_file.write_text("", encoding="utf-8")
    done, _calls = run(fake={"GITHUB_OUTPUT": str(output_file)})
    assert done.returncode == 0, done.stderr
    assert "admin_url=https://t-admin.run.app" in output_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# I-2: the update path. The single most likely regression in this file is reintroducing the
# console's create-only conditional on --no-allow-unauthenticated - this script is a copy of the
# console's - which would leave every test above green while making every UPDATE leave whatever
# binding the live service already carried. That is the security boundary this script exists to
# hold, so it must be reachable by the fake, not just the create path.
# ---------------------------------------------------------------------------

def test_no_allow_unauthenticated_and_iap_hold_on_update(run):
    reused, reused_calls = run(fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": _DECLARED_ENV_NAMES,
        "FAKE_LIVE_IMAGE": _ADMIN_DIGEST,
    })
    assert reused.returncode == 0, reused.stderr
    assert "--no-allow-unauthenticated" in reused_calls
    assert "--iap" in reused_calls
    assert "reused" in reused.stdout.lower()

    rolled, rolled_calls = run(fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": _DECLARED_ENV_NAMES,
        "FAKE_LIVE_IMAGE": "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:stale",
    })
    assert rolled.returncode == 0, rolled.stderr
    assert "--no-allow-unauthenticated" in rolled_calls
    assert "--iap" in rolled_calls
    assert "rolled" in rolled.stdout.lower()


def test_drift_is_refused_naming_only_names(run):
    done, calls = run(fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": f"{_DECLARED_ENV_NAMES};LEGACY_FLAG",
    })
    combined = done.stdout + done.stderr
    assert done.returncode != 0
    assert "LEGACY_FLAG" in combined
    assert "run deploy" not in calls
    # The security-relevant half: only the NAME may appear, never a "NAME=value" pairing -
    # printing a value would republish whatever the refusal is protecting.
    assert "LEGACY_FLAG=" not in combined


def test_admin_env_prune_proceeds(run):
    done, calls = run({"ADMIN_ENV_PRUNE": "1"}, fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": f"{_DECLARED_ENV_NAMES};LEGACY_FLAG",
    })
    assert done.returncode == 0, done.stderr
    assert "run deploy" in calls


def test_require_vars_failure_refuses_before_any_mutation(run):
    done, calls = run({"GCP_PROJECT_NUMBER": ""})
    assert done.returncode != 0
    assert "GCP_PROJECT_NUMBER" in done.stderr
    assert calls == "", "it refused before mutating anything - no gcloud call was ever made"


def test_a_failed_policy_read_warns_and_does_not_claim_no_public_binding(run):
    """I-1's regression test."""
    done, calls = run(fake={"FAKE_POLICY_RC": "1"})
    assert done.returncode == 0, done.stderr
    combined = done.stdout + done.stderr
    assert "could not read" in combined.lower() or "could not confirm" in combined.lower()
    assert "no public invoker binding" not in combined.lower(), (
        "a failed policy read must not be reported as a confirmed absence of a public binding")
    assert "remove-iam-policy-binding" not in calls
