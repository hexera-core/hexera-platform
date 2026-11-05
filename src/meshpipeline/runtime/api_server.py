# Responsibility: Start the API process: bind adapters, then serve the application.
# Boundaries: a process entry point; it defines no route.
from __future__ import annotations

from meshpipeline.runtime.composition import install_adapters
from meshpipeline.runtime.logging import setup_logging
from meshpipeline.runtime.observability.sentry import setup_sentry

setup_logging()   # human-readable by default; JSON when LOG_FORMAT=json


async def _readiness_probe() -> tuple[bool, dict]:
    checks: dict[str, str] = {}

    # Postgres
    try:
        from sqlalchemy import text

        from meshpipeline.persistence.session import _get_engine
        async with _get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"down: {type(exc).__name__}"

    # Redis - through the adapters' client builder, so the redis SDK stays in the one module
    # allowed to import it (composition may reach into adapters; the API never does).
    try:
        from meshpipeline.adapters._shared.redis_client import async_client
        _r = async_client(socket_connect_timeout=2)
        try:
            await _r.ping()
            checks["redis"] = "ok"
        finally:
            await _r.aclose()
    except Exception as exc:
        checks["redis"] = f"down: {type(exc).__name__}"

    # object store (MinIO local / GCS hosted) - a backend-agnostic reachability probe
    try:
        import asyncio as _asyncio

        from meshpipeline.contracts.object_storage import get_object_store
        _store = get_object_store()
        # exists() on a sentinel key round-trips to the store without needing the object
        # to be there (a 404 still proves reachability).
        await _asyncio.to_thread(lambda: _store.exists(object_key=".healthz"))
        checks["object_store"] = "ok"
    except Exception as exc:
        checks["object_store"] = f"down: {type(exc).__name__}"

    try:
        from meshpipeline.adapters._shared.resilience import breaker_states
        circuits = breaker_states()
    except Exception:
        circuits = {}

    hard_ok = all(v == "ok" for v in checks.values())
    # An OPEN circuit is degraded but not "not ready" - surface it, stay up.
    body = {"status": "ready" if hard_ok else "not_ready", "checks": checks, "circuits": circuits}
    return hard_ok, body


# process composition (runs at import, before uvicorn serves `app`)
setup_sentry("api")
# Select the settings-configured pipeline launcher and inject it into the product-owned
# contract, so dispatch() invokes the neutral seam (never a backend factory).
install_adapters()

from meshpipeline.api.app import (  # noqa: E402  (imported after composition)
    app,
    set_readiness_probe,
    set_startup_validator,
)
from meshpipeline.runtime.startup import validate_and_summarize  # noqa: E402

set_readiness_probe(_readiness_probe)
# Boot-time config validation runs in the app's lifespan (before it serves), as it always
# has - the app takes it from the runtime rather than importing every settings owner itself.
set_startup_validator(validate_and_summarize)

# Distributed tracing - no-op unless OTEL_TRACES_ENABLED=true. Instruments the app + httpx
# (LLM calls) so a request traces through to the worker job.
from meshpipeline.runtime.observability.otel import setup_tracing  # noqa: E402

setup_tracing(app)

__all__ = ["app"]
