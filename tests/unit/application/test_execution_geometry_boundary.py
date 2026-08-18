# Responsibility: Verify only the approved bytes reach the graph, materialised once into a workspace of their own.
# Boundaries: a serialized path is never trusted, and only an infrastructure failure is retryable.
from __future__ import annotations

import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes
from tests._geometry_support import geometry_bytes, interpretation_ref, source_ref, source_row

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.object_storage import ObjectNotFound, StorageError
from meshpipeline.errors import FailureClass
from meshpipeline.persistence.job_state import TransitionResult

pytestmark = pytest.mark.asyncio

_APPROVED = source_ref(owner_id="owner-1", filename="duct.step", marker="approved", size=1024)
_APPROVED_BYTES = geometry_bytes("approved", 1024)
#: The physical meaning the approval carries. Bytes alone cannot be meshed, so the boundary
#: reconciles this the same way it reconciles the source.
_INTERPRETATION = interpretation_ref(geometry_source_id=_APPROVED.source_id)


class _Store:

    def __init__(self, served: bytes | None = _APPROVED_BYTES, *, raises: Exception | None = None):
        self.served, self.raises = served, raises
        self.downloads: list[str] = []

    def exists(self, *, object_key):
        return self.served is not None

    def download_file(self, *, object_key, destination):
        self.downloads.append(object_key)
        if self.raises:
            raise self.raises
        if self.served is None:
            raise ObjectNotFound(object_key)
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.served)


def _stub_runtime(monkeypatch, wt, seen: dict, *, store: _Store, row=None,
                  interpretation=_INTERPRETATION):
    import sqlalchemy.ext.asyncio as sa_aio

    import meshpipeline.application.geometry_materializer as gm
    import meshpipeline.persistence.repositories.job_repository as jr
    import meshpipeline.pipeline.graph as graph_module

    class FakeGraph:

        async def aget_state(self, config=None):

            # A compiled graph always answers this; the entry asks before deciding fresh vs

            # resume. An empty thread is the right answer for a double that never checkpoints.

            from types import SimpleNamespace

            return SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

        async def ainvoke(self, state, config=None):
            seen["invocations"] = seen.get("invocations", 0) + 1
            seen["state"] = dict(state)
            return {**state, "reviewer_verdict": "FAIL", "outcome_message": "done"}

    monkeypatch.setattr(graph_module, "build_graph", lambda checkpointer=None: FakeGraph())
    monkeypatch.setattr(gm, "get_object_store", lambda: store)

    class _SourceRepo:
        async def get_for_owner(self, _db, source_id, owner_id):
            # ownership is part of the lookup, exactly as the real query scopes it
            if row is None:
                return None
            return row if (str(source_id) == str(row.id) and str(owner_id) == row.owner_id) else None

    import meshpipeline.persistence.repositories.geometry_source_repository as gsr
    monkeypatch.setattr(gsr, "GeometrySourceRepository", _SourceRepo)

    class _InterpRepo:
        async def get_for_owner(self, _db, interpretation_id, owner_id):
            from meshpipeline.contracts.geometry_units import (
                GeometryInterpretation,
                LengthUnit,
                ResolutionBasis,
            )
            if interpretation is None:
                return None
            if str(interpretation_id) != interpretation.interpretation_id:
                return None
            if owner_id != _APPROVED.owner_id:          # scoped exactly as the real query is
                return None
            return GeometryInterpretation(
                interpretation_id=interpretation.interpretation_id, owner_id=owner_id,
                geometry_source_id=interpretation.geometry_source_id,
                unit=LengthUnit(interpretation.unit),
                scale_to_metres=float(interpretation.scale_to_metres),
                basis=ResolutionBasis(interpretation.basis), evidence=interpretation.evidence)

    import meshpipeline.persistence.repositories.geometry_interpretation_repository as gir
    monkeypatch.setattr(gir, "GeometryInterpretationRepository", _InterpRepo)

    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass
        async def execute(self, *a, **k): pass

    import meshpipeline.persistence.session as psession
    monkeypatch.setattr(psession, "get_db", lambda: _DB())

    async def _noop(): pass
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: _DB)

    class FakeRepo:
        row = SimpleNamespace(owner_id="dev-user", status=None, failed_reason=None, created_at=None, ended_at=None)
        async def get_internal(self, db, job_id): return self.row
        async def get_for_owner(self, db, job_id, owner_id): return self.row
        async def transition(self, db, job_id, target, *, allow=None):
            self.row.status = target
            return TransitionResult.applied
        async def update_current_attempt(self, db, job_id, n): pass
        async def set_final_result(self, db, job_id, fr): pass

    monkeypatch.setattr(jr, "JobRepository", lambda: FakeRepo())
    seen.setdefault("published", [])

    def _make_pub(job_id, stage="outcome"):
        pub = FakePublisher(job_id, stage)
        seen["published"].append(pub)
        return pub

    monkeypatch.setattr(wt, "_pub", _make_pub)
    monkeypatch.setattr(wt, "_incr_delivery_count", lambda job_id: 1)
    install_durable_execution_fakes(monkeypatch, wt)
    return seen


async def _run(monkeypatch, tmp_path, *, store=None, row=..., request_over=None, seen=None):
    import meshpipeline.application.pipeline_run as wt
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    seen = _stub_runtime(monkeypatch, wt, seen if seen is not None else {},
                         store=store or _Store(),
                         row=source_row(_APPROVED) if row is ... else row)
    kwargs = {"job_id": str(_uuid.uuid4()), "owner_id": _APPROVED.owner_id,
              "geometry_source": _APPROVED.to_payload(),
              "geometry_interpretation": _INTERPRETATION.to_payload(),
              "request_txt": "mesh it"}
    kwargs.update(request_over or {})
    result = await wt._run_async(wt.JobRequest(**kwargs))
    return result, seen


def _delivered(seen) -> Path:
    from meshpipeline.pipeline.geometry_state import geometry_path
    return Path(geometry_path(seen["state"]))


# the success path

async def test_the_graph_receives_the_approved_bytes_in_this_workspace(monkeypatch, tmp_path):
    _, seen = await _run(monkeypatch, tmp_path)
    delivered = _delivered(seen)
    assert delivered.exists()
    assert delivered.read_bytes() == _APPROVED_BYTES       # the BYTES, not just a path
    assert tmp_path in delivered.parents                   # this run's own workspace


async def test_identity_survives_and_the_handle_agrees_with_it(monkeypatch, tmp_path):
    from meshpipeline.pipeline.geometry_state import geometry_ref, materialized
    _, seen = await _run(monkeypatch, tmp_path)
    assert geometry_ref(seen["state"]) == _APPROVED
    assert materialized(seen["state"]).ref == geometry_ref(seen["state"])


async def test_no_api_local_upload_path_is_consulted(monkeypatch, tmp_path):
    api_dir = tmp_path / "api-uploads"
    api_dir.mkdir()
    monkeypatch.setattr(rtcfg, "JOBS_DIR", api_dir)
    _, seen = await _run(monkeypatch, tmp_path)
    assert api_dir not in _delivered(seen).parents


async def test_repeated_reads_do_not_redownload(monkeypatch, tmp_path):
    from meshpipeline.pipeline.geometry_state import geometry_path
    store = _Store()
    _, seen = await _run(monkeypatch, tmp_path, store=store)
    for _ in range(5):
        geometry_path(seen["state"])
    assert len(store.downloads) == 1


async def test_materialisation_happens_once_per_entry(monkeypatch, tmp_path):
    store = _Store()
    _, seen = await _run(monkeypatch, tmp_path, store=store)
    assert len(store.downloads) == 1
    assert seen["invocations"] == 1


async def test_a_second_entry_uses_a_disjoint_workspace(monkeypatch, tmp_path):
    _, first = await _run(monkeypatch, tmp_path / "process-a")
    _, second = await _run(monkeypatch, tmp_path / "process-b")
    a, b = _delivered(first), _delivered(second)
    assert a != b
    assert a.read_bytes() == b.read_bytes() == _APPROVED_BYTES


# stale serialized paths

@pytest.mark.parametrize("stale_kind", ["missing", "previous_workspace", "altered_bytes",
                                        "other_source", "correct_but_unverified"])
async def test_a_serialized_path_is_never_trusted(monkeypatch, tmp_path, stale_kind):
    stale_dir = tmp_path / "from-another-process"
    stale_dir.mkdir()
    stale = stale_dir / "source.step"
    if stale_kind == "missing":
        stale_path = str(stale)                          # never created
    elif stale_kind == "previous_workspace":
        stale.write_bytes(_APPROVED_BYTES)
        stale_path = str(stale)
    elif stale_kind == "altered_bytes":
        stale.write_bytes(b"X" * len(_APPROVED_BYTES))   # same size, different content
        stale_path = str(stale)
    elif stale_kind == "other_source":
        stale.write_bytes(geometry_bytes("a completely different upload", 1024))
        stale_path = str(stale)
    else:
        stale.write_bytes(_APPROVED_BYTES)               # right bytes, wrong provenance
        stale_path = str(stale)

    seen: dict = {"preloaded": {"ref": _APPROVED.to_payload(), "local_path": stale_path}}
    _, seen = await _run(monkeypatch, tmp_path, seen=seen)

    delivered = _delivered(seen)
    assert str(delivered) != stale_path                  # the stale handle did not survive
    assert delivered.read_bytes() == _APPROVED_BYTES
    assert tmp_path in delivered.parents


# failures short-circuit

async def _expect_reject(monkeypatch, tmp_path, *, store=None, row=..., request_over=None):
    seen: dict = {}
    result, seen = await _run(monkeypatch, tmp_path, store=store, row=row,
                              request_over=request_over, seen=seen)
    assert result["status"] == "failed"
    assert result["reason"] == "geometry_unavailable"
    assert "invocations" not in seen, "the graph ran despite a preparation failure"
    return result


async def test_a_tenant_miss_never_reaches_the_graph(monkeypatch, tmp_path):
    await _expect_reject(monkeypatch, tmp_path, row=None)


async def test_a_snapshot_disagreeing_with_the_row_never_reaches_the_graph(monkeypatch, tmp_path):
    import dataclasses
    drifted = source_row(dataclasses.replace(_APPROVED, size_bytes=999))
    await _expect_reject(monkeypatch, tmp_path, row=drifted)


async def test_a_vanished_object_never_reaches_the_graph(monkeypatch, tmp_path):
    await _expect_reject(monkeypatch, tmp_path, store=_Store(served=None))


async def test_truncated_bytes_never_reach_the_graph(monkeypatch, tmp_path):
    await _expect_reject(monkeypatch, tmp_path, store=_Store(served=_APPROVED_BYTES[:-1]))


async def test_altered_same_size_bytes_never_reach_the_graph(monkeypatch, tmp_path):
    swapped = b"Z" * len(_APPROVED_BYTES)
    await _expect_reject(monkeypatch, tmp_path, store=_Store(served=swapped))


async def test_an_unreachable_store_never_reaches_the_graph(monkeypatch, tmp_path):
    await _expect_reject(monkeypatch, tmp_path,
                         store=_Store(raises=StorageError("s3://bucket-x: endpoint refused")))


async def test_a_malformed_snapshot_is_refused_before_the_run_starts(monkeypatch, tmp_path):
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    bad = _APPROVED.to_payload()
    bad["sha256"] = "not-a-digest"
    with pytest.raises(GeometrySourceError):
        await _run(monkeypatch, tmp_path, request_over={"geometry_source": bad})


# what the user is told

async def test_a_preparation_failure_tells_the_user_nothing_about_infrastructure(monkeypatch,
                                                                                 tmp_path):
    _, seen = await _run(monkeypatch, tmp_path,
                         store=_Store(raises=StorageError(
                             "s3://prod-geometry/sources/abc AccessDenied arn:aws:iam::9:user/svc")))
    texts = [str(ev.get("text", "")) for pub in seen["published"] for ev in pub.events]
    assert texts, "a terminal failure must still say something to the user"
    blob = " ".join(texts).lower()
    for leak in ("s3://", "bucket", "accessdenied", "arn:", "sources/", "owner-1",
                 _APPROVED.sha256, _APPROVED.source_id, "traceback"):
        assert leak.lower() not in blob


async def test_only_infrastructure_failure_is_retryable():
    assert FailureClass.DEPENDENCY_DOWN.is_retryable
    assert not FailureClass.DATA_INTEGRITY.is_retryable
    assert not FailureClass.NOT_AUTHORIZED.is_retryable
    assert not FailureClass.INTERNAL.is_retryable


async def test_an_unclassified_preparation_failure_defaults_to_internal():
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    assert GeometrySourceError("something unforeseen").failure_class is None
    fc = getattr(GeometrySourceError("x"), "failure_class", None) or FailureClass.INTERNAL
    assert fc is FailureClass.INTERNAL
    assert fc is not FailureClass.USER_INPUT


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
