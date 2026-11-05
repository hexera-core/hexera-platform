"""Every native run's logs are kept, for every engine (contracts/mesh_execution.run_mesh): a retry no longer
overwrites the first run's failure. The kept logs are never part of a submission (archive, digest)."""
from __future__ import annotations

import io
import tarfile

from meshpipeline.contracts import mesh_execution as ME


class _Runner:
    def __init__(self):
        self.n = 0

    def run(self, workspace, *, engine, timeout):
        self.n += 1
        (workspace / "log.snappyHexMesh").write_text(f"run {self.n} snappy")
        (workspace / "case").mkdir(exist_ok=True)
        (workspace / "case" / "cartesianMesh.log").write_text(f"run {self.n} cfmesh")
        (workspace / "gmshfea.log").write_text(f"run {self.n} gmsh")
        return {"rc": 1, "timed_out": False}


def test_each_run_keeps_its_own_logs(tmp_path, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(ME, "_executor", runner)
    monkeypatch.setattr(ME, "_launch_check", None)
    for _ in range(2):
        ME.run_mesh(tmp_path, engine="snappy", timeout=10)
    first = ME.read_native_run(tmp_path) - 1
    kept = tmp_path / ME.NATIVE_LOGS_DIR
    for k, n in ((first, 1), (first + 1, 2)):
        d = kept / f"run_{k}"
        assert (d / "log.snappyHexMesh").read_text() == f"run {n} snappy"
        assert (d / "case" / "cartesianMesh.log").read_text() == f"run {n} cfmesh"
        assert (d / "gmshfea.log").read_text() == f"run {n} gmsh"


def test_kept_logs_never_enter_a_submission(tmp_path):
    from meshpipeline.adapters.mesh_execution.cloud_run_client import _tar_dir
    from meshpipeline.application.native_submission import workspace_digest

    (tmp_path / "case.stl").write_text("solid")
    before = workspace_digest(tmp_path)
    (tmp_path / ME.NATIVE_LOGS_DIR / "run_1").mkdir(parents=True)
    (tmp_path / ME.NATIVE_LOGS_DIR / "run_1" / "log.x").write_text("old")
    assert workspace_digest(tmp_path) == before
    names = tarfile.open(fileobj=io.BytesIO(_tar_dir(tmp_path)), mode="r:gz").getnames()
    assert not any(ME.NATIVE_LOGS_DIR in n for n in names) and "case.stl" in names
