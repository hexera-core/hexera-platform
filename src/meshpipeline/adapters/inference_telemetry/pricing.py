# Responsibility: Turn token counts into a cost estimate from the declared per-model prices.
# Boundaries: arithmetic over a price table; it meters nothing and bills nothing.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# (input, output, cached_input) USD per 1M tokens. Verified 2026-07-16 against each provider's
# own model page - see the Gate-1 provider audit for the retrieval record.
_PRICES: dict[str, tuple[float, float, float]] = {
    "deepinfra:zai-org/GLM-5.2":                       (0.93, 3.00, 0.18),
    "deepinfra:Qwen/Qwen3-VL-235B-A22B-Instruct":      (0.20, 0.88, 0.11),
    "deepinfra:deepseek-ai/DeepSeek-V4-Pro":           (1.30, 2.60, 0.10),
    "deepinfra:deepseek-ai/DeepSeek-V4-Flash":         (0.09, 0.18, 0.018),
    "deepinfra:deepseek-ai/DeepSeek-V3.2":             (0.26, 0.38, 0.13),
    "deepinfra:zai-org/GLM-4.7-Flash":                 (0.06, 0.40, 0.01),
    # Direct DeepSeek meters cache HITS separately and very cheaply; that is why intake is
    # materially cheaper here than the same family via a reseller.
    "deepseek:deepseek-v4-pro":                        (0.435, 0.87, 0.003625),
    "deepseek:deepseek-v4-flash":                      (0.14, 0.28, 0.0028),
}

# The reviewer's configured model (Qwen3-VL-235B-A22B-Thinking) is deliberately ABSENT: its
# price could not be confirmed on DeepInfra's catalogue during the Gate-1 audit (only the
# -Instruct variant was listed). Guessing it would fabricate cost evidence. It prices at 0.0 and
# logs a warning until the id and price are confirmed - see the Gate-1 open item.
# unpriced_route_models() below answers which configured models this affects, so the omission is
# a listable fact rather than a log line somebody has to be watching for.

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


def unpriced_route_models() -> list[str]:
    # THE machine-detectable form of the gap. An unpriced model does not fail: it meters at $0.00,
    # so the deployment under-bills silently and the telemetry looks healthy. Pricing is measured
    # resource cost, so a caller - an operator, a release gate, a test - has to be able to ask
    # which configured models are metering at zero before that number reaches an invoice.
    from meshpipeline.settings.routes import configured_route_targets
    return sorted({f"{provider}:{model}"
                   for _, _, provider, model in configured_route_targets()
                   if _declared_price(provider, model) is None})


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


def estimate_cost(provider: str, model: str, *, input_tokens: int, output_tokens: int,
                  cached_input_tokens: int = 0) -> float:
    p_in, p_out, p_cached = price_for(provider, model)
    fresh_input = max(0, input_tokens - cached_input_tokens)
    return (fresh_input * p_in + cached_input_tokens * p_cached + output_tokens * p_out) / 1e6
