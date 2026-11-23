# Responsibility: Layer 3 of the CRS v4 checker - did the repair keep the part the customer sent?
# Owns: FaithfulReport and `check_faithful` / `check_faithful_steps`.
# Boundaries: no OCCT here; the measurement is the `faithful_check` kernel call (faithful_kernel).
"""Layer 3 (faithful). The bound is min(0.1 mm, 0.001 x diagonal), always in millimetres.

Why mm is not an assumption: shapes cross the process boundary as BREP files, and a BREP is always
millimetres (the STEP reader converts on read, see xcaf_io), so an inch part is measured in mm too.

"The original" is the file the product was handed. For an injected benchmark file that is the
DAMAGED input, never the clean source: the product never sees the clean source, so measuring
against it would use knowledge it does not have.

Declared ids are face indices `F<k>` (1-based in the shape's face map): `declared_added` indexes
the repaired shape and is exempt in the repaired -> original direction; `declared_removed` indexes
the original and is exempt in the original -> repaired direction.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from meshpipeline.cad.repair_v4.verdict.valid import CALL_TIMEOUT_S, CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool


@dataclass(frozen=True)
class FaithfulReport:
    ok: bool
    max_dev_mm: float
    bound_mm: float
    area_ratio: float
    volume_ratio: float
    bodies_before: int
    bodies_after: int
    declared_added: list[str] = field(default_factory=list)
    declared_removed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str | None:
        """The first reason it failed (`fill_too_large`, `deviation_exceeds_bound`, ...)."""
        return self.reasons[0] if self.reasons else None


class DeclaredIdError(ValueError):
    """A declared added/removed id does not match the shapes. The CALLER's bug (a stale or mistranslated
    id), so it is neither a finding about the file nor `CheckerUnavailable`."""


def _unavailable(call: str, result: CallResult) -> CheckerUnavailable:
    return CheckerUnavailable(f"{call}: {result.status}: {result.error}")


def check_faithful(
    pool: WorkerPool,
    original: str,
    repaired: str,
    *,
    declared_added: list[str],
    declared_removed: list[str],
) -> FaithfulReport:
    """`original` and `repaired` are BREP paths. Raises `CheckerUnavailable` if no measurement could
    be made (worker timeout or crash, unreadable BREP, bad declared id): that is not a finding."""
    result = pool.call(
        "faithful_check",
        {
            "original_brep": str(original),
            "repaired_brep": str(repaired),
            "declared_added": list(declared_added),
            "declared_removed": list(declared_removed),
        },
        timeout_s=CALL_TIMEOUT_S,
    )
    if result.status == "error" and (result.error or "").startswith("DeclaredIdMismatch:"):
        raise DeclaredIdError(result.error)
    if not result.ok or result.value is None:
        raise _unavailable("faithful_check", result)
    v = result.value
    return FaithfulReport(
        ok=bool(v["ok"]),
        max_dev_mm=float(v["max_dev_mm"]),
        bound_mm=float(v["bound_mm"]),
        area_ratio=float(v["area_ratio"]),
        volume_ratio=float(v["volume_ratio"]),
        bodies_before=int(v["bodies_before"]),
        bodies_after=int(v["bodies_after"]),
        declared_added=list(declared_added),
        declared_removed=list(declared_removed),
        reasons=list(v["reasons"]),
    )


def _to_brep(pool: WorkerPool, step: str, out: Path) -> str:
    read = pool.call("read_step_xcaf", {"path": str(step), "brep_out": str(out)}, timeout_s=CALL_TIMEOUT_S)
    if not read.ok or read.value is None:
        raise _unavailable("read_step_xcaf", read)
    return str(read.value["brep"])


_ORIGINALS: dict[tuple[str, int, int], str] = {}
_ORIGINALS_DIR: list[Path] = []


def _original_brep(pool: WorkerPool, step: str) -> str:
    """The original's BREP, read once per (path, mtime, size) and kept for the process: the search
    judges thousands of states against ONE original, and re-reading it each time is pure waste."""
    stat = os.stat(step)
    key = (os.path.abspath(step), stat.st_mtime_ns, stat.st_size)
    cached = _ORIGINALS.get(key)
    if cached is not None and os.path.exists(cached):
        return cached
    if not _ORIGINALS_DIR:
        root = Path(tempfile.mkdtemp(prefix="faithful_originals_"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        _ORIGINALS_DIR.append(root)
    out = _ORIGINALS_DIR[0] / f"original_{len(_ORIGINALS)}.brep"
    _ORIGINALS[key] = _to_brep(pool, step, out)
    return _ORIGINALS[key]


def check_faithful_steps(
    pool: WorkerPool,
    original_step: str,
    repaired_step: str,
    *,
    declared_added: list[str],
    declared_removed: list[str],
) -> FaithfulReport:
    """`check_faithful` on two STEP files (read to BREP, in mm, in a worker). The original's BREP is
    memoised across calls."""
    work = Path(tempfile.mkdtemp(prefix="faithful_"))
    try:
        return check_faithful(
            pool,
            _original_brep(pool, original_step),
            _to_brep(pool, repaired_step, work / "repaired.brep"),
            declared_added=declared_added,
            declared_removed=declared_removed,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
