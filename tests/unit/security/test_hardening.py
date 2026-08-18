# Responsibility: Verify rate limiting, security headers and error reporting, failing open when the store is down.
from __future__ import annotations

import pytest

import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts import rate_limit


class _FakeRateLimitStore:

    def __init__(self, broken: bool = False):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken = broken

    async def incr_window(self, identity: str, window: int, ttl_seconds: int) -> int:
        if self.broken:
            raise ConnectionError("rate-limit store down")
        key = f"{identity}:{window}"
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] == 1:
            self.ttls[key] = ttl_seconds
        return self.counts[key]


@pytest.fixture(autouse=True)
def _no_store_leak():
    yield
    rate_limit.set_rate_limit_store(None)


def _client(monkeypatch, limit, fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meshpipeline.api.middleware import hardening

    monkeypatch.setattr(rtcfg, "RATE_LIMIT_PER_MINUTE", limit)
    app = FastAPI()

    @app.get("/api/v1/thing")
    async def thing():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.add_middleware(hardening.RateLimitMiddleware)
    app.middleware("http")(hardening.security_headers_middleware)
    client = TestClient(app)
    rate_limit.set_rate_limit_store(fake)     # runtime composition's job, done here by hand
    return client


def test_rate_limit_429_after_threshold(monkeypatch):
    client = _client(monkeypatch, limit=3, fake=_FakeRateLimitStore())
    for _ in range(3):
        assert client.get("/api/v1/thing", headers={"X-User-Id": "u1"}).status_code == 200
    r = client.get("/api/v1/thing", headers={"X-User-Id": "u1"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # a different identity is unaffected
    assert client.get("/api/v1/thing", headers={"X-User-Id": "u2"}).status_code == 200


def test_rate_limit_exempts_health_and_disabled_mode(monkeypatch):
    fake = _FakeRateLimitStore()
    client = _client(monkeypatch, limit=1, fake=fake)
    for _ in range(5):
        assert client.get("/health").status_code == 200   # exempt prefix
    assert not fake.counts                                # never even counted

    client = _client(monkeypatch, limit=0, fake=fake)     # 0 = disabled
    for _ in range(5):
        assert client.get("/api/v1/thing").status_code == 200


def test_rate_limit_fails_open_when_the_store_is_down(monkeypatch):
    client = _client(monkeypatch, limit=1, fake=_FakeRateLimitStore(broken=True))
    for _ in range(4):
        assert client.get("/api/v1/thing", headers={"X-User-Id": "u1"}).status_code == 200


def test_security_headers_present(monkeypatch):
    client = _client(monkeypatch, limit=0, fake=_FakeRateLimitStore())
    r = client.get("/api/v1/thing")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_sentry_noop_without_dsn(monkeypatch):
    from meshpipeline.runtime.observability import sentry as hardening

    monkeypatch.setattr(provcfg, "SENTRY_DSN", "")
    assert hardening.setup_sentry("api") is None          # must not raise


def test_sentry_survives_missing_sdk(monkeypatch):
    import builtins

    from meshpipeline.runtime.observability import sentry as hardening

    monkeypatch.setattr(provcfg, "SENTRY_DSN", "https://x@example.invalid/1")
    real_import = builtins.__import__

    def no_sentry(name, *a, **k):
        if name == "sentry_sdk":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sentry)
    assert hardening.setup_sentry("api") is None          # logs, never raises
