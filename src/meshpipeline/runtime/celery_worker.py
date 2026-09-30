# Responsibility: Start the pipeline worker: bind adapters per fork, and own the worker's logging, metrics and shutdown lifecycle.
# Boundaries: a process entry point.
from celery.signals import (
    setup_logging,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
    worker_shutting_down,
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
    # ...and a drain marker a previous run of this container left under the same pid, which would
    # otherwise make every job this worker takes hand itself straight back (runtime/worker_drain).
    from meshpipeline.runtime.worker_drain import clear_drain
    clear_drain()
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
    # A job running here polls this while its graph runs, and hands itself back to the queue once
    # the main process has begun shutting down (application/worker_handoff.py).
    from meshpipeline.contracts.worker_drain import set_drain_probe
    from meshpipeline.runtime.worker_drain import drain_requested
    set_drain_probe(drain_requested)


@worker_shutting_down.connect
def _on_worker_shutting_down(**kwargs):
    # Main process, the moment a shutdown begins: SIGTERM from `docker stop`, which is what the
    # fleet's shutdown script and docker's own stop on a VM deletion both send. Celery stops taking
    # new work by itself. This tells the job ALREADY running in a pool process, which a warm
    # shutdown would otherwise wait on for as long as the job takes - far longer than a VM that is
    # being deleted lives. Seeing the marker, the job hands itself back and the next worker runs it.
    from meshpipeline.runtime.worker_drain import request_drain
    request_drain()


class CeleryTap:
    """This worker's own view of its work, from inside its MAIN process (runtime/idle_retire.py).

    `celery.worker.state.reserved_requests` holds every task this worker has received and not yet
    finished - the running one included - and the main process keeps it even when the task runs in
    a prefork pool process. The consumer is the one `worker_ready` hands over; `call_soon` is
    kombu's thread-safe way into its event loop, and is exactly what `celery control
    cancel_consumer` / `add_consumer` do, without the broker round trip."""

    def __init__(self, consumer, queue: str = "simulation_jobs") -> None:
        self._consumer = consumer
        self._queue = queue

    def holding(self) -> int:
        from celery.worker import state
        return len(state.reserved_requests)

    def taking(self) -> bool:
        task_consumer = getattr(self._consumer, "task_consumer", None)
        return bool(task_consumer is not None and task_consumer.consuming_from(self._queue))

    def configured(self) -> bool:
        """Does this worker consume the sizing queue at all (`--queues simulation_jobs`)?"""
        if self.taking():
            return True
        try:
            selected = self._consumer.app.amqp.queues.consume_from
        except AttributeError:
            return False
        return bool(selected) and self._queue in selected

    def stop_taking(self) -> None:
        self._consumer.call_soon(self._consumer.cancel_task_queue, self._queue)

    def resume_taking(self) -> None:
        self._consumer.call_soon(self._consumer.add_task_queue, self._queue)

    def neighbour_holding(self, timeout: float = 3.0) -> int | None:
        """How many checks the geometry worker on this VM is running - it goes down with the VM too.

        Asked of `geometry@<this hostname>` over celery's own inspect channel: deploy/gcp/worker/
        startup.sh gives both containers the VM's hostname so that name is known here. None when
        nobody answers - on a VM with no geometry worker, or one too broken to answer, which then has
        nothing running it could finish."""
        import socket
        node = f"geometry@{socket.gethostname()}"
        try:
            replies = celery_app.control.inspect(destination=[node], timeout=timeout).active() or {}
        except Exception:  # noqa: BLE001 - an unanswerable question is "nobody answered"
            return None
        if node not in replies:
            return None
        return len(replies[node] or [])


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    # Main process: serve the aggregated multiprocess registry over HTTP.
    from meshpipeline.runtime.metrics_server import start_worker_metrics_server
    start_worker_metrics_server()
    # ...and, on a fleet VM, watch for idleness: the autoscaler only scales OUT, so an idle worker
    # removes its own VM from the group rather than letting the group pick a busy one
    # (runtime/idle_retire.py). `sender` is the consumer; a no-op everywhere but a fleet VM.
    from meshpipeline.runtime.idle_retire import SIZING_QUEUE, start_idle_retire
    start_idle_retire(CeleryTap(sender, SIZING_QUEUE))


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**kwargs):
    # Each fork on shutdown: flush its gauges.
    from meshpipeline.runtime.metrics_server import mark_process_dead
    mark_process_dead()


setup_sentry("worker")
