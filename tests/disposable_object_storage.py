# Responsibility: Prove a bucket was created for THIS validation run, and remove it and everything in it afterwards.
# Owns: the task-bucket identity, the isolation refusal, the protected-bucket fingerprint and the cleanup.
# Boundaries: it isolates validation storage; it never touches production code paths and never scans a shared bucket.
from __future__ import annotations

import hashlib
import json
import uuid

#: Every bucket this module may create or destroy starts here. It is the one shape `drop()` accepts,
#: so a name that did not come from `task_bucket_name` cannot be deleted through this authority even
#: by a caller that wants to.
TASK_PREFIX = "meshtest-"


class NotIsolatedError(RuntimeError):
    pass


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def task_bucket_name(run_id: str) -> str:
    # S3 bucket names are lowercase, DNS-shaped and at most 63 characters. The run id is hex, so the
    # only thing that can go wrong is length, and the prefix is what makes ownership legible.
    name = f"{TASK_PREFIX}{(run_id or '').strip().lower()}"
    if len(name) <= len(TASK_PREFIX):
        raise NotIsolatedError("a validation run needs a non-empty run id to name its bucket")
    return name[:63]


def require_isolated(bucket: str | None, *, protected: str | None) -> str:
    # THE refusal. A mutation-bearing tier that reached the protected bucket is exactly the defect
    # this module exists for, so the check is a hard stop before anything is created or started -
    # not a warning, and not something a caller can pass through with a default.
    name = (bucket or "").strip()
    if not name:
        raise NotIsolatedError(
            "no validation bucket is configured. A tier that can write objects must be given a "
            "task-owned bucket; without one it would write into whatever bucket the container "
            "already had, which is how 496 objects were stranded in the shared bucket.")
    if not name.startswith(TASK_PREFIX):
        raise NotIsolatedError(
            f"validation bucket {name!r} does not carry the {TASK_PREFIX!r} prefix, so it cannot be "
            "proven task-owned and must not be created or destroyed by this harness.")
    guarded = (protected or "").strip()
    if guarded and name == guarded:
        raise NotIsolatedError(
            f"the validation bucket is the protected bucket {guarded!r}. Isolation by prefix inside "
            "a shared bucket is not isolation: the bucket itself has to be task-owned so its "
            "removal can be proven.")
    return name


def agree(*configured: str | None) -> str:
    # Every participating process must have been handed the SAME bucket. Two processes with two
    # buckets means one of them is writing somewhere nobody will clean up.
    seen = {(c or "").strip() for c in configured}
    if len(seen) != 1:
        raise NotIsolatedError(
            f"participating processes disagree about the validation bucket: {sorted(seen)}. "
            "One of them would write objects the cleanup authority never sees.")
    return seen.pop()


def _client():
    # Through the TYPED settings authority, never a direct environment read: the endpoint and
    # credentials have exactly one home, and this harness must not become a second one.
    from minio import Minio

    import meshpipeline.settings.providers as provcfg

    # secure=False for the same reason the production adapter uses it: the store is a service on
    # the local stack, reached over plain HTTP.
    return Minio(provcfg.MINIO_ENDPOINT, access_key=provcfg.MINIO_ACCESS_KEY,
                 secret_key=provcfg.MINIO_SECRET_KEY, secure=False)


def protected_bucket() -> str:
    import meshpipeline.settings.providers as provcfg

    return provcfg.MINIO_BUCKET


def provision(bucket: str, *, protected: str | None) -> str:
    name = require_isolated(bucket, protected=protected)
    client = _client()
    if client.bucket_exists(name):
        raise NotIsolatedError(
            f"bucket {name!r} already exists. A run identity is used once; reusing one would let "
            "this run inherit another run's objects and delete them as its own.")
    client.make_bucket(name)
    return name


def inventory(bucket: str) -> list[dict]:
    # The COMPLETE normalised listing - every version and delete marker, not a count. A count
    # cannot tell an added object from a deleted one, and both are containment failures.
    client = _client()
    if not client.bucket_exists(bucket):
        return []
    rows = []
    for obj in client.list_objects(bucket, recursive=True, include_version=True):
        rows.append({
            "key": obj.object_name,
            "version_id": obj.version_id or "",
            "size": int(obj.size or 0),
            "etag": (obj.etag or "").strip('"'),
            "is_delete_marker": bool(getattr(obj, "is_delete_marker", False)),
            "last_modified": obj.last_modified.isoformat() if obj.last_modified else "",
        })
    rows.sort(key=lambda r: (r["key"], r["version_id"]))
    return rows


def fingerprint(bucket: str) -> str:
    # Deliberately excludes last_modified: it is evidence, not identity, and a re-read of an
    # untouched object must fingerprint identically.
    #
    # INCOMPLETE UPLOADS ARE DELIBERATELY NOT IN HERE. An earlier version folded them in, on the
    # reasonable-sounding grounds that an abandoned upload is residue an object listing cannot see.
    # It is - but the listing that reports them is server-wide on this MinIO, so including it made
    # every bucket's fingerprint carry a global value: one unrelated in-flight upload anywhere
    # would have failed an unrelated run's containment check. They are counted separately instead,
    # by `multipart_delta`, which is a statement about the run rather than about the bucket.
    rows = inventory(bucket)
    canon = "\n".join(
        f'{r["key"]}|{r["version_id"]}|{r["size"]}|{r["etag"]}|{r["is_delete_marker"]}'
        for r in rows)
    return hashlib.sha256(canon.encode()).hexdigest()


def _owns(client, bucket: str, key: str, upload_id: str) -> bool:
    # THE ownership question, asked of the server. `ListParts` is bucket-scoped: it answers for the
    # named bucket and raises NoSuchUpload when that bucket is not the owner. A wrong-bucket
    # NoSuchUpload therefore means "not mine", never "gone" - the upload may be very much alive
    # under another bucket, and treating the error as absence is how orphans get created.
    from minio.error import S3Error

    try:
        client._list_parts(bucket, key, upload_id)
        return True
    except S3Error as exc:
        if exc.code in ("NoSuchUpload", "NoSuchBucket", "NoSuchKey"):
            return False
        raise


def owned_uploads(bucket: str) -> list[tuple[str, str]]:
    client = _client()
    return [(k, u) for k, u in incomplete_uploads(bucket) if _owns(client, bucket, k, u)]


def multipart_delta(before: list[tuple[str, str]], after: list[tuple[str, str]]) -> list[str]:
    # What a validation run must never do: leave MORE in-progress uploads on the server than it
    # found. Pre-existing entries are somebody else's problem; new ones are this run's residue.
    added = sorted({u for _, u in after} - {u for _, u in before})
    return added


def incomplete_uploads(bucket: str, *, page_size: int | None = None) -> list[tuple[str, str]]:
    # (object_name, upload_id) pairs. minio-py exposes multipart listing only privately, and an
    # abandoned upload is invisible to `list_objects` while still holding storage - so a cleanup
    # that only removed objects would leave it behind and the bucket would refuse to be deleted.
    #
    # MEASURED, NOT ASSUMED: on MinIO RELEASE.2025-04-22 this listing is SERVER-WIDE. Querying any
    # bucket returns every in-progress upload on the server, and a brand-new empty bucket reports
    # the same list as `mesh-artifacts`. So the result is NOT per-bucket evidence, and it must
    # never be folded into a per-bucket fingerprint: one unrelated in-flight upload would then make
    # every unrelated bucket compare unequal.
    #
    # `AbortMultipartUpload`, by contrast, IS bucket-scoped: it removes the entry only when the
    # bucket named is the upload's owning bucket, and returns 204 without effect otherwise. That
    # asymmetry is why cleanup aborts every listed upload against its OWN bucket - the ones it owns
    # go, the rest are harmless no-ops.
    client = _client()
    if not client.bucket_exists(bucket):
        return []
    # PAGINATED. `_list_multipart_uploads` returns one page; a server holding more uploads than the
    # page limit would silently hand back a prefix, and a cleanup that trusted it would report
    # success with residue still on the server.
    out: list[tuple[str, str]] = []
    key_marker, upload_id_marker = None, None
    for _ in range(1000):  # a bound, not a timeout: each pass consumes at least one page
        # `page_size` exists so a test can force real pagination without staging ten thousand
        # uploads; production callers leave it alone and take the server's default page.
        result = client._list_multipart_uploads(
            bucket, key_marker=key_marker, upload_id_marker=upload_id_marker,
            max_uploads=page_size)
        out += [(u.object_name, u.upload_id) for u in (result.uploads or [])]
        if not result.is_truncated:
            break
        key_marker, upload_id_marker = result.next_key_marker, result.next_upload_id_marker
    return out


def server_multipart_uploads() -> list[tuple[str, str]]:
    # The same server-wide list, named for what it actually is so callers cannot mistake it for a
    # property of one bucket. Used as a "did this run leave more behind than it found" counter.
    import meshpipeline.settings.providers as provcfg

    return incomplete_uploads(provcfg.MINIO_BUCKET)


def drop(bucket: str) -> dict:
    # Removes EXACTLY this task bucket: its incomplete uploads, every version and delete marker,
    # then the bucket itself - and then proves the bucket is gone by asking the server again. No
    # pattern deletion, no shared-bucket sweep. Raises on any failure, because a cleanup error that
    # is logged and swallowed is how residue becomes permanent.
    name = require_isolated(bucket, protected=None)
    client = _client()
    counts = {"aborted_uploads": 0, "removed_versions": 0}
    if not client.bucket_exists(name):
        return {**counts, "bucket_existed": False}

    # OWNERSHIP IS PROVEN, NOT INFERRED. `remove_bucket` succeeds even while this bucket still owns
    # an in-progress upload - measured - and once the bucket is gone that upload can never be
    # addressed again, because the abort needs the owning bucket's name. So the bucket may only be
    # removed after the server itself confirms this bucket owns nothing.
    #
    # The listing is server-wide and cannot answer "is this mine". `ListParts(bucket, key, id)` can:
    # it is bucket-scoped and returns NoSuchUpload when the named bucket is not the owner. That
    # probe is the classifier; a shrinking global set is not, because it cannot distinguish "mine,
    # aborted" from "somebody else's, changed underneath me".
    for _ in range(10):
        owned = [(k, u) for k, u in incomplete_uploads(name) if _owns(client, name, k, u)]
        if not owned:
            break
        for key, upload_id in owned:
            client._abort_multipart_upload(name, key, upload_id)
            counts["aborted_uploads"] += 1

    still_owned = [(k, u) for k, u in incomplete_uploads(name) if _owns(client, name, k, u)]
    if still_owned:
        raise NotIsolatedError(
            f"refusing to delete {name!r}: it still owns {len(still_owned)} in-progress upload(s) "
            f"{[u for _, u in still_owned]}. Deleting it would strand them permanently.")

    for obj in client.list_objects(name, recursive=True, include_version=True):
        client.remove_object(name, obj.object_name, version_id=obj.version_id)
        counts["removed_versions"] += 1

    client.remove_bucket(name)

    # INDEPENDENT verification: ask the server, do not trust the calls above having returned.
    if client.bucket_exists(name):
        raise NotIsolatedError(f"task bucket {name!r} still exists after cleanup")
    return {**counts, "bucket_existed": True}


def assert_protected_unchanged(before: str, after: str, *, bucket: str,
                               before_rows: list[dict] | None = None,
                               after_rows: list[dict] | None = None) -> None:
    if before == after:
        return
    detail = ""
    if before_rows is not None and after_rows is not None:
        b = {(r["key"], r["version_id"]) for r in before_rows}
        a = {(r["key"], r["version_id"]) for r in after_rows}
        added, removed = sorted(a - b), sorted(b - a)
        detail = f"\n  added: {added[:20]}\n  removed: {removed[:20]}"
    raise NotIsolatedError(
        f"the protected bucket {bucket!r} changed across this validation run - "
        f"{before} != {after}.{detail}")


def snapshot_json(bucket: str) -> str:
    return json.dumps({"bucket": bucket, "fingerprint": fingerprint(bucket),
                       "rows": inventory(bucket)}, sort_keys=True)
