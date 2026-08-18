# Responsibility: Verify ONE environment classification decides hardening, and every hosted name gets it.
# Boundaries: it exercises the real import/startup boundary in isolated processes; it asserts on no source text.
from __future__ import annotations

import subprocess
import sys

import pytest

import meshpipeline.settings.policy as policy

#: DERIVED from the authority, never restated. Add or remove an exempt name in settings/policy.py
#: and these cases follow automatically - a second handwritten inventory here would be exactly the
#: duplicate classification this batch exists to remove.
DEV_ENVS = sorted(policy._AUTH_OPTIONAL_ENVS)

#: Hosted names an operator plausibly picks. None is special-cased anywhere: hardening follows from
#: NOT being exempt, which is why the unknown name below is hardened like the four familiar ones.
HOSTED_ENVS = ["production", "staging", "prod", "hosted"]
UNKNOWN_ENV = "aurora-eu-west-1"

#: Every variable the assertions depend on is passed EXPLICITLY, including as an empty string.
#: A key omitted here is supplied by the developer's gitignored .env - measured: an absent
#: POSTGRES_PASSWORD picks up 'localdev' from it - which would make these tests pass or fail
#: depending on whose machine ran them. python-dotenv's override=False treats a set-but-empty
#: variable as already configured, so an explicit "" is what actually blocks that leak.
_BASE = {
    "DEEPSEEK_API_KEY": "x",
    "DEEPINFRA_API_KEY": "x",
    "MESH_API_KEY": "",
    "USER_TOKEN_SECRET": "",
    "CORS_ORIGINS": "https://app.example.com",
    "POSTGRES_PASSWORD": "pw",
    "DATABASE_URL": "",
    "WEB_SEARCH_ENABLED": "false",
    "REDIS_URL": "redis://localhost:6379/0",
}

#: What a hosted deployment must already have before startup.validate() is even reachable.
_SECRETS = {"MESH_API_KEY": "k", "USER_TOKEN_SECRET": "s"}

_VALIDATE = "import meshpipeline.runtime.startup as s; s.validate(); print('STARTED')"
#: Imports the POLICY alone - never runtime.startup. Whatever this refuses, startup cannot own.
_POLICY_ONLY = "import meshpipeline.settings.policy; print('POLICY-IMPORTED')"


def _run(code: str, tmp_path, **env) -> subprocess.CompletedProcess:
    # A fresh process per case: policy classifies at IMPORT, so in-process reloads would carry a
    # previous case's decision. cwd=tmp_path keeps the directories validate() creates out of the tree.
    return subprocess.run([sys.executable, "-c", code], env={**_BASE, **env},
                          cwd=str(tmp_path), capture_output=True, text=True)


def _starts(tmp_path, **env) -> tuple[bool, str]:
    p = _run(_VALIDATE, tmp_path, **env)
    return (p.returncode == 0 and "STARTED" in p.stdout), p.stderr


# the predicate itself

@pytest.mark.parametrize("name", DEV_ENVS)
def test_recognised_development_environments_are_not_hardened(name):
    assert policy.requires_hardened_runtime(name) is False


@pytest.mark.parametrize("name", [*HOSTED_ENVS, UNKNOWN_ENV, ""])
def test_every_other_environment_is_hardened(name):
    assert policy.requires_hardened_runtime(name) is True


def test_normalization_matches_the_policy_authority():
    # policy applies .lower() to ENV and nothing else. Case folds; whitespace does NOT, so a
    # padded value is not the dev environment and is hardened - the safe direction, preserved.
    for name in DEV_ENVS:
        assert policy.requires_hardened_runtime(name.upper()) is False
        assert policy.requires_hardened_runtime(f" {name} ") is True


# 1-6: which environments demand the auth secrets

@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_a_hosted_environment_refuses_to_start_without_the_auth_secrets(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name)
    assert not ok, f"ENV={env_name!r} started with self-asserted identity"
    assert "MESH_API_KEY" in err and "USER_TOKEN_SECRET" in err
    assert "self-asserted" in err


@pytest.mark.parametrize("env_name", DEV_ENVS)
def test_a_development_environment_still_starts_without_secrets(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name)
    assert ok, f"ENV={env_name!r} no longer starts for local single-tenant use: {err[-400:]}"


def test_the_unset_default_environment_still_starts(tmp_path):
    # ENV absent entirely -> policy's own default ("dev"). A bare clone must still run.
    p = subprocess.run([sys.executable, "-c", _VALIDATE], env=dict(_BASE),
                       cwd=str(tmp_path), capture_output=True, text=True)
    assert p.returncode == 0 and "STARTED" in p.stdout, p.stderr[-400:]


def test_case_is_folded_but_padding_is_not_at_the_real_boundary(tmp_path):
    ok, _ = _starts(tmp_path, ENV="DEV")
    assert ok, "an upper-case dev environment stopped being recognised"
    ok, err = _starts(tmp_path, ENV=" dev ")
    assert not ok, "a padded environment name was accepted as development"
    assert "MESH_API_KEY" in err


# 7-10: the hardened startup requirements, identical for every hosted environment

@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_wildcard_cors_is_refused_in_every_hardened_environment(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name, CORS_ORIGINS="*", **_SECRETS)
    assert not ok, f"ENV={env_name!r} started with wildcard CORS"
    assert "CORS_ORIGINS" in err


@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_missing_database_configuration_is_refused_in_every_hardened_environment(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name, POSTGRES_PASSWORD="", DATABASE_URL="", **_SECRETS)
    assert not ok, f"ENV={env_name!r} started with no database credential"
    assert "DATABASE_URL" in err or "POSTGRES_PASSWORD" in err


@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_an_enabled_provider_without_its_credential_is_refused(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name, DEEPINFRA_API_KEY="", **_SECRETS)
    assert not ok, f"ENV={env_name!r} started with an uncredentialed enabled provider"
    assert "ENABLED provider" in err


@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_a_complete_hardened_configuration_starts(env_name, tmp_path):
    ok, err = _starts(tmp_path, ENV=env_name, **_SECRETS)
    assert ok, f"ENV={env_name!r} refused a complete hardened configuration: {err[-500:]}"


# 11: whose refusal is it

@pytest.mark.parametrize("env_name", [*HOSTED_ENVS, UNKNOWN_ENV])
def test_the_auth_secret_refusal_belongs_to_settings_policy(env_name, tmp_path):
    # Importing the policy ALONE - runtime.startup is never touched - already refuses. So the
    # refusal cannot be startup's to claim, and deleting the duplicate branches there removed
    # nothing that was enforcing anything.
    p = _run(_POLICY_ONLY, tmp_path, ENV=env_name)
    assert p.returncode != 0, f"ENV={env_name!r}: settings.policy imported without the secrets"
    assert "POLICY-IMPORTED" not in p.stdout
    assert "MESH_API_KEY" in p.stderr and "USER_TOKEN_SECRET" in p.stderr


def test_startup_no_longer_reads_the_secrets_policy_owns(tmp_path):
    # The ownership boundary, stated as behaviour rather than as a source scan: startup's module
    # namespace does not carry USER_TOKEN_SECRET at all, so it cannot re-check it.
    import meshpipeline.runtime.startup as startup
    assert not hasattr(startup, "USER_TOKEN_SECRET"), (
        "runtime.startup re-imported USER_TOKEN_SECRET - settings.policy owns that refusal")
