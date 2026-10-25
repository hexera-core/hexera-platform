# Responsibility: Verify that no guessed clock blocks a clean file on the way from upload to the
# stage - the check's limits grow with the file, a worker that still beats is never called stuck, the
# part is shown (a preview) before it is measured and stays shown when the full reading fails, the
# skin is sized to the model, and the chat's hold follows the worker's progress.
# Boundaries: the geometry check's plumbing with an in-memory store; the CAD reading is stubbed
# where it would be slow, the triangle (CFD) path runs for real on a small box.
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_progress as gp


class _Store:
    """Uploads and reads, like the object store."""

    def __init__(self):
        self.written: dict[str, bytes] = {}
        self.history: list[tuple[str, dict]] = []

    def upload_file(self, *, local_path, object_key, **_):
        data = Path(local_path).read_bytes()
        self.written[object_key] = data
        if object_key.endswith("scout.json"):
            self.history.append((object_key, json.loads(data)))

    def get_bytes(self, *, object_key):
        from meshpipeline.contracts.object_storage import ObjectNotFound

        if object_key not in self.written:
            raise ObjectNotFound(object_key)
        return self.written[object_key]

    def delete_object(self, *, object_key):
        self.written.pop(object_key, None)


@pytest.fixture()
def store(monkeypatch, tmp_path):
    from meshpipeline.contracts import object_storage

    s = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: s)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return s


# ------------------------------------------------------------------------------ the limits ----
def test_the_limits_grow_with_the_file_and_a_small_file_keeps_its_own():
    assert gc.step_limits("scout", None) == (gc.SCOUT_SOFT_LIMIT_S, gc.SCOUT_HARD_LIMIT_S)
    assert gc.step_limits("scout", 200_000) == (gc.SCOUT_SOFT_LIMIT_S + 12, gc.SCOUT_HARD_LIMIT_S + 12)
    soft, hard = gc.step_limits("scout", 45_857_044)               # jubilee_frame
    assert soft > 3000 and hard - soft == gc.SCOUT_HARD_LIMIT_S - gc.SCOUT_SOFT_LIMIT_S
    assert gc.step_limits("naming", None) == (gc.NAMING_SOFT_LIMIT_S, gc.NAMING_HARD_LIMIT_S)
    assert gc.step_limits("naming", 45_857_044)[0] > gc.NAMING_SOFT_LIMIT_S
    assert gc.step_limits("scout", 1e12)[1] <= gc.LIMIT_CEILING_S  # the backstop's ceiling


def test_the_pending_marker_carries_the_limits_the_task_is_queued_with(store):
    from meshpipeline.contracts import geometry_check as seam

    queued = []
    seam.set_scout_enqueuer(lambda **kw: queued.append(kw))
    try:
        source = {"size_bytes": 45_857_044, "suffix_hint": ".step"}
        assert gc.start_scout("s-1", "o1", source=source, interpretation=None)
    finally:
        seam.set_scout_enqueuer(None)
    marker = gc.read_check("s-1")
    assert (marker["soft_limit_s"], marker["hard_limit_s"]) == gc.step_limits("scout", 45_857_044)
    assert gc.limits_of(marker) == tuple(float(v) for v in gc.step_limits("scout", 45_857_044))


def test_the_worker_is_queued_with_the_limits_sized_to_the_file(monkeypatch):
    from meshpipeline.adapters.pipeline_execution import celery as tasks
    from meshpipeline.runtime import composition

    seen = {}
    monkeypatch.setattr(tasks.scout_geometry, "apply_async", lambda **kw: seen.update(kw), raising=False)
    composition._enqueue_geometry_scout(session_id="s-1", owner_id="o1", source={"size_bytes": 45_857_044},
                                        interpretation=None)
    assert (seen["soft_time_limit"], seen["time_limit"]) == gc.step_limits("scout", 45_857_044)
    assert seen["kwargs"]["source"] == {"size_bytes": 45_857_044}     # the task's own kwargs are untouched


# --------------------------------------------------------------------------- the time box ----
T0 = 1_000_000.0


def _pending(**limits):
    return {"status": "pending", "written_at": T0, **limits}


def _scout_record(beat_at=None, ended=False, marker=T0):
    part = {"picked_at": T0 + 1, "marker": marker, "steps": {"measuring": {"at": T0 + 2}}, "current": "measuring"}
    if beat_at is not None:
        part["beat_at"] = beat_at
    if ended:
        part["ended_at"] = beat_at or T0 + 3
    return {"scout": part}


def test_a_check_whose_worker_still_beats_is_never_called_stuck_however_long_it_takes():
    late = T0 + 10 * gc.SCOUT_HARD_LIMIT_S
    progress = _scout_record(beat_at=late - 20)
    assert gc.time_boxed(_pending(), None, now=late, progress=progress)["status"] == "pending"


def test_a_check_whose_worker_went_quiet_is_failed_with_the_way_on():
    progress = _scout_record(beat_at=T0 + 60)
    reported = gc.time_boxed(_pending(), None, now=T0 + 60 + gp.BEAT_STALE_S + 1, progress=progress)
    assert reported["status"] == "failed" and reported["reason"] == gc.SCOUT_STOPPED
    assert reported["retry"] == "scout"
    # within the quiet allowance it is still at work
    assert gc.time_boxed(_pending(), None, now=T0 + 60 + gp.BEAT_STALE_S - 1, progress=progress)["status"] == "pending"


def test_a_check_with_no_heartbeat_is_held_to_its_own_limits_not_the_small_files():
    soft, hard = gc.step_limits("scout", 45_857_044)
    marker = _pending(soft_limit_s=soft, hard_limit_s=hard)
    past_small = T0 + gc.SCOUT_HARD_LIMIT_S + gc.STALE_MARGIN_S + 5
    assert gc.time_boxed(marker, None, now=past_small)["status"] == "pending"
    assert gc.time_boxed(marker, None, now=T0 + hard + gc.STALE_MARGIN_S + 5)["status"] == "failed"
    # an old marker with no limits on it: the small-file limits, as before
    assert gc.time_boxed(_pending(), None, now=past_small)["status"] == "failed"


def test_an_earlier_attempts_heartbeat_says_nothing_of_this_one():
    progress = _scout_record(beat_at=T0 - 5000, marker=T0 - 6000)
    assert gp.beating(progress["scout"], T0 + 10, marker=T0) is None


def test_a_preview_whose_scout_stopped_stands_as_the_checks_result():
    preview = {"status": "scouted", "written_at": T0, "preview": gc.PREVIEW_RUNNING,
               "facts": {"notes": [], "validity": {"kind": "unchecked", "clean": None}},
               "proposal": {"notes": []}}
    progress = _scout_record(beat_at=T0 + 30)
    kept = gc.time_boxed(preview, None, now=T0 + 30 + gp.BEAT_STALE_S + 1, progress=progress)
    assert kept["status"] == "scouted" and kept["preview"] == gc.PREVIEW_KEPT
    assert any("did not finish" in n for n in kept["proposal"]["notes"])
    # still beating: still a preview being measured
    assert gc.time_boxed(preview, None, now=T0 + 60, progress=progress)["preview"] == gc.PREVIEW_RUNNING
    assert gc.is_preview(preview) and not gc.is_preview(kept)


# ------------------------------------------------------------------------- the heartbeat ----
def test_the_naming_never_puts_back_an_old_copy_of_the_scouts_heartbeat(store):
    scout = gp.Tracker("s-2", "scout")
    scout.begin(marker=T0)
    naming = gp.Tracker("s-2", "naming")
    naming.begin()
    scout.beat()
    beat = gp.read_progress("s-2")["scout"]["beat_at"]
    naming.step(gp.NAMING)                         # the naming writes after the scout's beat
    after = gp.read_progress("s-2")
    assert after["scout"]["beat_at"] == beat and after["naming"]["current"] == gp.NAMING


def test_the_heartbeat_beats_until_it_is_stopped(store):
    import time

    t = gp.Tracker("s-3", "scout")
    t.begin()
    t.start_heartbeat(every_s=0.05)
    first = gp.read_progress("s-3")["scout"]["beat_at"]
    time.sleep(0.3)
    t.stop_heartbeat()
    assert gp.read_progress("s-3")["scout"]["beat_at"] > first


# --------------------------------------------------------------- the preview (CFD and FEA) ----
def _box_stl(path: Path, size=(0.1, 0.05, 0.02)) -> Path:
    from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary

    write_stl_binary(path, _box_triangles((0, 0, 0), size))
    return path


def _triangle_source():
    return {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/box.stl", "sha256": "0" * 64,
            "size_bytes": 684, "original_filename": "box.stl", "suffix_hint": ".stl"}


def _triangle_check(monkeypatch, tmp_path):
    """A triangle file (the CFD path's STL) through the real scout, the file fetched from disk and
    the pictures not drawn."""
    import meshpipeline.render.scout_snapshots as snaps_mod

    stl = _box_stl(tmp_path / "box_mm.stl", size=(100.0, 50.0, 20.0))

    def fetch(ref, work):
        dest = work / "geometry.stl"
        dest.write_bytes(stl.read_bytes())
        return dest
    monkeypatch.setattr(gc, "_fetch", fetch)
    monkeypatch.setattr(snaps_mod, "render_snapshots", lambda skin, openings, out: [])
    monkeypatch.setattr(snaps_mod, "render_upright_sheet",
                        lambda skin, out, **k: (_ for _ in ()).throw(RuntimeError("nothing is drawn in this test")))


def test_the_part_is_shown_before_it_is_measured_and_the_full_reading_replaces_it(store, monkeypatch, tmp_path):
    _triangle_check(monkeypatch, tmp_path)
    gc.write_status("cfd-1", "pending")
    result = gc.run_geometry_check(session_id="cfd-1", owner_id="o1", source=_triangle_source())
    statuses = [(r["status"], r.get("preview")) for _, r in store.history]
    # pending, then the preview the stage opens on, then the full reading - which is no preview
    assert statuses[1] == ("scouted", gc.PREVIEW_RUNNING)
    assert statuses[-1] == ("scouted", None) and result["status"] == "scouted" and "preview" not in result
    preview = store.history[1][1]
    assert preview["facts"]["size_mm"] == [100.0, 50.0, 20.0]
    assert preview["skin_key"].endswith("skin.json") and preview["proposal"]["openings"] == []
    assert preview["facts"]["validity"]["kind"] == "unchecked"


def test_a_full_reading_that_fails_leaves_the_preview_to_confirm_never_a_failed_check(store, monkeypatch, tmp_path):
    import meshpipeline.cad.scout_mesh as scout_mesh_mod

    _triangle_check(monkeypatch, tmp_path)

    def soft_limit(*a, **k):
        class SoftTimeLimitExceeded(Exception):
            pass
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(scout_mesh_mod, "scout_mesh", soft_limit)
    gc.write_status("cfd-2", "pending")
    result = gc.run_geometry_check(session_id="cfd-2", owner_id="o1", source=_triangle_source())
    assert result["status"] == "scouted" and result["preview"] == gc.PREVIEW_KEPT
    assert result["preview_reason"] == gc.SCOUT_TOO_LONG
    stored = gc.read_check("cfd-2")
    assert stored["status"] == "scouted" and stored["facts"]["size_mm"] == [100.0, 50.0, 20.0]
    assert "sessions/cfd-2/geometry_check/skin.json" in store.written
    # an FEA part confirms on it: a closed solid is not ruled out by a clean check that did not run
    from meshpipeline.application import fea_check
    assert fea_check.blocker(stored["facts"])[0] == ""


def test_a_failure_before_anything_was_drawn_is_still_a_failed_check_with_a_retry(store, monkeypatch, tmp_path):
    def fetch(ref, work):
        raise RuntimeError("the retrieved geometry does not match the upload")
    monkeypatch.setattr(gc, "_fetch", fetch)
    gc.write_status("cfd-3", "pending")
    result = gc.run_geometry_check(session_id="cfd-3", owner_id="o1", source=_triangle_source())
    assert result["status"] == "failed" and result["retry"] == "scout"


def test_pictures_that_cannot_be_drawn_never_fail_the_check(store, monkeypatch, tmp_path):
    import meshpipeline.render.scout_snapshots as snaps_mod

    _triangle_check(monkeypatch, tmp_path)
    monkeypatch.setattr(snaps_mod, "render_snapshots",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no display")))
    gc.write_status("cfd-4", "pending")
    result = gc.run_geometry_check(session_id="cfd-4", owner_id="o1", source=_triangle_source())
    assert result["status"] == "scouted" and result["snapshots"] == []
    assert any("pictures could not be drawn" in n for n in result["facts"]["notes"])


def test_the_naming_waits_for_the_full_reading_not_the_preview(store, monkeypatch):
    monkeypatch.setattr(gc, "NAMING_WAIT_S", 0.0)
    monkeypatch.setattr(gc, "naming_limits_for", lambda sid: (120, 420))
    gc.write_status("n-1", "scouted", preview=gc.PREVIEW_RUNNING, facts={})
    calls = []
    real = gc.read_check

    def read(sid):
        calls.append(sid)
        if len(calls) == 2:      # the full reading lands while the naming waits
            gc.write_status("n-1", "scouted", facts={"full": True})
        return real(sid)
    monkeypatch.setattr(gc, "read_check", read)
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    assert gc._wait_for_scout("n-1")["facts"] == {"full": True}


def test_the_progress_line_says_the_part_is_shown_while_it_is_measured():
    stored = {"status": "scouted", "written_at": T0 + 50, "preview": "running"}
    view = gp.progress_view(stored, None, _scout_record(beat_at=T0 + 40), now=T0 + 60,
                            scout_limit_s=900, naming_limit_s=600)
    assert view["state"] == "running" and view["live"] and view["preview"]
    assert "press Proceed now" in view["note"]


# ---------------------------------------------------------------------- the chat's hold ----
def test_the_chat_keeps_holding_while_the_worker_is_at_work():
    from meshpipeline.application.geometry_hold import WAIT_GRACE_S, hold_decision

    stored = {"status": "pending"}
    asked = {"requested_at": T0}
    late = T0 + WAIT_GRACE_S + 600
    assert hold_decision(stored, asked, None, now=late) is None
    assert hold_decision(stored, asked, None, now=late, working=True) == "wait"


# ---------------------------------------------------------------------------- the skin ----
def test_a_small_skin_is_drawn_as_it_is():
    from meshpipeline.render.skin_mesh import simplified

    tris = np.random.default_rng(1).random((500, 3, 3))
    assert simplified(tris, 1000) is not None and np.array_equal(simplified(tris, 1000), tris)


def test_a_big_skin_is_drawn_simplified_with_its_size_kept():
    from meshpipeline.render.skin_mesh import simplified

    # a finely tessellated sphere, 200 x 400 = 80,000 triangles, radius 0.5
    u, v = np.meshgrid(np.linspace(0, np.pi, 201), np.linspace(0, 2 * np.pi, 401), indexing="ij")
    p = np.stack([0.5 * np.sin(u) * np.cos(v), 0.5 * np.sin(u) * np.sin(v), 0.5 * np.cos(u)], axis=-1)
    a, b, c, d = p[:-1, :-1], p[1:, :-1], p[1:, 1:], p[:-1, 1:]
    tris = np.concatenate([np.stack([a, b, c], axis=-2).reshape(-1, 3, 3),
                           np.stack([a, c, d], axis=-2).reshape(-1, 3, 3)])
    out = simplified(tris, 10_000)
    assert 1_000 < len(out) <= 10_000
    lo, hi = out.reshape(-1, 3).min(axis=0), out.reshape(-1, 3).max(axis=0)
    assert np.allclose(hi - lo, 1.0, atol=0.03)


def test_the_stage_skin_of_a_big_file_says_it_was_simplified(tmp_path, monkeypatch):
    import meshpipeline.render.skin_mesh as skin_mesh

    stl = _box_stl(tmp_path / "box.stl")
    assert "simplified_from" not in gc.skin_payload(stl)              # a small skin: drawn as it is
    monkeypatch.setattr(skin_mesh, "simplified", lambda tris: tris[:6])   # as if past the budget
    payload = gc.skin_payload(stl)
    assert payload["simplified_from"] == 12 and payload["patches"][0]["tri_count"] == 6


def test_a_small_model_is_tessellated_exactly_as_before(monkeypatch):
    from meshpipeline.cad import occ_box

    calls = []
    monkeypatch.setattr(occ_box, "face_count", lambda shape: occ_box.FULL_DETAIL_FACES)
    monkeypatch.setattr(occ_box, "mesh_to_size", lambda shape, f, a, **k: calls.append((f, a, k)) or ((0,) * 6, f))
    occ_box.mesh_for_model(object(), 1 / 1500, 0.3)
    assert calls == [(1 / 1500, 0.3, {})]
    calls.clear()
    monkeypatch.setattr(occ_box, "face_count", lambda shape: 40_536)          # jubilee_frame
    occ_box.mesh_for_model(object(), 1 / 1500, 0.3)
    (f, a, k), = calls
    assert f == pytest.approx(1 / 1500 * occ_box.coarsening(40_536)) and a == occ_box.MAX_ANGULAR
    assert k == {"min_size": True} and occ_box.coarsening(40_536) == pytest.approx(3.676, abs=1e-3)


def test_a_real_step_part_is_drawn_and_meshed_unchanged_when_small(tmp_path):
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.StlAPI import StlAPI_Writer

    from meshpipeline.cad.occ_box import face_count, mesh_for_model, mesh_to_size, triangle_count

    a = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    b = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    assert face_count(a) == 6
    box_a, lin_a = mesh_for_model(a, 1 / 1500, 0.3)
    box_b, lin_b = mesh_to_size(b, 1 / 1500, 0.3)
    assert box_a == box_b and lin_a == lin_b and triangle_count(a) == triangle_count(b) == 12
    assert StlAPI_Writer is not None


# ------------------------------------------------------------------------ the assembly join ----
def test_the_assembly_join_is_given_time_for_its_size(tmp_path):
    from meshpipeline.cad.ingest.assembly_files import (
        JOIN_TIMEOUT_CEILING_S,
        JOIN_TIMEOUT_S,
        Member,
        join_timeout_s,
    )

    small = tmp_path / "a.step"
    small.write_bytes(b"x" * 1000)
    assert join_timeout_s([Member(small, "a.step")]) == pytest.approx(JOIN_TIMEOUT_S, abs=0.01)
    big = tmp_path / "b.step"
    with big.open("wb") as fh:
        fh.truncate(60_000_000)
    assert join_timeout_s([Member(big, "b.step")]) == pytest.approx(JOIN_TIMEOUT_S + 240.0)
    huge = tmp_path / "c.step"
    with huge.open("wb") as fh:
        fh.truncate(900_000_000)
    assert join_timeout_s([Member(huge, "c.step")]) == JOIN_TIMEOUT_CEILING_S
