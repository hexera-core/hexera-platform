# Responsibility: Report debited consumption to the billing provider's meter, on a schedule.
# Boundaries: a bounded, resumable sweep - safe to interrupt and safe to run twice. What a job COSTS
#             is settings/policy.py; the reporting itself is application/metering_service.py.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def report_pending_usage() -> dict:
    import asyncio
    return asyncio.run(_report_async())


async def _report_async() -> dict:
    from meshpipeline.application import metering_service
    from meshpipeline.persistence.session import get_db

    # ONE TRANSACTION PER SWEEP, committed at the end. The stamps and the provider calls are not
    # atomic with each other and cannot be - one is a database write and the other is HTTP - so the
    # order inside metering_service is what makes the failure survivable: report first, stamp after.
    # A crash between them re-reports, and the idempotency key makes that harmless.
    async with get_db() as db:
        out = await metering_service.report_pending_usage(db)
    if out.get("skipped"):
        logger.info("meter sweep left %s entr(ies) unreported for the next run", out["skipped"])
    return out
