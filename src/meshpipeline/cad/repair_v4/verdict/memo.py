# Responsibility: Per-pool memos of the checker's invariant work: a STEP file's read, its layer 1 report and
#                 a layer 3 report, so one input is never measured twice by the same workers.
# Owns: `content_key`, `read_step`, `remember`/`recall` and the per-pool LRU stores.
# Boundaries: no OCCT and no judgement. It never stores a failure: a call that did not complete, or a report
#             that raised, is measured again next time. Callers decide what a result means (valid.py, faithful.py).
"""Why this is safe: every memoised value is a pure function of what keys it.

- Keys carry the file's absolute path AND the sha256 of its bytes, so a rewritten file is a new key, and a
  report whose detail quotes the path is returned only for that path.
- Memos hang off the pool (`WeakKeyDictionary`): they live exactly as long as the workers that measured them,
  so a new pool (new kernel code, a test's scripted pool) starts empty.
- Stored reports are deep-copied in and out, so a caller that changes one changes nothing else.

Why it is worth it: the re-probe runs the cheap tier and then the full tier on the same unchanged file, and a
verdict read each STEP up to three times. Layers 1 and 3 were most of a file's wall-clock (Task 43).
"""

from __future__ import annotations

import copy
import itertools
import os
import shutil
import tempfile
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S
from meshpipeline.cad.repair_v4.verdict.cache import file_sha256
from meshpipeline.cad.repair_v4.worker_pool import CallResult

#: The layer's one-call limit, the same number valid.py publishes as `CALL_TIMEOUT_S`. It comes from
#: `repair_v4.limits` and not from valid.py because valid.py imports THIS module (importing it back would be a
#: cycle); limits.py is the one place it is written.
READ_TIMEOUT_S = CHECKER_CALL_TIMEOUT_S
MAX_READS = 32  # BREP files kept per pool; the oldest is deleted
MAX_REPORTS = 2048  # layer 1 and layer 3 reports kept per pool (small objects)


class _LRU:
    def __init__(self, size: int, on_evict: Callable[[Any], None] | None = None) -> None:
        self._size, self._on_evict = size, on_evict
        self._items: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key]

    def put_if_absent(self, key: Hashable, value: Any) -> bool:
        """False when `key` was already there (a concurrent caller measured it too); the stored one stays,
        because a caller may be using it right now."""
        evicted = []
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = value
            while len(self._items) > self._size:
                evicted.append(self._items.popitem(last=False)[1])
        if self._on_evict is not None:
            for value in evicted:
                self._on_evict(value)
        return True


class _PoolMemo:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="verdict_memo_")
        self.names = itertools.count()
        self.reads = _LRU(MAX_READS, on_evict=lambda value: _remove(value["brep"]))
        self.reports = _LRU(MAX_REPORTS)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


_MEMOS: weakref.WeakKeyDictionary[Any, _PoolMemo] = weakref.WeakKeyDictionary()
_MEMOS_LOCK = threading.Lock()


def _memo(pool: Any) -> _PoolMemo | None:
    """The pool's memo, made on first use and removed (with its BREP files) when the pool goes. None for a
    pool that cannot be weakly referenced: then nothing is memoised and every call is measured."""
    with _MEMOS_LOCK:
        try:
            memo = _MEMOS.get(pool)
            if memo is None:
                memo = _PoolMemo()
                _MEMOS[pool] = memo
                weakref.finalize(pool, shutil.rmtree, memo.dir, ignore_errors=True)
        except TypeError:
            return None
    return memo


def content_key(path: str) -> tuple[str, str] | None:
    """(absolute path, sha256 of the bytes), or None when the file cannot be read: an unreadable path is not
    memoised, and the kernel call says what is wrong with it."""
    try:
        return os.path.abspath(path), file_sha256(path)
    except OSError:
        return None


def read_step(pool: Any, path: str, key: tuple[str, str] | None, brep_out: str) -> CallResult:
    """`read_step_xcaf` of `path`, once per (pool, key). Only a successful read is kept, in a BREP the memo
    owns; the returned value is the caller's own copy, but its BREP is shared and must be treated as
    read-only. With no key, or a pool that cannot hold a memo, it is a plain read into `brep_out`."""
    memo = _memo(pool) if key is not None else None
    if memo is None:
        return pool.call("read_step_xcaf", {"path": str(path), "brep_out": brep_out}, timeout_s=READ_TIMEOUT_S)
    stored = memo.reads.get(key)
    if stored is not None and os.path.exists(stored["brep"]):
        return CallResult(ok=True, value=copy.deepcopy(stored), error=None, status="ok", seconds=0.0)
    own = os.path.join(memo.dir, f"read_{next(memo.names)}.brep")
    result = pool.call("read_step_xcaf", {"path": str(path), "brep_out": own}, timeout_s=READ_TIMEOUT_S)
    if result.ok and result.value is not None and stored is None:
        memo.reads.put_if_absent(key, copy.deepcopy(result.value))
    return result


def recall(pool: Any, key: Hashable) -> Any | None:
    """A copy of the report stored under `key` for this pool, or None."""
    memo = _memo(pool)
    if memo is None:
        return None
    stored = memo.reports.get(key)
    return None if stored is None else copy.deepcopy(stored)


def remember(pool: Any, key: Hashable, report: Any) -> None:
    memo = _memo(pool)
    if memo is not None:
        memo.reports.put_if_absent(key, copy.deepcopy(report))
