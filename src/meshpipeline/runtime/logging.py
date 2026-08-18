# Responsibility: Configure how every process logs, in human or JSON form.
# Boundaries: formatting and level only.
from __future__ import annotations

import json
import logging
import sys

_PLAIN = "%(asctime)s %(levelname)s %(name)s - %(message)s"
_CORRELATION_FIELDS = ("job_id", "owner_id", "request_id", "engine")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in _CORRELATION_FIELDS:
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging() -> None:
    from meshpipeline.settings.policy import OBSERVABILITY
    level = getattr(logging, OBSERVABILITY.log_level, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if OBSERVABILITY.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_PLAIN))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
