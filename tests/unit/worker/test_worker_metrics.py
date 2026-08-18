# Responsibility: Verify the metrics exporter is inert without a multiprocess directory and clears stale files.
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_exporter_is_noop_without_multiproc_dir(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    from meshpipeline.runtime import metrics_server
    # all three entry points must be safe no-ops (never raise) when unconfigured
    metrics_server.reset_multiproc_dir()
    metrics_server.start_worker_metrics_server()
    metrics_server.mark_process_dead()


def test_reset_clears_stale_db_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    (tmp_path / "counter_123.db").write_bytes(b"stale")
    (tmp_path / "gauge_456.db").write_bytes(b"stale")
    from meshpipeline.runtime import metrics_server
    metrics_server.reset_multiproc_dir()
    assert list(tmp_path.glob("*.db")) == []


def test_worker_wires_metrics_signals():
    src = (APP / "runtime" / "celery_worker.py").read_text()
    assert "worker_ready" in src and "worker_init" in src
    assert "start_worker_metrics_server" in src
    assert "worker_process_shutdown" in src and "mark_process_dead" in src
    # the circuit gauge is multiprocess-safe
    assert 'multiprocess_mode="max"' in (APP / "metrics.py").read_text()
