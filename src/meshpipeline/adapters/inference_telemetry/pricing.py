# Responsibility: Turn token counts into a cost estimate from the declared per-model prices.
# Boundaries: arithmetic over a price table; it meters nothing and bills nothing.
from __future__ import annotations

import logging

from meshpipeline.contracts.model_inference import UnpricedModel

logger = logging.getLogger(__name__)

# (input, output, cached_input) USD per 1M tokens. DeepInfra and DeepSeek verified 2026-07-16
# against each provider's own model page - see the Gate-1 provider audit for the retrieval
# record. OpenAI fetched 2026-09-14 from developers.openai.com/api/docs/pricing. DeepInfra's
# Qwen3-VL-235B-A22B-Thinking row confirmed 2026-09-15 against api.deepinfra.com/models/list.
_PRICES: dict[str, tuple[float, float, float]] = {
    "deepinfra:zai-org/GLM-5.2":                       (0.93, 3.00, 0.18),
    "deepinfra:Qwen/Qwen3-VL-235B-A22B-Instruct":      (0.20, 0.88, 0.11),
    # DeepInfra publishes no cached-input discount for this model, so cached is billed at the
    # full input rate - the choice that cannot under-bill.
    "deepinfra:Qwen/Qwen3-VL-235B-A22B-Thinking":      (0.45, 3.49, 0.45),
    "deepinfra:deepseek-ai/DeepSeek-V4-Pro":           (1.30, 2.60, 0.10),
    "deepinfra:deepseek-ai/DeepSeek-V4-Flash":         (0.09, 0.18, 0.018),
    "deepinfra:deepseek-ai/DeepSeek-V3.2":             (0.26, 0.38, 0.13),
    "deepinfra:zai-org/GLM-4.7-Flash":                 (0.06, 0.40, 0.01),
    # Direct DeepSeek meters cache HITS separately and very cheaply; that is why intake is
    # materially cheaper here than the same family via a reseller.
    "deepseek:deepseek-v4-pro":                        (0.435, 0.87, 0.003625),
    "deepseek:deepseek-v4-flash":                      (0.14, 0.28, 0.0028),
    # Fetched from developers.openai.com/api/docs/pricing on 2026-09-14.
    "openai:gpt-6-astra":                              (10.00, 50.00, 1.00),
    "openai:gpt-5.6-sol":                              (4.00,  20.00, 0.40),
    "openai:gpt-5.6-terra":                            (2.00,  12.00, 0.20),
    "openai:gpt-5.6-luna":                             (0.20,   1.20, 0.02),
}

# (30-minute write, 12-hour write) USD per 1M tokens: what the provider bills for tokens it WRITES to its
# prompt cache, above the fresh-input rate. Read 2026-11-23 from the standard-tier table of
# developers.openai.com/api/docs/pricing (columns "30m write" / "12h write"): gpt-6-astra $12.50 / $25.00
# against $10.00 input, i.e. 1.25x and 2.5x. A model that reports cache writes but has no row here is
# UNPRICED FOR WRITES (see `cache_write_unpriced`): the current page no longer lists gpt-5.6-sol/terra/luna
# at all, so their write rates are not discoverable and are deliberately NOT inferred from the astra ratio.
_CACHE_WRITE_PRICES: dict[str, tuple[float, float]] = {
    "openai:gpt-6-astra": (12.50, 25.00),
}
#: Models whose provider reports cache-write tokens but whose write rate is not confirmed. Their writes are
#: billed at the input rate (the pre-fix behaviour, an under-count) and flagged, never at an invented rate.
_CACHE_WRITE_UNPRICED: frozenset[str] = frozenset({
    "openai:gpt-5.6-sol", "openai:gpt-5.6-terra", "openai:gpt-5.6-luna"})

_warned: set[str] = set()


#: THE override setting. One declared name, parsed and validated once - never a variable whose
#: name is assembled from the provider and model being priced.
_OVERRIDE_SETTING = "MODEL_PRICE_OVERRIDES"


def _declared_price(provider: str, model: str) -> tuple[float, float, float] | None:
    # The lookup WITHOUT the warning, so asking "is this model priced?" is not itself a report
    # that it is unpriced. None means no confirmed price exists - never (0.0, 0.0, 0.0), which is
    # a real answer for a free model and must stay distinguishable from the absence of one.
    from meshpipeline.settings.inference_overrides import price_overrides
    key = f"{provider}:{model}"
    override = price_overrides().get(key)
    if override is not None:
        return override
    return _PRICES.get(key)


def require_price(provider: str, model: str) -> tuple[float, float, float]:
    # THE FAIL-CLOSED FORM of price_for. Where a spending cap depends on the meter, an unpriced model
    # must not be dialled at all: price_for would return (0.0, 0.0, 0.0), every call would cost
    # "nothing", and the cap could never trip. A declared (0.0, 0.0, 0.0) - a free model - is a real
    # price and passes.
    declared = _declared_price(provider, model)
    if declared is None:
        raise UnpricedModel(
            f"no price is declared for {provider}:{model}, so its spend cannot be metered or capped; "
            f"refusing to call it. Add '{provider}:{model}=<in>,<out>,<cached>' (USD per 1M tokens) to "
            f"{_OVERRIDE_SETTING} once the provider's price is confirmed.")
    return declared


def unpriced_route_models(extra: tuple[str, ...] = ()) -> list[str]:
    # THE machine-detectable form of the gap. An unpriced model does not fail: it meters at $0.00,
    # so the deployment under-bills silently and the telemetry looks healthy. Pricing is measured
    # resource cost, so a caller - an operator, a release gate, a test - has to be able to ask
    # which configured models are metering at zero before that number reaches an invoice.
    from meshpipeline.settings.routes import configured_route_targets
    # `extra` is `provider:model` keys that are callable without being a role route (the repair_v4
    # proposer's chain), so they are held to the same standard.
    keys = {f"{provider}:{model}" for _, _, provider, model in configured_route_targets()}
    keys.update(extra)
    return sorted(k for k in keys if _declared_price(*k.split(":", 1)) is None)


def price_for(provider: str, model: str) -> tuple[float, float, float]:
    key = f"{provider}:{model}"
    declared = _declared_price(provider, model)
    if declared is not None:
        return declared
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "no verified price for %s - cost telemetry for it will report 0.0. Add "
            "'%s=<in>,<out>,<cached>' (per 1M tokens) to %s once the provider's price is "
            "confirmed.", key, key, _OVERRIDE_SETTING)
    return (0.0, 0.0, 0.0)


def cache_write_prices(provider: str, model: str) -> tuple[float, float] | None:
    """(30-minute, 12-hour) cache-write USD per 1M tokens, or None when none is confirmed."""
    return _CACHE_WRITE_PRICES.get(f"{provider}:{model}")


def cache_write_unpriced() -> list[str]:
    """Models that report cache writes and have no confirmed write rate: their spend is under-counted."""
    return sorted(_CACHE_WRITE_UNPRICED - set(_CACHE_WRITE_PRICES))


def estimate_cost(provider: str, model: str, *, input_tokens: int, output_tokens: int,
                  cached_input_tokens: int = 0, cache_write_tokens: int = 0,
                  cache_write_30m_tokens: int = 0) -> float:
    # input_tokens INCLUDES both the cached and the cache-write tokens (the provider reports them as
    # details of it), so fresh input is what is left after both. Write tokens bill at their own rate.
    p_in, p_out, p_cached = price_for(provider, model)
    key = f"{provider}:{model}"
    writes = _CACHE_WRITE_PRICES.get(key)
    if cache_write_tokens and writes is None:
        if key in _CACHE_WRITE_UNPRICED and ("cw:" + key) not in _warned:
            _warned.add("cw:" + key)
            logger.warning(
                "%s reported %d cache-write tokens and has no confirmed cache-write price: billing them "
                "at the input rate, which UNDER-counts spend. Add its write rate to _CACHE_WRITE_PRICES.",
                key, cache_write_tokens)
        writes = (p_in, p_in)
    w30 = min(cache_write_30m_tokens, cache_write_tokens)
    w_other = cache_write_tokens - w30          # unattributed TTL is billed at the dearer 12h rate
    p30, p12 = writes if writes else (p_in, p_in)
    fresh_input = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    return (fresh_input * p_in + cached_input_tokens * p_cached + w30 * p30 + w_other * p12
            + output_tokens * p_out) / 1e6
