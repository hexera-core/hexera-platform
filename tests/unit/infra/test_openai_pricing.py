# Responsibility: Verify every OpenAI model a route may reach meters at a real price.
from __future__ import annotations

import pytest

from meshpipeline.adapters.inference_telemetry import pricing

# Fetched from developers.openai.com/api/docs/pricing on 2026-09-14. USD per 1M tokens.
EXPECTED = {
    "gpt-6-astra":    (10.00, 50.00, 1.00),
    "gpt-5.6-sol":    (4.00,  20.00, 0.40),
    "gpt-5.6-terra":  (2.00,  12.00, 0.20),
    "gpt-5.6-luna":   (0.20,   1.20, 0.02),
}


@pytest.mark.parametrize("model", sorted(EXPECTED))
def test_each_openai_model_carries_its_published_price(model):
    assert pricing.price_for("openai", model) == EXPECTED[model]


def test_an_unpriced_model_still_reports_zero_rather_than_inventing_one():
    assert pricing.price_for("openai", "gpt-does-not-exist") == (0.0, 0.0, 0.0)
