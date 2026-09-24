# Responsibility: Verify a deployment either has a working reader for the look or says it takes no look.
# Boundaries: it exercises the real import boundary in isolated processes, and reads the deploy scripts' and
#             the template's own text for the parts that are not code.
#
# THE DEFECT. GEOMETRY_VISION_PROVIDER defaulted to openai and the template carried only DEEPSEEK_API_KEY and
# DEEPINFRA_API_KEY. So vision/client.py's OpenAI adapter returned None, reader() returned None, and every row
# stored "the configured reader has no key in this environment, so nothing looked". settings/policy.py's own
# comment stated the contradiction and nothing acted on it: a key that is WRITTEN DOWN is not a key that is
# READ, and a contradiction a comment knows about is not a contradiction anything refuses.
#
# WHAT CHANGED. The template carries OPENAI_API_KEY, the deploy wires its Secret Manager container into the
# API and the worker fleet, and a hosted environment whose configured reader has no key refuses to start.
# `off` is the one way to say a deployment takes no look, because a default is not a confirmation.
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import meshpipeline.settings.policy as policy
from meshpipeline.settings import inventory

REPO = Path(__file__).resolve().parents[3]

#: Everything the import path needs before it can reach the reader question, passed explicitly so a
#: developer's gitignored .env cannot decide the outcome. PYTHONPATH is forwarded because a checkout runs
#: from src/ on the path while an image runs from site-packages, and this must hold in both.
_BASE = {
    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    "PATH": os.environ.get("PATH", ""),
    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    "PYTHONUTF8": "1",
    "DEEPSEEK_API_KEY": "x",
    "DEEPINFRA_API_KEY": "x",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "MESH_API_KEY": "k",
    "USER_TOKEN_SECRET": "s",
    "CORS_ORIGINS": "https://app.example.com",
    "POSTGRES_PASSWORD": "pw",
    "DATABASE_URL": "",
    "WEB_SEARCH_ENABLED": "false",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _imports(**env) -> tuple[bool, str]:
    """Does `import meshpipeline.settings.policy` succeed under this environment, and what did it say?

    A fresh process per case: policy decides at IMPORT, so an in-process reload would carry the previous
    case's module state. Same technique as tests/unit/settings/test_hardened_runtime_policy.py.
    """
    done = subprocess.run([sys.executable, "-c", "import meshpipeline.settings.policy"],
                          env={**_BASE, **env}, capture_output=True, text=True, timeout=180)
    return done.returncode == 0, done.stdout + done.stderr


# ---------------------------------------------------------------- a fresh deployment is told

def test_the_template_carries_the_key_the_default_reader_needs():
    # THE FRESH-DEPLOYMENT FIX. Before this, the one thing a new deployment needed in order to look at an
    # uploaded part was the one thing the template did not mention.
    rendered = inventory.render_env()
    assert "\nOPENAI_API_KEY=" in rendered, (
        "the generated template does not carry OPENAI_API_KEY, so a fresh deployment reading .env.example "
        "has no way to know the look needs it")
    assert (REPO / ".env.example").read_text(encoding="utf-8") == rendered, (
        ".env.example is not what the catalogue renders - regenerate it: "
        "python -m meshpipeline.settings.inventory > .env.example")


def test_the_template_says_what_the_reader_costs_and_what_the_alternative_is():
    # An operator deciding whether to open an OpenAI account should not have to read policy.py to find the
    # price or the fallback.
    template = (REPO / ".env.example").read_text(encoding="utf-8")
    block = template[template.index("GEOMETRY_VISION_TIMEOUT_SECONDS"):template.index("GEOMETRY_AGENT_STEP_PROVIDER")]
    assert "$5.30" in block, "the template does not say what the measured reader costs"
    assert "deepinfra" in block.lower(), "the template does not name the fallback that needs no new account"


def test_the_default_provider_and_the_default_model_agree():
    # A provider switch is a model switch. gpt-5.6-luna is an OpenAI model id; had the default provider
    # been moved to DeepInfra to reuse the key that already exists, every look would have 404'd.
    assert policy.GEOMETRY_VISION_PROVIDER == "openai"
    assert policy.GEOMETRY_VISION_MODEL.startswith("gpt-"), (
        f"the default reader model {policy.GEOMETRY_VISION_MODEL!r} is not an OpenAI model id but the "
        f"default provider is openai")


# ---------------------------------------------------------------- the missing key is loud

def test_the_reason_names_the_variable_that_would_fix_it():
    reason = policy.vision_reader_has_no_key()
    if reason:
        assert "OPENAI_API_KEY" in reason and "GEOMETRY_VISION_PROVIDER" in reason
    # And with a key present it says nothing at all - the sentence is the condition, so an empty string
    # has to mean "this deployment can look".
    assert isinstance(reason, str)


@pytest.mark.parametrize("env_name", ["production", "staging", "prod", "hosted", "aurora-eu-west-1"])
def test_a_hosted_deployment_with_no_reader_key_refuses_to_start(env_name):
    # LOUD, not a row per upload. Same shape as the auth secrets and the durable checkpointer: outside the
    # dev set, a configuration that silently degrades the product is a refusal.
    ok, said = _imports(ENV=env_name, GEOMETRY_VISION_PROVIDER="openai", OPENAI_API_KEY="")
    assert not ok, f"ENV={env_name} started with no reader key: {said}"
    assert "OPENAI_API_KEY" in said and "GEOMETRY_VISION_PROVIDER=off" in said, (
        f"the refusal does not name the key or the way to decline the look: {said}")


@pytest.mark.parametrize("env_name", ["production", "hosted"])
def test_a_hosted_deployment_that_says_it_takes_no_look_starts(env_name):
    # A DEFAULT IS NOT A CONFIRMATION, but `off` is. This is the only way past the refusal without a key,
    # and it is a statement somebody wrote rather than something nobody set.
    ok, said = _imports(ENV=env_name, GEOMETRY_VISION_PROVIDER="off", OPENAI_API_KEY="")
    assert ok, f"ENV={env_name} with GEOMETRY_VISION_PROVIDER=off refused to start: {said}"


@pytest.mark.parametrize("env_name", ["production", "hosted"])
def test_a_hosted_deployment_with_the_key_starts(env_name):
    ok, said = _imports(ENV=env_name, GEOMETRY_VISION_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    assert ok, f"ENV={env_name} with a reader key refused to start: {said}"


@pytest.mark.parametrize("env_name", ["dev", "test", "ci", "local"])
def test_a_developer_without_an_openai_account_can_still_run_the_stack(env_name):
    # The cost of the refusal is paid by hosted deployments only. A developer runs everything and the look
    # records that it did not happen, which is the truthful outcome on a workstation.
    ok, said = _imports(ENV=env_name, GEOMETRY_VISION_PROVIDER="openai", OPENAI_API_KEY="")
    assert ok, f"ENV={env_name} refused to start over a missing reader key: {said}"


def test_the_key_for_a_switched_provider_is_the_one_that_is_checked():
    # A provider with no key is a look that does not happen, never a fall-through. So a deployment that
    # switched to DeepInfra is judged on DEEPINFRA_API_KEY and not on the three keys it does not use.
    ok, said = _imports(ENV="production", GEOMETRY_VISION_PROVIDER="deepinfra",
                        GEOMETRY_VISION_MODEL="Qwen/Qwen3-VL-235B-A22B-Instruct",
                        DEEPINFRA_API_KEY="x", OPENAI_API_KEY="")
    assert ok, f"a DeepInfra reader with a DeepInfra key was refused: {said}"
    ok, said = _imports(ENV="production", GEOMETRY_VISION_PROVIDER="deepinfra",
                        GEOMETRY_VISION_MODEL="Qwen/Qwen3-VL-235B-A22B-Instruct",
                        DEEPINFRA_API_KEY="", OPENAI_API_KEY="sk-test")
    assert not ok, "a DeepInfra reader with no DeepInfra key started because an OpenAI key was set"
    assert "DEEPINFRA_API_KEY" in said


def test_a_reader_nobody_can_use_is_refused_at_import_and_not_once_per_upload():
    # `auto` is the interesting one: the package accepts it, and it picks a provider by whichever key
    # happens to be set. A part read by a model nobody chose is not a look anybody measured.
    for bad in ("auto", "gemini", ""):
        ok, said = _imports(ENV="dev", GEOMETRY_VISION_PROVIDER=bad)
        assert not ok, f"GEOMETRY_VISION_PROVIDER={bad!r} was accepted"
        assert "GEOMETRY_VISION_PROVIDER" in said


# ---------------------------------------------------------------- the deploy cannot ship without one

def test_every_reader_the_platform_accepts_has_a_secret_container_in_the_deploy():
    # DERIVED from policy's own list, so adding a provider there and forgetting the deploy is a failure
    # here rather than a deployment that cannot look.
    validator = (REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh").read_text(encoding="utf-8")
    for provider in policy.GEOMETRY_VISION_PROVIDERS:
        if provider == "off":
            continue
        assert re.search(rf"^\s*{provider}\)", validator, re.M), (
            f"deploy/gcp/scripts/validate-config.sh does not check the key for GEOMETRY_VISION_PROVIDER="
            f"{provider}, so a deployment could ship with that reader and no key")


def test_the_deploy_says_so_when_no_container_is_named_for_the_reader():
    # What this LAYER can prove is limited: generated.env carries container NAMES, never values, and the
    # container is provisioned as optional - so a named one may still hold no version. It therefore warns
    # rather than refuses, and the refusal on the actual key is settings/policy.py's, at import, in the
    # process that can read it. Each check proves what it can see and no more.
    validator = (REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY_SECRET" in validator, (
        "the deploy validator does not look for a container for the default reader's key")
    assert "GEOMETRY_VISION_PROVIDER=off" in validator, (
        "the deploy validator does not tell an operator how to say the deployment takes no look")
    warned = validator[validator.index('if [ -z "${reader_container}" ]; then'):]
    assert warned.lstrip().startswith('if') and "warn " in warned[:400], (
        "the unnamed-container case is not reported at all")


def test_the_deploy_refuses_the_two_configurations_no_key_could_rescue():
    # An unusable provider name and an OpenAI model id on a non-OpenAI provider are wrong whatever the key
    # is, so those ARE refusals: one would roll out a service that cannot start, the other a deployment
    # where every look 404s and the row calls it a failed look.
    validator = (REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh").read_text(encoding="utf-8")
    assert "is not a reader this deployment can use" in validator
    assert "an OpenAI model id that provider does not serve" in validator


def test_the_default_readers_key_reaches_both_the_api_and_the_workers():
    # The look runs on the worker that drains geometry_look, not on the API, so a key mounted only on the
    # API is a key the look never sees. Both are asserted because only one of them was the obvious one.
    api = (REPO / "deploy" / "gcp" / "scripts" / "create-api-service.sh").read_text(encoding="utf-8")
    fleet = (REPO / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh").read_text(encoding="utf-8")
    startup = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY:OPENAI_API_KEY_SECRET" in api, "the API service mounts no reader key"
    assert "openai-api-key-secret:OPENAI_API_KEY_SECRET" in fleet, "the worker fleet carries no reader key"
    assert "OPENAI_API_KEY:openai-api-key-secret" in startup, "a worker instance resolves no reader key"


def test_the_reader_key_is_provisioned_as_a_container_and_never_as_a_value():
    secrets = (REPO / "deploy" / "gcp" / "scripts" / "create-secrets.sh").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY|${OPENAI_SECRET}" in secrets, (
        "create-secrets.sh provisions no container for the reader's key")
    bootstrap = (REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY_SECRET=${OPENAI_API_KEY_SECRET:-openai-api-key}" in bootstrap, (
        "bootstrap-env.sh writes no container NAME for the reader's key")


def test_the_reader_key_cannot_travel_as_an_ordinary_environment_value():
    # The roster in create-api-service.sh is what stops a credential being smuggled through API_EXTRA_ENV.
    # A new secret-bearing name that is not on it is a hole in that guard.
    api = (REPO / "deploy" / "gcp" / "scripts" / "create-api-service.sh").read_text(encoding="utf-8")
    block = api[api.index("SECRET_BEARING=("):]
    block = block[:block.index(")")]
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert name in block, f"{name} is secret-bearing but is not on create-api-service.sh's roster"
