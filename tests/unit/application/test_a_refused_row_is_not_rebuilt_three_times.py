# Responsibility: Verify the delivery loop stops on a database refusal no retry can change, and keeps its three attempts for everything that may still succeed.
# Boundaries: the retry decision and what it logs; the uploader's own delivery contract is test_artifact_delivery_contract.
from __future__ import annotations

import uuid

import pytest
from asyncpg import exceptions as apg
from sqlalchemy.exc import DBAPIError

from meshpipeline.application import artifact_uploader as AU
from meshpipeline.contracts.object_storage import StorageError

JOB = uuid.uuid4()

# THE STATEMENT AND THE BOUND ROW AS MEASURED on this venv (asyncpg 0.30.0, SQLAlchemy 2.0.36)
# against the live schema, by calling ArtifactRepository.deliver_artifact with the bundle's real
# size. size_bytes is parameter $8, which is the only reason a column can be named at all.
_STATEMENT = (
    "INSERT INTO artifacts (id, job_id, artifact_type, logical_key, delivery_attempt, "
    "execution_generation, storage_key, size_bytes, checksum) VALUES ($1::UUID, $2::UUID, "
    "$3::artifacttype, $4::VARCHAR, $5::INTEGER, $6::INTEGER, $7::VARCHAR, $8::INTEGER, "
    "$9::VARCHAR) ON CONFLICT ON CONSTRAINT uq_artifacts_job_logical DO UPDATE SET "
    "size_bytes = $15::INTEGER RETURNING artifacts.id")
_BUNDLE_BYTES = 10_616_027_998          # the OpenFOAM case bundle of job 0ff6f9e5
_PARAMS = (uuid.uuid4(), JOB, "mesh_bundle", "mesh_bundle", 0, 1, f"jobs/{JOB}/case.tar.gz",
           _BUNDLE_BYTES, None)


def _wrap(cause: BaseException) -> AU.RequiredArtifactDeliveryError:
    # upload_job_artifacts raises its wrapper "from exc", so what the loop catches is a
    # RequiredArtifactDeliveryError whose own type says nothing about a database.
    try:
        raise AU.RequiredArtifactDeliveryError(
            f"required artifact mesh_bundle could not be registered: {cause}") from cause
    except AU.RequiredArtifactDeliveryError as wrapped:
        return wrapped


def _dbapi_error(driver_exc: BaseException, sqlstate: str) -> DBAPIError:
    # The SHAPE the loop is handed, measured end to end. SQLAlchemy raises a plain DBAPIError -
    # NOT exc.DataError, because the asyncpg dialect's wrapper is not a DBAPI DataError subclass,
    # so a predicate keyed on that class would look right and match nothing real. The wrapper on
    # .orig is decorated by the dialect with sqlstate AND pgcode copied off the driver exception,
    # and its __cause__ is the asyncpg exception.
    dialect_err = Exception(f"{type(driver_exc)}: {driver_exc}")
    dialect_err.sqlstate = dialect_err.pgcode = sqlstate
    dialect_err.__cause__ = driver_exc
    return DBAPIError(_STATEMENT, _PARAMS, dialect_err)


def _int4_refusal() -> AU.RequiredArtifactDeliveryError:
    # artifacts.size_bytes was int4 and the bundle was 10,616,027,998 bytes: asyncpg refuses the
    # BIND, before the statement is ever sent. Job 0ff6f9e5's error verbatim.
    return _wrap(_dbapi_error(apg.DataError(
        f"invalid input for query argument $8: {_BUNDLE_BYTES} (value out of int32 range)"),
        "22000"))


class _Log:
    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @staticmethod
    def _fmt(a): return a[0] % a[1:] if len(a) > 1 else str(a[0])

    def info(self, *a, **k): pass
    def warning(self, *a, **k): self.warnings.append(self._fmt(a))
    def error(self, *a, **k): self.errors.append(self._fmt(a))


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): pass


class _Pub:
    calls: list = []

    def __init__(self, job_id): pass
    def note(self, msg, op_id=""): _Pub.calls.append(("note", op_id))
    def error(self, msg, op_id=""): _Pub.calls.append(("error", op_id))


@pytest.fixture
def run(monkeypatch):
    _Pub.calls = []
    state = {"uploads": 0, "sleeps": []}

    class _NoSleep:
        @staticmethod
        async def sleep(s): state["sleeps"].append(s)
    monkeypatch.setattr(AU, "asyncio", _NoSleep)

    async def _persist(owner_id, job_id, orphans): return None

    async def _go(raises: BaseException):
        async def _upload(*a, **k):
            state["uploads"] += 1
            raise raises
        monkeypatch.setattr(AU, "upload_job_artifacts", _upload)
        log = _Log()
        out = await AU.deliver_succeeded_run(
            lambda: _Session(), job_id=str(JOB), owner_id="o", workspace="/ws", engine="cfmesh",
            delivery_attempt=0, execution_generation=1, jlog=log, publish=_Pub,
            persist_orphans=_persist)
        return out, log, state

    return _go


# THE REFUSAL THAT CAN NEVER SUCCEED

async def test_a_size_the_column_cannot_hold_is_refused_once_not_three_times(run):
    out, _, state = await run(_int4_refusal())
    assert state["uploads"] == 1, (
        f"the surfaces were rebuilt and the bundle re-uploaded {state['uploads']} times for an "
        "error that is a property of the value and the column")
    assert state["sleeps"] == [], "the loop still backed off before a retry it never made"
    assert out.succeeded is False and out.failed_reason is not None


async def test_the_log_names_the_value_and_the_column(run):
    _, log, _ = await run(_int4_refusal())
    refusal = "\n".join(log.errors)
    assert "22000" in refusal, f"the SQLSTATE that decided this is nowhere in the log: {refusal}"
    assert f"artifacts.size_bytes={_BUNDLE_BYTES}" in refusal, (
        f"the log does not name the value and the column that disagree: {refusal}")


async def test_the_refusal_is_read_through_the_wrapper_not_off_the_outer_type():
    from meshpipeline.application.artifact_uploader import _db_refusal_detail

    wrapped = _int4_refusal()
    assert type(wrapped).__name__ == "RequiredArtifactDeliveryError"
    assert not hasattr(wrapped, "sqlstate") and not hasattr(wrapped, "orig"), (
        "the outer object carries the evidence after all - this test proves nothing")
    assert _db_refusal_detail(wrapped) is not None, "the wrapper hid the refusal from the loop"
    assert _db_refusal_detail(AU.RequiredArtifactDeliveryError("no cause at all")) is None


# EVERYTHING THAT MAY STILL SUCCEED KEEPS ITS THREE ATTEMPTS

async def test_a_storage_blip_still_gets_all_three_attempts(run):
    out, _, state = await run(_wrap(StorageError("connection reset by peer")))
    assert state["uploads"] == 3, "a transient storage failure lost its retries"
    assert state["sleeps"] == [1, 2] and out.succeeded is False


async def test_a_delivery_conflict_still_gets_all_three_attempts(run):
    # deliver_artifact returns conflict from two branches and the second is an explicit race (a
    # concurrent delete between the INSERT-conflict and the read). A retry is what resolves that,
    # and the loop cannot tell the two branches apart.
    _, _, state = await run(AU.ArtifactDeliveryConflict(
        "required artifact mesh_bundle conflicts with an existing ready deliverable"))
    assert state["uploads"] == 3, "a conflict that may be a race was made terminal"


async def test_a_half_rolled_migration_still_gets_all_three_attempts(run):
    # Class 42: this worker reached the new code before the migration landed. A later attempt can
    # be served by an instance that has it, so it must keep every retry.
    _, _, state = await run(_wrap(_dbapi_error(
        apg.UndefinedColumnError('column "size_bytes" does not exist'), "42703")))
    assert state["uploads"] == 3, "an undefined column during a deploy was made terminal"


async def test_the_run_is_still_downgraded_and_nothing_is_announced_as_delivered(run):
    out, _, _ = await run(_int4_refusal())
    assert out.delivered == [] and isinstance(out.error, AU.RequiredArtifactDeliveryError)
    assert ("note", "delivered") not in _Pub.calls, "an undelivered mesh was announced as ready"
    assert ("error", "delivery-failed") in _Pub.calls
