# Responsibility: Expose measuring an uploaded geometry as a Celery task, and hand one to the queue.
# Boundaries: task registration and the enqueue call; the work itself is application/geometry_measurement.py.
from __future__ import annotations

import logging

from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
from meshpipeline.application import geometry_measurement as _measure

logger = logging.getLogger(__name__)


# MAX_RETRIES 0, on purpose. A measurement that failed on the file will fail on the file again, and
# a measurement that failed on a dependency is not worth a retry budget either: nothing downstream
# is blocked by its absence, and the customer's conversation has already moved on. The row records
# what happened, which is the thing a retry was going to be diagnosed from anyway.
@celery_app.task(name="tasks.geometry.measure_source", bind=False, max_retries=0,
                 soft_time_limit=1200, time_limit=1500, queue="geometry_measurement")
def measure_source(source_id: str, owner_id: str, purpose: str = _measure.DEFAULT_PURPOSE) -> dict:
    return _measure.measure_and_store_blocking(source_id, owner_id, purpose=purpose)


def enqueue(source_id: str, owner_id: str, *, purpose: str) -> None:
    measure_source.apply_async(args=[str(source_id), str(owner_id)], kwargs={"purpose": purpose},
                               queue="geometry_measurement")
    logger.info("geometry measurement queued - source_id=%s", source_id)


# MAX_RETRIES 0 for the same reason as the measurement, and one more: a look costs a provider call, and
# a task that retries a provider outage three times is three calls for one description nobody is
# waiting on. The look's own client already retries inside the deadline. A look that did not happen
# leaves the row exactly as the measurement wrote it, and the next upload of the same bytes tries again.
#
# ITS OWN QUEUE, so a backlog of looks cannot delay a measurement. The measurement is the thing a
# conversation opens holding; the look is the thing it is better for having.
@celery_app.task(name="tasks.geometry.look_at_source", bind=False, max_retries=0,
                 soft_time_limit=600, time_limit=900, queue="geometry_look")
def look_at_source(source_id: str, owner_id: str) -> dict:
    from meshpipeline.application import geometry_vision as _look
    return _look.look_and_store_blocking(source_id, owner_id)


def enqueue_look(source_id: str, owner_id: str) -> None:
    look_at_source.apply_async(args=[str(source_id), str(owner_id)], queue="geometry_look")
    logger.info("geometry look queued - source_id=%s", source_id)
