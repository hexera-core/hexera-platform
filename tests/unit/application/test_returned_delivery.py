# Responsibility: Verify the STEP a repair returns reaches the customer - a passthrough labelled as their unchanged file, never repaired - with the hints sidecar beside whichever STEP they receive.
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from tests.object_store_double import InMemoryObjectStore

from meshpipeline.application.repair_report_delivery import (
    MESH_HINTS_LOGICAL_KEY,
    RETURNED_CAD_LOGICAL_KEY,
    deliver_returned,
)
from meshpipeline.contracts.object_storage import set_object_store
from meshpipeline.persistence.models import ArtifactType
from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome


class _Db:
    async def commit(self):
        return None


def _factory():
    @asynccontextmanager
    async def _f():
        yield _Db()
    return _f


@pytest.fixture
def store():
    s = InMemoryObjectStore()
    set_object_store(s)
    yield s
    set_object_store(None)


@pytest.fixture
def rows(monkeypatch):
    seen: list[dict] = []

    async def _deliver(self, db, **kw):
        seen.append(kw)
        return DeliveryOutcome.created

    monkeypatch.setattr("meshpipeline.persistence.repositories.artifact_repository."
                        "ArtifactRepository.deliver_artifact", _deliver)
    return seen


def _returned(tmp_path, *, source="passthrough", hints=True) -> dict:
    step = tmp_path / "part-repaired.step"
    step.write_bytes(b"ISO-10303-21; the customer's own bytes")
    side = tmp_path / "part-repaired.step.mesh-hints.json"
    if hints:
        side.write_text(json.dumps({"schema": "crs-mesh-hints/1", "faces": []}))
    return {"path": str(step), "source": source, "sha256": "x",
            "hints_path": str(side) if hints else ""}


def test_a_passthrough_is_delivered_as_the_unchanged_step_with_its_sidecar(tmp_path, store, rows):
    job = str(uuid.uuid4())
    out = asyncio.run(deliver_returned(_factory(), job_id=job, returned=_returned(tmp_path)))

    assert out == {"step": "created", "hints": "created"}
    step_row, hints_row = rows
    # LABELLED HONESTLY: never the repaired class, never a `repaired-` key
    assert step_row["artifact_type"] is ArtifactType.returned_cad
    assert step_row["logical_key"] == RETURNED_CAD_LOGICAL_KEY
    assert step_row["storage_key"].startswith(f"jobs/{job}/returned-")
    assert "repaired" not in step_row["storage_key"]
    assert store.get_bytes(object_key=step_row["storage_key"]) == \
        b"ISO-10303-21; the customer's own bytes"
    # THE SIDECAR BESIDE IT
    assert hints_row["artifact_type"] is ArtifactType.mesh_hints
    assert hints_row["logical_key"] == MESH_HINTS_LOGICAL_KEY
    assert hints_row["storage_key"].startswith(f"jobs/{job}/mesh-hints-")
    assert json.loads(store.get_bytes(object_key=hints_row["storage_key"]))["schema"] == \
        "crs-mesh-hints/1"


def test_a_repaired_step_already_delivered_gets_only_its_sidecar(tmp_path, store, rows):
    out = asyncio.run(deliver_returned(_factory(), job_id=str(uuid.uuid4()),
                                       returned=_returned(tmp_path, source="repaired"),
                                       step_delivered=True))
    assert out == {"step": "", "hints": "created"}
    assert [r["artifact_type"] for r in rows] == [ArtifactType.mesh_hints]


def test_a_repaired_step_is_never_delivered_here_as_returned(tmp_path, store, rows):
    """A repaired STEP goes through deliver_repaired_cad; this never relabels one."""
    out = asyncio.run(deliver_returned(_factory(), job_id=str(uuid.uuid4()),
                                       returned=_returned(tmp_path, source="repaired")))
    # no STEP the customer holds from here, so no sidecar either: hints are an addition to a STEP
    assert out == {"step": "", "hints": ""}
    assert rows == []


def test_a_passthrough_without_hints_delivers_the_step_alone(tmp_path, store, rows):
    out = asyncio.run(deliver_returned(_factory(), job_id=str(uuid.uuid4()),
                                       returned=_returned(tmp_path, hints=False)))
    assert out == {"step": "created", "hints": ""}
    assert [r["artifact_type"] for r in rows] == [ArtifactType.returned_cad]


def test_a_store_that_refuses_never_fails_the_run(tmp_path, store, rows, monkeypatch):
    def _boom(**_kw):
        raise OSError("store down")

    monkeypatch.setattr(store, "upload_file", _boom)
    out = asyncio.run(deliver_returned(_factory(), job_id=str(uuid.uuid4()),
                                       returned=_returned(tmp_path)))
    assert out == {"step": "", "hints": ""}


def test_a_fenced_worker_is_refused_rather_than_swallowed(tmp_path, store, rows):
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced

    async def _fence(db):
        raise StaleWorkerFenced("repair report registration")

    with pytest.raises(StaleWorkerFenced):
        asyncio.run(deliver_returned(_factory(), job_id=str(uuid.uuid4()),
                                     returned=_returned(tmp_path), fence_commit=_fence))


def test_the_new_labels_match_their_revision():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "alembic" / "versions"
              / "0022_returned_cad_hints.py").read_text()
    values = {n.targets[0].id: n.value.value for n in ast.parse(source).body
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
              and isinstance(n.value, ast.Constant)}
    assert values["_RETURNED_LABEL"] == ArtifactType.returned_cad.value
    assert values["_HINTS_LABEL"] == ArtifactType.mesh_hints.value


def test_neither_new_class_is_required():
    from meshpipeline.application import artifact_policy as ap

    for label in (ArtifactType.returned_cad.value, ArtifactType.mesh_hints.value):
        assert not ap.is_required_class("gmsh", label)
        assert ap.required_ready("gmsh", [label]) is False


def test_the_terminal_path_returns_the_step_inside_the_fence_whatever_the_status():
    """The orchestrator hands back the returned STEP in the repair-evidence block - which runs on
    EVERY terminal run with a report, a refused or failed one included, and holds the fence - and
    never in the success-only artifact branch."""
    import inspect
    from pathlib import Path

    import meshpipeline.application.pipeline_run as wt
    body = Path(inspect.getfile(wt)).read_text()
    block = body[body.index("deliver_repair_report("):]
    block = block[:block.index("if final_status == JobStatus.succeeded:")]
    assert "deliver_returned(" in block
    assert '.get("returned")' in block
    # inside the fenced try: before the fence's own except
    assert block.index("deliver_returned(") < block.index("except _StaleFenced")
    assert "fence_commit=_fence_report" in block[block.index("deliver_returned("):]
