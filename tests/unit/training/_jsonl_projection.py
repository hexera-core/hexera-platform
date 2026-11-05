# Responsibility: Read a JSONL capture projection back into an event log, for tests.
# Boundaries: a reader over the diagnostic projection, never the durable authority.
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.capture.events import EventLog


def log_from_path(job_id: str, path: Path) -> EventLog:
    records = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue                      # a malformed line contributes no event
    return EventLog.from_records(job_id, records)


def log_from_dir(job_id: str, base_dir) -> EventLog:
    return log_from_path(job_id, Path(base_dir) / job_id / "events.jsonl")
