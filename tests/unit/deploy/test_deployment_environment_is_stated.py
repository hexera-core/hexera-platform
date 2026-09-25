# Responsibility: Prove a deployment that has not said which environment it is cannot deploy an API.
# Boundaries: it runs the two deploy scripts against a config file it wrote and a fake gcloud; it
#             creates nothing and calls no cloud.
#
# THE DEFECT. deploy/gcp/scripts/create-api-service.sh read `APP_ENV="${APP_ENV:-dev}"` and put the
# result in the service's spec as ENV. Nothing in the repository ever set APP_ENV - bootstrap-env.sh
# did not emit the key, so no generated env carried it - which means `:-dev` was not a fallback for
# local use, it was the value EVERY hosted deployment shipped with.
#
# WHAT ENV=dev SWITCHES OFF. settings/policy.py classifies a deployment by that one value:
# `requires_hardened_runtime()` is false for dev, development, local, test, testing and ci, and true
# for everything else, and it gates the refusals on an unset MESH_API_KEY, an unset USER_TOKEN_SECRET
# (so X-User-Id is self-asserted and any caller can act as any customer), a wildcard CORS_ORIGINS, a
# missing database credential and an in-memory checkpointer. The live prod API ran with all of them
# relaxed, behind API_ALLOW_UNAUTHENTICATED=1, because of one default nobody typed.
#
# WHY AN ABSENCE IS NEVER THE WHOLE ASSERTION HERE. "the refusal did not appear" is satisfied by a
# script that fell over earlier for an unrelated reason, which is this project's signature failure.
# Every test below that checks a configuration is ACCEPTED also checks the run reached the far end -
# `configuration valid` from the validator, and the deployed env list from the API script.
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
POLICY = REPO / "src" / "meshpipeline" / "settings" / "policy.py"

#: Everything validate-config.sh requires before it reaches the API block, and nothing else. A fleet
#: is deliberately NOT declared: the floor rule would report its own refusal and this file is about
#: one message.
_MINIMAL_ENV = {
    "DEPLOYMENT_ID": "isolated", "GCP_PROJECT_ID": "p", "GCP_PROJECT_NUMBER": "1",
    "GCP_REGION": "europe-west2", "ARTIFACT_REGISTRY_REPOSITORY": "r",
    "CLOUDRUN_MESH_JOB": "m", "MESH_SERVICE_ACCOUNT": "mesh-sa", "GCP_MESH_BUCKET": "bucket-x",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created",
    "CLOUDRUN_API_SERVICE": "isolated-api", "VPC_NETWORK": "default", "VPC_SUBNET": "default",
    "OPENAI_API_KEY_SECRET": "openai-api-key",
}

_FAKE_GCLOUD = """#!/bin/bash
printf '%s\\n' "$*" >> "${GCLOUD_LOG}"
exit 0
"""


def _write_env(tmp_path: Path, **overrides: str) -> Path:
    values = {**_MINIMAL_ENV, **overrides}
    path = tmp_path / "generated.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def _bare_env() -> dict[str, str]:
    """Only what bash needs.

    NOTHING OF THE CALLER'S ENVIRONMENT, and that is load-bearing rather than tidy. load_env refuses
    a run whose ambient GCP_PROJECT_ID disagrees with the file, and the pytest conftest loads this
    repository's .env - so an inherited environment makes these scripts fail for reasons that have
    nothing to do with APP_ENV, and every "the refusal did not appear" assertion would pass on that.
    """
    return {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
                                       "COMSPEC", "TEMP", "TMP") if k in os.environ}


def _run(script: str, env_file: Path, extra: dict[str, str] | None = None,
         path_prefix: str | None = None) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine, and these are bash scripts")
    env = {**_bare_env(), "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1", **(extra or {})}
    if path_prefix:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(["bash", str(SCRIPTS / script)], capture_output=True, text=True,
                          timeout=300, check=False, env=env)


def _fake_gcloud_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A gcloud that records what it was asked and always succeeds.

    create-api-service.sh reaches the far end only if every gcloud call returns 0, and the recorded
    argument list is what lets the POSITIVE test assert the environment that was actually deployed
    rather than merely the absence of a refusal.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gcloud",):
        path = bin_dir / name
        path.write_text(_FAKE_GCLOUD, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "gcloud.log"
    log.write_text("", encoding="utf-8")
    return bin_dir, log


# ------------------------------------------------------------------ the provisioner itself refuses

def test_the_api_service_refuses_to_deploy_an_unstated_environment(tmp_path):
    env_file = _write_env(tmp_path, APP_IMAGE="reg/app@sha256:abc")
    bin_dir, log = _fake_gcloud_dir(tmp_path)
    done = _run("create-api-service.sh", env_file, {"GCLOUD_LOG": str(log)}, str(bin_dir))
    assert done.returncode != 0, f"an unstated environment deployed anyway:\n{done.stdout}{done.stderr}"
    assert "APP_ENV is not set" in done.stdout + done.stderr, (
        f"it refused for some other reason, which is not this rule:\n{done.stdout}{done.stderr}")
    # AND IT REFUSED BEFORE IT CREATED ANYTHING. A refusal after the identity and the seven secret
    # bindings is a half-provisioned deployment, and the point of checking a value read from a file
    # is that it costs nothing to check first.
    assert log.read_text(encoding="utf-8").strip() == "", (
        f"gcloud was called before the environment was settled: {log.read_text(encoding='utf-8')}")


def test_a_stated_environment_is_what_the_service_receives(tmp_path):
    # The positive case, asserted on the ENV that reached `gcloud run deploy` rather than on the
    # absence of the refusal above - an absence is satisfied by a script that died one line earlier.
    env_file = _write_env(tmp_path, APP_IMAGE="reg/app@sha256:abc", APP_ENV="production")
    bin_dir, log = _fake_gcloud_dir(tmp_path)
    done = _run("create-api-service.sh", env_file, {"GCLOUD_LOG": str(log)}, str(bin_dir))
    assert done.returncode == 0, f"a stated environment did not deploy:\n{done.stdout}{done.stderr}"
    # `gc` prefixes every call with --project, so the rollout is matched on the subcommand.
    deploys = [line for line in log.read_text(encoding="utf-8").splitlines()
               if " run deploy " in f" {line} "]
    assert len(deploys) == 1, f"expected one rollout, got {deploys}"
    assert "ENV=production" in deploys[0], f"the service was not given ENV=production: {deploys[0]}"
    assert "ENV=dev" not in deploys[0]


# ------------------------------------------------------------------------- the validator refuses

def test_the_validator_refuses_a_configuration_with_no_environment(tmp_path):
    done = _run("validate-config.sh", _write_env(tmp_path))
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert "APP_ENV is not set" in out, out


def test_a_dev_environment_is_accepted(tmp_path):
    # The rule must name a real condition rather than refuse every deployment, and the run has to
    # have REACHED the end for that to mean anything.
    done = _run("validate-config.sh", _write_env(tmp_path, APP_ENV="dev"))
    out = done.stdout + done.stderr
    assert "APP_ENV is not set" not in out, out
    assert "configuration valid" in out, out


def test_a_hosted_environment_with_no_auth_secrets_and_wildcard_cors_is_refused(tmp_path):
    # The three things ENV=dev was silently exempting. Each has a refusal in the runtime, so without
    # this the deploy's verdict is a revision that never becomes ready, after everything is created.
    done = _run("validate-config.sh", _write_env(tmp_path, APP_ENV="production"))
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert "MESH_API_KEY_SECRET names no Secret" in out, out
    assert "USER_TOKEN_SECRET_SECRET names no" in out, out
    assert "API_CORS_ORIGINS is the wildcard" in out, out


def test_a_fully_stated_hosted_environment_is_accepted(tmp_path):
    done = _run("validate-config.sh", _write_env(
        tmp_path, APP_ENV="production", MESH_API_KEY_SECRET="mesh-api-key",
        USER_TOKEN_SECRET_SECRET="user-token-secret",
        API_CORS_ORIGINS="https://app.example.com"))
    out = done.stdout + done.stderr
    assert "configuration valid" in out, out


def test_the_case_of_the_name_is_folded_the_way_the_runtime_folds_it(tmp_path):
    # policy.py lowercases ENV before it classifies it, so `Production` is hosted there. A validator
    # that compared the raw string would pass this configuration and let the runtime refuse it.
    done = _run("validate-config.sh", _write_env(tmp_path, APP_ENV="Production"))
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert "MESH_API_KEY_SECRET names no Secret" in out, out


# --------------------------------------------------------------------- no default may come back

def test_no_deploy_script_supplies_a_default_environment():
    # The defect was a default, so the shape of the defect is what is banned. Paired with the
    # positive assertion below, because "no script says APP_ENV:-dev" is also true of a repository
    # that stopped mentioning APP_ENV at all.
    # `${APP_ENV:-}` is the empty expansion and is what these scripts are supposed to use; anything
    # between the `:-` and the closing brace is a supplied default. Comment lines are skipped because
    # both scripts quote the old expression in the comment that explains why it is gone.
    offenders = []
    for path in sorted((REPO / "deploy").rglob("*.sh")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"APP_ENV:-[^}\s]", line):
                offenders.append(f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()}")
    assert not offenders, (
        "a deploy script defaults APP_ENV. The permissive answer is the one nobody has to type, "
        f"which is how the live prod API came to run with ENV=dev: {offenders}")
    bootstrap = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    assert "APP_ENV=${APP_ENV:-}" in bootstrap, (
        "bootstrap-env.sh no longer emits the APP_ENV key at all, so an operator has nothing to fill "
        "in and every deployment hits the refusal with no instruction in front of it")


def test_the_validators_dev_roster_is_the_applications_dev_roster():
    # validate-config.sh restates settings/policy.py's _AUTH_OPTIONAL_ENVS, because a shell script
    # cannot import it. A restatement that drifts is a fact that lies, so the two are pinned here.
    policy = POLICY.read_text(encoding="utf-8")
    match = re.search(r"_AUTH_OPTIONAL_ENVS\s*=\s*frozenset\(\{([^}]*)\}\)", policy)
    assert match, "policy.py no longer declares _AUTH_OPTIONAL_ENVS as a frozenset literal"
    application = set(re.findall(r'"([a-z]+)"', match.group(1)))
    assert application, "the roster parsed out of policy.py is empty, which cannot be right"

    validator = (SCRIPTS / "validate-config.sh").read_text(encoding="utf-8")
    shell = re.search(r'case\s+"\s(dev[^"]*?)\s"\s+in', validator)
    assert shell, "validate-config.sh no longer carries the dev-name roster this test pins"
    assert set(shell.group(1).split()) == application, (
        f"validate-config.sh treats {sorted(set(shell.group(1).split()))} as the dev environments "
        f"and the application treats {sorted(application)} as them. A name in one list and not the "
        "other is a deployment refused that would have worked, or accepted that will not.")
