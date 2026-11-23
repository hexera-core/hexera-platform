# Responsibility: Put the REAL model router under the v4 proposer, outside a server process, and refuse to start a
#                 model arm that could not reach it.
# Owns: `require_model_key`, `install_router`, `probe_route_chain` and `NoModelKey`.
# Boundaries: it composes the router the API composes (`CachingModelRouter` over the OpenAI-compatible adapter,
#             a local capacity controller) and nothing else. It invents no credential path: the key is whatever
#             `settings.providers.OPENAI_API_KEY` read from the environment when it was imported.
"""The proposer's route to a model.

`AIProposer` calls `contracts.model_inference.call_repair_v4_propose`, which asks the INSTALLED router. A server
installs one in `runtime.composition`; a wave shard (`devtools/validation/v4_eval/run_arm.py`) is not a server, so
without this module a `search_ai` shard fails on its first round with "no model router configured" - which the
proposer reads as a failed round, three in a row end the AI for that file, and the arm quietly degrades into
`search_det`. `require_model_key` stops that at the START of the shard instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any


class NoModelKey(RuntimeError):
    """A model arm was asked for but this process holds no model credential."""


def require_model_key(key: str | None = None) -> str:
    """The model key this process holds, or NoModelKey. `key` is for tests; the default reads the provider setting."""
    if key is None:
        from meshpipeline.settings import providers as provcfg

        key = provcfg.OPENAI_API_KEY
    if not key or not key.strip():
        raise NoModelKey(
            "a model arm needs OPENAI_API_KEY in the shard's environment and none is set. The v4 VM carries no "
            "model key unless the launcher names a Secret Manager secret (v4-model-secret metadata -> secret.env)")
    return key


def install_router(*, inner: Any = None) -> Any:
    """Install the router the proposer's calls go through, and the capacity controller it admits against.

    `inner` defaults to the real router module (`adapters.model_inference.router`); tests pass nothing and fake the
    PROVIDER CLIENT beneath it, so the router's pricing, retry and normalisation all run for real. The wrapper is the
    same `CachingModelRouter` the API installs, whose `call_repair_v4_propose` forwards without recording (a replayed
    answer would cost $0 against the ledger). Returns the installed router."""
    from meshpipeline.adapters.model_capacity.local import LocalCapacityController
    from meshpipeline.adapters.model_inference import router as real_router
    from meshpipeline.contracts import model_capacity, model_inference
    from meshpipeline.contracts.decision_cache import CachingModelRouter

    model_capacity.set_capacity_controller(LocalCapacityController())
    installed = CachingModelRouter(inner if inner is not None else real_router, model_of=lambda step: "",
                                   store=_NoRecords(), mode="off")
    model_inference.set_model_router(installed)
    return installed


class _NoRecords:
    """A decision store that keeps nothing: the wave never records or replays a model answer."""

    def get(self, key: str) -> None:
        return None

    def put(self, key: str, value: Any) -> None:
        return None


def probe_route_chain(routes: Sequence[str]) -> str:
    """The first route that passes the tool-and-image probe (`proposer.select_route`), or NoUsableRoute. Costs cents
    per shard, ahead of any file; it is not metered against the ledger."""
    from meshpipeline.cad.repair_v4.proposer import select_route

    return asyncio.run(select_route(list(routes)))
