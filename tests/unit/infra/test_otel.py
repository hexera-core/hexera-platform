# Responsibility: Verify tracing is a safe no-op by default and is wired into both the API and the worker.
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_tracing_disabled_by_default_is_a_safe_noop(monkeypatch):
    monkeypatch.delenv("OTEL_TRACES_ENABLED", raising=False)
    from meshpipeline.runtime.observability.otel import setup_tracing, tracing_enabled
    assert tracing_enabled() is False
    setup_tracing()              # no-op - must not raise or import OTel
    setup_tracing(app=object())  # no-op even with an "app"


def test_tracing_wired_into_api_and_worker():
    assert "setup_tracing(app)" in (APP / "runtime" / "api_server.py").read_text()
    assert "setup_tracing()" in (APP / "runtime" / "celery_worker.py").read_text()
