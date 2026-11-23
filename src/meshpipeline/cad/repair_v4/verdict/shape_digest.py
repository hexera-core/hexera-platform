# Responsibility: An exact content digest of an OCCT shape, and a small per-worker LRU keyed by it, so a kernel
#                 result that is a pure function of one shape is computed once per worker.
# Owns: `digest`, `WorkerMemo`.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. It never decides anything:
#             callers memoise only results that depend on nothing but the digested shape.
"""Why the digest is exact: it is the sha256 of the shape's `BinTools` serialisation, which writes geometry,
topology, locations, orientations, tolerances and flags with raw IEEE doubles (a 1-ulp change to one coordinate
changes it; tested natively). Two shapes with the same digest are the same input to any OCCT algorithm, so a
deterministic result computed on one is the result on the other.

It is NOT a similarity measure: a STEP round trip never reproduces a shape bit for bit (Lane A, 60/60 files), so the
digest never claims the round-tripped shape equals the input. It pays off where shapes really are identical: the
untouched bodies of a search candidate, and states the search reaches more than once.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

_DIR: list[str] = []


def digest(shape: Any) -> str:
    """sha256 of `shape`'s BinTools serialisation, triangulation and normals included: bounding boxes (and so the
    Booleans) read a triangulation when one is present, so it is part of the input."""
    from OCP.BinTools import BinTools, BinTools_FormatVersion

    if not _DIR:
        _DIR.append(tempfile.mkdtemp(prefix="shape_digest_"))
    path = os.path.join(_DIR[0], f"{os.getpid()}.bin")
    if not BinTools.Write_s(shape, path, True, True, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT):
        raise RuntimeError("BinTools could not serialise the shape")
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def file_digest(path: str) -> str:
    """sha256 of a file's bytes: a BREP file IS the shape it is read into."""
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


class WorkerMemo:
    """A bounded LRU for one worker process. Only a computed value is stored; a raise stores nothing."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._items: OrderedDict[Hashable, Any] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        if key in self._items:
            self._items.move_to_end(key)
            self.hits += 1
            return self._items[key]
        self.misses += 1
        value = compute()
        self._items[key] = value
        while len(self._items) > self._size:
            self._items.popitem(last=False)
        return value
