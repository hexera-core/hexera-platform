# Responsibility: The neutral port the CRS v4/v5 stores reach a `gs://` bucket through (the verdict cache, recipe
#                 memory, the wave model ledger), so the repair engine never imports a cloud SDK or an adapter.
# Owns: `bind`, `bucket`, `precondition_failed`, `not_found`, `split_gs`, `GcsUnbound`.
# Boundaries: no SDK here. The binding (a bucket opener plus the SDK's two exception types) is supplied at runtime
#             by whoever composes the process: runtime/composition.py, the eval harness's entry points, the tests -
#             each through `adapters/mesh_execution/gcs_exchange.bind_gcs_buckets`.
"""A gs:// bucket port for the CRS repair stores."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class GcsUnbound(RuntimeError):
    """A gs:// store was asked for while no bucket binding was composed into this process."""


_binding: tuple[Callable[[str], Any], type[BaseException], type[BaseException]] | None = None


def bind(bucket_of: Callable[[str], Any], precondition_failed: type[BaseException],
         not_found: type[BaseException]) -> None:
    """`bucket_of(name)` opens a Bucket-like (`blob(key)`, `list_blobs(prefix=)`); the two exception types are what
    a create-if-absent (HTTP 412) and a delete of a missing object (HTTP 404) raise."""
    global _binding
    _binding = (bucket_of, precondition_failed, not_found)


def unbind() -> None:
    global _binding
    _binding = None


def _bound() -> tuple[Callable[[str], Any], type[BaseException], type[BaseException]]:
    if _binding is None:
        raise GcsUnbound("no gs:// bucket binding is composed into this process "
                         "(adapters/mesh_execution/gcs_exchange.bind_gcs_buckets)")
    return _binding


def bucket(name: str) -> Any:
    return _bound()[0](name)


def precondition_failed() -> type[BaseException]:
    return _bound()[1]


def not_found() -> type[BaseException]:
    return _bound()[2]


def split_gs(uri: str) -> tuple[str, str]:
    """(bucket, key) of a `gs://bucket/key` uri."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// uri: {uri}")
    name, _, key = uri[len("gs://"):].partition("/")
    return name, key


__all__ = ["GcsUnbound", "bind", "bucket", "not_found", "precondition_failed", "split_gs", "unbind"]
