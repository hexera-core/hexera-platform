# Responsibility: Verify the order the upload starts the geometry check in - the "pending" marker
# is stored before the task is published, and a check nobody will run is marked failed, not left
# pending forever.
# Boundaries: the enqueue helper alone, with the store and the queue seam faked.
from __future__ import annotations

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import upload
from meshpipeline.application import geometry_check as gc
from meshpipeline.contracts import geometry_check as seam


def _run(monkeypatch, *, enqueuer):
    calls: list[tuple] = []
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    monkeypatch.setattr(gc, "write_status",
                        lambda sid, status, **f: calls.append(("status", status, f.get("reason"))))

    def _seam(**kw):
        calls.append(("enqueue", kw["session_id"], None))
        return enqueuer(**kw)

    seam.set_scout_enqueuer(_seam if enqueuer is not None else None)
    try:
        upload._enqueue_geometry_check("sess", "owner", source_payload={"k": "v"}, interpretation_payload=None)
    finally:
        seam.set_scout_enqueuer(None)
    return calls


def test_pending_is_stored_before_the_task_is_published(monkeypatch):
    calls = _run(monkeypatch, enqueuer=lambda **kw: None)
    assert calls == [("status", "pending", None), ("enqueue", "sess", None)]


def test_a_check_nobody_will_run_is_marked_failed_not_left_pending(monkeypatch):
    calls = _run(monkeypatch, enqueuer=None)         # no worker adapter installed
    assert calls[0] == ("status", "pending", None)
    assert calls[-1][:2] == ("status", "failed") and "could not be started" in calls[-1][2]


def test_a_broker_error_is_marked_failed_and_the_upload_stands(monkeypatch):
    def _down(**kw):
        raise ConnectionError("broker unreachable")
    calls = _run(monkeypatch, enqueuer=_down)
    assert calls[-1][:2] == ("status", "failed")
