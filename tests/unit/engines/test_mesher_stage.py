# Responsibility: Verify a running mesher's stage is read from its own logs in plain words - and
# carried from the remote mesher to the run's stream: the beacon writes it beside the result, the
# waiting worker relays each new stage once, and the relay publishes it on the loop it belongs to.
# Boundaries: logs written in a temp workspace, a stood-in bucket and publisher; no mesher, no GCS.
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from meshpipeline.contracts import mesh_progress
from meshpipeline.engines.mesher_stage import read_stage


def _log(ws, name, text, age=0.0):
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_no_log_yet_is_no_stage(tmp_path):
    assert read_stage(tmp_path) is None


def test_the_newest_log_names_the_tool_at_work(tmp_path):
    _log(tmp_path, "blockMesh.log", "done", age=60)
    _log(tmp_path, "surfaceFeatureExtract.log", "reading", age=1)
    assert read_stage(tmp_path) == {"key": "features", "label": "finding the sharp edges",
                                    "tool": "surfaceFeatureExtract"}


def test_snappy_is_read_by_the_last_phase_header_it_printed(tmp_path):
    log = _log(tmp_path, "snappyHexMesh.log", "Refinement phase\n----------------\nFeature refinement iteration 0\n")
    assert read_stage(tmp_path)["key"] == "castellating"
    log.write_text(log.read_text() + "Morphing phase\n--------------\nMorph iteration 0\n")
    assert read_stage(tmp_path)["label"] == "snapping the cells to the surface"
    log.write_text(log.read_text() + "Shrinking and layer addition phase\n----------------------------------\n")
    assert read_stage(tmp_path) == {"key": "layers", "label": "adding wall layers", "tool": "snappyHexMesh"}


def test_cfmesh_is_read_from_a_log_in_a_sub_case_too(tmp_path):
    _log(tmp_path, "p1/cartesianMesh.log", "Creating octree\nCreating polyMesh from octree\n")
    assert read_stage(tmp_path)["label"] == "making the cells"


def test_a_log_nobody_knows_is_no_stage(tmp_path):
    _log(tmp_path, "something_else.log", "x")
    assert read_stage(tmp_path) is None


def test_the_worker_relays_each_new_stage_once(monkeypatch):
    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc

    class _Blob:
        def __init__(self, gen, doc):
            self.generation, self._doc = gen, doc

        def download_as_bytes(self):
            return json.dumps(self._doc).encode()

    class _Bucket:
        blob_ = None

        def get_blob(self, key):
            return self.blob_

    got: list[dict] = []
    bucket, seen = _Bucket(), {}
    with mesh_progress.relaying(got.append):
        crc._relay_progress(bucket, "p", seen)                    # nothing written yet
        bucket.blob_ = _Blob(1, {"key": "castellating", "label": "refining"})
        crc._relay_progress(bucket, "p", seen)
        crc._relay_progress(bucket, "p", seen)                    # same generation: said once
        bucket.blob_ = _Blob(2, {"key": "layers", "label": "adding wall layers"})
        crc._relay_progress(bucket, "p", seen)
    assert [g["key"] for g in got] == ["castellating", "layers"]
    crc._relay_progress(object(), "p", {})                        # a broken bucket: never raises


def test_the_beacon_writes_the_stage_as_it_moves_on(tmp_path, monkeypatch):
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    from meshpipeline.runtime import mesh_runner

    written: list[dict] = []
    monkeypatch.setattr(gcs_exchange, "upload_progress", lambda uri, stage: written.append(stage))
    monkeypatch.setattr(mesh_runner, "BEACON_EVERY_S", 0.02)
    _log(tmp_path, "snappyHexMesh.log", "Refinement phase\n")
    stop = threading.Event()
    t = threading.Thread(target=mesh_runner._beacon, args=(str(tmp_path), "gs://b/k/out.tar.gz", stop))
    t.start()
    time.sleep(0.15)
    _log(tmp_path, "snappyHexMesh.log", "Refinement phase\nMorphing phase\n")
    time.sleep(0.15)
    stop.set()
    t.join(2)
    assert [w["key"] for w in written] == ["castellating", "snapping"]


def test_the_relay_publishes_new_stages_on_the_runs_loop_from_the_meshers_thread():
    published: list[tuple] = []

    class _Pub:
        async def amesh_stage(self, key, label, op_id=""):
            published.append((key, label, op_id))

    async def _run():
        loop = asyncio.get_running_loop()
        sink = mesh_progress.publishing_sink(_Pub(), loop, op_prefix="mesh_stage:g0/a1:1")

        def _mesher():
            for key in ("castellating", "castellating", "layers"):
                mesh_progress.report({"key": key, "label": key})

        with mesh_progress.relaying(sink):
            await asyncio.to_thread(_mesher)      # the context travels into the thread
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert published == [("castellating", "castellating", "mesh_stage:g0/a1:1:1"),
                         ("layers", "layers", "mesh_stage:g0/a1:1:2")]
    mesh_progress.report({"key": "x", "label": "y"})             # nothing installed: a no-op


def test_each_attempt_has_its_own_stage_identity(tmp_path):
    a = mesh_progress.stage_op_prefix(tmp_path / "generation_0" / "attempt_1", 1)
    b = mesh_progress.stage_op_prefix(tmp_path / "generation_0" / "attempt_2", 1)
    assert a == "mesh_stage:generation_0/attempt_1:1" and a != b
