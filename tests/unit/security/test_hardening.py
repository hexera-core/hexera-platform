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


# ---------------------------------------------------------------- per-caller buckets (2026-10-24) ----
# Behind Cloud Run every request's socket address is the front end's (169.254.169.126) and API-key
# callers send no X-User-Id, so keying on "X-User-Id, else the client address" put every caller in
# the world in ONE bucket of RATE_LIMIT_PER_MINUTE. Each caller now has its own.
PROXY = "169.254.169.126"


def _key() -> str:
    from meshpipeline.contracts import api_key

    return api_key.mint().presented


def _limits(monkeypatch, *, global_=0, address=0, hops=1):
    monkeypatch.setattr(rtcfg, "RATE_LIMIT_GLOBAL_PER_MINUTE", global_)
    monkeypatch.setattr(rtcfg, "RATE_LIMIT_ADDRESS_CEILING_PER_MINUTE", address)
    monkeypatch.setattr(rtcfg, "RATE_LIMIT_TRUSTED_PROXY_HOPS", hops)


def _get(client, **headers):
    return client.get("/api/v1/thing", headers=headers).status_code


def test_two_api_keys_behind_the_same_proxy_do_not_share_a_bucket(monkeypatch):
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=3, fake=_FakeRateLimitStore())
    a, b = _key(), _key()
    for _ in range(3):
        assert _get(client, Authorization=f"Bearer {a}", **{"X-Forwarded-For": "203.0.113.7"}) == 200
    # key A is over its limit ...
    assert _get(client, Authorization=f"Bearer {a}", **{"X-Forwarded-For": "203.0.113.7"}) == 429
    # ... and key B, from the very same address, is not affected
    for _ in range(3):
        assert _get(client, Authorization=f"Bearer {b}", **{"X-Forwarded-For": "203.0.113.7"}) == 200


def test_anonymous_callers_are_told_apart_by_their_own_address_not_the_proxys(monkeypatch):
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=2, fake=_FakeRateLimitStore())
    for _ in range(2):
        assert _get(client, **{"X-Forwarded-For": "198.51.100.1"}) == 200
    assert _get(client, **{"X-Forwarded-For": "198.51.100.1"}) == 429
    assert _get(client, **{"X-Forwarded-For": "198.51.100.2"}) == 200


def test_a_made_up_forwarded_for_buys_no_fresh_bucket(monkeypatch):
    """Google's front end APPENDS the address it saw: only the right-most entry is trusted, so a
    caller that writes its own X-Forwarded-For is still counted at its real address."""
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=2, fake=_FakeRateLimitStore())
    codes = [_get(client, **{"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.9"}) for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_behind_a_load_balancer_the_second_entry_from_the_right_is_the_caller(monkeypatch):
    _limits(monkeypatch, hops=2)
    client = _client(monkeypatch, limit=1, fake=_FakeRateLimitStore())
    assert _get(client, **{"X-Forwarded-For": "198.51.100.3, 35.1.2.3"}) == 200
    assert _get(client, **{"X-Forwarded-For": "198.51.100.4, 35.1.2.3"}) == 200     # same LB, another caller
    assert _get(client, **{"X-Forwarded-For": "198.51.100.3, 35.1.2.3"}) == 429


def test_signed_users_each_have_a_bucket_and_a_forged_signature_does_not(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.api.security import expected_user_sig

    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "s3cret")
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=1, fake=_FakeRateLimitStore())
    u1 = {"X-User-Id": "u1", "X-User-Sig": expected_user_sig("u1")}
    u2 = {"X-User-Id": "u2", "X-User-Sig": expected_user_sig("u2")}
    assert _get(client, **u1) == 200 and _get(client, **u2) == 200
    assert _get(client, **u1) == 429
    # a made-up user id with no valid signature is counted at its address, not in a bucket of its own
    assert _get(client, **{"X-User-Id": "x1", "X-User-Sig": "nope", "X-Forwarded-For": "198.51.100.5"}) == 200
    assert _get(client, **{"X-User-Id": "x2", "X-User-Sig": "nope", "X-Forwarded-For": "198.51.100.5"}) == 429


def test_the_address_ceiling_stops_a_flood_of_made_up_keys_but_never_signed_users(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.api.security import expected_user_sig

    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "s3cret")
    _limits(monkeypatch, address=3)
    client = _client(monkeypatch, limit=100, fake=_FakeRateLimitStore())
    codes = [_get(client, Authorization=f"Bearer {_key()}", **{"X-Forwarded-For": "198.51.100.6"}) for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    # the console signs for every user from one address: none of them is held to the address ceiling
    for i in range(6):
        u = f"user{i}"
        assert _get(client, **{"X-User-Id": u, "X-User-Sig": expected_user_sig(u),
                               "X-Forwarded-For": "198.51.100.6"}) == 200


def test_the_global_ceiling_holds_every_caller_together(monkeypatch):
    _limits(monkeypatch, global_=4)
    client = _client(monkeypatch, limit=100, fake=_FakeRateLimitStore())
    codes = [_get(client, Authorization=f"Bearer {_key()}", **{"X-Forwarded-For": f"198.51.100.{i}"})
             for i in range(6)]
    assert codes == [200, 200, 200, 200, 429, 429]


def test_a_strangers_wrong_secret_under_a_real_keys_prefix_does_not_spend_its_bucket(monkeypatch):
    """A key's public prefix is shown in the console and in logs: the bucket is the whole key."""
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=2, fake=_FakeRateLimitStore())
    real = _key()
    prefix = real.rsplit("_", 1)[0]
    for i in range(3):
        _get(client, Authorization=f"Bearer {prefix}_wrongsecret{i}", **{"X-Forwarded-For": "198.51.100.66"})
    assert _get(client, Authorization=f"Bearer {real}", **{"X-Forwarded-For": "203.0.113.8"}) == 200


def test_the_ceilings_still_run_when_the_per_caller_rate_is_off(monkeypatch):
    _limits(monkeypatch, global_=2)
    client = _client(monkeypatch, limit=0, fake=_FakeRateLimitStore())
    codes = [_get(client, **{"X-Forwarded-For": f"198.51.100.{i}"}) for i in range(3)]
    assert codes == [200, 200, 429]


def test_an_anonymous_address_is_held_to_the_lower_of_its_rate_and_the_address_ceiling(monkeypatch):
    _limits(monkeypatch, address=2)
    client = _client(monkeypatch, limit=100, fake=_FakeRateLimitStore())
    codes = [_get(client, **{"X-Forwarded-For": "198.51.100.77"}) for _ in range(3)]
    assert codes == [200, 200, 429]


def test_a_signature_with_bytes_no_hex_digest_has_is_refused_never_an_error(monkeypatch):
    import meshpipeline.settings.policy as polcfg

    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "s3cret")
    _limits(monkeypatch)
    client = _client(monkeypatch, limit=5, fake=_FakeRateLimitStore())
    r = client.get("/api/v1/thing", headers=[("X-User-Id", "u1"), ("X-User-Sig", "café".encode("latin-1"))])
    assert r.status_code == 200          # counted at its address, like any unproven caller
