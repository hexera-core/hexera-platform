# Responsibility: Verify the two structured inference overrides replace their dynamic namespaces exactly.
# Boundaries: it runs the real parsers and the real price/budget lookups; it asserts on no source text.
from __future__ import annotations

import importlib

import pytest

from meshpipeline.settings import inference_overrides as io
from meshpipeline.settings.env import ConfigurationError
from meshpipeline.settings.providers import LLM_PROVIDER_KEY_ENV


@pytest.fixture
def price(monkeypatch):
    def _set(raw: str):
        monkeypatch.setenv("MODEL_PRICE_OVERRIDES", raw)
        return io.price_overrides()
    return _set


@pytest.fixture
def budget(monkeypatch):
    def _set(raw: str):
        monkeypatch.setenv("MODEL_DOMAIN_BUDGETS", raw)
        return io.domain_budgets()
    return _set


# the built-in tariff is untouched by this refactor

def test_every_built_in_price_is_unchanged():
    from meshpipeline.adapters.inference_telemetry.pricing import _PRICES, price_for
    assert _PRICES, "the price table is empty - the subject of this test is gone"
    for key, expected in _PRICES.items():
        provider, _, model = key.partition(":")
        assert price_for(provider, model) == expected


def test_an_unpriced_model_still_reports_zero_and_warns(caplog):
    from meshpipeline.adapters.inference_telemetry import pricing
    pricing._warned.clear()
    with caplog.at_level("WARNING"):
        assert pricing.price_for("deepinfra", "some/unpriced-model") == (0.0, 0.0, 0.0)
    assert "no verified price" in caplog.text
    assert "MODEL_PRICE_OVERRIDES" in caplog.text, "the warning names a setting that no longer exists"


def test_cost_arithmetic_is_unchanged():
    from meshpipeline.adapters.inference_telemetry.pricing import estimate_cost
    # deepseek:deepseek-v4-pro = (0.435, 0.87, 0.003625) per 1M
    got = estimate_cost("deepseek", "deepseek-v4-pro",
                        input_tokens=1_000_000, output_tokens=1_000_000, cached_input_tokens=0)
    assert got == pytest.approx(0.435 + 0.87)


# overrides reach only their own key

def test_an_override_applies_to_exactly_its_provider_and_model(price, monkeypatch):
    from meshpipeline.adapters.inference_telemetry import pricing
    monkeypatch.setenv("MODEL_PRICE_OVERRIDES",
                       "deepinfra:zai-org/GLM-5.2=1.0,2.0,0.5")
    assert pricing.price_for("deepinfra", "zai-org/GLM-5.2") == (1.0, 2.0, 0.5)
    # a different model on the same provider keeps its built-in price
    assert pricing.price_for("deepseek", "deepseek-v4-pro") == (0.435, 0.87, 0.003625)


def test_several_overrides_parse_independently(price):
    got = price("deepinfra:a=1,2,3;deepseek:b=4,5,6")
    assert got == {"deepinfra:a": (1.0, 2.0, 3.0), "deepseek:b": (4.0, 5.0, 6.0)}


def test_an_empty_setting_is_no_overrides(price):
    assert price("") == {}
    assert price("   ") == {}


# the schema refuses everything it does not support

def test_an_unsupported_provider_is_refused(price):
    assert "openai" not in LLM_PROVIDER_KEY_ENV
    with pytest.raises(ConfigurationError, match="not a provider this product supports"):
        price("openai:gpt-4=1,2,3")


@pytest.mark.parametrize("bad,match", [
    ("deepinfra:m=1,2", "exactly three prices"),
    ("deepinfra:m=1,2,3,4", "exactly three prices"),
    ("deepinfra:m=a,b,c", "non-numeric"),
    ("deepinfra:m=-1,2,3", "negative"),
    ("deepinfra:m=inf,2,3", "non-finite"),
    ("deepinfra:m=nan,2,3", "non-finite"),
    ("deepinfra", "not '<key>=<value>'"),
    ("nocolon=1,2,3", "not 'provider:model'"),
    ("deepinfra:m=1,2,3;deepinfra:m=4,5,6", "appears twice"),
])
def test_malformed_prices_are_refused(price, bad, match):
    with pytest.raises(ConfigurationError, match=match):
        price(bad)


def test_a_refusal_never_echoes_a_credential(price, monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "sk-super-secret-value")
    with pytest.raises(ConfigurationError) as exc:
        price("openai:gpt-4=1,2,3")
    assert "sk-super-secret" not in str(exc.value)


# budgets

def test_budget_defaults_come_from_the_routes():
    from meshpipeline.adapters.model_inference.routes import all_routes, domain_budget
    for route in all_routes().values():
        assert domain_budget(route.primary.domain.key) >= route.concurrency_budget


def test_a_budget_override_applies_to_exactly_its_domain(monkeypatch):
    from meshpipeline.adapters.model_inference.routes import all_routes, domain_budget
    routes = all_routes()
    target = routes["builder"].primary.domain.key
    other = routes["intake"].primary.domain.key
    before_other = domain_budget(other)
    monkeypatch.setenv("MODEL_DOMAIN_BUDGETS", f"{target}=99")
    assert domain_budget(target) == 99
    assert domain_budget(other) == before_other, "a budget leaked into another domain"


def test_an_unknown_domain_falls_back_without_reading_the_environment(monkeypatch):
    from meshpipeline.adapters.model_inference.routes import domain_budget
    monkeypatch.setenv("MODEL_DEFAULT_CONCURRENCY_BUDGET", "5")
    # No MODEL_BUDGET_<DOMAIN> variable exists to be read; the declared fallback answers.
    monkeypatch.setenv("MODEL_BUDGET_DEEPINFRA_DEFAULT_GHOST", "4242")
    assert domain_budget("deepinfra:default:ghost-model") == 5


@pytest.mark.parametrize("bad,match", [
    ("deepinfra:default=4", "not 'provider:account:model'"),
    ("openai:default:m=4", "not a provider this product supports"),
    ("deepinfra:default:m=zero", "not a whole number"),
    ("deepinfra:default:m=0", "must be positive"),
    ("deepinfra:default:m=-3", "must be positive"),
    ("deepinfra:default:m=4;deepinfra:default:m=5", "appears twice"),
])
def test_malformed_budgets_are_refused(budget, bad, match):
    with pytest.raises(ConfigurationError, match=match):
        budget(bad)


def test_domain_budget_accepts_no_caller_supplied_default():
    import inspect

    from meshpipeline.adapters.model_inference.routes import domain_budget
    params = inspect.signature(domain_budget).parameters
    # `routes` is which routes to SEARCH, not a value to fall back to. A parameter that supplies a
    # configuration value would be a second default authority, which is what this forbids.
    assert not {"default", "fallback", "budget"} & set(params), (
        "domain_budget takes a caller-supplied default; the catalogue owns defaults")
    assert set(params) <= {"domain_key", "routes"}, f"unexpected parameters: {sorted(params)}"


def test_the_structured_settings_are_declared():
    from meshpipeline.settings import inventory as cat
    for name in ("MODEL_PRICE_OVERRIDES", "MODEL_DOMAIN_BUDGETS"):
        entry = cat.get(name)
        # Template, not internal: these are where previously-accepted OPERATOR configuration had
        # to migrate to, so an operator must be able to find them without reading the source.
        assert entry.exposure == "template"
        assert "provider" in entry.help and "=" in entry.help, "the schema is not documented"
    importlib.reload(cat)
