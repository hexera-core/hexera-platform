# Responsibility: Start the pipeline worker: bind adapters per fork, and own the worker's logging and metrics lifecycle.
# Boundaries: a process entry point.
from celery.signals import (
    setup_logging,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
)

from meshpipeline.adapters.pipeline_execution.celery_app import celery_app  # noqa: F401  (celery -A target)
from meshpipeline.runtime.observability.sentry import setup_sentry


@setup_logging.connect
def _configure_logging(**kwargs):
    # Connecting this signal stops Celery hijacking logging; we own the format
    # (human-readable, or JSON when LOG_FORMAT=json - same as the API).
    from meshpipeline.runtime.logging import setup_logging as _setup
    _setup()


@worker_init.connect
def _on_worker_init(**kwargs):
    # Main process, before forking: clear stale per-pid metric files.
    from meshpipeline.runtime.metrics_server import reset_multiproc_dir
    reset_multiproc_dir()
    # Distributed tracing (no-op unless OTEL_TRACES_ENABLED=true): instruments Celery
    # (broker context propagation) + httpx, so a worker job continues the API's trace.
    from meshpipeline.runtime.observability.otel import setup_tracing
    setup_tracing()


@worker_process_init.connect
def _on_worker_process_init(**kwargs):
    # Each fork, before running tasks: runtime composition - bind the product contracts to
    # their settings-selected concrete adapters (LLM router, event stream, object store,
    # search, training export, mesh executor, pipeline launcher).
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()


@worker_ready.connect
def _on_worker_ready(**kwargs):
    # Main process: serve the aggregated multiprocess registry over HTTP.
    from meshpipeline.runtime.metrics_server import start_worker_metrics_server
    start_worker_metrics_server()


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**kwargs):
    # Each fork on shutdown: flush its gauges.
    from meshpipeline.runtime.metrics_server import mark_process_dead
    mark_process_dead()


setup_sentry("worker")
