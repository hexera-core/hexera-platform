# Responsibility: Assemble the versioned API surface - which routers are mounted, under which prefix and tag.
# Boundaries: mounting only; every endpoint, schema and guard belongs to the module being mounted.
from fastapi import APIRouter

from meshpipeline.api.v1 import (
    admin_billing,
    api_keys,
    billing,
    chat,
    client_config,
    credits,
    geometry,
    organization,
    simulation,
    stripe_webhook,
    upload,
    ws,
)

router = APIRouter(prefix="/api/v1")
router.include_router(upload.router,     prefix="/upload",     tags=["upload"])
router.include_router(client_config.router, prefix="/client-config", tags=["client-config"])
router.include_router(simulation.router, prefix="/simulation", tags=["simulation"])
router.include_router(ws.router,         prefix="/ws",         tags=["stream"])
router.include_router(chat.router,       prefix="/chat",       tags=["chat"])
router.include_router(credits.router,    prefix="/credits",    tags=["credits"])
router.include_router(organization.router, prefix="/organization", tags=["organization"])
router.include_router(api_keys.router,   prefix="/api-keys",   tags=["api-keys"])
router.include_router(geometry.router,   prefix="/geometry",   tags=["geometry"])
router.include_router(billing.router,    prefix="/billing",    tags=["billing"])
# THE CROSS-TENANT SURFACE, mounted apart from /billing because it is a different trust level, not a
# different noun: everything under /billing answers for ONE tenant and scopes on it, while these
# routes deliberately read across all of them behind their own credential (ADMIN_API_KEY).
router.include_router(admin_billing.router, prefix="/admin/billing", tags=["admin"])
# THE WEBHOOK IS NOT UNDER /billing, deliberately. Everything under that prefix answers a proven
# caller presenting our credential; this one answers Stripe, which holds no credential of ours and
# authenticates by signature instead. Separate paths keep that difference visible to anyone reading
# the route table, and let a proxy or WAF treat the public endpoint differently from the private ones.
router.include_router(stripe_webhook.router, prefix="/webhooks/stripe", tags=["webhooks"])
