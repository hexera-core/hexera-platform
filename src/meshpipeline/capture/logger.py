# Responsibility: Write one run's training events.
# Boundaries: fail-open: a capture failure logs a warning and never fails the job it was recording.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TrainingLogger:

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def log(self, event_type: str, payload: dict, *,
            attempt: int | None = None, op_id: str | None = None) -> None:
        try:
            from meshpipeline.capture import trace
            attrs: dict = {}
            if attempt is not None:
                attrs["attempt"] = attempt
            if op_id:
                attrs["op_id"] = op_id
            trace.add_event(self.job_id, event_type, payload, attributes=attrs or None)
        except Exception as exc:
            logger.warning("TrainingLogger: failed to emit %r for job %s: %s",
                           event_type, self.job_id, exc)
