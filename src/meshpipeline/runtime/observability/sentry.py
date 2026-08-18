# Responsibility: Set up error reporting when a DSN is configured.
# Boundaries: optional; a missing SDK is reported and disables reporting rather than failing the process.
from __future__ import annotations

import logging

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg

logger = logging.getLogger(__name__)


def setup_sentry(role: str) -> None:
    if not provcfg.SENTRY_DSN:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.error("SENTRY_DSN is set but sentry-sdk is not installed - "
                     "error tracking DISABLED")
        return
    sentry_sdk.init(
        dsn=provcfg.SENTRY_DSN,
        environment=polcfg.ENV,
        traces_sample_rate=0.0,      # tracing is OTel's job (see otel.py)
        send_default_pii=False,
    )
    sentry_sdk.set_tag("service.role", role)
    logger.info("Sentry error tracking enabled (role=%s, env=%s)", role, polcfg.ENV)
