# Responsibility: Expose the scheduled maintenance operations as Celery tasks.
# Boundaries: task registration only - every operation's logic lives in application/maintenance/.
from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
from meshpipeline.application.maintenance import cleanup as _cleanup
from meshpipeline.application.maintenance import export as _export
from meshpipeline.application.maintenance import reconcile as _reconcile


@celery_app.task(name="tasks.cleanup.purge_expired_workspaces")
def purge_expired_workspaces() -> dict:
    return _cleanup.purge_expired_workspaces()


@celery_app.task(name="tasks.cleanup.reap_stalled_jobs")
def reap_stalled_jobs() -> dict:
    return _cleanup.reap_stalled_jobs()


@celery_app.task(name="tasks.cleanup.purge_expired_geometry_sources")
def purge_expired_geometry_sources() -> dict:
    return _cleanup.purge_expired_geometry_sources()


@celery_app.task(name="tasks.cleanup.reconcile_orphan_artifacts")
def reconcile_orphan_artifacts() -> dict:
    return _reconcile.reconcile_orphan_artifacts()


@celery_app.task(
    name="tasks.export_conversation_data.export_conversation_sample",
    bind=False,
    max_retries=3,
    default_retry_delay=60,
    queue="training_export",
)
def export_conversation_sample(job_id: str, state: dict,
                               created_at: str | None = None,
                               ended_at: str | None = None) -> dict:
    return _export.export_conversation_sample(job_id, state, created_at=created_at, ended_at=ended_at)
