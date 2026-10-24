# Responsibility: Apply the cross-cutting HTTP protections: rate limiting and security headers.
# Boundaries: transport-level protection; it authorises nothing.
from __future__ import annotations

import hmac
import logging
import time

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.rate_limit import incr_window
from meshpipeline.settings import plans

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
# `/api/v1/webhooks/stripe` IS EXEMPT, and it is the only exempt path that is neither a probe nor a
# static file. Stripe delivers in bursts from a small set of source addresses, so every event in a
# burst shares one bucket here and the tail of it would 429. Stripe treats a 429 as a failure,
# retries on a backoff, and DISABLES an endpoint that keeps failing - so throttling this path does
# not shed load, it silently switches billing off.
#
# WHAT THAT COSTS, stated plainly: this is a public endpoint with no limiter in front of it. What
# stands in its place is signature verification, which rejects an unsigned body before it is read as
# data and before any database work happens. That is a cheaper refusal than the limiter's own Redis
# round trip, so an unsigned flood is turned away more cheaply here than it would have been there.
_EXEMPT_PATHS = ("/health", "/readyz", "/metrics", "/static", "/ui",
                 "/api/v1/webhooks/stripe")


def _is_exempt(path: str) -> bool:
    # Segment-aware, never a bare prefix: "/uifoo" is not "/ui", and a sibling of an exempt path
    # does not inherit its exemption. Exact match, or the path is INSIDE that route's subtree.
    return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PATHS)


def client_address(scope) -> str:
    """THE CALLER'S OWN ADDRESS, not the proxy's. Behind Cloud Run every request arrives from the
    front end (169.254.169.126), so the socket address is the same for every caller in the world.
    Google's front end APPENDS the address it saw to X-Forwarded-For, so the trustworthy entry is
    counted from the RIGHT: RATE_LIMIT_TRUSTED_PROXY_HOPS = 1 is the last entry (Cloud Run reached
    directly), 2 the one before it (a load balancer in front of Cloud Run appends its own). Anything
    further left was written by the client and is never trusted - a caller cannot buy itself a fresh
    bucket by sending a made-up X-Forwarded-For. No header (a local run): the socket address."""
    hops = max(1, int(rtcfg.RATE_LIMIT_TRUSTED_PROXY_HOPS))
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            chain = [p.strip() for p in v.decode("latin-1").split(",") if p.strip()]
            if chain:
                return chain[-hops] if len(chain) >= hops else chain[0]
            break
    client = scope.get("client")
    return client[0] if client else "unknown"


def caller_identity(scope) -> tuple[str, bool]:
    """(the bucket a request is counted in, whether the caller PROVED it). Decided before any
    database read, from what the request carries - the credential itself, not the address it came
    from (2026-10-24: keyed on X-User-Id else the client address, every API-key caller and every
    browser behind Cloud Run shared ONE bucket of 240 a minute, so one busy client throttled all):

      a bearer API key      -> its public prefix (mk_live_<id>); proven later, by api/security.py,
                               which also holds the key's owner to its plan's rate
      a signed X-User-Id    -> that user (the signature is checked here; it is one HMAC)
      X-User-Id, no secret  -> that user (dev and single-tenant: identity is self-asserted there)
      anything else         -> the caller's own address (client_address)

    Each user and each key has its own bucket: the users of one organisation never share one, so a
    busy user cannot starve the rest of the account."""
    from meshpipeline.contracts import api_key

    headers = dict(scope.get("headers", []))
    scheme, _, presented = headers.get(b"authorization", b"").decode("latin-1").strip().partition(" ")
    if scheme.lower() == "bearer":
        key = api_key.parse(presented.strip())
        if key is not None:
            return f"key:{key.key_prefix}", False
    user = headers.get(b"x-user-id", b"").decode("latin-1").strip()
    if user:
        import meshpipeline.settings.policy as polcfg

        if not polcfg.USER_TOKEN_SECRET:
            return f"user:{user}", False
        sig = headers.get(b"x-user-sig", b"").decode("latin-1").strip()
        from meshpipeline.api.security import expected_user_sig

        if sig and hmac.compare_digest(sig, expected_user_sig(user)):
            return f"user:{user}", True
    return f"addr:{client_address(scope)}", False


class RateLimitMiddleware:

    _WINDOW_TTL_SECONDS = 90   # > one 60s window, so a window outlives its own counting

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # The DEFAULT plan's rate, from the same authority a keyed caller's plan is read from -
        # this middleware runs before any credential has been examined, so the plan is not knowable
        # here. A caller presenting an API key is additionally held to its own plan's rate, where
        # that plan IS known (api/security.py).
        limit = plans.limits_for(None).rate_limit_per_minute
        if scope["type"] != "http" or limit <= 0:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if _is_exempt(path):
            return await self.app(scope, receive, send)

        ident, proven = caller_identity(scope)
        # THE SAFETY CEILINGS, each its own bucket, each 0 = off:
        #   global   - every caller together: what the service as a whole is ever asked to absorb
        #   address  - one address, for callers that have not proven who they are: a flood of made-up
        #              keys or user ids from one address cannot spread itself over fresh buckets.
        #              A caller with a valid signature is exempt - the console signs for every user
        #              from one address, and it must never throttle all of them as one.
        buckets = [(ident, limit)]
        if not proven and not ident.startswith("addr:"):
            buckets.insert(0, (f"addr:{client_address(scope)}", int(rtcfg.RATE_LIMIT_ADDRESS_CEILING_PER_MINUTE)))
        buckets.insert(0, ("global", int(rtcfg.RATE_LIMIT_GLOBAL_PER_MINUTE)))
        window = int(time.time() // 60)
        over = False
        for bucket, ceiling in buckets:
            if ceiling <= 0:
                continue
            try:
                n = await incr_window(bucket, window, self._WINDOW_TTL_SECONDS)
            except Exception as exc:
                logger.warning("rate limiter unavailable - failing open: %s", exc)
                return await self.app(scope, receive, send)
            if n > ceiling:
                over = True
                break

        if over:
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
