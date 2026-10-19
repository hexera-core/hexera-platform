# Responsibility: Verify the worker clears finished jobs off its own disk, and never a job that may still run.
from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.application.maintenance import worker_disk as wd
from meshpipeline.persistence.models import JobStatus

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def disk(tmp_path, monkeypatch):
    ws, jobs, corpus = tmp_path / "workspaces", tmp_path / "jobs", tmp_path / "corpus"
    for d in (ws, jobs, corpus):
        d.mkdir()
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", ws)
    monkeypatch.setattr(rtcfg, "JOBS_DIR", jobs)
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", corpus)
    monkeypatch.setattr(polcfg, "FAILED_JOB_RETENTION_HOURS", 24)
    return ws, jobs, corpus


def _job_on_disk(ws: Path, jobs: Path, job_id: str, *, preview: bool = True) -> None:
    attempt = ws / job_id / "generation_0" / "attempt_1"
    attempt.mkdir(parents=True)
    (attempt / "mesh_manifest.json").write_text("{}")
    (ws / job_id / "geometry").mkdir()
    (ws / job_id / "geometry" / "part.stl").write_text("solid")
    if preview:
        (jobs / job_id).mkdir()
        (jobs / job_id / "preview.msh").write_text("MESH")


class _Db:
    """Stands in for the database: the facts, the dispute parents, and what got marked purged."""

    def __init__(self, monkeypatch, facts: dict, *, parents: set[str] | None = None,
                 fail: bool = False) -> None:
        self.marked: list[str] = []

        async def _facts(_sf, ids):
            if fail:
                raise RuntimeError("database unreachable")
            return {j: f for j, f in facts.items() if j in set(ids)}

        async def _parents(_sf):
            return set(parents or ())

        async def _mark(_sf, ids):
            self.marked.extend(ids)

        monkeypatch.setattr(wd, "_load_facts", _facts)
        monkeypatch.setattr(wd, "_unfinished_dispute_parents", _parents)
        monkeypatch.setattr(wd, "_mark_purged", _mark)


def _run(**kw):
    kw.setdefault("free_fraction", lambda _p: 0.9)
    return asyncio.run(wd.reclaim_finished_job_dirs(None, now=NOW, **kw))


def _facts(status, hours_ago):
    return wd.JobFacts(status=status, ended_at=None if hours_ago is None
                       else NOW - timedelta(hours=hours_ago))


# --- the routine rule ---------------------------------------------------------------------------

def test_an_old_finished_job_is_cleared_and_marked_purged(disk, monkeypatch):
    ws, jobs, _ = disk
    old_ok, old_failed, old_cancelled = _id(), _id(), _id()
    for j in (old_ok, old_failed, old_cancelled):
        _job_on_disk(ws, jobs, j)
    db = _Db(monkeypatch, {old_ok: _facts(JobStatus.succeeded, 30),
                           old_failed: _facts(JobStatus.failed, 30),
                           old_cancelled: _facts(JobStatus.cancelled, 30)})
    out = _run()
    assert out["removed"] == 3
    for j in (old_ok, old_failed, old_cancelled):
        assert not (ws / j).exists() and not (jobs / j).exists()
    assert sorted(db.marked) == sorted([old_ok, old_failed, old_cancelled])


def test_a_recently_finished_job_is_kept_for_a_dispute(disk, monkeypatch):
    ws, jobs, _ = disk
    recent = _id()
    _job_on_disk(ws, jobs, recent)
    db = _Db(monkeypatch, {recent: _facts(JobStatus.succeeded, wd.KEEP_SUCCEEDED_HOURS - 1)})
    assert _run()["removed"] == 0
    assert (ws / recent / "generation_0" / "attempt_1" / "mesh_manifest.json").exists()
    assert (jobs / recent / "preview.msh").exists()
    assert db.marked == []


def test_a_failed_job_follows_the_failed_retention_window(disk, monkeypatch):
    ws, jobs, _ = disk
    monkeypatch.setattr(polcfg, "FAILED_JOB_RETENTION_HOURS", 2)
    gone, kept = _id(), _id()
    _job_on_disk(ws, jobs, gone)
    _job_on_disk(ws, jobs, kept)
    _Db(monkeypatch, {gone: _facts(JobStatus.failed, 3), kept: _facts(JobStatus.failed, 1)})
    _run()
    assert not (ws / gone).exists() and (ws / kept).exists()


@pytest.mark.parametrize("status", [JobStatus.pending, JobStatus.running, JobStatus.queued,
                                    JobStatus.pending_review])
@pytest.mark.parametrize("pressure", [False, True])
def test_a_job_that_may_still_run_is_never_touched(disk, monkeypatch, status, pressure):
    ws, jobs, _ = disk
    live = _id()
    _job_on_disk(ws, jobs, live)
    # a handed-back or redelivered job keeps its old end time / generation dirs - still not terminal
    _Db(monkeypatch, {live: _facts(status, 500)})
    _run(free_fraction=lambda _p: 0.01 if pressure else 0.9)
    assert (ws / live / "generation_0" / "attempt_1").exists()
    assert (jobs / live / "preview.msh").exists()


def test_kept_jobs_dispute_parents_and_pending_exports_are_never_touched(disk, monkeypatch):
    ws, jobs, corpus = disk
    own, parent, sibling_parent, exporting = _id(), _id(), _id(), _id()
    for j in (own, parent, sibling_parent, exporting):
        _job_on_disk(ws, jobs, j)
    sample = corpus / "sample-1"
    sample.mkdir()
    (sample / ".job_id").write_text(exporting)
    _Db(monkeypatch, {j: _facts(JobStatus.succeeded, 500)
                      for j in (own, parent, sibling_parent, exporting)},
        parents={sibling_parent})
    out = _run(keep={own, parent}, free_fraction=lambda _p: 0.01)
    assert out["removed"] == 0
    for j in (own, parent, sibling_parent, exporting):
        assert (ws / j).exists()
    # ...and once the export has finished, it goes
    (sample / ".export_complete").write_text("done")
    _run(keep={own, parent})
    assert not (ws / exporting).exists()


def test_directories_that_are_not_known_jobs_are_left_alone(disk, monkeypatch):
    ws, jobs, _ = disk
    unknown = _id()                                   # e.g. an upload session dir, or a deleted job
    _job_on_disk(ws, jobs, unknown)
    (ws / "scratch").mkdir()
    (jobs / "direct-abc").mkdir()
    (ws / unknown.upper()).mkdir()                    # not the canonical spelling of a job id
    _Db(monkeypatch, {})
    _run(free_fraction=lambda _p: 0.01)
    assert (ws / unknown).exists() and (jobs / unknown / "preview.msh").exists()
    assert (ws / "scratch").exists() and (jobs / "direct-abc").exists()
    assert (ws / unknown.upper()).exists()


def test_only_the_preview_is_removed_from_the_job_record(disk, monkeypatch):
    ws, jobs, _ = disk
    old = _id()
    _job_on_disk(ws, jobs, old)
    (jobs / old / "something_else.json").write_text("{}")
    _Db(monkeypatch, {old: _facts(JobStatus.succeeded, 30)})
    _run()
    assert not (jobs / old / "preview.msh").exists()
    assert (jobs / old / "something_else.json").exists()


def test_a_symlinked_job_dir_is_not_followed(disk, monkeypatch, tmp_path):
    ws, jobs, _ = disk
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("x")
    job = _id()
    (ws / job).symlink_to(outside, target_is_directory=True)
    _Db(monkeypatch, {job: _facts(JobStatus.succeeded, 500)})
    _run(free_fraction=lambda _p: 0.01)
    assert (outside / "precious").exists()


# --- disk pressure ------------------------------------------------------------------------------

def test_under_pressure_the_oldest_finished_jobs_go_first_until_enough_is_free(disk, monkeypatch):
    ws, jobs, _ = disk
    oldest, middle, newest = _id(), _id(), _id()
    for j in (oldest, middle, newest):
        _job_on_disk(ws, jobs, j)
    _Db(monkeypatch, {oldest: _facts(JobStatus.succeeded, 5), middle: _facts(JobStatus.failed, 3),
                      newest: _facts(JobStatus.succeeded, 0.1)})

    def free(_p):                                     # each cleared job frees a tenth of the disk
        left = sum(1 for j in (oldest, middle, newest) if (ws / j).exists())
        return 0.10 + 0.10 * (3 - left)

    out = _run(free_fraction=free)
    assert out["under_pressure"] is True
    assert not (ws / oldest).exists() and not (ws / middle).exists()
    assert (ws / newest).exists(), "stopped once TARGET_FREE_FRACTION was free again"


def test_under_pressure_a_just_cancelled_job_waits_one_lease(disk, monkeypatch):
    ws, jobs, _ = disk
    monkeypatch.setattr(rtcfg, "WORKER_LEASE_SECONDS", 900)
    fresh, settled = _id(), _id()
    _job_on_disk(ws, jobs, fresh)
    _job_on_disk(ws, jobs, settled)
    _Db(monkeypatch, {fresh: _facts(JobStatus.cancelled, 0.1),
                      settled: _facts(JobStatus.cancelled, 0.5)})
    _run(free_fraction=lambda _p: 0.01)
    assert (ws / fresh).exists() and not (ws / settled).exists()


def test_no_pressure_means_the_keep_window_holds(disk, monkeypatch):
    ws, jobs, _ = disk
    recent = _id()
    _job_on_disk(ws, jobs, recent)
    _Db(monkeypatch, {recent: _facts(JobStatus.succeeded, 0.1)})
    assert _run(free_fraction=lambda _p: wd.LOW_FREE_FRACTION + 0.01)["removed"] == 0
    assert (ws / recent).exists()


# --- failure is harmless ------------------------------------------------------------------------

def test_a_database_error_deletes_nothing_and_never_reaches_the_run(disk, monkeypatch):
    ws, jobs, _ = disk
    job = _id()
    _job_on_disk(ws, jobs, job)
    _Db(monkeypatch, {job: _facts(JobStatus.succeeded, 500)}, fail=True)
    out = asyncio.run(wd.reclaim_quietly(None, keep=()))
    assert "error" in out
    assert (ws / job).exists()


def test_dispute_parents_are_read_from_unfinished_dispatch_payloads():
    parent = _id()

    class _Rows:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            sql = str(stmt)
            assert "dispatch_payload" in sql and "NOT IN" in sql.upper()
            return _Rows([({"user_dispute": {"of_job_id": parent}},), ({},), (None,),
                          ({"user_dispute": None},)])

    assert asyncio.run(wd._unfinished_dispute_parents(_Session)) == {parent}


def test_marking_purged_rechecks_that_the_job_is_terminal():
    seen = []

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt): seen.append(str(stmt))
        async def commit(self): pass

    asyncio.run(wd._mark_purged(_Session, [_id()]))
    assert "workspace_purged" in seen[0] and "status IN" in seen[0]


# --- wiring -------------------------------------------------------------------------------------

def test_the_run_frees_disk_after_its_claim_and_before_it_writes_geometry():
    import meshpipeline.application.pipeline_run as pr
    src = inspect.getsource(pr._run_async)
    at = src.index("_wd.reclaim_quietly(")
    assert src.index("_fence.claim_delivery(") < at < src.index("_gm.prepare_for_execution(")
    assert "keep={job_id, _dispute_parent}" in src[at:at + 200]
