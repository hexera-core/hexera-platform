# Responsibility: Hold the maintenance schedule to a hosted home - every beat entry runs somewhere on GCP, and
#                 the fleet itself runs no scheduler.
# Boundaries: static reads of celery_app.py and the fleet's startup script, plus the sweep entrypoint's own
#             roster; nothing is started.
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
STARTUP = REPO / "deploy" / "gcp" / "worker" / "startup.sh"
CELERY_APP = REPO / "src" / "meshpipeline" / "adapters" / "pipeline_execution" / "celery_app.py"
SWEEP_STAGE = REPO / "deploy" / "gcp" / "scripts" / "create-maintenance-sweep.sh"

# THE DEFECT THIS PINS. celery_app.py has declared a beat schedule - the stalled-job reaper every
# ten minutes, the byte-level purges hourly - since the compose stack was built, and the compose
# stack runs it. No hosted deployment ever did: the fleet's startup.sh runs one simulation worker
# per instance and nothing else, so a job whose instance the autoscaler replaced stayed `running`
# for good, holding its organisation's credits. The schedule's hosted home is now the maintenance
# sweep, a scheduled Cloud Run job; this file refuses a beat entry that has no such home.


def _beat_tasks() -> set[str]:
    src = CELERY_APP.read_text(encoding="utf-8")
    block = src[src.index("beat_schedule={"):]
    block = block[:block.index("worker_prefetch_multiplier")]
    return set(re.findall(r'"task":\s*"([^"]+)"', block))


def _code_lines(script: str) -> list[str]:
    return [line.split("#", 1)[0] for line in script.splitlines()]


def test_the_fleet_startup_runs_no_scheduler():
    # A beat beside every worker runs the schedule once per instance, and a managed group has no
    # first instance to run it on alone. Comments may name beat; commands may not.
    code = _code_lines(STARTUP.read_text(encoding="utf-8"))
    assert not any(re.search(r"\bbeat\b", line) for line in code), "the fleet startup runs a beat"


def test_the_fleet_consumes_no_cleanup_queue():
    # Why a beat there would be pointless as well as duplicated: its cleanup tasks would have no
    # consumer. The fleet runs the simulation queue and, beside it, the geometry-check queue
    # (tests/unit/deploy/test_geometry_check_queue.py) - never the cleanup queue. If it ever takes
    # the cleanup queue, revisit where the schedule lives.
    code = _code_lines(STARTUP.read_text(encoding="utf-8"))
    assert any("--queues simulation_jobs" in line for line in code)
    assert not any("cleanup_tasks" in line for line in code)


def test_the_fleet_startup_says_where_the_schedule_runs():
    assert "create-maintenance-sweep.sh" in STARTUP.read_text(encoding="utf-8")


def test_every_beat_entry_has_a_hosted_home_and_no_stale_one_is_claimed():
    from meshpipeline.runtime import maintenance_sweep as sweep
    hosted = {name for name, _ in sweep.HOSTED_STEPS}
    elsewhere = set(sweep.ELSEWHERE)
    assert hosted.isdisjoint(elsewhere), "an entry is both run by the sweep and said to run elsewhere"
    beat = _beat_tasks()
    assert beat, "no beat schedule found in celery_app.py"
    missing = beat - hosted - elsewhere
    assert not missing, (
        f"beat entries with no hosted home: {sorted(missing)} - add each to HOSTED_STEPS in "
        "runtime/maintenance_sweep.py, or state in ELSEWHERE where it runs on a hosted deployment")
    stale = (hosted | elsewhere) - beat
    assert not stale, f"the sweep claims beat entries that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("task,module,attr", [
    ("tasks.cleanup.reap_stalled_jobs",
     "meshpipeline.application.maintenance.cleanup", "reap_stalled_jobs"),
    ("tasks.cleanup.purge_expired_geometry_sources",
     "meshpipeline.application.maintenance.cleanup", "purge_expired_geometry_sources"),
    ("tasks.cleanup.reconcile_orphan_artifacts",
     "meshpipeline.application.maintenance.reconcile", "reconcile_orphan_artifacts"),
])
def test_each_hosted_step_calls_the_function_its_celery_task_calls(monkeypatch, task, module, attr):
    from meshpipeline.adapters.pipeline_execution import maintenance_tasks
    from meshpipeline.runtime import maintenance_sweep as sweep

    mod = importlib.import_module(module)
    calls: list[str] = []
    monkeypatch.setattr(mod, attr, lambda: calls.append(task) or {"ok": True})
    assert dict(sweep.HOSTED_STEPS)[task]() == {"ok": True}
    assert calls == [task]
    # and the Celery task body reaches the same function, so the two paths cannot drift
    assert f".{attr}()" in inspect.getsource(getattr(maintenance_tasks, attr))


def test_the_meter_entry_is_carried_by_the_meter_sweep():
    from meshpipeline.runtime import maintenance_sweep as sweep
    assert "meter_sweep" in sweep.ELSEWHERE["tasks.billing.report_pending_usage"]
    assert (REPO / "src" / "meshpipeline" / "runtime" / "meter_sweep.py").exists()


def test_the_reaper_runs_first():
    # It is the sweep with a person waiting on it; the other two only free bytes.
    from meshpipeline.runtime import maintenance_sweep as sweep
    assert sweep.HOSTED_STEPS[0][0] == "tasks.cleanup.reap_stalled_jobs"


def test_the_hosted_cadence_is_the_reapers_beat_interval():
    src = CELERY_APP.read_text(encoding="utf-8")
    reap = src[src.index('"reap-stalled-jobs"'):][:300]
    assert '"schedule": 600.0' in reap, "the reaper's beat interval moved; move the sweep's schedule with it"
    assert "*/10 * * * *" in SWEEP_STAGE.read_text(encoding="utf-8")
