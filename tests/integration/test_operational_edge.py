# Responsibility: Verify the hardened operational surface is absent and ticket minting is rate-limited.
# Boundaries: the real application over real Redis and PostgreSQL; only the environment is varied.

# Two contracts meet here. Documentation, schema and metrics are DEVELOPMENT conveniences and must
# not exist in a hardened deployment - as an ordinary 404, never a 401, because an authentication
# challenge confirms the route is there. And minting a WebSocket ticket is authenticated API work
# that signs a ticket and reads the database, so it is ordinary rate-limited traffic; the socket
# itself is not, because the HTTP limiter never sees a websocket scope at all.
from __future__ import annotations

import os
import uuid

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meshpipeline.settings.policy import _AUTH_OPTIONAL_ENVS

pytestmark = pytest.mark.asyncio

#: Derived from the policy authority, never restated: every declared development name, plus the
#: spellings whose classification the operator most often gets wrong.
DEV_ENVS = sorted(_AUTH_OPTIONAL_ENVS)
HARDENED_ENVS = ["production", "staging", "prod", "hosted", "aurora-eu-west-1", " dev "]

OPERATIONAL = ["/api/docs", "/api/redoc", "/openapi.json", "/metrics"]
PUBLIC = ["/health", "/readyz"]


@pytest.fixture()
def edge(monkeypatch):
    # Building the app for a named environment means writing the environment and re-importing the
    # package, because the routes a hardened instance registers are decided at construction. Both
    # are process-wide, so this fixture OWNS them and gives back exactly what it found.
    #
    # Leaking either is not a tidiness problem, it is a broken tier: an `ENV=production` left in
    # os.environ made the suite's own schema fixture refuse to migrate a local database, and every
    # later test in the process errored in setup. sys.modules is restored to the ORIGINAL module
    # objects rather than merely emptied, so anything that imported meshpipeline before this test
    # keeps the very classes it already holds.
    import importlib
    import sys
    saved_modules = {k: v for k, v in sys.modules.items() if k.startswith("meshpipeline")}

    def build(env: str, **extra):
        base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x",
                "ENV": env, "MESH_API_KEY": "k" * 32, "USER_TOKEN_SECRET": "s" * 32,
                "CORS_ORIGINS": "https://app.example.com"}
        base.update(extra)
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        for mod in [m for m in list(sys.modules) if m.startswith("meshpipeline")]:
            del sys.modules[mod]
        return importlib.import_module("meshpipeline.api.app")

    yield build

    for mod in [m for m in list(sys.modules) if m.startswith("meshpipeline")]:
        del sys.modules[mod]
    sys.modules.update(saved_modules)


def _client(app_mod) -> TestClient:
    return TestClient(app_mod.app, raise_server_exceptions=False)


# the operational surface, by environment


@pytest.mark.parametrize("env", DEV_ENVS)
async def test_development_keeps_the_operational_surface(env, edge):
    c = _client(edge(env))
    for path in OPERATIONAL + PUBLIC:
        assert c.get(path).status_code == 200, f"{env}: {path} is not available in development"


@pytest.mark.parametrize("env", HARDENED_ENVS)
async def test_a_hardened_environment_hides_the_operational_surface(env, edge):
    c = _client(edge(env))
    for path in OPERATIONAL:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 404, f"{env}: {path} returned {r.status_code}, not 404"
        # a challenge would confirm the route exists
        assert "www-authenticate" not in {k.lower() for k in r.headers}, (
            f"{env}: {path} advertised authentication instead of being absent")
    for path in PUBLIC:
        assert c.get(path).status_code == 200, f"{env}: {path} must stay public"


@pytest.mark.parametrize("env", HARDENED_ENVS)
async def test_a_hardened_router_registers_no_operational_route(env, edge):
    app = edge(env).app
    paths = {getattr(r, "path", "") for r in app.routes}
    for hidden in ("/api/docs", "/api/redoc", "/openapi.json", "/metrics",
                   "/docs/oauth2-redirect"):
        assert hidden not in paths, f"{env}: {hidden} is still registered"
    assert "/health" in paths and "/readyz" in paths


@pytest.mark.parametrize("variant", [
    "/api/docs/", "/metrics/", "/api/%64ocs", "/API/DOCS", "/api//docs",
    "/openapi.json/", "/docs", "/redoc", "/api/openapi.json", "/docs/oauth2-redirect",
])
async def test_no_alias_or_redirect_reaches_a_hidden_surface(variant, edge):
    c = _client(edge("production"))
    r = c.get(variant, follow_redirects=False)
    assert r.status_code == 404, (
        f"{variant} returned {r.status_code} ({r.headers.get('location','')}) - a redirect to a "
        "hidden path is not a 404")


async def test_development_schema_still_describes_the_registered_api(edge):
    c = _client(edge("dev"))
    schema = c.get("/openapi.json").json()
    documented = set(schema["paths"])
    assert "/api/v1/ws/ticket" in documented, "the schema stopped describing the real API"
    assert "/metrics" not in documented, "the scrape endpoint is advertised in the schema"


async def test_development_metrics_still_record_and_serve(edge):
    c = _client(edge("dev"))
    c.get("/health")
    body = c.get("/metrics").text
    assert "http_requests_total" in body, "instrumentation stopped recording"


async def test_hardened_instrumentation_still_runs_without_the_endpoint(edge):
    # Hiding the scrape endpoint must not switch off the middleware that records requests.
    c = _client(edge("production"))
    assert c.get("/health").status_code == 200
    assert c.get("/metrics").status_code == 404
    # a normal API call still flows through the stack
    assert c.get("/api/v1/simulation/" + str(uuid.uuid4())).status_code in (401, 404, 422)


async def test_a_hidden_route_reveals_nothing_about_itself(edge):
    c = _client(edge("production"))
    r = c.get("/api/docs")
    body = r.text.lower()
    for leak in ("disabled", "hardened", "production", "environment", "openapi", "swagger",
                 "k" * 32, "s" * 32, "postgres", "redis", "minio", "/srv"):
        assert leak not in body, f"the 404 body mentions {leak!r}"
    assert len(r.content) < 200, "the 404 body is larger than the ordinary shape"


# WebSocket ticket minting is ordinary authenticated API traffic


#: The services the repository's own integration runner provides, so this file needs no special
#: environment to take part in that tier. The MESH_EDGE_* names exist for running this one file
#: outside the runner and are only consulted when the tier's variables are absent.
TASK_REDIS = os.environ.get("REDIS_URL") or os.environ.get("MESH_EDGE_REDIS_URL", "")
TASK_DB = os.environ.get("DATABASE_URL") or os.environ.get("MESH_EDGE_DATABASE_URL", "")

_needs_services = pytest.mark.skipif(
    not (TASK_REDIS and TASK_DB),
    reason="a real Redis and PostgreSQL are required - run `make test-integration`")


@pytest.fixture()
async def ticket_app(monkeypatch):
    # Production factory, production limiter, production ticket authority, real Redis, real
    # PostgreSQL. A task-specific limit makes the boundary deterministic without touching the
    # production default; the identity is unique so the bucket cannot collide with other traffic.
    # Environment and sys.modules are owned and handed back, for the reason given on `edge`.
    import importlib
    import sys
    saved_modules = {k: v for k, v in sys.modules.items() if k.startswith("meshpipeline")}
    for k, v in {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "ENV": "dev",
                 "REDIS_URL": TASK_REDIS, "DATABASE_URL": TASK_DB,
                 "RATE_LIMIT_PER_MINUTE": "3", "MESH_API_KEY": "", "USER_TOKEN_SECRET": ""}.items():
        monkeypatch.setenv(k, v)
    for mod in [m for m in list(sys.modules) if m.startswith("meshpipeline")]:
        del sys.modules[mod]

    # The REAL migration path, not metadata.create_all: the production migration authority
    # refuses a schema that carries Hexera tables without an alembic_version row, and it is right
    # to - a hand-built schema is one nobody can say a revision for.
    from tests import harness_provisioning as hp
    await hp.ensure_schema(TASK_DB)

    from meshpipeline.runtime.composition import install_adapters
    install_adapters()
    app_mod = importlib.import_module("meshpipeline.api.app")

    import httpx
    import redis.asyncio as aioredis
    r = aioredis.from_url(TASK_REDIS, decode_responses=True)

    # httpx over the ASGI app rather than TestClient: TestClient runs each request in its own
    # portal loop, and the process-cached async engine binds pooled connections to the loop that
    # opened them - so every second request died with "attached to a different loop". That is a
    # harness artefact, not the server's behaviour (uvicorn runs one loop per process), and using
    # one loop here removes it without weakening anything the test asserts.
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app),
                               base_url="http://edge.test")

    class H:
        def __init__(self):
            self.client = client
            self.ws_app = app_mod.app
            self.redis = r
            self.limit = 3

        async def post_ticket(self, ident: str) -> int:
            resp = await client.post("/api/v1/ws/ticket", json={"job_id": str(uuid.uuid4())},
                                     headers={"X-User-Id": ident})
            return resp.status_code

        async def keys(self, pattern: str) -> list[str]:
            return sorted(await self.redis.keys(pattern))

    h = H()
    yield h
    await client.aclose()
    await r.aclose()
    from meshpipeline.persistence.session import dispose_engine
    await dispose_engine()
    for mod in [m for m in list(sys.modules) if m.startswith("meshpipeline")]:
        del sys.modules[mod]
    sys.modules.update(saved_modules)


def _ticket_body() -> dict:
    return {"job_id": str(uuid.uuid4())}


@_needs_services
async def test_ticket_minting_is_rate_limited_at_the_exact_boundary(ticket_app):
    h = ticket_app
    ident = f"task-{uuid.uuid4()}"
    codes = [await h.post_ticket(ident) for _ in range(h.limit + 2)]
    # the job does not belong to this identity, so a permitted request is a 404 - what matters is
    # that it was PROCESSED, and that the ones past the limit were not
    assert codes[:h.limit] == [404] * h.limit, f"permitted requests were rejected: {codes}"
    assert codes[h.limit:] == [429, 429], f"the limit was not enforced exactly: {codes}"


@_needs_services
async def test_a_rate_limited_ticket_request_mints_nothing(ticket_app):
    h = ticket_app
    ident = f"task-{uuid.uuid4()}"
    before = await h.keys("wsticket:*")
    for _ in range(h.limit + 3):
        await h.post_ticket(ident)
    after = await h.keys("wsticket:*")
    assert after == before, f"a throttled request still created ticket state: {set(after) - set(before)}"


@_needs_services
async def test_one_identity_is_not_charged_to_another(ticket_app):
    h = ticket_app
    a, b = f"task-{uuid.uuid4()}", f"task-{uuid.uuid4()}"
    for _ in range(h.limit + 1):
        await h.post_ticket(a)
    assert await h.post_ticket(b) != 429, (
        "a second identity was charged to the first identity's bucket")


@_needs_services
async def test_the_throttled_response_carries_no_internals(ticket_app):
    h = ticket_app
    ident = f"task-{uuid.uuid4()}"
    headers = {"X-User-Id": ident}
    last = None
    for _ in range(h.limit + 2):
        last = await h.client.post("/api/v1/ws/ticket", json={"job_id": str(uuid.uuid4())},
                                   headers=headers)
    assert last.status_code == 429
    body = last.text.lower()
    for leak in ("wsticket", "ticket", "redis", ident.lower(), "ratelimit:", "postgres", "secret"):
        assert leak not in body, f"the 429 body leaked {leak!r}"
    assert "retry-after" in {k.lower() for k in last.headers}, "the established retry guidance is gone"


@_needs_services
async def test_an_ordinary_api_route_keeps_its_existing_limit(ticket_app):
    h = ticket_app
    ident = f"task-{uuid.uuid4()}"
    codes = [(await h.client.get(f"/api/v1/simulation/{uuid.uuid4()}",
                                 headers={"X-User-Id": ident})).status_code
             for _ in range(h.limit + 2)]
    assert codes[-1] == 429, f"a non-ticket route stopped being limited: {codes}"


@_needs_services
async def test_an_exempt_probe_is_never_throttled(ticket_app):
    h = ticket_app
    codes = [(await h.client.get("/health")).status_code for _ in range(h.limit * 3)]
    assert set(codes) == {200}, f"a liveness probe was throttled: {set(codes)}"


@_needs_services
async def test_the_websocket_connection_is_not_ordinary_http_traffic(ticket_app):
    # Continuity: the socket still works after the exemption was removed, because the limiter
    # never sees a websocket scope at all.
    #
    # The budget is exhausted on THE IDENTITY THE SOCKET ITSELF PRESENTS, and the exhaustion is
    # confirmed (429) before the socket is opened. Exhausting some other bucket - a different
    # header, or the client address while the socket sends a header - would leave the socket in
    # an untouched bucket, and the test would pass whether or not websockets are exempt.
    # The budget is spent on an unrouted path: the limiter runs before routing, so the request is
    # counted, and nothing else in the application is involved.
    h = ticket_app
    ident = f"task-{uuid.uuid4()}"
    hdr = {"X-User-Id": ident}
    # ONE portal, entered for the whole exchange. A bare TestClient runs every request in a fresh
    # event loop, and the limiter's cached Redis client belongs to the loop that built it - so
    # alternate requests raised, the limiter FAILED OPEN, and the socket was never counted. That
    # would let this test pass whether websockets are exempt or not.
    with TestClient(h.ws_app, raise_server_exceptions=False) as ws_client:
        codes = [ws_client.get("/api/v1/no-such-route", headers=hdr).status_code
                 for _ in range(h.limit + 2)]
        assert codes == [404, 404, 404, 429, 429], (
            f"the budget was not spent one request at a time: {codes}")

        with pytest.raises(WebSocketDisconnect) as exc:
            with ws_client.websocket_connect(f"/api/v1/ws/{uuid.uuid4()}/stream?ticket=nope",
                                             headers=hdr):
                pass
    # 1008 is the application's own ticket policy answering. Being refused by the rate limiter
    # instead would not reach the route at all.
    assert exc.value.code == 1008, (
        f"the socket was closed with {exc.value.code}, not by the ticket policy - the HTTP "
        "limiter rejected a WebSocket upgrade")
