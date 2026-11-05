# Responsibility: Set up distributed tracing when it is configured.
# Boundaries: off by default and fully optional; with nothing configured it changes nothing.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_done = False


def tracing_enabled() -> bool:
    from meshpipeline.settings.policy import OBSERVABILITY
    return OBSERVABILITY.traces_enabled


def setup_tracing(app=None) -> None:
    global _done
    if not tracing_enabled() or _done:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        from meshpipeline.settings.policy import OBSERVABILITY
        service = OBSERVABILITY.service_name
        provider = TracerProvider(resource=Resource.create({"service.name": service}))

        if OBSERVABILITY.traces_exporter == "console":
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads OTEL_EXPORTER_OTLP_ENDPOINT

        trace.set_tracer_provider(provider)

        # Cross-process spans: httpx (LLM calls) + Celery (broker context propagation)
        # + SQLAlchemy. Each is best-effort so one missing instrumentor can't break boot.
        for name, _inst in (
            ("httpx",      lambda: __import__("opentelemetry.instrumentation.httpx",
                                              fromlist=["HTTPXClientInstrumentor"]).HTTPXClientInstrumentor().instrument()),
            ("celery",     lambda: __import__("opentelemetry.instrumentation.celery",
                                              fromlist=["CeleryInstrumentor"]).CeleryInstrumentor().instrument()),
            ("sqlalchemy", lambda: __import__("opentelemetry.instrumentation.sqlalchemy",
                                              fromlist=["SQLAlchemyInstrumentor"]).SQLAlchemyInstrumentor().instrument()),
        ):
            try:
                _inst()
            except Exception as exc:
                logger.debug("otel: %s instrumentation skipped (%s)", name, exc)

        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)

        _done = True
        logger.info("OpenTelemetry tracing enabled (service=%s exporter=%s)",
                    service, OBSERVABILITY.traces_exporter)
    except Exception as exc:
        logger.warning("OpenTelemetry setup failed (%s) - tracing disabled", exc)
