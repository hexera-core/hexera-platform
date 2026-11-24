# Responsibility: Verify a remote mesher that RAISES leaves an account of itself - a result
# document the waiting worker reads - instead of falling silent and being guessed at.
# Boundaries: patches the exchange and the engine dispatch; no GCS, no VM, no mesher.
"""A TWO-HOUR MESH WAS REPORTED AS "THE VM STOPPED ANSWERING".

Job `110daca9-4bb2-4fef-a649-bd500b676e36` on 2026-11-24 meshed the real Corintis cold plate on an
n2-highmem-64 for 1 h 53 m, raised inside `run_engine_local`, and the box deleted itself six
seconds later. What the worker then wrote down, twenty minutes after the fact, was

    rc=-3: [CLOUD_RUN_FAILED] cfmesh_conjugate: gone: a n2-highmem-64 VM stopped answering part
    way through the mesh (no word for 20 minutes)

which is the worker's own inference from silence (`cloud_run_client`, the GONE_MARKER branch). It
was all anyone had: no exception, no exit code, no stage, no log. The one piece of evidence that
survived was the live stage beside the result, and it said `sending the mesh back` - because
`returning.set()` sat in a `finally` and fired on the way out of a run that sent nothing back,
while `_beacon` counts handing back as the mesher's output MOVING, so `progress_at` went on
advancing for a run that had already stopped.

Two things are asserted here, and they are the two halves of that:
  1. a mesher that raises UPLOADS a result document saying so, with the exception in it, the same
     way `contract_refusal` already answers in a document rather than by going quiet;
  2. the live stage is not flipped to "sending the mesh back" unless something is being sent.
"""
from __future__ import annotations

import threading
import time

import pytest

from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE
from meshpipeline.runtime import mesh_runner


class _Boom(RuntimeError):
    """What the engine raises in these tests; named so it is recognisable in a tail."""


def _raiser_frame(exc: _Boom) -> _Boom:
    """Give `exc` a real traceback, since that is what the document has to carry."""
    try:
        raise exc
    except _Boom as raised:
        return raised


# --------------------------------------------------------------------------------------------
# the document itself
# --------------------------------------------------------------------------------------------
def test_the_document_says_the_mesh_was_never_decided():
    """rc is the infrastructure code: no plan was held against a mesh, so there is no verdict."""
    doc = mesh_runner.died(_raiser_frame(_Boom("cartesianMesh went away")))
    assert doc["rc"] == RC_INFRASTRUCTURE
    assert doc["timed_out"] is False


def test_the_document_carries_the_exception_and_where_it_raised():
    doc = mesh_runner.died(_raiser_frame(_Boom("cartesianMesh went away")))
    tail = doc["log_tail"]
    assert "_Boom" in tail, "the exception type is the first thing anyone needs"
    assert "cartesianMesh went away" in tail, "and its message"
    assert "Traceback" in tail and "_raiser_frame" in tail, (
        "the frames are the whole value of this document: without them a dead mesher is still "
        "only a code"
    )


def test_the_tail_is_bounded_so_the_document_stays_a_document():
    big = _raiser_frame(_Boom("x" * (4 * mesh_runner.DIED_TAIL_BYTES)))
    doc = mesh_runner.died(big)
    assert len(doc["log_tail"]) <= mesh_runner.DIED_TAIL_BYTES


def test_the_end_of_the_tail_is_what_is_kept():
    """The exception and the frames that raised are at the END, so a trim must take the front."""
    big = _raiser_frame(_Boom("y" * (4 * mesh_runner.DIED_TAIL_BYTES)))
    tail = mesh_runner.died(big)["log_tail"]
    assert "_raiser_frame" in tail, "the traceback is at the end and has to survive the trim"


# --------------------------------------------------------------------------------------------
# putting it where the worker looks
# --------------------------------------------------------------------------------------------
def test_an_empty_workspace_is_what_rides_with_it(monkeypatch):
    """Not the workspace that failed: the document is the point, and the workspace of a mesh at
    the cell budget is hundreds of gigabytes nothing would read."""
    import pathlib

    from meshpipeline.adapters.mesh_execution import gcs_exchange
    seen: list[tuple[str, list[str], dict]] = []

    def _upload(output_uri: str, workspace: str, result: dict) -> None:
        seen.append((output_uri, sorted(p.name for p in pathlib.Path(workspace).iterdir()), result))

    monkeypatch.setattr(gcs_exchange, "upload_result", _upload)
    mesh_runner.say_it_died("gs://b/jobs/h/output.tar.gz", _raiser_frame(_Boom("gone")))
    assert len(seen) == 1
    uri, children, doc = seen[0]
    assert uri == "gs://b/jobs/h/output.tar.gz"
    assert children == [], "the archive that rides with the document must be an empty one"
    assert doc["rc"] == RC_INFRASTRUCTURE


def test_it_never_replaces_the_cause_it_is_reporting(monkeypatch):
    """This runs ON the failure path. An exchange that is itself broken must not become the
    exception the caller sees - the mesher's own is the one worth having."""
    from meshpipeline.adapters.mesh_execution import gcs_exchange

    def _upload(*_a, **_k) -> None:
        raise OSError("the bucket is unreachable too")

    monkeypatch.setattr(gcs_exchange, "upload_result", _upload)
    mesh_runner.say_it_died("gs://b/jobs/h/output.tar.gz", _raiser_frame(_Boom("gone")))


# --------------------------------------------------------------------------------------------
# the run: what happens when the engine raises
# --------------------------------------------------------------------------------------------
def _patch_run(monkeypatch, *, engine_result=None, engine_raises=None):
    """Stand in for everything `run` reaches: the exchange, the dispatch and the beacon.

    The beacon stub keeps the `returning` event it is handed, which is the only way to see from
    outside whether the live stage was flipped - the event is local to `run`.
    """
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    from meshpipeline.engines import dispatch

    kept: dict = {"uploads": [], "returning": None}

    def _download(_uri: str) -> str:
        import tempfile
        return tempfile.mkdtemp(prefix="test_ws_")

    def _upload(output_uri: str, workspace: str, result: dict) -> None:
        import pathlib
        kept["uploads"].append({"uri": output_uri, "rc": result.get("rc"),
                                "children": sorted(p.name for p in pathlib.Path(workspace).iterdir()),
                                "returning_at_upload": kept["returning"].is_set()})

    def _engine(_ws, *, engine: str, timeout: int) -> dict:
        if engine_raises is not None:
            raise engine_raises
        return dict(engine_result or {"rc": 0, "timed_out": False})

    def _beacon(_ws, _uri, _stop, *, returning=None, **_k) -> None:
        kept["returning"] = returning

    monkeypatch.setattr(gcs_exchange, "download_workspace", _download)
    monkeypatch.setattr(gcs_exchange, "upload_result", _upload)
    monkeypatch.setattr(dispatch, "run_engine_local", _engine)
    monkeypatch.setattr(mesh_runner, "_beacon", _beacon)
    return kept


def test_a_raising_engine_leaves_a_document_behind_and_still_raises(monkeypatch):
    kept = _patch_run(monkeypatch, engine_raises=_Boom("the mesher went away"))
    with pytest.raises(_Boom):
        mesh_runner.run("gs://b/jobs/h/input.tar.gz", "gs://b/jobs/h/output.tar.gz",
                        "cfmesh_conjugate", 60)
    assert len(kept["uploads"]) == 1, (
        "a mesher that raises has to say so where the worker looks; with nothing uploaded the "
        "worker can only report that the machine stopped answering"
    )
    assert kept["uploads"][0]["rc"] == RC_INFRASTRUCTURE
    assert kept["uploads"][0]["children"] == []


def test_the_live_stage_is_not_flipped_by_a_mesher_that_sends_nothing(monkeypatch):
    """The defect exactly: `returning.set()` was in a `finally`, so the last word on a dead run
    was "sending the mesh back"."""
    kept = _patch_run(monkeypatch, engine_raises=_Boom("the mesher went away"))
    with pytest.raises(_Boom):
        mesh_runner.run("gs://b/jobs/h/input.tar.gz", "gs://b/jobs/h/output.tar.gz",
                        "cfmesh_conjugate", 60)
    assert kept["returning"] is not None, "the beacon is started with a returning event"
    assert kept["returning"].is_set() is False, (
        "nothing is being handed back, so the stage must not say it is - and `_beacon` treats "
        "handing back as the mesher's output moving, which keeps progress_at advancing for a run "
        "that has stopped"
    )


def test_a_mesh_that_finished_still_says_it_is_being_sent_back(monkeypatch):
    """The other half, and why the flag cannot simply be dropped: an upload of gigabytes takes
    minutes, and the pulse through it is what stops the worker reading a live remote as gone."""
    kept = _patch_run(monkeypatch, engine_result={"rc": 0, "timed_out": False})
    out = mesh_runner.run("gs://b/jobs/h/input.tar.gz", "gs://b/jobs/h/output.tar.gz",
                          "cfmesh_conjugate", 60)
    assert out["rc"] == 0
    assert len(kept["uploads"]) == 1
    assert kept["uploads"][0]["returning_at_upload"] is True, (
        "the stage has to be handing-back BEFORE the upload starts, not after it"
    )
    assert kept["returning"].is_set() is True


# --------------------------------------------------------------------------------------------
# why the flag matters: what the beacon does with it
# --------------------------------------------------------------------------------------------
def test_the_beacon_publishes_handing_back_and_counts_it_as_movement(tmp_path, monkeypatch):
    """Pins the consequence the tests above are protecting, off the beacon itself."""
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    said: list[dict] = []
    monkeypatch.setattr(gcs_exchange, "upload_progress",
                        lambda _uri, stage: said.append(dict(stage)))

    stop, returning = threading.Event(), threading.Event()
    returning.set()
    thread = threading.Thread(target=mesh_runner._beacon, args=(str(tmp_path), "gs://b/jobs/h/output.tar.gz", stop),
                              kwargs={"returning": returning, "every_s": 0.01, "pulse_s": 0.01},
                              daemon=True)
    thread.start()
    for _ in range(500):
        if said:
            break
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert said, "the beacon should have published a stage"
    assert said[0]["key"] == mesh_runner.RETURNING["key"]
    assert said[0]["label"] == mesh_runner.RETURNING["label"]
    assert said[0]["progress_at"] == said[0]["at"], (
        "handing back counts as the output moving, which is why a run that is NOT handing back "
        "must never set the flag"
    )
