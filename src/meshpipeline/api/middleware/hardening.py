# Responsibility: Apply the cross-cutting HTTP protections: rate limiting and security headers.
# Boundaries: transport-level protection; it authorises nothing.
from __future__ import annotations

import logging
import time

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.rate_limit import incr_window

logger = logging.getLogger(__name__)

# Never throttled: liveness/readiness, the metrics scrape, and the SPA with its assets. Each is
# either a probe an orchestrator makes constantly or a static file, and none of them costs
# anything a limit protects.
#
# `/api/v1/ws` USED to be here, for the long-lived socket. It never needed to be: this middleware
# returns immediately for any scope that is not `http`, so a WebSocket upgrade never reaches the
# limiter in the first place. What the entry actually did was exempt the ticket-minting POST -
# an authenticated endpoint that signs a ticket and reads the database - and, because the match
# was a bare startswith, anything else beginning with those characters (/api/v1/wsfoo). It is
# gone; ticket minting is ordinary authenticated API traffic and is limited as such.
_EXEMPT_PATHS = ("/health", "/readyz", "/metrics", "/static", "/ui")


def _is_exempt(path: str) -> bool:
    # Segment-aware, never a bare prefix: "/uifoo" is not "/ui", and a sibling of an exempt path
    # does not inherit its exemption. Exact match, or the path is INSIDE that route's subtree.
    return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PATHS)


class RateLimitMiddleware:

    _WINDOW_TTL_SECONDS = 90   # > one 60s window, so a window outlives its own counting

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = rtcfg.RATE_LIMIT_PER_MINUTE
        if scope["type"] != "http" or limit <= 0:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if _is_exempt(path):
            return await self.app(scope, receive, send)

        ident = ""
        for k, v in scope.get("headers", []):
            if k == b"x-user-id":
                ident = v.decode("latin-1")
                break
        if not ident:
            client = scope.get("client")
            ident = client[0] if client else "unknown"

        window = int(time.time() // 60)
        try:
            n = await incr_window(ident, window, self._WINDOW_TTL_SECONDS)
        except Exception as exc:
            logger.warning("rate limiter unavailable - failing open: %s", exc)
            return await self.app(scope, receive, send)

        if n > limit:
            retry = 60 - int(time.time() % 60)
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"retry-after", str(retry).encode())]})
            await send({"type": "http.response.body",
                        "body": b'{"detail":"Rate limit exceeded, retry shortly"}'})
            return
        return await self.app(scope, receive, send)


async def security_headers_middleware(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp
