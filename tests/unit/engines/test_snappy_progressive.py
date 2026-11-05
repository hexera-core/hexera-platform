# Responsibility: Verify the progressive mesh snapshot - off changes nothing, the parallel path never
#                 snapshots, the background box is never read as a partial, and the intermediate goes.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import meshpipeline.events as E
import meshpipeline.settings.mesh_snapshot as mscfg
from meshpipeline.artifact_keys import PARTIAL_STAGES, partial_key_beside
from meshpipeline.contracts import mesh_progress
from meshpipeline.engines.snappy import progressive as prog

NATIVE = Path(__file__).parents[3] / "src" / "meshpipeline" / "engines" / "snappy" / "native.py"

_HDR = "FoamFile{{ version 2.0; format ascii; class {cls}; object {obj}; }}\n"


def _polymesh(root: Path, *, complete: bool = True) -> Path:
    """A minimal ASCII polyMesh: one square face on one patch. Enough for the boundary reader."""
    pm = root / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "points").write_text(
        _HDR.format(cls="vectorField", obj="points")
        + "4\n(\n(0 0 0)\n(1 0 0)\n(1 1 0)\n(0 1 0)\n)\n")
    (pm / "faces").write_text(
        _HDR.format(cls="faceList", obj="faces") + "1\n(\n4(0 1 2 3)\n)\n")
    (pm / "boundary").write_text(
        _HDR.format(cls="polyBoundaryMesh", obj="boundary")
        + "1\n(\nwalls\n{\ntype wall;\nnFaces 1;\nstartFace 0;\n}\n)\n")
    (pm / "owner").write_text("0\n")
    if complete:
        (pm / "neighbour").write_text("0\n")
    return pm


# ---------------------------------------------------------------- the flag, off and on


def test_with_the_flag_off_the_invocation_is_exactly_todays(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", False)
    assert prog.snappy_command(nproc=1) == "snappyHexMesh -overwrite"
    assert prog.snapshots_enabled(nproc=1) is False


def test_with_the_flag_on_a_serial_run_keeps_its_phase_meshes(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    assert prog.snapshots_enabled(nproc=1) is True
    assert prog.snappy_command(nproc=1) == "snappyHexMesh"


def test_the_parallel_path_never_snapshots(monkeypatch):
    # Even with the switch on: a frame there needs a reconstructParMesh per snapshot, minutes of
    # serial CPU charged against the mesh's own timeout, on the largest jobs.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    for nproc in (2, 4, 12):
        assert prog.snapshots_enabled(nproc=nproc) is False


def test_the_parallel_invocation_in_the_engine_still_overwrites():
    # The decision lives in the serial branch only; the mpirun line is untouched, so a parallel
    # run writes processor*/1/polyMesh and never ws/1/polyMesh - which is what makes the runner's
    # watcher self-gating.
    src = NATIVE.read_text()
    assert "snappyHexMesh -overwrite -parallel" in src
    assert src.count("snappy_command(nproc=nproc)") == 1


def test_the_runner_starts_no_watcher_when_the_switch_is_off(monkeypatch, tmp_path):
    from meshpipeline.runtime import mesh_runner

    started: list = []
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(mesh_runner, "_snapshot_watch",
                        lambda *a, **k: started.append(a))         # never reached when off
    monkeypatch.setattr(mesh_runner, "_beacon", lambda *a, **k: None)
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.download_workspace",
                        lambda uri: str(tmp_path))
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.upload_result",
                        lambda *a, **k: None)
    monkeypatch.setattr("meshpipeline.engines.dispatch.run_engine_local",
                        lambda ws, **k: {"rc": 0})
    mesh_runner.run("gs://b/jobs/x/input.tar.gz", "gs://b/jobs/x/output.tar.gz", "snappy", 60)
    assert started == []


# ---------------------------------------------------------------- the background box


def test_the_background_box_is_never_read_as_a_partial(tmp_path):
    # Mid-run constant/polyMesh holds blockMesh's background box. Refused by PATH, so no caller
    # can reach it by remembering not to pass it.
    box = _polymesh(tmp_path / "constant")
    with pytest.raises(ValueError, match="numbered time directory"):
        prog.partial_payload(box, units="mm")


def test_only_a_numbered_time_directory_counts_as_one(tmp_path):
    _polymesh(tmp_path / "constant")
    _polymesh(tmp_path / "system")
    _polymesh(tmp_path / "1")
    assert [d.name for d in prog.time_dirs(tmp_path)] == ["1"]
    assert prog.castellated_polymesh(tmp_path) == tmp_path / "1" / "polyMesh"


def test_a_half_written_phase_mesh_is_not_a_partial(tmp_path):
    _polymesh(tmp_path / "1", complete=False)       # snappy still writing: no neighbour yet
    assert prog.castellated_polymesh(tmp_path) is None


# ---------------------------------------------------------------- the payload


def test_the_partial_says_what_it_is_and_is_not(tmp_path):
    pm = _polymesh(tmp_path / "1")
    payload = prog.partial_payload(pm, units="mm")
    assert payload["kind"] == "polymesh" and payload["mesh_units"] == "mm"
    assert payload["is_mesh"] is False and payload["partial"] is True
    assert payload["stage"] == "castellated"
    assert [p["name"] for p in payload["patches"]] == ["walls"]
    assert payload["patches"][0]["face_count"] == 2          # one quad, fanned into triangles
    json.dumps(payload)                                       # it travels as JSON


def test_the_intermediate_is_deleted_the_moment_it_is_read(tmp_path):
    _polymesh(tmp_path / "1")
    payload = prog.take_castellated(tmp_path, units="mm")
    assert payload is not None
    assert not (tmp_path / "1").exists(), "a second copy of the mesh rode home in the archive"


def test_the_intermediate_is_deleted_even_when_no_frame_comes_of_it(tmp_path, monkeypatch):
    _polymesh(tmp_path / "1")
    monkeypatch.setattr(prog, "partial_payload",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no numpy")))
    with pytest.raises(RuntimeError):
        prog.take_castellated(tmp_path, units="mm")
    assert not (tmp_path / "1").exists()


# ---------------------------------------------------------------- the final mesh


def test_the_final_mesh_lands_where_every_consumer_expects_it(tmp_path):
    _polymesh(tmp_path / "constant")                  # blockMesh's background box
    (tmp_path / "constant" / "polyMesh" / "points").write_text("THE BOX")
    _polymesh(tmp_path / "1")
    _polymesh(tmp_path / "2")
    (tmp_path / "2" / "polyMesh" / "points").write_text("THE MESH")
    assert prog.promote_final_mesh(tmp_path) is True
    assert (tmp_path / "constant" / "polyMesh" / "points").read_text() == "THE MESH"
    assert prog.time_dirs(tmp_path) == []


def test_a_promotion_with_nothing_to_promote_fails_closed(tmp_path):
    _polymesh(tmp_path / "constant")                  # only the background box exists
    assert prog.promote_final_mesh(tmp_path) is False
    assert not (tmp_path / "constant" / "polyMesh").exists(), \
        "the background box survived promotion and would be delivered as the mesh"


def test_a_snapshot_cannot_take_a_phase_mesh_the_promotion_claimed(tmp_path):
    _polymesh(tmp_path / "constant")
    _polymesh(tmp_path / "1")
    assert prog.promote_final_mesh(tmp_path) is True
    _polymesh(tmp_path / "1")                          # a late look finds a directory again
    assert prog.take_castellated(tmp_path, units="mm") is None


# ---------------------------------------------------------------- the key, and the event


def test_the_partial_is_a_sibling_of_the_result():
    out = "jobs/" + "a" * 64 + "/output.tar.gz"
    assert partial_key_beside(out, "castellated") == "jobs/" + "a" * 64 + "/partial-castellated.json"
    assert PARTIAL_STAGES == (prog.CASTELLATED,)


def test_a_phase_nobody_publishes_cannot_become_an_object_path():
    with pytest.raises(ValueError):
        partial_key_beside("jobs/x/output.tar.gz", "../../etc")


def test_the_event_carries_the_pointer_and_nothing_else():
    wire = E.mesh_partial("builder", "castellated", "jobs/abc/partial-castellated.json").wire()
    assert wire["type"] == "mesh_partial" and wire["type"] in E.EVENT_TYPES
    assert wire["phase"] == "castellated"
    assert wire["object_key"] == "jobs/abc/partial-castellated.json"
    assert not any(k in wire for k in ("patches", "points_b64", "payload"))


def test_the_pointer_is_published_once_even_while_the_stage_repeats():
    published: list[tuple] = []

    class _Pub:
        async def amesh_stage(self, key, label, op_id=""):
            pass

        async def amesh_partial(self, phase, object_key, op_id=""):
            published.append((phase, object_key))

    async def _run():
        loop = asyncio.get_running_loop()
        sink = mesh_progress.publishing_sink(_Pub(), loop, op_prefix="mesh_stage:g0/a1:1")
        with mesh_progress.relaying(sink):
            def _beacons():
                for _ in range(3):
                    mesh_progress.report({"key": "castellating", "label": "refining",
                                          "partial_key": "jobs/x/partial-castellated.json",
                                          "partial_stage": "castellated"})
            await asyncio.to_thread(_beacons)
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert published == [("castellated", "jobs/x/partial-castellated.json")]
