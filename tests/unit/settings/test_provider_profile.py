# Responsibility: Verify enabled providers are derived from the routes, and only their credentials are required.
from __future__ import annotations

import subprocess
import sys

import pytest

import meshpipeline.adapters.model_inference.routes as routecfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.settings.env import ConfigurationError


def test_enabled_providers_are_derived_from_the_routes_not_hardcoded():
    # every shipped route resolves to openai; deepseek and deepinfra are supported but unrouted
    assert routecfg.enabled_llm_providers() == {"openai"}


def test_no_missing_credentials_when_the_enabled_profile_is_configured(monkeypatch):
    monkeypatch.setattr(provcfg, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")
    assert routecfg.missing_provider_credentials() == []


def test_an_enabled_provider_without_a_key_is_reported(monkeypatch):
    monkeypatch.setattr(provcfg, "OPENAI_API_KEY", "")     # enabled (every route uses it) but unset
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "")
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "")
    missing = routecfg.missing_provider_credentials()
    assert any("openai" in m and "OPENAI_API_KEY" in m for m in missing)
    # Unkeyed AND unrouted: a provider the product supports is not a provider it demands a key
    # for. This is the half that keeps a retired vendor from blocking a boot it has no part in.
    assert not any("deepinfra" in m or "deepseek" in m for m in missing)


def test_a_disabled_provider_is_never_required(monkeypatch):
    # a profile whose routes reference ONLY deepinfra must not demand a deepseek key
    monkeypatch.setattr(routecfg, "enabled_llm_providers", lambda: {"deepinfra"})
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "x")
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "")    # unset, but disabled -> not required
    assert routecfg.missing_provider_credentials() == []


def test_an_openai_profile_is_satisfied_by_its_own_key(monkeypatch):
    # openai is SELECTABLE, so a profile may enable it. The credential must be resolved through
    # LLM_PROVIDER_KEY_ENV itself - a second roster of env-name-to-value silently reports every
    # provider it was never updated for as uncredentialed, and prod then refuses to boot.
    monkeypatch.setattr(routecfg, "enabled_llm_providers", lambda: {"openai"})
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setattr(provcfg, "OPENAI_API_KEY", "sk-real")
    assert routecfg.missing_provider_credentials() == []


def test_an_openai_profile_without_its_key_is_reported(monkeypatch):
    monkeypatch.setattr(routecfg, "enabled_llm_providers", lambda: {"openai"})
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setattr(provcfg, "OPENAI_API_KEY", "")
    missing = routecfg.missing_provider_credentials()
    assert any("openai" in m and "OPENAI_API_KEY" in m for m in missing)


def test_tavily_key_is_required_only_when_tavily_search_is_enabled(monkeypatch):
    monkeypatch.setattr(provcfg, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(provcfg, "TAVILY_API_KEY", "")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "tavily")
    assert any("tavily" in m for m in routecfg.missing_provider_credentials())
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")   # searxng needs no key
    assert routecfg.missing_provider_credentials() == []


def test_the_providers_module_imports_without_any_llm_keys():
    code = ("import meshpipeline.settings.providers as p\n"
            "assert p.OPENAI_API_KEY == '' and p.DEEPINFRA_API_KEY == '' "
            "and p.DEEPSEEK_API_KEY == ''\n"
            "print('ok')\n")
    # Explicitly empty rather than absent: the process environment outranks the developer's
    # .env, which is what makes an unkeyed import reproducible from a working checkout.
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "src",
           "OPENAI_API_KEY": "", "DEEPINFRA_API_KEY": "", "DEEPSEEK_API_KEY": ""}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr


# Every provider with a client, not just the one the routes happen to use: the message an
# operator meets is the same question whichever vendor a role is pointed at, and the openai case
# is the live one now.
@pytest.mark.parametrize("module,factory,setting", [
    ("openai_api", "get_openai_client", "OPENAI_API_KEY"),
    ("deepinfra", "get_deepinfra_client", "DEEPINFRA_API_KEY"),
    ("deepseek", "get_deepseek_client", "DEEPSEEK_API_KEY"),
])
def test_a_client_for_an_uncredentialed_provider_fails_clearly(module, factory, setting,
                                                               monkeypatch):
    import importlib
    get_client = getattr(
        importlib.import_module(f"meshpipeline.adapters.model_inference.{module}"), factory)
    monkeypatch.setattr(provcfg, setting, "")
    with pytest.raises(ConfigurationError, match=setting):
        get_client()


# startup wiring: production hard-fails, dev warns
def _prod_ready(monkeypatch, startup):
    # USER_TOKEN_SECRET is deliberately absent: settings.policy refuses the IMPORT of any hardened
    # environment missing it, so validate() never runs without one and no longer reads it. See
    # tests/unit/settings/test_hardened_runtime_policy.py for that ownership boundary.
    monkeypatch.setattr(startup, "ENV", "production")
    monkeypatch.setattr(startup, "MESH_API_KEY", "k")
    monkeypatch.setattr(startup, "CORS_ORIGINS", ["https://app.example.com"])
    monkeypatch.setattr(startup, "POSTGRES_PASSWORD", "pw")
    monkeypatch.setattr(startup, "DATABASE_URL", "")


def test_production_startup_rejects_a_missing_enabled_credential(monkeypatch):
    import meshpipeline.runtime.startup as startup
    _prod_ready(monkeypatch, startup)
    monkeypatch.setattr(startup, "missing_provider_credentials",
                        lambda: ["openai inference (set OPENAI_API_KEY)"])
    with pytest.raises(ConfigurationError, match="ENABLED provider"):
        startup.validate()


def test_production_startup_passes_when_the_enabled_profile_is_complete(monkeypatch):
    import meshpipeline.runtime.startup as startup
    _prod_ready(monkeypatch, startup)
    monkeypatch.setattr(startup, "missing_provider_credentials", lambda: [])
    startup.validate()      # no raise


def test_dev_startup_only_warns_about_missing_credentials(monkeypatch):
    import meshpipeline.runtime.startup as startup
    monkeypatch.setattr(startup, "ENV", "dev")
    monkeypatch.setattr(startup, "missing_provider_credentials",
                        lambda: ["openai inference (set OPENAI_API_KEY)"])
    with pytest.warns(RuntimeWarning, match="missing credentials"):
        startup.validate()   # warns, does NOT raise
