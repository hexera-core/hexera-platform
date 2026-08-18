# Responsibility: Declare how this process reports on itself - log shape, tracing and the optional Langfuse sink.
# Owns: ObservabilitySettings, built once from the catalogue's declared defaults.
# Boundaries: reporting only; it changes no product behaviour and gates no data.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.settings.env import bool_env, optional_env


@dataclass(frozen=True)
class ObservabilitySettings:
    log_level: str
    log_json: bool
    traces_enabled: bool
    traces_exporter: str
    service_name: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str

    @property
    def langfuse_enabled(self) -> bool:
        # All three, or none: a half-configured sink silently drops what it was given.
        return bool(self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_host)


def load_observability() -> ObservabilitySettings:
    return ObservabilitySettings(
        log_level=optional_env("LOG_LEVEL", "INFO").strip().upper(),
        log_json=optional_env("LOG_FORMAT", "").strip().lower() == "json",
        traces_enabled=bool_env("OTEL_TRACES_ENABLED", "false"),
        traces_exporter=optional_env("OTEL_TRACES_EXPORTER", "otlp").strip().lower(),
        service_name=optional_env("OTEL_SERVICE_NAME", "mesh-api"),
        langfuse_public_key=optional_env("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=optional_env("LANGFUSE_SECRET_KEY", ""),
        langfuse_host=optional_env("LANGFUSE_HOST", ""),
    )
