# Responsibility: Derive the channel and key names a job's events are carried on.
# Boundaries: naming only, in one place so publisher and subscriber cannot disagree.
from __future__ import annotations


def channel_for(job_id: str) -> str:
    return f"jobs:{job_id}:events"


def log_key_for(job_id: str) -> str:
    return f"jobs:{job_id}:eventlog"


def seq_key_for(job_id: str) -> str:
    return f"jobs:{job_id}:eventseq"


def opkey_set_for(job_id: str) -> str:
    return f"jobs:{job_id}:eventops"


def fence_key_for(job_id: str) -> str:
    return f"jobs:{job_id}:eventfence"
