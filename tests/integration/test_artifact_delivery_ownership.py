# Responsibility: Verify artifact delivery publishes under the exact claimed execution ownership.
# Boundaries: the delivery span only - the graph boundary and terminal authority are other suites.
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.engine_workspaces import build_workspace

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.models import Artifact, SimulationJob

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)
if not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("a real object store is required for delivery", allow_module_level=True)

#: The two artifact-delivery emissions, by the site IDs the validated inventory gave them.
E033 = ("application/artifact_uploader.py", "note")
E034 = ("application/artifact_uploader.py", "error")


class _AbsentSnapshot:
    created_at = None
    next: tuple = ()
    values: dict = {}


# Returns the prepared deliverable state and nothing else. It publishes nothing, binds nothing and
# invents no identity - `build_graph` is the seam _run_async already imports.
class _DeliverableGraph:
    def __init__(self, state: dict, entered: asyncio.Event | None = None,
                 release: asyncio.Event | None = None):
        self._state, self._entered, self._release = state, entered, release

    async def aget_state(self, config=None):
        return _AbsentSnapshot()

    async def ainvoke(self, state, config=None):
        if self._entered is not None:
            self._entered.set()
            await self._release.wait()
        return dict(self._state)


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=1, max_overflow=1,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _with_session(fn):
    engine, Session = _sessions()
    try:
        async with Session() as db:
            return await fn(db)
    finally:
        await engine.dispose()


async def _read_row(job_id: uuid.UUID):
    async def _q(db):
        return (await db.execute(
            select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
    return await _with_session(_q)


async def _artifact_rows(job_id: uuid.UUID) -> list:
    async def _q(db):
        return (await db.execute(select(Artifact).where(Artifact.job_id == job_id))).scalars().all()
    return await _with_session(_q)


async def _seed_job(job_id: uuid.UUID, owner_id: str) -> None:
    async def _q(db):
        db.add(SimulationJob(id=job_id, owner_id=owner_id))
        await db.commit()
    await _with_session(_q)


# Wrap the REAL publisher methods; record context, then call the original unchanged.
def _install_emission_recorder(monkeypatch, seen: dict) -> None:
    import sys

    from meshpipeline.adapters.event_stream.redis import JobPublisher

    root = Path(sys.modules["meshpipeline"].__file__).parent

    def _wrap(name: str):
        original = getattr(JobPublisher, name)

        def w(self, *a, **k):
            frame = sys._getframe(1)
            try:
                rel = Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = frame.f_code.co_filename
            if (rel, name) in (E033, E034):
                own = fence.current_ownership()
                seen.setdefault((rel, name), {
                    "ownership": own,
                    "publisher_class": type(self).__name__,
                    "publisher_job_id": str(getattr(self, "job_id", "")),
                    "op_id": k.get("op_id", ""),
                })
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, name, w)

    _wrap("note")
    _wrap("error")


# apply_delivery is the first seam after the span closes on the success path.
def _install_post_delivery_recorder(monkeypatch, seen: dict) -> None:
    from meshpipeline.application import final_result as fr
    real = fr.apply_delivery

    def w(*a, **k):
        seen.setdefault("post_delivery", fence.current_ownership())
        return real(*a, **k)

    monkeypatch.setattr(fr, "apply_delivery", w)


def _success_state(ws: Path) -> dict:
    return {"reviewer_verdict": "PASS", "executor_success": True,
            "openfoam_workspace": str(ws), "engine": "gmsh", "retry_count": 0}


async def _run(monkeypatch, ws: Path, *, entered=None, release=None):
    # THE production composition - the same call api_server, celery_worker and run_job make. The
    # object store and publisher factory come from it, so delivery uses the real adapters.
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

    job_id = uuid.uuid4()
    owner_id = f"delivery-{job_id.hex[:8]}"
    await _seed_job(job_id, owner_id)
    seen: dict = {}

    import meshpipeline.pipeline.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_graph",
                        lambda checkpointer: _DeliverableGraph(_success_state(ws), entered, release))
    _install_emission_recorder(monkeypatch, seen)
    _install_post_delivery_recorder(monkeypatch, seen)

    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    return job_id, owner_id, seen, asyncio.create_task(
        _run_async(JobRequest(job_id=str(job_id), owner_id=owner_id)))


# E033 - the success announcement


async def test_the_delivery_announcement_publishes_under_the_claimed_ownership(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    job_id, _owner, seen, run = await _run(monkeypatch, ws)
    result = await run

    assert E033 in seen, f"the delivery announcement was never reached (result={result!r})"
    rec = seen[E033]
    own = rec["ownership"]
    assert own is not None, "E033 published with NO execution ownership bound"
    assert rec["publisher_class"] == "JobPublisher", \
        f"E033 was observed on {rec['publisher_class']}, not the production publisher"
    assert rec["publisher_job_id"] == str(job_id)

    row = await _read_row(job_id)
    assert str(own.job_id) == str(job_id)
    assert own.execution_generation == row.execution_generation
    assert own.claim_epoch == row.execution_claim_epoch
    assert (own.worker_token == row.active_worker_token) is True, \
        "the bound token does not match the durable row"
    assert rec["op_id"] == "delivered", "the delivery announcement changed its operation identity"


async def test_the_delivered_artifacts_exist_once_and_the_span_closes(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    job_id, _owner, seen, run = await _run(monkeypatch, ws)
    await run

    rows = await _artifact_rows(job_id)
    assert rows, "delivery announced success but persisted no artifact row"
    keys = [r.storage_key for r in rows]
    assert len(keys) == len(set(keys)), "a duplicate artifact row was created"

    assert seen.get("post_delivery", "absent") is None, (
        "execution ownership was still bound at apply_delivery - the span must close with the "
        "delivery call, leaving terminal work unowned")
    assert fence.current_ownership() is None


# E034 - the delivery-failure emission


async def test_the_delivery_failure_publishes_under_the_claimed_ownership(monkeypatch, tmp_path):
    # A workspace whose required bundle member is missing fails the uploader's existing contract
    # check. That is a real production branch, not an injected hook.
    ws = build_workspace(tmp_path, "gmsh", omit=("gmsh_spec.json",))
    job_id, _owner, seen, run = await _run(monkeypatch, ws)
    await run

    assert E034 in seen, "the delivery-failure emission was never reached"
    rec = seen[E034]
    own = rec["ownership"]
    assert own is not None, "E034 published with NO execution ownership bound"
    assert rec["publisher_class"] == "JobPublisher"
    assert rec["publisher_job_id"] == str(job_id)

    row = await _read_row(job_id)
    assert str(own.job_id) == str(job_id)
    assert own.execution_generation == row.execution_generation
    assert own.claim_epoch == row.execution_claim_epoch
    assert (own.worker_token == row.active_worker_token) is True
    assert rec["op_id"] == "delivery-failed", "the failure emission changed its operation identity"


async def test_a_failed_delivery_leaves_the_span_closed_and_terminal_unowned(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh", omit=("gmsh_spec.json",))
    _job_id, _owner, seen, run = await _run(monkeypatch, ws)
    await run

    assert seen.get("post_delivery", "absent") is None, \
        "ownership survived a failed delivery into the terminal path"
    assert fence.current_ownership() is None
    leftover = [t for t in asyncio.all_tasks()
                if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")]
    assert leftover == [], "a heartbeat task outlived the failed delivery"


# a superseded worker never reaches delivery


async def test_a_stale_worker_is_refused_before_delivery_and_changes_nothing(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    entered, release = asyncio.Event(), asyncio.Event()
    job_id, _owner, seen, run = await _run(monkeypatch, ws, entered=entered, release=release)

    # Hold the first execution inside its graph - after the claim, before the current-owner check.
    waiter = asyncio.create_task(entered.wait())
    done, _ = await asyncio.wait({waiter, run}, return_when=asyncio.FIRST_COMPLETED)
    assert run not in done, f"the run ended before the barrier: {run.result()!r}"
    waiter.cancel()

    first = await _read_row(job_id)

    # Expire the live lease on the DURABLE ROW - what the passage of time does - then let the real
    # claim authority decide. Nothing about ownership is mocked; only the clock is moved.
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    async def _expire(db):
        await db.execute(update(SimulationJob).where(SimulationJob.id == job_id).values(
            lease_expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await db.commit()
    await _with_session(_expire)

    # A REAL takeover through the production claim authority - no mocked ownership result.
    from meshpipeline.application import execution_fence as _fence
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    engine, Session = _sessions()
    try:
        claimed = await _fence.claim_delivery(
            Session, JobRepository(), str(job_id), jlog=_NullLog(),
            backend="takeover", backend_execution_id=f"takeover-{uuid.uuid4().hex[:8]}")
    finally:
        await engine.dispose()
    assert not isinstance(claimed, _fence.DeliveryRefused), "the takeover itself was refused"
    second = await _read_row(job_id)
    assert second.execution_generation > first.execution_generation, "no takeover occurred"

    release.set()
    result = await run

    assert result.get("status") == "fenced" and result.get("skipped") == "not_owner", \
        f"the superseded worker was not fenced before finalize: {result!r}"
    assert E033 not in seen and E034 not in seen, \
        "a superseded worker published an artifact-delivery event"
    assert await _artifact_rows(job_id) == [], "a superseded worker persisted an artifact row"
    assert "post_delivery" not in seen, "a superseded worker reached apply_delivery"


class _NullLog:
    def info(self, *a, **k): ...
    def warning(self, *a, **k): ...
    def error(self, *a, **k): ...
    def debug(self, *a, **k): ...
