# Responsibility: Assemble the FastAPI application: routers, middleware, lifespan and the health probes.
# Owns: startup validation, the readiness probe and the static UI mount.
# Boundaries: composition and boundary concerns; no request handler's logic lives here.
# Collaborates with: api/v1/ for the routes and runtime/composition.py for adapter binding.
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path as _Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.api.auth import router as auth_router
from meshpipeline.api.v1.router import router as v1_router
from meshpipeline.persistence.session import dispose_engine

logger = logging.getLogger(__name__)
# Logging format is configured by the process entrypoint (runtime/api_server, runtime/
# celery_worker) - the app assembly only takes a logger, it does not own process setup.

# Process-startup config validation is a RUNTIME concern (it spans every settings owner), so
# the entrypoint injects it - the same shape as the readiness probe below. Without it the app
# still serves: a process that composed nothing has nothing to validate.
_startup_validator = None


def set_startup_validator(validator) -> None:
    global _startup_validator
    _startup_validator = validator



async def _startup_checks() -> None:
    try:
        from sqlalchemy import text

        from meshpipeline.persistence.session import _get_engine
        engine = _get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Startup check: DB reachable")
    except Exception as exc:
        logger.warning("Startup check: DB unreachable - %s", exc)
        return

    try:
        from alembic.config import Config as _AlembicConfig
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        _alembic_candidates = [
            _Path.cwd() / "alembic.ini",
            _Path(__file__).parent.parent / "alembic.ini",
        ]
        _alembic_ini = next((p for p in _alembic_candidates if p.exists()), None)
        if _alembic_ini is None:
            logger.warning("Startup check: alembic.ini not found - skipping migration head check")
            return

        alembic_cfg = _AlembicConfig(str(_alembic_ini))
        script = ScriptDirectory.from_config(alembic_cfg)
        expected_heads = set(script.get_heads())

        from meshpipeline.persistence.session import _get_engine
        engine = _get_engine()
        async with engine.connect() as conn:
            def _read_heads(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                return set(ctx.get_current_heads())
            current_heads = await conn.run_sync(_read_heads)

        if current_heads == expected_heads:
            logger.info("Startup check: Alembic at head (%s)", expected_heads)
        else:
            missing = expected_heads - current_heads
            logger.warning(
                "Startup check: Alembic NOT at head - current=%s expected=%s missing=%s "
                "(run 'alembic upgrade head' to apply pending migrations)",
                current_heads, expected_heads, missing,
            )
    except Exception as exc:
        logger.warning("Startup check: Alembic head check failed - %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Mesh API starting up")

    if _startup_validator is not None:
        logger.info(_startup_validator())

    await _startup_checks()

    yield

    await dispose_engine()
    logger.info("Mesh API shut down cleanly")



from meshpipeline import __version__

# THE operational surface decision, made where the routes are REGISTERED rather than hidden by a
# later guard. In a hardened environment FastAPI is given no documentation, ReDoc or schema URL,
# so those paths are never in the router at all: the answer is an ordinary 404, indistinguishable
# from any other unknown path, instead of a 401 that would confirm something is there.
# Development keeps all of it - reading the schema is how the API is worked on.
_HARDENED = polcfg.requires_hardened_runtime(polcfg.ENV)

app = FastAPI(
    title="Mesh API",
    description="AI-powered CFD mesh generation",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if _HARDENED else "/api/docs",
    redoc_url=None if _HARDENED else "/api/redoc",
    openapi_url=None if _HARDENED else "/openapi.json",
    # the Swagger OAuth2 landing page is a documentation asset too, and it is registered
    # independently of docs_url
    swagger_ui_oauth2_redirect_url=None if _HARDENED else "/docs/oauth2-redirect",
)

from meshpipeline.api.middleware.hardening import RateLimitMiddleware, security_headers_middleware

# The mesh-viewer surface payloads are tens of MB of base64 - gzip pays for
# itself immediately; everything else small stays untouched (minimum_size).
app.add_middleware(GZipMiddleware, minimum_size=8192)

# Request throttling per identity (X-User-Id, else client IP); fail-open.
app.add_middleware(RateLimitMiddleware)

app.middleware("http")(security_headers_middleware)

_cors_origins = polcfg.CORS_ORIGINS
_allow_creds = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    # Blameless 500: log the real error server-side; never leak exception type /
    # stack / internals to the client. (FastAPI handles HTTPException/4xx itself.)
    from fastapi.responses import JSONResponse
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error - please try again. "
                           "If it persists, this is on our side, not your request."},
    )


app.include_router(v1_router)

# Mounted OUTSIDE /api/v1 deliberately. It is not part of the versioned product surface a caller
# with an API key uses; it is the console's own sign-in seam, gated on MESH_API_KEY, and keeping
# it off /api/v1 means an edge rule can exclude it by path without carving a hole in the version.
app.include_router(auth_router, tags=["auth"])

# Instrumentation ALWAYS runs - request metrics keep being recorded in-process either way. What a
# hardened deployment does not get is the public scrape endpoint: /metrics enumerates route
# templates, status classes and process/runtime detail to anyone who asks. Exposing it is a
# deliberate development convenience, not a default.
_instrumentator = Instrumentator().instrument(app)
if not _HARDENED:
    _instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)

import os as _os


class _RevalidatingStatic(StaticFiles):

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


if _os.path.isdir(rtcfg.STATIC_DIR):
    app.mount("/static", _RevalidatingStatic(directory=rtcfg.STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui", status_code=307)

    # The shell must revalidate for the same reason every asset under /static does. Without
    # this it was the ONE cached file: browsers kept serving an old index.html against freshly
    # revalidated CSS/JS, so a UI change appeared half-applied until a manual hard refresh.
    _SHELL_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/ui", include_in_schema=False)
    async def serve_ui():
        return FileResponse(f"{rtcfg.STATIC_DIR}/index.html", headers=_SHELL_HEADERS)

    @app.get("/ui/{full_path:path}", include_in_schema=False)
    async def serve_ui_paths(full_path: str):
        return FileResponse(f"{rtcfg.STATIC_DIR}/index.html", headers=_SHELL_HEADERS)
else:
    logger.warning("STATIC_DIR '%s' not found - /ui and /static routes disabled", rtcfg.STATIC_DIR)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "version": __version__}


# Readiness probe - INJECTED by runtime composition (runtime/api_server.py). The probe
# reaches into the infra adapters (object store, circuit breakers); keeping it out of the
# app assembly is what makes api/app.py adapter-neutral (correction 1). Until a probe is
# injected the app reports "unknown" rather than importing an adapter itself.
_readiness_probe = None


def set_readiness_probe(probe) -> None:
    global _readiness_probe
    _readiness_probe = probe


@app.get("/readyz", tags=["health"])
async def readyz():
    from fastapi.responses import JSONResponse
    if _readiness_probe is None:
        return JSONResponse({"status": "unknown", "checks": {}, "circuits": {}}, status_code=200)
    hard_ok, body = await _readiness_probe()
    return JSONResponse(body, status_code=200 if hard_ok else 503)
