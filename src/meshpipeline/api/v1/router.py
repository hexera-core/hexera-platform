# Responsibility: Assemble the versioned API surface - which routers are mounted, under which prefix and tag.
# Boundaries: mounting only; every endpoint, schema and guard belongs to the module being mounted.
from fastapi import APIRouter

from meshpipeline.api.v1 import chat, client_config, credits, simulation, upload, ws

router = APIRouter(prefix="/api/v1")
router.include_router(upload.router,     prefix="/upload",     tags=["upload"])
router.include_router(client_config.router, prefix="/client-config", tags=["client-config"])
router.include_router(simulation.router, prefix="/simulation", tags=["simulation"])
router.include_router(ws.router,         prefix="/ws",         tags=["stream"])
router.include_router(chat.router,       prefix="/chat",       tags=["chat"])
router.include_router(credits.router,    prefix="/credits",    tags=["credits"])
