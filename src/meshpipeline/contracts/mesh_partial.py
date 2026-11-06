# Responsibility: Declare how the product reads a boundary surface of a mesh-so-far that a running
#                 mesher stored beside its result.
# Owns: the injected reader and the neutral accessor the API calls.
# Boundaries: no storage client, no bucket name and no key derivation - runtime composition injects
#             the provider's (adapters/mesh_execution/gcs_exchange); with none injected there is
#             nothing to read and the caller is told so, which is the ordinary state of a local
#             stack that runs no remote mesher.
# Collaborates with: api/v1/simulation.py (serves the bytes), runtime/composition.py (binds it),
#                    artifact_keys.partial_key_beside (where such an object lives).
"""A NARROW READ, AND DELIBERATELY NOTHING MORE.

One capability: given a key, the bytes, or None. It cannot list, cannot write and cannot delete,
and the key is never a caller's - the route reads it from the job's own event log. Read-only here
is the whole point: it is what lets the deployment grant the API `roles/storage.objectViewer` on
the exchange and no more, and serving the bytes through the API rather than a signed URL is what
lets that bucket keep public access prevented, with no CORS rule on it.
"""
from __future__ import annotations

from collections.abc import Callable

#: key -> the object's bytes, or None when there is no such object
Reader = Callable[[str], bytes | None]

_reader: Reader | None = None


def set_partial_reader(reader: Reader | None) -> None:
    global _reader
    _reader = reader


def partial_reader() -> Reader | None:
    """The reader, or None where none is configured: no remote exchange, so no frame to read."""
    return _reader


def read_partial(object_key: str) -> bytes | None:
    reader = partial_reader()
    return None if reader is None else reader(object_key)


__all__ = ["Reader", "partial_reader", "read_partial", "set_partial_reader"]
