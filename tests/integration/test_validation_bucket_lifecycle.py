# Responsibility: Verify a validation run gets its own bucket and that removing it is proven, not attempted.
# Boundaries: the isolation authority against a real object store; it never touches the protected bucket's contents.
from __future__ import annotations

import io
import os
import uuid

import pytest

if not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("a real object store is required", allow_module_level=True)

from tests import disposable_object_storage as dos  # noqa: E402


def _client():
    return dos._client()


@pytest.fixture()
def task_bucket():
    # Each test gets its own bucket and is responsible for it even when it fails. The teardown does
    # NOT swallow: a suite about proving cleanup cannot be allowed to leak the very thing it tests,
    # and an earlier draft of this fixture hid six leaked buckets by catching everything.
    name = dos.task_bucket_name(dos.new_run_id())
    before = {u for _, u in dos.incomplete_uploads(dos.protected_bucket())}
    yield name
    # ABORT BEFORE REMOVAL, with the real client. A bucket removed while it still owns an upload
    # orphans that upload permanently - the abort needs the owning bucket's name and the bucket is
    # gone - so a suite that creates multipart uploads must never let its teardown run through a
    # monkeypatched client or skip the aborts.
    client = _client()
    if client.bucket_exists(name):
        for key, upload_id in dos.incomplete_uploads(name):
            try:
                client._abort_multipart_upload(name, key, upload_id)
            except Exception:  # noqa: BLE001 - the assertion below is the report
                pass
    dos.drop(name)
    assert not client.bucket_exists(name), f"the suite leaked task bucket {name}"
    after = {u for _, u in dos.incomplete_uploads(dos.protected_bucket())}
    assert not (after - before), (
        f"this test left {len(after - before)} unaddressable multipart upload(s) on the server")


def _put(bucket: str, key: str, body: bytes = b"x") -> None:
    _client().put_object(bucket, key, io.BytesIO(body), len(body))


# identity


def test_each_validation_run_gets_a_distinct_bucket():
    names = {dos.task_bucket_name(dos.new_run_id()) for _ in range(25)}
    assert len(names) == 25, "two runs would share a bucket"
    assert all(n.startswith(dos.TASK_PREFIX) for n in names)
    assert all(len(n) <= 63 and n == n.lower() for n in names)


def test_a_run_creates_its_own_bucket_and_it_really_exists(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    assert _client().bucket_exists(task_bucket)


def test_the_same_bucket_cannot_be_provisioned_twice(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.provision(task_bucket, protected="mesh-artifacts")
    assert "already exists" in str(e.value)


# refusals


def test_the_protected_bucket_is_refused():
    # Refused twice over: it carries no task prefix, and it is the protected name. The prefix rule
    # fires first, which is the stricter of the two.
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.require_isolated("mesh-artifacts", protected="mesh-artifacts")
    assert "task-owned" in str(e.value)
    with pytest.raises(dos.NotIsolatedError) as e2:
        dos.require_isolated(dos.TASK_PREFIX + "shared", protected=dos.TASK_PREFIX + "shared")
    assert "protected bucket" in str(e2.value)


def test_a_missing_bucket_configuration_is_refused():
    for empty in (None, "", "   "):
        with pytest.raises(dos.NotIsolatedError) as e:
            dos.require_isolated(empty, protected="mesh-artifacts")
        assert "no validation bucket" in str(e.value)


def test_a_bucket_without_the_task_prefix_is_refused():
    # This is what stops "isolation" by prefix inside the shared bucket, and what stops `drop()`
    # from ever being pointed at something it did not create.
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.require_isolated("mesh-artifacts-validation", protected="mesh-artifacts")
    assert "task-owned" in str(e.value)


def test_processes_that_disagree_about_the_bucket_are_refused():
    assert dos.agree("meshtest-a", "meshtest-a", "meshtest-a") == "meshtest-a"
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.agree("meshtest-a", "meshtest-b")
    assert "disagree" in str(e.value)
    with pytest.raises(dos.NotIsolatedError):
        dos.agree("meshtest-a", None)


def test_drop_refuses_a_bucket_it_did_not_create():
    with pytest.raises(dos.NotIsolatedError):
        dos.drop("mesh-artifacts")


# cleanup


def test_cleanup_removes_ordinary_objects_and_the_bucket(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    for i in range(5):
        _put(task_bucket, f"sources/{uuid.uuid4()}", b"payload" * i or b"p")
    assert len(dos.inventory(task_bucket)) == 5

    counts = dos.drop(task_bucket)
    assert counts["removed_versions"] == 5, counts
    assert not _client().bucket_exists(task_bucket), "the bucket survived cleanup"


def test_cleanup_removes_every_version_and_delete_marker(task_bucket):
    from minio.commonconfig import ENABLED
    from minio.versioningconfig import VersioningConfig

    dos.provision(task_bucket, protected="mesh-artifacts")
    _client().set_bucket_versioning(task_bucket, VersioningConfig(ENABLED))
    key = f"sources/{uuid.uuid4()}"
    _put(task_bucket, key, b"one")
    _put(task_bucket, key, b"two")            # a second version
    _client().remove_object(task_bucket, key)  # a delete marker over both

    rows = dos.inventory(task_bucket)
    assert len(rows) >= 3, rows
    assert any(r["is_delete_marker"] for r in rows), "no delete marker was created to clean up"

    dos.drop(task_bucket)
    assert not _client().bucket_exists(task_bucket)


def test_cleanup_aborts_an_incomplete_multipart_upload(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    key = f"sources/{uuid.uuid4()}"
    # start a multipart upload and never complete it
    cid = _client()._create_multipart_upload(task_bucket, key, {})
    _client()._upload_part(task_bucket, key, b"a" * (5 * 1024 * 1024), {}, cid, 1)
    stranded = dos.incomplete_uploads(task_bucket)
    assert key in [k for k, _ in stranded], f"the fixture did not strand an upload: {stranded}"

    counts = dos.drop(task_bucket)
    assert counts["aborted_uploads"] >= 1, counts
    assert not _client().bucket_exists(task_bucket)


def test_cleanup_aborts_uploads_with_and_without_parts_and_several_per_key(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    before = {u for _, u in dos.server_multipart_uploads()}

    no_parts = f"sources/{uuid.uuid4()}"
    _client()._create_multipart_upload(task_bucket, no_parts, {})          # zero parts

    with_part = f"sources/{uuid.uuid4()}"
    cid = _client()._create_multipart_upload(task_bucket, with_part, {})
    _client()._upload_part(task_bucket, with_part, b"a" * (5 * 1024 * 1024), {}, cid, 1)

    shared = f"sources/{uuid.uuid4()} with space"                          # exact key preserved
    _client()._create_multipart_upload(task_bucket, shared, {})
    _client()._create_multipart_upload(task_bucket, shared, {})            # two for one key

    added = {u for _, u in dos.server_multipart_uploads()} - before
    assert len(added) == 4, f"the fixture did not strand four uploads: {len(added)}"
    keys = [k for k, _ in dos.server_multipart_uploads()]
    assert shared in keys, "the exact key with a space was not preserved by the listing"

    counts = dos.drop(task_bucket)
    assert counts["aborted_uploads"] >= 4, f"cleanup under-counted its aborts: {counts}"
    left = {u for _, u in dos.server_multipart_uploads()} & added
    assert left == set(), f"cleanup left {len(left)} upload(s) behind"


def test_a_bucket_is_not_removed_while_it_still_owns_an_upload(task_bucket):
    # remove_bucket succeeds even with a live upload, so the abort loop is the only thing standing
    # between a cancelled run and an orphan no API can address afterwards.
    dos.provision(task_bucket, protected="mesh-artifacts")
    before = {u for _, u in dos.server_multipart_uploads()}
    _client()._create_multipart_upload(task_bucket, f"sources/{uuid.uuid4()}", {})
    mine = {u for _, u in dos.server_multipart_uploads()} - before
    assert mine, "no upload was stranded"

    dos.drop(task_bucket)
    assert not ({u for _, u in dos.server_multipart_uploads()} & mine), (
        "the bucket was removed while it still owned an upload - that upload is now unaddressable")


def test_the_upload_listing_follows_every_page(task_bucket, monkeypatch):
    # THIS MinIO never truncates the multipart listing - it ignores max-uploads and answers in one
    # page - so real pagination cannot be provoked against it and a test that tried would be
    # vacuous. The loop still has to be right for a conformant server, so it is driven here by a
    # stub that returns two truncated pages: an implementation that read only the first would come
    # back with half the uploads.
    from types import SimpleNamespace

    pages = [
        SimpleNamespace(uploads=[SimpleNamespace(object_name="a", upload_id="u1")],
                        is_truncated=True, next_key_marker="a", next_upload_id_marker="u1"),
        SimpleNamespace(uploads=[SimpleNamespace(object_name="b", upload_id="u2")],
                        is_truncated=False, next_key_marker=None, next_upload_id_marker=None),
    ]

    class Paged:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, n): return getattr(self._inner, n)
        def bucket_exists(self, name): return True
        def _list_multipart_uploads(self, *a, **k): return pages.pop(0)

    real = dos._client
    monkeypatch.setattr(dos, "_client", lambda: Paged(real()))
    got = dos.incomplete_uploads(task_bucket)
    monkeypatch.undo()

    assert [u for _, u in got] == ["u1", "u2"], f"the listing stopped early: {got}"


def test_an_abort_failure_propagates(task_bucket, monkeypatch):
    dos.provision(task_bucket, protected="mesh-artifacts")
    _client()._create_multipart_upload(task_bucket, f"sources/{uuid.uuid4()}", {})
    real = dos._client

    class RefusingAbort:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, n): return getattr(self._inner, n)
        def _abort_multipart_upload(self, *a, **k): raise OSError("test: abort refused")
        def _list_multipart_uploads(self, *a, **k): return self._inner._list_multipart_uploads(*a, **k)

    monkeypatch.setattr(dos, "_client", lambda: RefusingAbort(real()))
    with pytest.raises(OSError):
        dos.drop(task_bucket)
    monkeypatch.undo()


def test_list_parts_identifies_the_actual_owner_and_a_wrong_bucket_says_so(task_bucket):
    # The classifier the whole cleanup rests on. The listing is server-wide and cannot answer
    # "is this mine"; ListParts is bucket-scoped and can.
    dos.provision(task_bucket, protected="mesh-artifacts")
    witness = dos.task_bucket_name(dos.new_run_id())
    dos.provision(witness, protected="mesh-artifacts")
    try:
        key = f"sources/{uuid.uuid4()}"
        _client()._create_multipart_upload(task_bucket, key, {})
        mine = dos.owned_uploads(task_bucket)
        assert key in [k for k, _ in mine], "the owning bucket did not recognise its own upload"
        assert key not in [k for k, _ in dos.owned_uploads(witness)], (
            "a bucket claimed ownership of another bucket's upload")
        assert key in [k for k, _ in dos.incomplete_uploads(witness)], (
            "the server-wide listing no longer shows it - the design assumption changed")
    finally:
        dos.drop(witness)


def test_cleanup_refuses_to_delete_a_bucket_that_still_owns_an_upload(task_bucket, monkeypatch):
    # Deleting it would strand the upload for good, so cleanup must fail loudly instead.
    dos.provision(task_bucket, protected="mesh-artifacts")
    _client()._create_multipart_upload(task_bucket, f"sources/{uuid.uuid4()}", {})

    real = dos._client

    class NoAbort:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, n): return getattr(self._inner, n)
        def _abort_multipart_upload(self, *a, **k): return None      # pretends to succeed
        def _list_parts(self, *a, **k): return self._inner._list_parts(*a, **k)
        def _list_multipart_uploads(self, *a, **k): return self._inner._list_multipart_uploads(*a, **k)

    monkeypatch.setattr(dos, "_client", lambda: NoAbort(real()))
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.drop(task_bucket)
    assert "still owns" in str(e.value)
    monkeypatch.undo()


def test_cleanup_aborts_only_what_the_task_bucket_owns(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    witness = dos.task_bucket_name(dos.new_run_id())
    dos.provision(witness, protected="mesh-artifacts")
    try:
        wkey = f"sources/{uuid.uuid4()}"
        _client()._create_multipart_upload(witness, wkey, {})
        _client()._create_multipart_upload(task_bucket, f"sources/{uuid.uuid4()}", {})

        dos.drop(task_bucket)
        surviving = [k for k, _ in dos.owned_uploads(witness)]
        assert wkey in surviving, "cleanup aborted an unrelated bucket's upload"
    finally:
        dos.drop(witness)


def test_cleanup_verification_reports_a_bucket_that_did_not_go_away(task_bucket, monkeypatch):
    dos.provision(task_bucket, protected="mesh-artifacts")
    _put(task_bucket, "sources/leftover")

    real = dos._client

    class Stubborn:
        # Everything real except the removal, so the objects are genuinely deleted and only the
        # bucket lingers - the shape a half-completed cleanup actually has.
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, n): return getattr(self._inner, n)
        def remove_bucket(self, name): return None
        def _list_multipart_uploads(self, *a, **k): return self._inner._list_multipart_uploads(*a, **k)

    monkeypatch.setattr(dos, "_client", lambda: Stubborn(real()))
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.drop(task_bucket)
    assert "still exists after cleanup" in str(e.value)
    monkeypatch.undo()
    dos.drop(task_bucket)


def test_a_cleanup_error_propagates_rather_than_being_swallowed(task_bucket, monkeypatch):
    dos.provision(task_bucket, protected="mesh-artifacts")
    _put(task_bucket, "sources/kept")
    real = dos._client

    class Refusing:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, n): return getattr(self._inner, n)
        def remove_object(self, *a, **k): raise OSError("test: provider refused the delete")
        def _list_multipart_uploads(self, *a, **k): return self._inner._list_multipart_uploads(*a, **k)

    monkeypatch.setattr(dos, "_client", lambda: Refusing(real()))
    with pytest.raises(OSError):
        dos.drop(task_bucket)
    monkeypatch.undo()


# the protected-bucket fingerprint


def test_the_fingerprint_detects_an_added_object(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    before, rows_before = dos.fingerprint(task_bucket), dos.inventory(task_bucket)
    _put(task_bucket, f"sources/{uuid.uuid4()}")
    after, rows_after = dos.fingerprint(task_bucket), dos.inventory(task_bucket)

    assert before != after
    with pytest.raises(dos.NotIsolatedError) as e:
        dos.assert_protected_unchanged(before, after, bucket=task_bucket,
                                       before_rows=rows_before, after_rows=rows_after)
    assert "added" in str(e.value)


def test_the_fingerprint_detects_a_deleted_or_replaced_object(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    key = f"sources/{uuid.uuid4()}"
    _put(task_bucket, key, b"original")
    before, rows_before = dos.fingerprint(task_bucket), dos.inventory(task_bucket)

    _client().remove_object(task_bucket, key)
    deleted = dos.fingerprint(task_bucket)
    assert deleted != before, "a deletion left the fingerprint unchanged"

    # replacement: same key, different bytes - a count-only comparison would call this contained
    _put(task_bucket, key, b"replaced-with-different-bytes")
    replaced, rows_replaced = dos.fingerprint(task_bucket), dos.inventory(task_bucket)
    assert len(rows_replaced) == len(rows_before), "this case is only meaningful at equal counts"
    assert replaced != before, "a replacement at the same count left the fingerprint unchanged"
    with pytest.raises(dos.NotIsolatedError):
        dos.assert_protected_unchanged(before, replaced, bucket=task_bucket,
                                       before_rows=rows_before, after_rows=rows_replaced)


def test_a_stranded_upload_is_counted_by_delta_not_by_the_bucket_fingerprint(task_bucket):
    # An abandoned upload is residue an object listing cannot see - but the listing that reports it
    # is SERVER-WIDE on this MinIO, so it is not a property of any one bucket. The fingerprint
    # therefore stays per-bucket and the residue is caught by comparing before/after upload sets.
    dos.provision(task_bucket, protected="mesh-artifacts")
    _put(task_bucket, f"sources/{uuid.uuid4()}")
    before_fp = dos.fingerprint(task_bucket)
    before_uploads = dos.server_multipart_uploads()

    key = f"sources/{uuid.uuid4()}"
    cid = _client()._create_multipart_upload(task_bucket, key, {})
    _client()._upload_part(task_bucket, key, b"a" * (5 * 1024 * 1024), {}, cid, 1)

    assert len(dos.inventory(task_bucket)) == 1, "the upload must be invisible to an object listing"
    assert dos.fingerprint(task_bucket) == before_fp, (
        "a server-wide value leaked into a per-bucket fingerprint")
    assert dos.multipart_delta(before_uploads, dos.server_multipart_uploads()), (
        "the stranded upload was not counted as this run's residue")


def test_the_multipart_listing_is_server_wide_not_per_bucket(task_bucket):
    # The measurement this module's design rests on, asserted so a MinIO upgrade that makes the
    # listing per-bucket shows up here rather than silently changing what the delta means.
    dos.provision(task_bucket, protected="mesh-artifacts")
    other = dos.task_bucket_name(dos.new_run_id())
    dos.provision(other, protected="mesh-artifacts")
    try:
        key = f"sources/{uuid.uuid4()}"
        _client()._create_multipart_upload(task_bucket, key, {})
        seen_elsewhere = [k for k, _ in dos.incomplete_uploads(other)]
        assert key in seen_elsewhere, (
            "this MinIO no longer reports uploads server-wide - multipart_delta's meaning changed")
        for k, u in dos.incomplete_uploads(task_bucket):
            _client()._abort_multipart_upload(task_bucket, k, u)
    finally:
        dos.drop(other)


def test_an_untouched_bucket_fingerprints_identically(task_bucket):
    dos.provision(task_bucket, protected="mesh-artifacts")
    _put(task_bucket, f"sources/{uuid.uuid4()}")
    first = dos.fingerprint(task_bucket)
    second = dos.fingerprint(task_bucket)
    assert first == second, "the fingerprint is not stable across reads"
    dos.assert_protected_unchanged(first, second, bucket=task_bucket)
