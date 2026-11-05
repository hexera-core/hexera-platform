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


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """The module remembers things per process (what was promoted, when the attempt started, what
    it has already said). A test must not inherit another's."""
    prog._PROMOTED.clear()
    prog._TORN.clear()
    prog._ATTEMPT_AT = None
    prog._SAID_PARALLEL = False
    yield
    prog._PROMOTED.clear()
    prog._TORN.clear()
    prog._ATTEMPT_AT = None
    prog._SAID_PARALLEL = False


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


def test_a_run_asked_for_frames_that_cannot_publish_them_says_why_once(monkeypatch, caplog):
    # THE DEPLOYED CASE: every environment meshes with 8 ranks, so the flag alone publishes
    # nothing. The reason is in the log rather than left to be inferred from silence - and it is
    # said once, not once per look, because the decision cannot change mid-run.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    with caplog.at_level("INFO", logger=prog.__name__):
        for _ in range(5):
            assert prog.snapshots_enabled(nproc=8) is False
    said = [r.getMessage() for r in caplog.records if "MESH_SNAPPY_RANKS=1" in r.getMessage()]
    assert len(said) == 1, said
    assert "8 snappy ranks" in said[0]


def test_a_serial_run_says_nothing_about_ranks(monkeypatch, caplog):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    with caplog.at_level("INFO", logger=prog.__name__):
        assert prog.snapshots_enabled(nproc=1) is True
    assert not [r for r in caplog.records if "MESH_SNAPPY_RANKS" in r.getMessage()]


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
    # A read that COMPLETED and found no frame in it (past the work ceiling, no boundary) has had
    # everything the directory can give: it goes, and `1/` does not ride home in the archive.
    _polymesh(tmp_path / "1")
    monkeypatch.setattr(prog, "partial_payload", lambda *a, **k: None)
    assert prog.take_castellated(tmp_path, units="mm") is None
    assert not (tmp_path / "1").exists()
    assert prog.drop_time_dirs(tmp_path) == []          # nothing staged, nothing left


def test_a_torn_read_keeps_the_frame_for_the_next_look(tmp_path, monkeypatch):
    # snappy writes a polyMesh straight to its final paths, so the all-five-present test cannot
    # prove the last file is finished. Deleting on a failed read ended snapshotting for the whole
    # run over a file a second from complete; now the next look reads the same frame again.
    _polymesh(tmp_path / "1")
    calls: list = []

    def _torn(*a, **k):
        calls.append(a)
        raise RuntimeError("owner is half written")

    monkeypatch.setattr(prog, "partial_payload", _torn)
    assert prog.take_castellated(tmp_path, units="mm") is None       # never raises at the watcher
    assert len(calls) == 1
    assert prog.take_castellated(tmp_path, units="mm") is None
    assert len(calls) == 2, "the frame was thrown away instead of being read again"
    # and when it finally reads, the frame is a frame and the directory goes
    monkeypatch.undo()
    payload = prog.take_castellated(tmp_path, units="mm")
    assert payload is not None and payload["partial"] is True
    assert prog.drop_time_dirs(tmp_path) == [], "a staged frame rode home in the archive"


def test_a_frame_kept_for_a_retry_still_cannot_reach_the_archive(tmp_path, monkeypatch):
    # The staged frame is the one copy of the mesh a failed read leaves behind. drop_time_dirs
    # runs on every exit path the engine has (native.py), so it must take the staging with it.
    _polymesh(tmp_path / "1")
    monkeypatch.setattr(prog, "partial_payload",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("torn")))
    assert prog.take_castellated(tmp_path, units="mm") is None
    assert prog.drop_time_dirs(tmp_path) == [prog._STAGING]
    assert not any(p.name.startswith(".snapshot") for p in tmp_path.iterdir())


def test_the_claim_is_held_over_the_move_and_not_over_the_decimation(tmp_path):
    # The lock exists so the watcher cannot delete a directory the promotion is about to move. It
    # must not also make a finished mesh wait on a viewer frame: promote_final_mesh runs on the
    # engine thread the moment snappy returns, so the read and the decimation happen with nothing
    # held - which is observable as the promotion completing while the payload is still building.
    _polymesh(tmp_path / "constant")
    _polymesh(tmp_path / "1")
    _polymesh(tmp_path / "2")
    (tmp_path / "2" / "polyMesh" / "points").write_text("THE MESH")
    import threading
    import unittest.mock as _m
    promoted, blocked = [], []
    real = prog.partial_payload

    def _slow(pm, **kw):
        out = real(pm, **kw)                       # the frame is read and decimated...
        t = threading.Thread(target=lambda: promoted.append(prog.promote_final_mesh(tmp_path)))
        t.start()
        t.join(5.0)                                # ...and the mesh may be promoted while it is
        blocked.append(t.is_alive())
        return out

    with _m.patch.object(prog, "partial_payload", _slow):
        out = prog.take_castellated(tmp_path, units="mm")
    assert blocked == [False], "a finished mesh waited on the viewer frame's decimation"
    assert promoted == [True]
    assert out is not None
    assert (tmp_path / "constant" / "polyMesh" / "points").read_text() == "THE MESH"


def test_a_stale_time_directory_is_never_promoted_as_this_attempts_mesh(tmp_path, monkeypatch):
    # Benign today - every dispatch extracts a fresh workspace - but _promote is the authoritative
    # stage, so a previous attempt's mesh must not be able to become this attempt's deliverable.
    # The roster parallel_stages.polymesh_complete already holds for constant/polyMesh.
    import os
    import time as _t
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    _polymesh(tmp_path / "constant")
    _polymesh(tmp_path / "1")
    old = _t.time() - 3600
    for f in (tmp_path / "1" / "polyMesh").iterdir():
        os.utime(f, (old, old))
    prog.snappy_command(nproc=1)                       # this attempt starts now
    assert prog.promote_final_mesh(tmp_path) is False, "a previous attempt's mesh was promoted"
    assert not (tmp_path / "constant" / "polyMesh").exists()


def test_a_stale_time_directory_is_never_published_as_this_attempts_frame(tmp_path, monkeypatch):
    import os
    import time as _t
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    _polymesh(tmp_path / "1")
    old = _t.time() - 3600
    for f in (tmp_path / "1" / "polyMesh").iterdir():
        os.utime(f, (old, old))
    prog.snappy_command(nproc=1)
    assert prog.take_castellated(tmp_path, units="mm") is None
    assert prog.castellated_polymesh(tmp_path) is not None, "the freshness test, not completeness"


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


def test_the_watcher_says_why_a_parallel_run_publishes_nothing(monkeypatch, tmp_path, caplog):
    # WHERE IT IS ACTUALLY REACHED: the engine asks snapshots_enabled only on its serial branch,
    # so a parallel run never consults the module - but the runner's watcher polls take_castellated
    # on every run the switch is on for. A workspace with processor* in it is that run.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    (tmp_path / "processor0" / "1" / "polyMesh").mkdir(parents=True)
    with caplog.at_level("INFO", logger=prog.__name__):
        for _ in range(4):
            assert prog.take_castellated(tmp_path, units="mm") is None
    said = [r.getMessage() for r in caplog.records if "MESH_SNAPPY_RANKS=1" in r.getMessage()]
    assert len(said) == 1, said
    assert "parallel" in said[0]


def test_a_serial_workspace_with_nothing_in_it_yet_says_nothing(monkeypatch, tmp_path, caplog):
    # The ordinary case: every look before the castellated phase is written. Silence.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    (tmp_path / "constant").mkdir()
    with caplog.at_level("INFO", logger=prog.__name__):
        assert prog.take_castellated(tmp_path, units="mm") is None
    assert caplog.records == []


# ------------------------------------------- the frame is sized before it is loaded (P1)
#
# WHY THESE EXIST. `partial_payload` used to load every mesh point and every boundary face and
# only THEN test the work ceiling - so the frame it was about to refuse was already allocated.
# This runs inside the mesher's own container, on Cloud Run with the workspace in that same
# memory (gce_vm:22), and that container has been OOM-killed at 16 GiB (bootstrap-env.sh): the
# load the ceiling exists to prevent is itself what kills the mesher, and the customer loses a
# built mesh so that we could decline to draw a picture. Each of these asserts that the GEOMETRY
# READER IS NEVER REACHED, because "returns None" was already true and was not the bug.


def _declaring(root: Path, *, faces: int | None = None, points: int | None = None) -> Path:
    """A complete minimal polyMesh whose HEADERS declare a size its arrays do not carry.

    Which is exactly what the guard must read: OpenFOAM declares `nFaces` per patch in `boundary`
    and a leading count on each list, so the number the loader would go on to allocate is readable
    without touching the arrays at all.
    """
    pm = _polymesh(root)
    if faces is not None:
        (pm / "boundary").write_text(
            _HDR.format(cls="polyBoundaryMesh", obj="boundary")
            + f"1\n(\nwalls\n{{\ntype wall;\nnFaces {faces};\nstartFace 0;\n}}\n)\n")
    if points is not None:
        (pm / "points").write_text(
            _HDR.format(cls="vectorField", obj="points")
            + f"{points}\n(\n(0 0 0)\n(1 0 0)\n(1 1 0)\n(0 1 0)\n)\n")
    return pm


@pytest.fixture
def _never_loaded(monkeypatch):
    """`boundary_surface` IS the load. Reaching it at all is the P1 bug."""
    from meshpipeline.engines.snappy import polymesh_surface as ps

    def _boom(*a, **k):
        raise AssertionError(
            "the phase mesh was loaded before its size was established - this is the load that "
            "OOM-kills the mesher and loses the customer's mesh")

    monkeypatch.setattr(ps, "boundary_surface", _boom)


def test_a_boundary_past_the_work_ceiling_is_refused_before_the_geometry_is_loaded(
        tmp_path, _never_loaded, caplog):
    pm = _declaring(tmp_path / "1", faces=prog.MAX_SOURCE_TRIANGLES)   # x2 triangles: past it
    with caplog.at_level("INFO", logger=prog.__name__):
        assert prog.partial_payload(pm, units="mm") is None
    assert any("work ceiling" in r.getMessage() for r in caplog.records)


def test_too_many_points_are_refused_before_a_single_one_is_read(tmp_path, _never_loaded):
    # The half that bites first on a big mesh: read_points reads EVERY point of the volume mesh,
    # not only the boundary's, as a blob plus a translated copy plus two arrays.
    pm = _declaring(tmp_path / "1", points=prog.MAX_SOURCE_POINTS + 1)
    assert prog.partial_payload(pm, units="mm") is None


def test_a_mesh_inside_both_ceilings_is_still_drawn(tmp_path, monkeypatch):
    # The guard refuses the oversized source and nothing else: the ordinary frame is unchanged.
    # `raising=False` so this reads the same before and after the guard exists - it is the
    # regression half of the pair, and it must not be able to pass only because of the fix.
    monkeypatch.setattr(prog, "_memory_room", lambda: 1.0, raising=False)
    pm = _declaring(tmp_path / "1", faces=1, points=4)
    payload = prog.partial_payload(pm, units="mm")
    assert payload is not None and payload["partial"] is True


def test_a_phase_mesh_that_does_not_declare_its_size_is_refused(tmp_path, _never_loaded):
    # No declared count means no way to know what loading it would cost. The conservative side of
    # that is no frame: a mesh is the deliverable and a frame is a courtesy.
    pm = _polymesh(tmp_path / "1")
    (pm / "boundary").write_text(_HDR.format(cls="polyBoundaryMesh", obj="boundary") + "0\n(\n)\n")
    assert prog.partial_payload(pm, units="mm") is None


def test_an_unreadable_points_header_is_refused_rather_than_loaded(tmp_path, _never_loaded):
    pm = _polymesh(tmp_path / "1")
    (pm / "points").write_text("FoamFile{ version 2.0; format binary; class vectorField; }\n")
    assert prog.declared_source_size(pm) is None
    assert prog.partial_payload(pm, units="mm") is None


def test_the_declared_size_comes_from_the_headers_and_not_from_the_arrays(tmp_path):
    # Proven by a header that disagrees with its own array: the number reported is the DECLARED
    # one, so it cost a few kilobytes and not the hundreds of megabytes the arrays measure.
    pm = _declaring(tmp_path / "1", faces=123_456, points=7_000_000)
    assert prog.declared_source_size(pm) == (123_456, 7_000_000)


def test_a_frame_is_declined_while_the_container_is_short_of_memory(tmp_path, _never_loaded,
                                                                    monkeypatch, caplog):
    # Independent of triangle count: the frame fits its own ceilings and still must not be read
    # now, because what it is read BESIDE is a snappy whose peak nothing here can see.
    monkeypatch.setattr(prog, "_memory_room", lambda: prog.MIN_MEMORY_ROOM / 2)
    pm = _declaring(tmp_path / "1", faces=1, points=4)
    with caplog.at_level("INFO", logger=prog.__name__):
        assert prog.partial_payload(pm, units="mm") is None
    assert any("memory is free" in r.getMessage() for r in caplog.records)


def test_unreadable_memory_information_never_refuses_a_frame(tmp_path, monkeypatch):
    # FAILS OPEN, always. Not being able to read a cgroup is a fact about the machine, never a
    # reason to withhold a courtesy - and never, ever a reason to disturb a mesh.
    monkeypatch.setattr(prog, "_memory_room", lambda: None)
    pm = _declaring(tmp_path / "1", faces=1, points=4)
    assert prog.partial_payload(pm, units="mm") is not None


def test_reading_the_memory_room_is_cheap_and_never_raises():
    room = prog._memory_room()
    assert room is None or 0.0 <= room <= 1.0


def test_a_mesh_with_room_to_spare_is_drawn_on_the_real_reading(tmp_path):
    # The real `_memory_room`, whatever this machine answers: an ordinary frame still comes back,
    # so the check cannot be the reason the feature is silent in a deployment.
    if (prog._memory_room() or 1.0) < prog.MIN_MEMORY_ROOM:
        pytest.skip("this machine is genuinely short of memory")
    pm = _declaring(tmp_path / "1", faces=1, points=4)
    assert prog.partial_payload(pm, units="mm") is not None


# ------------------------------------- a late frame never outlives the cleanup (P2)
#
# WHY THESE EXIST. The snapshot thread was never joined before `upload_result` published the
# finished result. Publishing the result is what starts the worker's collection, and the
# collection DELETES the partial object (cloud_run_client). So an upload still in flight could
# land after that delete and recreate the object after its own cleanup - and on a reused exchange
# bucket, which gets no lifecycle rule, that orphan is permanent. The frame is therefore settled
# BEFORE the result is published, under a bound: a frame is never worth delaying a mesh for.


@pytest.fixture
def _runner(monkeypatch, tmp_path):
    """`mesh_runner.run` with the cloud stubbed out: what happened, in the order it happened."""
    from meshpipeline.runtime import mesh_runner

    log: list[str] = []
    monkeypatch.setattr(mesh_runner, "_beacon", lambda *a, **k: None)
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.download_workspace",
                        lambda uri: str(tmp_path))
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.upload_result",
                        lambda *a, **k: log.append("result published"))
    monkeypatch.setattr("meshpipeline.engines.dispatch.run_engine_local",
                        lambda ws, **k: {"rc": 0})

    def run():
        mesh_runner.run("gs://b/jobs/x/input.tar.gz", "gs://b/jobs/x/output.tar.gz", "snappy", 60)
        return log

    return run


def test_an_upload_in_flight_finishes_before_the_result_is_published(monkeypatch, _runner):
    import time as _t

    from meshpipeline.runtime import mesh_runner

    log: list[str] = []
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)

    def _slow_frame(ws, output_uri, stop, partial, **kw):
        log.append("frame upload begins")
        _t.sleep(0.3)
        log.append("frame upload done")

    monkeypatch.setattr(mesh_runner, "_snapshot_watch", _slow_frame)
    out = _runner()
    log.extend(out)
    assert log == ["frame upload begins", "frame upload done", "result published"], log


def test_a_frame_that_will_not_settle_is_abandoned_rather_than_holding_the_mesh(
        monkeypatch, _runner, caplog):
    import threading as _th
    import time as _t

    from meshpipeline.runtime import mesh_runner

    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(mesh_runner, "SNAPSHOT_SETTLE_S", 0.2)
    release = _th.Event()
    monkeypatch.setattr(mesh_runner, "_snapshot_watch",
                        lambda *a, **k: release.wait(30))         # an upload that never returns
    try:
        with caplog.at_level("WARNING", logger=mesh_runner.__name__):
            began = _t.monotonic()
            assert _runner() == ["result published"]
            waited = _t.monotonic() - began
        assert waited < 5.0, f"a delivered mesh waited {waited:.1f}s on a viewer frame"
        assert any("abandoned" in r.getMessage() for r in caplog.records), caplog.text
    finally:
        release.set()


def test_the_watcher_drops_a_frame_the_finished_mesh_overtook(monkeypatch, tmp_path):
    # The window the join alone cannot close: the mesh finishes while the frame is being read and
    # decimated, which takes seconds. A frame whose moment has passed is dropped - the mesh itself
    # is about to be delivered, which is the better picture anyway.
    import threading as _th

    from meshpipeline.runtime import mesh_runner

    stop = _th.Event()
    uploads: list = []
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.upload_partial",
                        lambda *a, **k: uploads.append(a) or "jobs/x/partial-castellated.json")

    def _reads_then_the_mesh_finishes(ws, **kw):
        stop.set()                                   # upload_result is about to publish
        return {"partial": True}

    monkeypatch.setattr(prog, "take_castellated", _reads_then_the_mesh_finishes)
    partial: dict = {}
    mesh_runner._snapshot_watch(str(tmp_path), "gs://b/jobs/x/output.tar.gz", stop, partial,
                                every_s=0.01)
    assert uploads == [], "a frame was uploaded after the result was published"
    assert partial == {}, "a pointer was published for an object the cleanup will not catch"


def test_with_the_switch_off_the_runner_waits_for_nothing_and_starts_no_thread(
        monkeypatch, _runner, caplog):
    # OFF IS TODAY'S RUN, EXACTLY: no watcher, no join, nothing said. The settle is two statements
    # on an event nobody reads.
    import threading as _th
    import time as _t

    from meshpipeline.runtime import mesh_runner

    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(mesh_runner, "_snapshot_watch",
                        lambda *a, **k: pytest.fail("the watcher ran with the switch off"))
    before = _th.active_count()
    with caplog.at_level("INFO", logger=mesh_runner.__name__):
        began = _t.monotonic()
        assert _runner() == ["result published"]
        waited = _t.monotonic() - began
    assert waited < 1.0
    assert _th.active_count() <= before
    assert caplog.records == []


def test_settling_a_snapshot_side_that_never_existed_is_quiet_and_instant():
    import threading as _th

    from meshpipeline.runtime import mesh_runner

    stop = _th.Event()
    assert mesh_runner._settle_snapshots(stop, None) is True
    assert stop.is_set()


def test_the_settle_bound_is_short_enough_to_be_noise_beside_the_result_upload():
    # The promise that makes this safe: a frame is never worth delaying a delivered mesh for. The
    # result archive's own upload is budgeted in minutes, so the settle must be seconds.
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    from meshpipeline.runtime import mesh_runner

    assert 0 < mesh_runner.SNAPSHOT_SETTLE_S <= 10.0
    assert mesh_runner.SNAPSHOT_SETTLE_S < gcs_exchange.PARTIAL_UPLOAD_TIMEOUT_S / 10
