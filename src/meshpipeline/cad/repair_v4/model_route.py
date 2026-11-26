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


#: the ledger file id every route-probe call is settled under (kind `route_probe`), so a shard's probe spend is on
#: the same books as its files' and inside the same wave cap
ROUTE_PROBE_FILE_ID = "_route_probe"
#: the most a shard's probes may spend between them (each call reserves its full output allowance up front)
ROUTE_PROBE_USD_CEILING = 1.00


def probe_route_chain(routes: Sequence[str], *, ledger: Any) -> str:
    """The first route that passes the tool-and-image probe (`proposer.select_route`), or NoUsableRoute.

    METERED: every probe call is admitted against the shard's `ledger` before it is sent and settled with the
    router's priced reply after (file id `ROUTE_PROBE_FILE_ID`, kind `route_probe`); a call that failed unbilled is
    released. The ledger is REQUIRED - Task 49 found these startup calls were the one paid call outside the books.
    A probe the ledger has no room for is not sent, and counts as that route failing its probe."""
    from meshpipeline.cad.repair_v4.proposer import select_route

    return asyncio.run(select_route(list(routes), call=metered_call(ledger, ROUTE_PROBE_FILE_ID,
                                                                    ROUTE_PROBE_USD_CEILING, kind="route_probe")))


class LedgerRefused(RuntimeError):
    """The ledger had no room for a call; it was not sent."""


def metered_call(ledger: Any, file_id: str, ceiling_usd: float, *, kind: str,
                 max_output_tokens: int | None = None) -> Any:
    """A `call_repair_v4_propose` that is admitted by `ledger` before it is sent and settled (or released) after,
    for model calls that do not go through an `AIProposer` (the route probe, v5's describe and agent calls).
    `max_output_tokens` LOWERS the call's output allowance, and the reservation with it (v5's agent: one call has to
    fit its $0.15 per-defect cap, which the full allowance on the dearest route does not)."""
    from meshpipeline.cad.repair_v4.proposer import estimate_call_usd
    from meshpipeline.contracts.model_inference import (
        REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS,
        call_repair_v4_propose,
        repair_v4_route_prices,
    )

    async def call(messages: list, tools: list, *, route: str, **kw: Any) -> Any:
        out_tokens = REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS
        if max_output_tokens is not None:
            out_tokens = max(1, min(int(max_output_tokens), REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS))
            kw = {**kw, "max_output_tokens": out_tokens}
        est, _tokens = estimate_call_usd(messages, tools, prices=repair_v4_route_prices(route),
                                         output_tokens=out_tokens)
        admission = ledger.admit(file_id, est, file_ceiling_usd=ceiling_usd)
        if not admission.ok:
            raise LedgerRefused(f"the ledger refused the {kind} call ({admission.reason}); it was not sent")
        try:
            priced = await call_repair_v4_propose(messages, tools, route=route, **kw)
        except BaseException:
            ledger.release(admission.reservation)
            raise
        reply = priced.reply
        if reply.failure_marker:
            ledger.release(admission.reservation)      # a failed round was not billed
        else:
            ledger.settle(admission.reservation, file_id=file_id, job_id=kw.get("job_id", ""),
                          provider=priced.provider, model=priced.model, input_tokens=reply.input_tokens,
                          cached_input_tokens=reply.cached_input_tokens, output_tokens=reply.output_tokens,
                          cost_usd=priced.cost_usd, kind=kind, cache_write_tokens=reply.cache_write_tokens,
                          cache_write_30m_tokens=reply.cache_write_30m_tokens)
        return priced

    return call
