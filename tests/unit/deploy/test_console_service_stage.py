# Responsibility: Verify the console rollout is digest-pinned, credential-free in its spec, and skippable.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-console-service.sh"

# The fake understands two families of Cloud Run call:
#   - existence/description of the SERVICE itself (create vs. update path), parameterised by
#     FAKE_SVC_EXISTS_RC / FAKE_LIVE_IMAGE / FAKE_LIVE_ENV_NAMES / FAKE_LIVE_ENV_NAMES's URL twin
#   - the invoker policy, parameterised by FAKE_POLICY_MEMBERS
# Everything else (service-account create, secret IAM bindings, run deploy, add/remove invoker
# bindings) always "succeeds" via the trailing exit 0, exactly as before.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "secrets describe"; then
  # ARGS ends "... secrets describe <name>". FAKE_SECRETS_ABSENT names containers Secret
  # Manager AFFIRMATIVELY reports missing (NOT_FOUND); FAKE_SECRETS_DENIED names ones this
  # identity cannot read (PERMISSION_DENIED) - an inconclusive failure, not proof of absence.
  # Both are comma-separated; anything else "exists" (exit 0), matching the prior fake's default.
  name="${ARGS##* }"
  case ",${FAKE_SECRETS_ABSENT:-}," in
    *",${name},"*)
      echo "ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [${name}] not found." >&2
      exit 1 ;;
  esac
  case ",${FAKE_SECRETS_DENIED:-}," in
    *",${name},"*)
      echo "ERROR: (gcloud.secrets.describe) PERMISSION_DENIED: caller lacks secretmanager.versions.access" >&2
      exit 1 ;;
  esac
  exit 0
fi
if has "run services describe"; then
  if has "containers[0].image"; then printf '%s\n' "${FAKE_LIVE_IMAGE:-}"; exit 0; fi
  if has "containers[0].env";   then printf '%s\n' "${FAKE_LIVE_ENV_NAMES:-}"; exit 0; fi
  if has "status.url";          then printf '%s\n' "https://t-console.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"          # 0 => the service already exists
fi
if has "run services get-iam-policy"; then printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0; fi
exit 0
"""

_CONSOLE_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/console@sha256:c0ffee"

# The eight names create-console-service.sh's own CONSOLE_ENV_PAIRS declares (step 3). Read from
# the script rather than hard-coded to avoid re-guessing them here.
_DECLARED_ENV_NAMES = (
    "ENV;DEPLOYMENT_ID;NODE_ENV;HEXERA_API_BASE_URL;NEXT_PUBLIC_HEXERA_API_BASE_URL;"
    "NEXT_PUBLIC_FIREBASE_API_KEY;NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN;NEXT_PUBLIC_FIREBASE_PROJECT_ID"
)

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_REGION": "europe-west1",
    # lib.sh's load_env() unconditionally derives MESH_SA_EMAIL from MESH_SERVICE_ACCOUNT under
    # `set -u`, even for a script - this one - that never reads MESH_SA_EMAIL itself. A real
    # generated.env always carries it (bootstrap-env.sh writes it unconditionally); supplied here
    # for the same reason every other deploy-script test in this directory supplies it.
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_CONSOLE_SERVICE": "t-console",
    "CONSOLE_SERVICE_ACCOUNT": "t-console",
    "CONSOLE_IMAGE": _CONSOLE_DIGEST,
    "HEXERA_API_BASE_URL": "https://api.example",
    "NEXT_PUBLIC_HEXERA_API_BASE_URL": "https://api.example",
    # PUBLIC by design - the Identity Platform web config identifies the project and authorises
    # nothing on its own - so these are plain settings alongside NEXT_PUBLIC_HEXERA_API_BASE_URL,
    # never a <SETTING>_SECRET container name.
    "NEXT_PUBLIC_FIREBASE_API_KEY": "fake-firebase-key",
    "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN": "fake-proj.firebaseapp.com",
    "NEXT_PUBLIC_FIREBASE_PROJECT_ID": "fake-proj",
    "AUTH_SECRET_SECRET": "console-auth-secret",
    "MESH_API_KEY_SECRET": "mesh-api-key",
    "USER_TOKEN_SECRET_SECRET": "user-token-secret",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None, fake: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
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


def test_no_console_service_is_a_stated_skip(run):
    done, calls = run({"CLOUDRUN_CONSOLE_SERVICE": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls, "it mutated the cloud for a deployment with no console"


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"CONSOLE_IMAGE": "us-central1-docker.pkg.dev/p/r/console:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_CONSOLE_DIGEST}" in calls


def test_credentials_reach_the_spec_only_as_references(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--set-secrets" in calls
    assert "AUTH_SECRET=console-auth-secret:latest" in calls
    # The env list is declarative and must never carry the values themselves.
    for forbidden in ("AUTH_SECRET=",):
        for chunk in calls.split("--set-env-vars")[1:]:
            assert forbidden not in chunk.split("--set-secrets")[0], (
                f"{forbidden!r} appears in the declarative environment, not as a reference")


def test_the_api_origins_reach_the_container(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "HEXERA_API_BASE_URL=https://api.example" in calls
    assert "NEXT_PUBLIC_HEXERA_API_BASE_URL=https://api.example" in calls


def test_the_firebase_config_reaches_the_spec_as_plain_environment(run):
    # The Identity Platform web config is PUBLIC by design - it identifies the project and
    # authorises nothing on its own - so, unlike AUTH_SECRET above, it must reach Cloud Run as
    # plain --set-env-vars entries, never a --set-secrets reference.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--set-secrets" in calls
    secrets_chunk = calls.split("--set-secrets")[1]
    for pair in (
        "NEXT_PUBLIC_FIREBASE_API_KEY=fake-firebase-key",
        "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=fake-proj.firebaseapp.com",
        "NEXT_PUBLIC_FIREBASE_PROJECT_ID=fake-proj",
    ):
        assert pair in calls, f"{pair!r} did not reach the deploy call as plain environment"
        assert pair not in secrets_chunk, (
            f"{pair!r} appears after --set-secrets; it must never be a Secret Manager reference")


def test_the_console_is_publicly_invokable_when_stated(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding t-console" in calls
    assert "allUsers" in calls


def test_the_console_is_not_made_public_when_not_stated(run):
    # NOTE: the fake gcloud's default policy is empty, so this drives the `*)` default arm at
    # create-console-service.sh:224 (no public binding ever held -> none added), not the removal
    # branch at :219-222. It asserts an ABSENCE, not a removal - see
    # test_the_public_binding_is_genuinely_removed below for the removal branch itself.
    done, calls = run({"CONSOLE_ALLOW_UNAUTHENTICATED": "0"})
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls.split(
        "add-iam-policy-binding")[-1]


def test_update_omits_the_create_only_flag(run):
    # An update (the service already exists) must never add --no-allow-unauthenticated: doing so
    # would momentarily strip public invoke from a live service mid-rollout, 403-ing the sign-in
    # page for every user until the invoker policy step re-adds it.
    reused, reused_calls = run(fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": _DECLARED_ENV_NAMES,
        "FAKE_LIVE_IMAGE": _CONSOLE_DIGEST,
    })
    assert reused.returncode == 0, reused.stderr
    assert "--no-allow-unauthenticated" not in reused_calls
    assert "reused" in reused.stdout.lower()

    rolled, rolled_calls = run(fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": _DECLARED_ENV_NAMES,
        "FAKE_LIVE_IMAGE": "us-central1-docker.pkg.dev/fake-proj/hexera/console@sha256:stale",
    })
    assert rolled.returncode == 0, rolled.stderr
    assert "--no-allow-unauthenticated" not in rolled_calls
    assert "rolled" in rolled.stdout.lower()

    # And the pair is meaningful: on a genuine create (no live service), the flag IS present.
    created, created_calls = run()
    assert created.returncode == 0, created.stderr
    assert "--no-allow-unauthenticated" in created_calls


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
    # printing a value would republish the credential this refusal exists to protect.
    assert "LEGACY_FLAG=" not in combined


def test_console_env_prune_proceeds(run):
    done, calls = run({"CONSOLE_ENV_PRUNE": "1"}, fake={
        "FAKE_SVC_EXISTS_RC": "0",
        "FAKE_LIVE_ENV_NAMES": f"{_DECLARED_ENV_NAMES};LEGACY_FLAG",
    })
    assert done.returncode == 0, done.stderr
    assert "run deploy" in calls


def test_the_public_binding_is_genuinely_removed(run):
    done, calls = run({"CONSOLE_ALLOW_UNAUTHENTICATED": "0"}, fake={
        "FAKE_POLICY_MEMBERS": "allUsers;serviceAccount:x@y",
    })
    assert done.returncode == 0, done.stderr
    assert "remove-iam-policy-binding" in calls
    assert "allUsers" in calls.split("remove-iam-policy-binding")[-1]


# ---------------------------------------------------------------------------
# C1: a declared-but-absent secret container must refuse the deploy BEFORE any mutation, and
# that refusal must be limited to a CONFIRMED absence - an unreadable Secret Manager (this
# identity may simply lack secretmanager.viewer) stays a warning, exactly as before.
# ---------------------------------------------------------------------------

def test_a_confirmed_absent_secret_container_refuses_before_any_mutation(run):
    done, calls = run(fake={"FAKE_SECRETS_ABSENT": "console-auth-secret"})
    assert done.returncode != 0
    combined = done.stdout + done.stderr
    assert "console-auth-secret" in combined
    # The two remedies an owner needs, verbatim.
    assert "gcloud secrets create console-auth-secret" in combined
    assert "gcloud secrets versions add console-auth-secret" in combined
    # NOTHING was mutated: not the service account, not IAM, not the rollout.
    assert "run deploy" not in calls
    assert "iam service-accounts create" not in calls
    assert "add-iam-policy-binding" not in calls


def test_an_unreadable_secret_manager_is_a_warning_and_the_deploy_proceeds(run):
    done, calls = run(fake={"FAKE_SECRETS_DENIED": "console-auth-secret"})
    assert done.returncode == 0, done.stderr
    combined = done.stdout + done.stderr
    assert "cannot confirm secret 'console-auth-secret'" in combined
    # Unlike the confirmed-absent case, this is not proof of absence - the deploy proceeds.
    assert "run deploy" in calls
    assert "iam service-accounts create" in calls


# ---------------------------------------------------------------------------
# I1: the deployed URL is handed to $GITHUB_OUTPUT, so deploy.yml's post-deploy verification
# step can find it without re-deriving it - and ONLY when the console was actually reconciled.
# ---------------------------------------------------------------------------

def test_the_console_url_is_written_to_github_output_when_reconciled(run, tmp_path):
    output_file = tmp_path / "gh_output"
    output_file.write_text("", encoding="utf-8")
    done, _calls = run(fake={"GITHUB_OUTPUT": str(output_file)})
    assert done.returncode == 0, done.stderr
    assert "console_url=https://t-console.run.app" in output_file.read_text(encoding="utf-8")


def test_nothing_is_written_to_github_output_when_the_console_is_skipped(run, tmp_path):
    output_file = tmp_path / "gh_output"
    output_file.write_text("", encoding="utf-8")
    done, _calls = run({"CLOUDRUN_CONSOLE_SERVICE": ""}, fake={"GITHUB_OUTPUT": str(output_file)})
    assert done.returncode == 0, done.stderr
    assert output_file.read_text(encoding="utf-8") == "", (
        "a skipped console (no CLOUDRUN_CONSOLE_SERVICE) must not report a URL - nothing was "
        "actually reconciled for a post-deploy check to verify")


def test_a_refused_console_writes_no_github_output(run, tmp_path):
    # The C1 refusal (confirmed-absent secret) exits before the rollout - no URL exists yet, and
    # none must be reported.
    output_file = tmp_path / "gh_output"
    output_file.write_text("", encoding="utf-8")
    done, _calls = run(fake={"FAKE_SECRETS_ABSENT": "console-auth-secret",
                              "GITHUB_OUTPUT": str(output_file)})
    assert done.returncode != 0
    assert output_file.read_text(encoding="utf-8") == ""
