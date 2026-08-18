# Responsibility: Verify enabled providers are derived from the routes, and only their credentials are required.
from __future__ import annotations

import subprocess
import sys

import pytest

import meshpipeline.adapters.model_inference.routes as routecfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.settings.env import ConfigurationError


def test_enabled_providers_are_derived_from_the_routes_not_hardcoded():
    # the default profile points builder/planner/reviewers at deepinfra and intake at deepseek
    assert routecfg.enabled_llm_providers() == {"deepinfra", "deepseek"}


def test_no_missing_credentials_when_the_enabled_profile_is_configured(monkeypatch):
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "x")
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "y")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")
    assert routecfg.missing_provider_credentials() == []


def test_an_enabled_provider_without_a_key_is_reported(monkeypatch):
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "")   # enabled (routes use it) but unset
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "y")
    missing = routecfg.missing_provider_credentials()
    assert any("deepinfra" in m and "DEEPINFRA_API_KEY" in m for m in missing)
    assert not any("deepseek" in m for m in missing)       # deepseek IS configured


def test_a_disabled_provider_is_never_required(monkeypatch):
    # a profile whose routes reference ONLY deepinfra must not demand a deepseek key
    monkeypatch.setattr(routecfg, "enabled_llm_providers", lambda: {"deepinfra"})
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "x")
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "")    # unset, but disabled -> not required
    assert routecfg.missing_provider_credentials() == []


def test_tavily_key_is_required_only_when_tavily_search_is_enabled(monkeypatch):
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "x")
    monkeypatch.setattr(provcfg, "DEEPSEEK_API_KEY", "y")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(provcfg, "TAVILY_API_KEY", "")
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "tavily")
    assert any("tavily" in m for m in routecfg.missing_provider_credentials())
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")   # searxng needs no key
    assert routecfg.missing_provider_credentials() == []


def test_the_providers_module_imports_without_any_llm_keys():
    code = ("import meshpipeline.settings.providers as p\n"
            "assert p.DEEPINFRA_API_KEY == '' and p.DEEPSEEK_API_KEY == ''\n"
            "print('ok')\n")
    # Explicitly empty rather than absent: the process environment outranks the developer's
    # .env, which is what makes an unkeyed import reproducible from a working checkout.
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "src",
           "DEEPINFRA_API_KEY": "", "DEEPSEEK_API_KEY": ""}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr


def test_a_client_for_an_uncredentialed_provider_fails_clearly(monkeypatch):
    from meshpipeline.adapters.model_inference.deepinfra import get_deepinfra_client
    monkeypatch.setattr(provcfg, "DEEPINFRA_API_KEY", "")
    with pytest.raises(ConfigurationError, match="DEEPINFRA_API_KEY"):
        get_deepinfra_client()


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
                        lambda: ["deepinfra inference (set DEEPINFRA_API_KEY)"])
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
                        lambda: ["deepseek inference (set DEEPSEEK_API_KEY)"])
    with pytest.warns(RuntimeWarning, match="missing credentials"):
        startup.validate()   # warns, does NOT raise
