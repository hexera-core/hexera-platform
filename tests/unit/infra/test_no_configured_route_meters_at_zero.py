# Responsibility: Verify no model this deployment routes to bills at zero.
from __future__ import annotations

from meshpipeline.adapters.inference_telemetry.pricing import unpriced_route_models


def test_every_configured_route_model_has_a_confirmed_price():
    unpriced = unpriced_route_models()
    assert unpriced == [], (
        "these configured models meter at $0.00, so every job that uses them under-bills "
        f"silently: {unpriced}. Confirm the price on the provider's catalogue and add it to "
        "_PRICES, or point the route at a model that has one.")
