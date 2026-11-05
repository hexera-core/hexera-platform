# Responsibility: Verify neither execution backend retries a job, so a long run is never silently re-executed.
from __future__ import annotations

import re
from pathlib import Path

import pytest

import meshpipeline.settings.runtime as rtcfg

REPO = Path(__file__).parents[3]
CELERY = REPO / "src" / "meshpipeline" / "adapters" / "pipeline_execution" / "celery.py"
CELERY_APP = REPO / "src" / "meshpipeline" / "adapters" / "pipeline_execution" / "celery_app.py"
CLOUD_RUN_YAMLS = [
    REPO / "deploy" / "gcp" / "cloud-run" / "mesh-job.yaml",
]


def test_celery_pipeline_task_does_not_auto_retry():
    src = CELERY.read_text()
    assert "max_retries=0" in src, "the pipeline Celery task must not auto-retry (idempotency)"


def test_celery_acks_on_receipt_no_automatic_redelivery():
    src = CELERY_APP.read_text()
    assert "task_acks_late=False" in src, "acks_late must be False (no stalled-worker redelivery race)"
    assert "task_reject_on_worker_lost=False" in src


@pytest.mark.parametrize("yaml_path", CLOUD_RUN_YAMLS, ids=lambda p: p.name)
def test_cloud_run_jobs_do_not_retry_and_run_a_single_task(yaml_path):
    if not yaml_path.exists():
        pytest.skip(f"{yaml_path.name} not present")
    text = yaml_path.read_text()
    assert re.search(r"maxRetries:\s*0\b", text), f"{yaml_path.name}: Cloud Run maxRetries must be 0"
    assert re.search(r"parallelism:\s*1\b", text), f"{yaml_path.name}: parallelism must be 1"
    assert re.search(r"taskCount:\s*1\b", text), f"{yaml_path.name}: taskCount must be 1"


def test_celery_backend_timeout_coherently_bounds_the_pipeline_deadline():
    src = CELERY.read_text()
    m = re.search(r"time_limit=(\d+)", src)
    assert m, "celery task has no time_limit"
    assert int(m.group(1)) >= rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS, (
        "the Celery hard time_limit is below the logical pipeline deadline - a long job would be "
        "killed by the broker before the truthful pipeline-deadline failure")
