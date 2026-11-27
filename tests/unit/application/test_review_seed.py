# Responsibility: Verify a second look at a delivered mesh is seeded on a worker that never had it, from the object store alone, and is told when the mesh is kept nowhere (VER-17).
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from tests.engine_workspaces import build_workspace, engine_names

from meshpipeline.application import job_service as js
from meshpipeline.application import review_seed as RS
from meshpipeline.artifact_keys import review_seed_key
from meshpipeline.contracts import object_storage as OS
from meshpipeline.errors import FailureClass

JOB = str(uuid.uuid4())
PARENT = str(uuid.uuid4())


class _Log:
    def __init__(self): self.warnings = []; self.infos = []
    def warning(self, *a, **k): self.warnings.append(a)
    def info(self, *a, **k): self.infos.append(a)
    def error(self, *a, **k): pass


class _Store:
    """The object store, in memory: only what the seed's upload and restore use."""

    def __init__(self): self.objects: dict[str, bytes] = {}; self.downloads: list[str] = []
    def exists(self, *, object_key): return object_key in self.objects
    def put(self, key, path: Path): self.objects[key] = Path(path).read_bytes()

    def download_file(self, *, object_key, destination):
        self.downloads.append(object_key)
        if object_key not in self.objects:
            raise OS.ObjectNotFound(object_key)
        Path(destination).write_bytes(self.objects[object_key])


async def _anoop(_v=None) -> None:
    return None


def _dispute():
    return {"of_job_id": PARENT, "mode": "rebuild", "flags": [{"x": 0.0, "y": 0.0, "z": 0.0}],
            "comment": "I think this looks bad, look again"}


def _delivered_attempt(base: Path) -> Path:
    """A delivered FEA attempt as the worker that collected it left it (a big mesh made on a VM comes home here)."""
    ws = base / PARENT / "generation_1" / "attempt_2"
    ws.mkdir(parents=True)
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n" * 50)
    (ws / "mesh.inp").write_text("*NODE\n1, 0, 0, 0\n")
    (ws / "mesh_manifest.json").write_text(json.dumps({
        "mesh_mode": "gmsh", "engine_params": {"element_order": "2"}, "cell_count": 641_000,
        "flow_topology": "", "mesh_paths": {"surface": str((ws / "mesh.msh").resolve()),
                                            "volume": str((ws / "mesh.inp").resolve())}}))
    (ws / "request.txt").write_text("mesh the bracket for static stress")
    (ws / "fea_plan.json").write_text("{}")
    (ws / "gmsh_case.tar.gz").write_bytes(b"x" * 1000)          # the bundle: a download already
    (ws / "export_fluent.zip").write_bytes(b"x" * 1000)
    (ws / "kept_in_bucket.json").write_text("{}")                # names objects emptied after delivery
    (ws / "big_trace.log").write_bytes(b"0" * (RS.OTHER_FILE_MAX_BYTES + 1))
    return ws


@pytest.fixture
def two_workers(tmp_path, monkeypatch):
    """Worker A delivered the parent and kept its seed; worker B (this test's WORKSPACE_BASE) never had it."""
    import meshpipeline.settings.runtime as rtcfg
    a, b = tmp_path / "worker_a", tmp_path / "worker_b"
    b.mkdir()
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(a))
    ws = _delivered_attempt(a)
    seed = tmp_path / "seed.tgz"
    assert RS.pack(PARENT, ws, seed)
    store = _Store()
    store.put(review_seed_key(PARENT), seed)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(b))
    OS.set_object_store(store)
    yield {"a": a, "b": b, "store": store, "seed": seed, "parent_ws": ws}
    OS.set_object_store(None)


async def test_a_second_look_on_a_worker_that_never_had_the_mesh_is_seeded_from_the_object_store(two_workers):
    b = two_workers["b"]
    assert js.final_attempt_workspace(PARENT) is None, "worker B must start without the parent's workspace"
    state: dict = {}
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    restored = Path(state["openfoam_workspace"])
    assert restored == (b / PARENT / "generation_1" / "attempt_2"), restored
    assert restored.resolve().is_relative_to(b.resolve())
    assert state["engine"] == "gmsh" and state["mesh_manifest"]["cell_count"] == 641_000
    assert state["request_txt"] == "mesh the bracket for static stress"
    # the reviewer opens what the manifest names: pointed at this worker's copy, and there
    paths = state["mesh_manifest"]["mesh_paths"]
    assert paths["surface"] == str((restored / "mesh.msh").resolve()) and Path(paths["surface"]).is_file()
    assert Path(paths["volume"]).is_file()
    assert two_workers["store"].downloads == [review_seed_key(PARENT)]


async def test_a_worker_that_still_has_the_mesh_never_fetches_it(two_workers, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(two_workers["a"]))
    state: dict = {}
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert Path(state["openfoam_workspace"]) == two_workers["parent_ws"]
    assert two_workers["store"].downloads == []


def test_the_seed_carries_what_a_second_look_needs_and_nothing_the_downloads_hold(two_workers):
    names = [rel for rel, _p in RS.members(two_workers["parent_ws"])]
    assert set(names[:2]) == {"mesh.inp", "mesh.msh"}, "the files the reviewer opens come first"
    assert {"mesh_manifest.json", "request.txt", "fea_plan.json"} <= set(names)
    for left_out in ("gmsh_case.tar.gz", "export_fluent.zip", "kept_in_bucket.json", "big_trace.log"):
        assert left_out not in names, left_out


def test_the_same_attempt_packs_to_the_same_bytes(two_workers, tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(two_workers["a"]))     # packed where it was delivered
    again = tmp_path / "again.tgz"
    assert RS.pack(PARENT, two_workers["parent_ws"], again)
    assert again.read_bytes() == two_workers["seed"].read_bytes(), "a repeated delivery would trip the conflict guard"


async def test_a_mesh_kept_nowhere_ends_the_second_look_told_never_as_our_failure(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))
    OS.set_object_store(_Store())                       # the store is there; the seed is not (purged, or older)
    try:
        state: dict = {}
        with pytest.raises(js.DisputeMaterialGone) as ei:
            await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
        assert state == {}
        gone = ei.value
        assert gone.failure_class is FailureClass.USER_INPUT and not gone.failure_class.is_system
    finally:
        OS.set_object_store(None)

    said: list = []
    failed: list = []

    async def _fail_row(session_factory, job_id, fc, *, job_repo, jlog):
        failed.append((job_id, fc))

    monkeypatch.setattr("meshpipeline.application.geometry_materializer._fail_job_row", _fail_row)
    out = await js.refuse_dispute(None, JOB, gone, job_repo=None, jlog=_Log(), publish=said.append)
    assert out == {"job_id": JOB, "status": "failed", "reason": "dispute_mesh_gone"}
    assert failed == [(JOB, FailureClass.USER_INPUT)]
    assert said == [js.DISPUTE_MESH_GONE]
    assert "on our side" not in said[0] and "new run" in said[0]


async def test_a_store_that_cannot_be_reached_is_said_as_ours_and_retryable(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))

    class _Down(_Store):
        def exists(self, *, object_key):
            raise ConnectionError("store unreachable")

    OS.set_object_store(_Down())
    try:
        with pytest.raises(js._SystemFailure) as ei:
            await js.seed_dispute_run({}, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    finally:
        OS.set_object_store(None)
    assert not isinstance(ei.value, js.DisputeMaterialGone)
    assert ei.value.failure_class is FailureClass.DEPENDENCY_DOWN


def test_a_seed_that_escapes_its_place_is_refused(tmp_path, monkeypatch):
    import io
    import tarfile

    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path / "base"))
    bad = tmp_path / "bad.tgz"
    with tarfile.open(bad, "w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../../outside.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    store = _Store()
    store.put(review_seed_key(PARENT), bad)
    with pytest.raises(Exception):  # noqa: B017 - refused by the safe extraction, whatever its words
        RS.restore(PARENT, store=store)
    assert not (tmp_path / "outside.txt").exists()
    assert js.final_attempt_workspace(PARENT) is None
    shutil.rmtree(tmp_path / "base", ignore_errors=True)


@pytest.mark.parametrize("engine", engine_names())
def test_every_delivery_keeps_a_seed_beside_the_mesh_never_as_a_download(tmp_path, engine):
    import tarfile

    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.persistence.models import ArtifactType

    ws = build_workspace(tmp_path, engine)
    job = uuid.uuid4()
    scratch: list = []
    plan = au._build_plan(job, ws, scratch, engine)
    seed = [p for p in plan if p.logical_key == RS.SEED_LOGICAL_KEY]
    assert len(seed) == 1 and not seed[0].required, "a seed that cannot be made must never cost the delivery"
    assert seed[0].artifact_type is ArtifactType.viewer_data, "the console must never offer it for download"
    assert seed[0].object_key == review_seed_key(str(job))
    with tarfile.open(seed[0].local_path) as tar:
        names = tar.getnames()
    assert "attempt/mesh_manifest.json" in names and RS.SEED_RECORD in names
    assert not any(n.startswith("attempt/viewer_") or n.endswith(".tar.gz") for n in names), names


def test_a_parent_id_that_is_not_a_job_id_restores_nothing(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))
    store = _Store()
    assert RS.restore("../../etc", store=store) is None and store.downloads == []
