# Responsibility: Verify the retention cutoff comes from the setting and runs on the same schedule as other cleanups.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application.maintenance.geometry_retention import (
    CLAIM_EXPIRY,
    SOURCE_REQUIRING_STATES,
    retention_cutoff,
)

TASK_NAME = "tasks.cleanup.purge_expired_geometry_sources"


def test_the_setting_is_read_by_the_policy_that_computes_the_cutoff():
    now = datetime(2026, 8, 2, tzinfo=UTC)
    assert retention_cutoff(now) == now - timedelta(days=int(polcfg.UPLOAD_RETENTION_DAYS))


def test_thirty_days_is_the_shipped_default():
    assert int(polcfg.UPLOAD_RETENTION_DAYS) == 30


def test_the_maintenance_task_exists_and_delegates_to_the_application():
    import inspect

    from meshpipeline.adapters.pipeline_execution import maintenance_tasks

    fn = maintenance_tasks.purge_expired_geometry_sources
    body = inspect.getsource(fn)
    assert "_cleanup.purge_expired_geometry_sources" in body, "the adapter must delegate"


def _celery_app_source() -> str:
    from pathlib import Path

    import meshpipeline
    return (Path(meshpipeline.__file__).parent / "adapters" / "pipeline_execution"
            / "celery_app.py").read_text()


def test_the_task_is_on_the_production_beat_schedule():
    src = _celery_app_source()
    assert TASK_NAME in src, f"{TASK_NAME} is not on the beat schedule"
    after = src.split(TASK_NAME, 1)[1][:200]
    assert '"schedule"' in after and '"queue": "cleanup_tasks"' in after, (
        "the retention task must carry a real interval and share the existing cleanup queue")


def test_it_sits_beside_the_other_cleanups_rather_than_inventing_a_scheduler():
    src = _celery_app_source()
    assert TASK_NAME in src
    assert "tasks.cleanup.purge_expired_workspaces" in src, "the existing seam is still there"
    assert src.count("beat_schedule") == 1, "retention must not add a second scheduler"


def test_a_claim_expiry_exists_so_an_abandoned_claim_cannot_wedge_a_row():
    assert timedelta(0) < CLAIM_EXPIRY <= timedelta(hours=1)


def test_the_protected_states_are_the_ones_that_still_need_bytes():
    from meshpipeline.persistence.models import JobStatus

    assert JobStatus.succeeded not in SOURCE_REQUIRING_STATES
    assert JobStatus.failed not in SOURCE_REQUIRING_STATES
    for s in (JobStatus.pending, JobStatus.queued, JobStatus.running, JobStatus.pending_review):
        assert s in SOURCE_REQUIRING_STATES


@pytest.mark.parametrize("adapter", ["minio"])
def test_the_object_store_adapter_satisfies_the_delete_contract(adapter):
    import inspect

    mod = __import__(f"meshpipeline.adapters.object_storage.{adapter}", fromlist=["x"])
    store_cls = next(obj for name, obj in vars(mod).items()
                     if inspect.isclass(obj) and name.endswith("Store"))
    for method in ("delete_object", "exists"):
        assert hasattr(store_cls, method), f"{adapter} has no {method}"
        sig = inspect.signature(getattr(store_cls, method))
        assert "object_key" in sig.parameters, f"{adapter}.{method} takes no object_key"
        assert list(sig.parameters)[1:] == ["object_key"], (
            f"{adapter}.{method} must take exactly one exact key - a wider signature invites "
            "prefix deletion")


def test_the_purge_never_asks_a_store_for_a_prefix_delete():
    import inspect

    from meshpipeline.application.maintenance import geometry_retention

    src = inspect.getsource(geometry_retention)
    for forbidden in ("delete_prefix", "list_objects", "delete_objects", "remove_prefix"):
        assert forbidden not in src, f"{forbidden} would delete more than one exact key"
    assert "delete_object(object_key=" in src
