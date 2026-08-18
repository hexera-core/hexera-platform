# Responsibility: Verify readiness reports 200 or 503 from the probe, and a hard dependency down makes it unready.
from __future__ import annotations

import sys
import types

import pytest

# prometheus-fastapi-instrumentator is a real dependency (requirements/runtime.txt), present in CI and
# every runtime image; it is only absent from the thin local dev venv. Stub it IF MISSING so the
# real app + probe import here - the same pattern the import sweep uses. Where the real package
# is installed it is used unchanged.
if "prometheus_fastapi_instrumentator" not in sys.modules:
    try:
        import prometheus_fastapi_instrumentator  # noqa: F401
    except ImportError:
        _pfi = types.ModuleType("prometheus_fastapi_instrumentator")

        class _Chain:
            def __call__(self, *a, **k): return self
            def __getattr__(self, n): return self

        _pfi.Instrumentator = _Chain()
        sys.modules["prometheus_fastapi_instrumentator"] = _pfi

from fastapi.testclient import TestClient  # noqa: E402

import meshpipeline.runtime.api_server as srv  # noqa: E402  (the real probe)
from meshpipeline.api.app import app, set_readiness_probe  # noqa: E402  (the real route)
from meshpipeline.contracts import object_storage  # noqa: E402


# the ROUTE: hard_ok maps to the HTTP status contract
@pytest.fixture
def _restore_probe():
    yield
    set_readiness_probe(None)


def test_readyz_returns_200_and_ready_when_the_probe_reports_healthy(_restore_probe):
    async def _ok():
        return True, {"status": "ready", "checks": {"postgres": "ok", "redis": "ok",
                                                     "object_store": "ok"}, "circuits": {}}
    set_readiness_probe(_ok)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readyz_returns_503_and_not_ready_when_the_probe_reports_unhealthy(_restore_probe):
    async def _down():
        return False, {"status": "not_ready", "checks": {"postgres": "down: OSError"},
                       "circuits": {}}
    set_readiness_probe(_down)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"


# the PROBE: each hard dependency down => not ready
class _FakeConn:
    async def execute(self, *a, **k): return None


class _FakeConnCtx:
    async def __aenter__(self): return _FakeConn()
    async def __aexit__(self, *a): return False


class _FakeEngine:
    def connect(self): return _FakeConnCtx()


class _FakeRedis:
    async def ping(self): return True
    async def aclose(self): return None


class _FakeStore:
    def __init__(self, ok: bool): self.ok = ok
    def exists(self, *, object_key: str) -> bool:
        if not self.ok:
            raise OSError("object store unreachable")
        return False


def _wire(monkeypatch, *, pg=True, redis=True, store=True):
    if pg:
        monkeypatch.setattr("meshpipeline.persistence.session._get_engine", lambda: _FakeEngine())
    else:
        def _pg_down():
            raise OSError("postgres unreachable")
        monkeypatch.setattr("meshpipeline.persistence.session._get_engine", _pg_down)

    if redis:
        monkeypatch.setattr("meshpipeline.adapters._shared.redis_client.async_client",
                            lambda **k: _FakeRedis())
    else:
        def _redis_down(**k):
            raise OSError("redis unreachable")
        monkeypatch.setattr("meshpipeline.adapters._shared.redis_client.async_client", _redis_down)

    object_storage.set_object_store(_FakeStore(ok=store))


async def test_probe_is_ready_when_every_hard_dependency_is_healthy(monkeypatch):
    _wire(monkeypatch, pg=True, redis=True, store=True)
    hard_ok, body = await srv._readiness_probe()
    assert hard_ok is True
    assert body["status"] == "ready"
    assert all(v == "ok" for v in body["checks"].values())


@pytest.mark.parametrize("down", ["pg", "redis", "store"])
async def test_probe_is_not_ready_when_a_hard_dependency_is_down(monkeypatch, down):
    _wire(monkeypatch, pg=(down != "pg"), redis=(down != "redis"), store=(down != "store"))
    hard_ok, body = await srv._readiness_probe()
    assert hard_ok is False, f"{down} down but the probe still reported ready"
    assert body["status"] == "not_ready"
    key = {"pg": "postgres", "redis": "redis", "store": "object_store"}[down]
    assert body["checks"][key].startswith("down"), body["checks"]
