# Responsibility: Declare how delivery reads and deletes the big result files a mesh run kept in its exchange bucket (RUN-7).
# Owns: the injected reader and deleter, and the neutral accessors delivery calls.
# Boundaries: no storage client and no bucket names - runtime composition injects the provider's (adapters/mesh_execution/gcs_exchange); with none injected, no run keeps a file, so none is ever asked for.
from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO

#: (bucket, key) -> a binary reader of one kept object, read to its end
Reader = Callable[[str, str], BinaryIO]
#: (bucket, key) -> None: delete one kept object
Deleter = Callable[[str, str], None]

_reader: Reader | None = None
_deleter: Deleter | None = None


class KeptObjectsUnavailable(RuntimeError):
    """A result names files kept in a bucket, and this process has no way to read that bucket."""


def set_kept_objects(reader: Reader | None, deleter: Deleter | None) -> None:
    global _reader, _deleter
    _reader, _deleter = reader, deleter


def kept_reader() -> Reader:
    if _reader is None:
        raise KeptObjectsUnavailable(
            "this result keeps files in the exchange bucket, and no reader of it is configured - "
            "runtime composition must call set_kept_objects()")
    return _reader


def kept_deleter() -> Deleter | None:
    return _deleter


__all__ = ["Deleter", "KeptObjectsUnavailable", "Reader", "kept_deleter", "kept_reader",
           "set_kept_objects"]
