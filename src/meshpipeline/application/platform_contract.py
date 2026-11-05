# Responsibility: Say which version of the cross-process contract this build speaks, and hold a job when the side that would run it speaks another.
# Owns: the run contract fingerprint (API -> worker), the Celery header it travels in, and the "platform is updating" wait.
# Boundaries: decides nothing about the job itself; it only keeps a job from running on code that cannot read it.
# Collaborates with: adapters/pipeline_execution/celery.py (stamps and checks the header), contracts/mesh_execution.py (the worker <-> mesh job half).
"""THE PLATFORM CONTRACT (25 Oct: dev-areen's API and mesh job moved to a new build while the worker
fleet stayed on the old one).

Merges to main deploy the API and the mesh job but not the fleet (deploy.yml: images, migrate,
console, admin), so on shared dev the two sides run different builds most of the time. That is safe
while they agree on what passes between them, and only then. The RUN CONTRACT is a fingerprint of
exactly that, derived from the code so nobody has to remember to bump it:

  * the dispatch payload's schema version;
  * the run entry's parameter names - a kwarg the API adds is a TypeError in an old worker;
  * the pipeline state's schema version (the checkpoint a takeover resumes);
  * the final result's schema version (what the worker writes and the API reads).

It is never the build: two builds that agree run each other's jobs. A worker handed a job stamped
with ANOTHER contract does not run it: it tells the user the platform is updating, waits a little
and puts the job back on the queue for a worker that matches. A job with no stamp (sent by an API
from before this existed) runs as it always did.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from functools import lru_cache

logger = logging.getLogger(__name__)

#: The Celery message header the run contract travels in. Unknown headers are ignored by Celery, so
#: a worker from before this existed simply runs the job.
HEADER = "hexera_contract"
#: How long a worker holds a job it cannot run before putting it back on the queue. Long enough not
#: to spin the queue, short enough that a newly rolled worker picks it up soon.
HOLD_S = 30.0
#: What the user is told, once per pair of contracts (the op id dedupes the repeats).
UPDATING_NOTE = ("The platform is updating. Your job is waiting for it to finish and will start by "
                 "itself - nothing is needed from you.")


@lru_cache(maxsize=1)
def run_contract() -> str:
    """The run contract this build speaks: 12 hex characters."""
    from meshpipeline.application import (
        dispatch_contract,
        pipeline_run,  # noqa: F401 - registers the run entry
    )
    from meshpipeline.application.final_result import FINAL_RESULT_SCHEMA_VERSION
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION

    facts = {"dispatch": dispatch_contract.DISPATCH_SCHEMA_VERSION,
             "run_params": sorted(dispatch_contract.accepted_keys()),
             "state": STATE_SCHEMA_VERSION,
             "final_result": FINAL_RESULT_SCHEMA_VERSION}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:12]


def headers() -> dict[str, str]:
    """The headers a launch stamps on the job's message."""
    return {HEADER: run_contract()}


def message_contract(request) -> str:
    """The contract a Celery request was stamped with; "" when none (an older API sent it)."""
    if request is None:
        return ""
    seen = getattr(request, HEADER, None)
    if not seen:
        seen = (getattr(request, "headers", None) or {}).get(HEADER)
    return str(seen or "")


def mismatch(seen: str) -> bool:
    """True when the job was stamped with a contract this build does not speak."""
    return bool(seen) and seen != run_contract()


def hold_for_update(job_id: str, *, seen: str, requeue: Callable[[], None],
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """THE JOB WAITS, IT DOES NOT FAIL. Tell the user, wait HOLD_S, put the job back on the queue.
    Nothing about the job is changed: it stays queued, its delivery count is untouched (the run never
    started), and the next worker that speaks its contract runs it."""
    ours = run_contract()
    logger.warning("job %s was sent with platform contract %s but this worker speaks %s - the "
                   "platform is mid-update; holding it %.0f s and putting it back on the queue",
                   job_id, seen, ours, HOLD_S)
    # The maintenance channel, as for a job moved to another machine: this worker does not own the
    # job and never claimed it, so it does not speak AS the run. Best effort, never raises.
    from meshpipeline.application.maintenance.cleanup import publish_job_note
    publish_job_note(job_id, UPDATING_NOTE, f"platform_updating:{seen}:{ours}")
    sleep(HOLD_S)
    requeue()
    return {"job_id": job_id, "status": "waiting_for_platform_update",
            "sent_with": seen, "worker_speaks": ours}


__all__ = ["HEADER", "HOLD_S", "UPDATING_NOTE", "headers", "hold_for_update", "message_contract",
           "mismatch", "run_contract"]
