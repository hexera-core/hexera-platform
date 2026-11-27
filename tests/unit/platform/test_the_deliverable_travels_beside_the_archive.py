# Responsibility: the customer's bundle is carried as its own object, not inside the workspace tar.
# Boundaries: the exchange layer's archive/collect contract; no GCS and no engine run.
# Collaborates with: engines/bundled_delivery for the record that says a bundle exists.
"""The bundle `engines/bundled_delivery` builds is already a verified gzip archive.

Putting it inside `output.tar.gz` compresses gzip output a second time - which by definition
gains nothing - and writes it to disk again on the way out and again on the way in. Measured on
600 MiB of real gzip output, the pack-plus-extract round trip is 2.44 s with it inside and 0.88 s
with it beside: 2.7 s a GiB, about 62 s at the plate's 23.1 GiB bundle. The disk it stops
touching matters at least as much as the seconds.

WHAT MUST NOT CHANGE is that a run without a bundle, and every failure path, archives exactly
what it always did.
"""
from __future__ import annotations

import gzip
import json
import tarfile

import pytest

from meshpipeline.adapters.mesh_execution.gcs_exchange import (
    _bundle_beside,
    write_result_archive,
)
from meshpipeline.artifact_keys import bundle_key_beside, result_key_beside

BUNDLE = "mesh_bundle.tar.gz"


@pytest.fixture
def ws(tmp_path):
    """A workspace with a packed bundle and an ordinary file beside it."""
    d = tmp_path / "ws"
    (d / "constant").mkdir(parents=True)
    (d / "constant" / "faces").write_text("0.1 0.2 0.3\n" * 500)
    with gzip.open(d / BUNDLE, "wb") as fh:
        fh.write(b"the customer's cells" * 500)
    (d / "mesh_bundle.json").write_text(json.dumps(
        {"bundle": BUNDLE, "bytes": (d / BUNDLE).stat().st_size, "sha256": "x",
         "engine": "cfmesh_conjugate", "prefix": "mesh", "cells": {}}))
    return d


def _names(archive) -> set[str]:
    with tarfile.open(archive) as tf:
        return set(tf.getnames())


# ---------------------------------------------------------------- what the archive carries

def test_the_bundle_is_not_in_the_archive_when_it_travels_beside(ws, tmp_path):
    a = tmp_path / "out.tar.gz"
    write_result_archive(ws, a, skip=frozenset({BUNDLE}))
    got = _names(a)
    assert BUNDLE not in got
    # and everything else still travels, the record included - it is how home finds the bundle
    assert "mesh_bundle.json" in got
    assert "constant" in got


def test_nothing_is_skipped_unless_it_is_named(ws, tmp_path):
    """The default is the behaviour every other engine and every failure path still gets."""
    a = tmp_path / "out.tar.gz"
    write_result_archive(ws, a)
    assert BUNDLE in _names(a)


def test_skipping_the_bundle_takes_its_bytes_out_of_the_archive(ws, tmp_path):
    """The archive shrinks by what the bundle contributed to it, and by nothing else.

    Not asserted against the bundle's own size: this fixture's bundle is a repeated string and
    gzips to under 100 bytes, so at this scale the two numbers say nothing about each other. The
    seconds and gigabytes are measured separately, on real gzip output, in the module text.
    """
    inside = write_result_archive(ws, tmp_path / "in.tar.gz")
    beside = write_result_archive(ws, tmp_path / "be.tar.gz", skip=frozenset({BUNDLE}))
    assert beside < inside
    with tarfile.open(tmp_path / "in.tar.gz") as tf:
        contributed = tf.getmember(BUNDLE).size
    # a tar member costs its content plus a 512-byte header, padded to a 512-byte boundary
    assert inside - beside <= contributed + 1024


def test_the_archive_still_reports_its_own_size(ws, tmp_path):
    """`return_scope` is held against this number; a skip must not make it a different number."""
    a = tmp_path / "out.tar.gz"
    assert write_result_archive(ws, a, skip=frozenset({BUNDLE})) == a.stat().st_size


# ---------------------------------------------------------------- which bundle, if any

def test_a_workspace_with_a_packed_bundle_names_it(ws):
    rec = _bundle_beside(ws)
    assert rec is not None and rec["bundle"] == BUNDLE


def test_a_workspace_with_no_bundle_sends_the_old_way(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "faces").write_text("x")
    assert _bundle_beside(d) is None


def test_a_record_whose_bundle_is_gone_is_not_trusted(ws):
    """The record speaks for bytes; with the file gone it speaks for nothing."""
    (ws / BUNDLE).unlink()
    assert _bundle_beside(ws) is None


def test_a_record_naming_a_path_is_refused(ws):
    """The name becomes a path at home. A record is data, so it does not get to choose where."""
    (ws / "mesh_bundle.json").write_text(json.dumps(
        {"bundle": "../escape.tar.gz", "bytes": 1, "sha256": "x"}))
    assert _bundle_beside(ws) is None


def test_an_unreadable_record_sends_the_old_way(ws):
    (ws / "mesh_bundle.json").write_text("{not json")
    assert _bundle_beside(ws) is None


# ---------------------------------------------------------------- the sibling key

def test_the_bundle_object_sits_beside_the_archive_and_the_result():
    out = "jobs/abc/output.tar.gz"
    assert bundle_key_beside(out) == "jobs/abc/bundle.tar.gz"
    assert result_key_beside(out) == "jobs/abc/result.json"
    # three distinct objects in one prefix, so neither overwrites another
    assert len({out, bundle_key_beside(out), result_key_beside(out)}) == 3


def test_the_key_does_not_depend_on_the_engine_s_naming():
    """The exchange layer must not have to learn an engine's file names to find the object."""
    assert bundle_key_beside("a/b/c/output.tar.gz").endswith("/bundle.tar.gz")


# ---------------------------------------------------------------- the wiring, both sides

def test_the_uploader_joins_the_bundle_before_it_promises_it():
    """The result is what tells home a second object exists. Promise last, or a half-finished
    upload leaves home looking for a bundle that is in neither place.

    The upload runs on its own thread now, so the ORDERING LIVES IN THE JOIN rather than in the
    call: `sending.result()` is what makes a failed upload raise before the result is written.
    The behaviour itself is pinned by `test_a_bundle_upload_that_fails_raises_and_promises_nothing`;
    this reads the shape so the join cannot be dropped while those still pass on a fast machine.
    """
    import inspect

    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    src = inspect.getsource(GX.upload_result)
    assert "_bundle_beside(ws)" in src
    assert "skip=frozenset({str(beside[\"bundle\"])})" in src
    assert src.index("pool.submit(_upload") < src.index("write_result_archive("),         "the bundle should start before the archive is written"
    join = src.index("sending.result()")
    promise = src.index('"bundle_beside"')
    assert join < promise, "a failed bundle upload must raise before the result names it"
    assert promise < src.index("res_key"), "and the result document is written last"


def test_the_home_side_collects_it_after_the_extraction():
    """safe_extract moves members over the workspace only once the whole archive extracted, so a
    bundle landed first would be collateral of a refused archive."""
    import inspect

    from meshpipeline.adapters.mesh_execution import cloud_run_client as CRC

    src = inspect.getsource(CRC)
    assert 'result.get("bundle_beside")' in src
    assert src.index("safe_extract_tar(path=_archive") < src.index('result.get("bundle_beside")')


def test_the_home_side_refuses_a_name_that_cannot_be_a_file():
    import inspect

    from meshpipeline.adapters.mesh_execution import cloud_run_client as CRC

    src = inspect.getsource(CRC)
    assert '"/" in _name' in src and '_name in (".", "..")' in src


# ---------------------------------------------------------------- the bundle goes first

class _Blob:
    """Records what was uploaded, when, and how long it took - the order is the subject."""

    def __init__(self, store, key):
        self._store, self._key = store, key

    def upload_from_filename(self, path, **_k):
        import time
        self._store.append(("file", self._key, time.perf_counter()))
        hold = self._store.holds.get(self._key, 0.0)
        if self._store.fails == self._key:
            raise RuntimeError(f"the upload failed: {self._key}")
        if hold:
            time.sleep(hold)
        self._store.append(("done", self._key, time.perf_counter()))

    def upload_from_string(self, body, **_k):
        import time
        self._store.append(("json", self._key, time.perf_counter()))
        self._store.documents[self._key] = body


class _Store(list):
    def __init__(self):
        super().__init__()
        self.holds: dict = {}
        self.fails = ""
        self.documents: dict = {}

    def bucket(self, _name):
        return self

    def blob(self, key, **_k):
        return _Blob(self, key)


@pytest.fixture
def exchange(monkeypatch, ws):
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store = _Store()
    monkeypatch.setattr(GX, "storage_client", lambda: store)
    return store, ws


def _keys(store, kind):
    return [k for w, k, _t in store if w == kind]


def test_the_bundle_starts_uploading_before_the_archive_is_even_written(exchange):
    """It is 23.1 GB against the archive's ~1 GB. Queueing it behind the archive's gzip pass
    spends the whole of that pass doing nothing with the wire."""
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store, ws = exchange
    store.holds["jobs/j/bundle.tar.gz"] = 0.30
    GX.upload_result("gs://bkt/jobs/j/output.tar.gz", str(ws), {"rc": 0})
    started = _keys(store, "file")
    assert started[0] == "jobs/j/bundle.tar.gz", "the archive still goes first"
    assert "jobs/j/output.tar.gz" in started


def test_the_archive_finishes_inside_the_bundle_s_transfer(exchange):
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store, ws = exchange
    store.holds["jobs/j/bundle.tar.gz"] = 0.40
    GX.upload_result("gs://bkt/jobs/j/output.tar.gz", str(ws), {"rc": 0})
    at = {(w, k): t for w, k, t in store}
    # the archive's own upload completed before the bundle's did - i.e. it overlapped
    assert at[("done", "jobs/j/output.tar.gz")] < at[("done", "jobs/j/bundle.tar.gz")]


def test_the_result_document_is_written_last_and_names_the_bundle(exchange):
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store, ws = exchange
    GX.upload_result("gs://bkt/jobs/j/output.tar.gz", str(ws), {"rc": 0})
    assert _keys(store, "json") == ["jobs/j/result.json"]
    assert store.index(("json", "jobs/j/result.json",
                        [t for w, k, t in store
                         if (w, k) == ("json", "jobs/j/result.json")][0])) == len(store) - 1
    doc = json.loads(store.documents["jobs/j/result.json"])
    assert doc["bundle_beside"]["key"] == "jobs/j/bundle.tar.gz"
    assert doc["bundle_beside"]["bundle"] == BUNDLE


def test_a_bundle_upload_that_fails_raises_and_promises_nothing(exchange):
    """THE INVARIANT THE OVERLAP MUST NOT BREAK. A result that names a bundle which is not there
    makes the home side look for it in both places and read a delivered mesh as hollow."""
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store, ws = exchange
    store.fails = "jobs/j/bundle.tar.gz"
    with pytest.raises(RuntimeError, match="the upload failed"):
        GX.upload_result("gs://bkt/jobs/j/output.tar.gz", str(ws), {"rc": 0})
    assert store.documents == {}, "a result document was written for a bundle that never landed"


def test_a_workspace_with_no_bundle_uploads_exactly_two_objects(exchange, tmp_path):
    """No pool, no second object, and the same two writes as before any of this."""
    from meshpipeline.adapters.mesh_execution import gcs_exchange as GX

    store, _ws = exchange
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "faces").write_text("x")
    GX.upload_result("gs://bkt/jobs/j/output.tar.gz", str(plain), {"rc": 0})
    assert _keys(store, "file") == ["jobs/j/output.tar.gz"]
    doc = json.loads(store.documents["jobs/j/result.json"])
    assert "bundle_beside" not in doc
