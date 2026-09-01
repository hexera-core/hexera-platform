# Responsibility: Implement the object-store contract against MinIO or another S3-compatible service.
# Boundaries: storage operations and signed URLs; bucket naming rules are enforced here because S3 imposes them.
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.object_storage import ObjectNotFound, StorageError, StoredObject, normalize_key

logger = logging.getLogger(__name__)


class MinioStore:
    def __init__(self) -> None:
        self._bucket = provcfg.MINIO_BUCKET

    def _client(self, *, endpoint: str | None = None):
        from minio import Minio
        # secure follows MINIO_SECURE, defaulting to plain HTTP because the local stack's store is
        # a compose service on the same host. A hosted S3-compatible endpoint - Google Cloud
        # Storage through its S3-interoperability API, for instance - serves TLS only and refuses
        # a plain-HTTP request, so this cannot be a constant.
        # region is explicit: without it minio-py issues a live GetBucketLocation before signing,
        # which the signing client - built on an address this process may not be able to reach -
        # cannot complete. See MINIO_REGION. Giving it to BOTH clients keeps one source of truth
        # and makes a wrong region fail on the first upload instead of only on a download.
        return Minio(endpoint or provcfg.MINIO_ENDPOINT, access_key=provcfg.MINIO_ACCESS_KEY,
                     secret_key=provcfg.MINIO_SECRET_KEY, secure=provcfg.MINIO_SECURE,
                     region=provcfg.MINIO_REGION)

    def _ensure_bucket(self, client) -> None:
        # local convenience only - a hosted bucket is provisioned by IaC, never by app code
        try:
            if not client.bucket_exists(self._bucket):
                client.make_bucket(self._bucket)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MinioStore: bucket check failed: %s", exc)

    def upload_file(self, *, local_path: Path, object_key: str,
                    content_type: str | None = None,
                    metadata: Mapping[str, str] | None = None) -> StoredObject:
        key = normalize_key(object_key)
        try:
            client = self._client()
            self._ensure_bucket(client)
            size = Path(local_path).stat().st_size
            res = client.fput_object(
                bucket_name=self._bucket, object_name=key, file_path=str(local_path),
                content_type=content_type or "application/octet-stream",
                metadata=dict(metadata) if metadata else None)
            return StoredObject(object_key=key, size_bytes=size,
                                content_type=content_type,
                                checksum=getattr(res, "etag", None))
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"minio upload failed for {key}: {exc}") from exc

    def get_bytes(self, *, object_key: str) -> bytes:
        from minio.error import S3Error
        key = normalize_key(object_key)
        resp = None
        try:
            resp = self._client().get_object(self._bucket, key)
            return resp.read()
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFound(f"object not found: {key}") from exc
            raise StorageError(f"could not read {key}") from exc
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()

    def download_file(self, *, object_key: str, destination: Path) -> None:
        key = normalize_key(object_key)
        from minio.error import S3Error
        try:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            self._client().fget_object(self._bucket, key, str(destination))
        except S3Error as exc:
            # An object that is NOT THERE is a different fact from a store we could not reach,
            # and callers act on the difference: a missing object is a data-integrity failure
            # nobody should retry, while an unreachable provider is transient. Collapsing both
            # into StorageError made a vanished upload look retryable. GCS already distinguishes
            # them; this keeps the two backends answering the same question the same way.
            if getattr(exc, "code", "") in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFound(f"minio object not found: {key}") from exc
            raise StorageError(f"minio download failed for {key}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"minio download failed for {key}: {exc}") from exc

    def create_download_url(self, *, object_key: str, expires_in: timedelta) -> str:
        key = normalize_key(object_key)
        try:
            # Signed against the PUBLIC address, not the one this process dials. The recipient is a
            # browser, which does not share this process's view of the network, and the host is
            # part of the signature - so signing with the internal address yields a URL that
            # cannot be resolved and cannot be repaired by rewriting it. See MINIO_PUBLIC_ENDPOINT.
            client = self._client(endpoint=provcfg.MINIO_PUBLIC_ENDPOINT)
            return client.presigned_get_object(self._bucket, key, expires=expires_in)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"minio signed url failed for {key}: {exc}") from exc

    def delete_object(self, *, object_key: str) -> None:
        key = normalize_key(object_key)
        try:
            self._client().remove_object(self._bucket, key)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"minio delete failed for {key}: {exc}") from exc

    def exists(self, *, object_key: str) -> bool:
        key = normalize_key(object_key)
        from minio.error import S3Error
        try:
            self._client().stat_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise StorageError(f"minio exists failed for {key}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"minio exists failed for {key}: {exc}") from exc

    def object_checksum(self, *, object_key: str) -> str | None:
        key = normalize_key(object_key)
        try:
            st = self._client().stat_object(self._bucket, key)
            return (getattr(st, "etag", None) or "").strip('"') or None
        except Exception:  # noqa: BLE001
            return None
