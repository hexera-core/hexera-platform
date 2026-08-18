# Responsibility: Verify two workers with separate roots reconcile a replay, and the export outlives both roots.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from meshpipeline.persistence.repositories import capture_repository as cap

OWNER = "owner-cross-worker"

# One worker: its own cwd, workspace and DATA_ROOT, writing capture through the shared authority.
#
# Retention is decided when THIS process starts, from its own environment - see _worker below,
# which sets DATA_COLLECTION_ENABLED before the interpreter runs. The worker does not turn
# collection on for itself after importing, and could not: settings/modes.py reads the variable
# once and freezes it onto polcfg.MODES, which is what capture consults.
_WORKER = """
import json, os, sys
sys.path.insert(0, os.environ["SRC"])
import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
assert polcfg.MODES.data_collection_enabled, (
    "this worker was started without DATA_COLLECTION_ENABLED - it would capture nothing and "
    "every assertion about durable operations would be vacuous")
rtcfg.JOBS_DIR = os.environ["DATA_ROOT"] + "/jobs"
rtcfg.CORPUS_DIR = os.environ["DATA_ROOT"] + "/corpus"
os.makedirs(rtcfg.JOBS_DIR, exist_ok=True); os.makedirs(rtcfg.CORPUS_DIR, exist_ok=True)

from meshpipeline.capture import scope, trace
job, payload = sys.argv[1], json.loads(sys.argv[2])
scope.bind(os.environ["OWNER"], job)
trace.add_event(job, "node_effect", payload, attributes={"op_id": "work:g1"})
print("PID " + str(os.getpid()))
"""


@pytest.fixture()
async def SessionLocal():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import meshpipeline.settings.providers as provcfg

    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=8)
    # Alembic is the only thing that creates this schema - see
    # tests/harness_provisioning.py. `create_all` built tables no migration
    # had produced, so a suite could pass against a schema production never has.
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def job(SessionLocal):
    from meshpipeline.persistence.models import SimulationJob
    row = SimulationJob(owner_id=OWNER)
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        return str(row.id)


def _worker(job_id: str, payload: dict, root: Path, cwd: Path, *, collecting: bool = True) -> str:
    # The worker owns its retention setting AT STARTUP, in its own environment. The pytest process
    # stays on the shipped default; nothing it does to its own environment after import could
    # reconfigure a process that is already running, and this test would be lying if it pretended
    # otherwise.
    root.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SRC": str(Path.cwd() / "src"), "OWNER": OWNER,
           "DATA_ROOT": str(root),
           "DATA_COLLECTION_ENABLED": "true" if collecting else "false"}
    proc = subprocess.run([sys.executable, "-c", _WORKER, job_id, json.dumps(payload)],
                          cwd=cwd, capture_output=True, text=True, timeout=180, env=env)
    assert proc.returncode == 0, proc.stderr[-2500:]
    return proc.stdout.split("PID ")[1].strip()


def _roots(tmp_path):
    return (tmp_path / "rootA", tmp_path / "rootB",
            tmp_path / "cwdA", tmp_path / "cwdB")


async def test_two_workers_with_separate_roots_reconcile_an_identical_replay(tmp_path, job):
    ra, rb, ca, cb = _roots(tmp_path)
    pid_a = _worker(job, {"verdict": "PASS"}, ra, ca)

    # externally durable before the second worker starts - an independent connection sees it
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 1

    pid_b = _worker(job, {"verdict": "PASS"}, rb, cb)          # the takeover replays it

    assert pid_a != pid_b
    rows = cap.trusted_operations(owner_id=OWNER, job_id=job)
    assert len(rows) == 1, rows
    assert rows[0]["payload"] == {"verdict": "PASS"}
    assert cap.conflicted_operations(owner_id=OWNER, job_id=job) == []


async def test_two_workers_with_separate_roots_detect_a_conflicting_replay(tmp_path, job):
    ra, rb, ca, cb = _roots(tmp_path)
    _worker(job, {"verdict": "PASS"}, ra, ca)
    _worker(job, {"verdict": "FAIL"}, rb, cb)

    assert cap.trusted_operations(owner_id=OWNER, job_id=job) == []
    conflicts = cap.conflicted_operations(owner_id=OWNER, job_id=job)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_evidence"][0]["competing_sha256"]


async def test_the_export_survives_both_local_roots_being_deleted(tmp_path, job):
    from meshpipeline.capture.events import EventLog
    ra, rb, ca, cb = _roots(tmp_path)
    _worker(job, {"verdict": "PASS"}, ra, ca)
    _worker(job, {"verdict": "PASS"}, rb, cb)

    shutil.rmtree(ra); shutil.rmtree(rb)
    assert not ra.exists() and not rb.exists()

    opened: list[str] = []
    real_read, real_open = Path.read_text, Path.open

    def _spy_read(self, *a, **kw):
        opened.append(str(self)); return real_read(self, *a, **kw)

    def _spy_open(self, *a, **kw):
        opened.append(str(self)); return real_open(self, *a, **kw)

    Path.read_text, Path.open = _spy_read, _spy_open
    try:
        events = EventLog(job, owner_id=OWNER).load()
    finally:
        Path.read_text, Path.open = real_read, real_open

    assert [e.payload for e in events] == [{"verdict": "PASS"}]
    assert not [p for p in opened if p.endswith(".jsonl")], f"the export read a file: {opened}"


async def test_a_foreign_tenant_cannot_export_the_operation(tmp_path, job):
    from meshpipeline.capture.events import EventLog
    ra, _, ca, _ = _roots(tmp_path)
    _worker(job, {"verdict": "PASS"}, ra, ca)

    assert EventLog(job, owner_id="stranger").load() == []
    assert EventLog(job, owner_id=OWNER).load()
