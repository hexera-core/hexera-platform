# Responsibility: Verify a half-declared console is refused before anything is created.
# Boundaries: read-only validation; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

_BASE = {
    # validate-config.sh's own required-config loop, plus MESH_SERVICE_ACCOUNT which lib.sh's
    # load_env derefs unconditionally - every sibling test in this directory (e.g.
    # test_fresh_install_contract.py's INDEP fixture) supplies the same set for the same reason.
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj", "GCP_PROJECT_NUMBER": "123456",
    "GCP_REGION": "europe-west1", "ARTIFACT_REGISTRY_REPOSITORY": "mesh",
    "CLOUDRUN_MESH_JOB": "t-mesh", "MESH_SERVICE_ACCOUNT": "t-mesh",
    "GCP_MESH_BUCKET": "t-exchange-123456",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created",
}


def _run(env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in {**_BASE, **env_extra}.items()) + "\n",
        encoding="utf-8")
    # tests/unit/conftest.py sets MINIO_ACCESS_KEY/MINIO_BUCKET/MINIO_ENDPOINT process-wide (the
    # "ordinary state of a working shell" fixture other unit tests rely on). Inherited here via
    # os.environ, they would make validate-config.sh believe an object store was configured and
    # refuse it for a MINIO_SECRET_KEY_SECRET this test has nothing to do with; strip them so this
    # subprocess sees only what the generated env actually declares.
    env = {**os.environ, "DEPLOY_ENV_FILE": str(env_file)}
    for _leaked in ("MINIO_ACCESS_KEY", "MINIO_BUCKET", "MINIO_ENDPOINT", "MINIO_SECRET_KEY"):
        env.pop(_leaked, None)
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True, env=env)


def test_a_console_without_an_api_base_url_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console"}, tmp_path)
    assert done.returncode != 0
    assert "HEXERA_API_BASE_URL" in done.stdout + done.stderr


def test_a_console_without_an_auth_secret_container_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "HEXERA_API_BASE_URL": "https://api.example",
                 "AUTH_SECRET_SECRET": ""}, tmp_path)
    assert done.returncode != 0
    assert "AUTH_SECRET_SECRET" in done.stdout + done.stderr


def test_inverted_console_scaling_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "HEXERA_API_BASE_URL": "https://api.example",
                 "AUTH_SECRET_SECRET": "console-auth-secret",
                 "CONSOLE_MIN_INSTANCES": "4", "CONSOLE_MAX_INSTANCES": "2"}, tmp_path)
    assert done.returncode != 0
    assert "CONSOLE_MIN_INSTANCES" in done.stdout + done.stderr


def test_no_console_is_not_an_error(tmp_path):
    done = _run({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_console_without_an_api_base_url_validates_clean_when_console_is_not_selected(tmp_path):
    # A fresh environment: the API service that would satisfy HEXERA_API_BASE_URL is not created
    # until stage 14, twelve stages after this validation runs at stage 2. A run that declares a
    # console but is not actually reconciling it this time (DEPLOY_COMPONENTS names something
    # else) must not be refused for a live resource it is not touching.
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "AUTH_SECRET_SECRET": "console-auth-secret",
                 "DEPLOY_COMPONENTS": "images"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_console_without_an_api_base_url_is_still_refused_when_console_is_selected(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "AUTH_SECRET_SECRET": "console-auth-secret",
                 "DEPLOY_COMPONENTS": "images,console"}, tmp_path)
    assert done.returncode != 0
    assert "HEXERA_API_BASE_URL" in done.stdout + done.stderr


#: A console that is otherwise completely declared. Every Firebase case below varies exactly one
#: thing against this, so a refusal can only be the value under test.
_CONSOLE_OK = {
    "CLOUDRUN_CONSOLE_SERVICE": "t-console",
    "HEXERA_API_BASE_URL": "https://api.example",
    "AUTH_SECRET_SECRET": "console-auth-secret",
    "NEXT_PUBLIC_FIREBASE_API_KEY": "AIzaFake",
    "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN": "fake-proj.firebaseapp.com",
    "NEXT_PUBLIC_FIREBASE_PROJECT_ID": "fake-proj",
}


def test_a_fully_configured_console_validates_clean(tmp_path):
    done = _run(_CONSOLE_OK, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_console_with_no_firebase_configuration_at_all_is_refused(tmp_path):
    # THE DEPLOYMENT NOBODY CAN SIGN IN TO. The old check refused a console whose credential
    # secret named nothing; task 13 deleted it with the env-var password path and nothing replaced
    # it. Empty Firebase values now validate, build, deploy - and serve "Sign-in is not configured
    # for this deployment" to every visitor, which is only discovered by a human opening the page.
    done = _run({**_CONSOLE_OK,
                 "NEXT_PUBLIC_FIREBASE_API_KEY": "",
                 "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN": "",
                 "NEXT_PUBLIC_FIREBASE_PROJECT_ID": ""}, tmp_path)
    assert done.returncode != 0
    assert "NEXT_PUBLIC_FIREBASE_API_KEY" in done.stdout + done.stderr


def test_each_firebase_value_is_required_on_its_own(tmp_path):
    # The console tests all three together, so any ONE of them missing produces the same dead
    # sign-in page. Checking only the API key would let the other two through.
    for missing in ("NEXT_PUBLIC_FIREBASE_API_KEY", "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
                    "NEXT_PUBLIC_FIREBASE_PROJECT_ID"):
        done = _run({**_CONSOLE_OK, missing: ""}, tmp_path)
        assert done.returncode != 0, f"an empty {missing} validated clean"
        assert missing in done.stdout + done.stderr, (
            f"the refusal for an empty {missing} did not name it")


def test_the_firebase_check_does_not_fire_for_a_run_that_is_not_touching_the_console(tmp_path):
    # Same rule as HEXERA_API_BASE_URL: these values are consumed only when the console service
    # itself is reconciled. A run that selects something else must not be refused for them.
    done = _run({**_CONSOLE_OK,
                 "NEXT_PUBLIC_FIREBASE_API_KEY": "",
                 "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN": "",
                 "NEXT_PUBLIC_FIREBASE_PROJECT_ID": "",
                 "DEPLOY_COMPONENTS": "images"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_no_console_is_not_refused_for_missing_firebase_configuration(tmp_path):
    # A deployment that serves no browser console has no sign-in page to break.
    done = _run({"NEXT_PUBLIC_FIREBASE_API_KEY": ""}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
