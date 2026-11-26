# Responsibility: Compose the concrete pieces a CRS v4/v5 evaluation wave's model router is built from (the real
#                 router module and a local capacity controller) into `cad/repair_v4/model_route.install_router`.
# Owns: `wave_router_parts`.
# Boundaries: composition only - the router, pricing and capacity logic are their own modules'. Called by the eval
#             harness's entry points and the tests, never by the product's request path (which composes its router
#             in runtime/composition.py).
"""The wave router's concrete parts."""

from __future__ import annotations

from typing import Any


def wave_router_parts() -> tuple[Any, Any]:
    """(the real router module, a fresh local capacity controller), for `model_route.set_wave_router_parts`."""
    from meshpipeline.adapters.model_capacity.local import LocalCapacityController
    from meshpipeline.adapters.model_inference import router as real_router

    return real_router, LocalCapacityController()


__all__ = ["wave_router_parts"]
