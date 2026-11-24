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

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from meshpipeline.cad.repair_v4.verdict import memo
from meshpipeline.cad.repair_v4.verdict.valid import CALL_TIMEOUT_S, CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool


@dataclass(frozen=True)
class FaithfulReport:
    ok: bool
    max_dev_mm: float
    bound_mm: float
    # None = the original has no area/volume to compare against; whether that is a change is in
    # `reasons` (`area_changed`, `volume_changed`), never inferred from the ratio.
    area_ratio: float | None
    volume_ratio: float | None
    bodies_before: int
    bodies_after: int
    declared_added: list[str] = field(default_factory=list)
    declared_removed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: the repaired shape's bbox diagonal over the original's, and the diagonal itself (mm). None = not measured
    #: (no extent, or an older row). Layer 3 gates nothing; report.py flags a clean output that shrank the part.
    bbox_ratio: float | None = None
    diag_after_mm: float | None = None

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
        area_ratio=None if v["area_ratio"] is None else float(v["area_ratio"]),
        volume_ratio=None if v["volume_ratio"] is None else float(v["volume_ratio"]),
        bodies_before=int(v["bodies_before"]),
        bodies_after=int(v["bodies_after"]),
        bbox_ratio=None if v.get("bbox_ratio") is None else float(v["bbox_ratio"]),
        diag_after_mm=None if v.get("diag_after_mm") is None else float(v["diag_after_mm"]),
        declared_added=list(declared_added),
        declared_removed=list(declared_removed),
        reasons=list(v["reasons"]),
    )


def _to_brep(pool: WorkerPool, step: str, key: tuple[str, str] | None, out: Path) -> str:
    """The BREP of `step`, read once per (pool, path, bytes) and shared with layer 1 (`memo.read_step`):
    the search judges thousands of states against ONE original, and layer 1 has just read the repaired
    file. `out` is used only when nothing can be memoised."""
    read = memo.read_step(pool, step, key, str(out))
    if not read.ok or read.value is None:
        raise _unavailable("read_step_xcaf", read)
    return str(read.value["brep"])


def check_faithful_steps(
    pool: WorkerPool,
    original_step: str,
    repaired_step: str,
    *,
    declared_added: list[str],
    declared_removed: list[str],
) -> FaithfulReport:
    """`check_faithful` on two STEP files (read to BREP, in mm, in a worker). Both reads, and the report
    itself, are memoised per (pool, both files' path and bytes, declared edits); a raise is never memoised."""
    original_key, repaired_key = memo.content_key(original_step), memo.content_key(repaired_step)
    report_key = None
    if original_key is not None and repaired_key is not None:
        report_key = ("faithful", original_key, repaired_key, tuple(declared_added), tuple(declared_removed))
        hit = memo.recall(pool, report_key)
        if hit is not None:
            return cast(FaithfulReport, hit)
    work = Path(tempfile.mkdtemp(prefix="faithful_"))
    try:
        report = check_faithful(
            pool,
            _to_brep(pool, original_step, original_key, work / "original.brep"),
            _to_brep(pool, repaired_step, repaired_key, work / "repaired.brep"),
            declared_added=declared_added,
            declared_removed=declared_removed,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if report_key is not None:
        memo.remember(pool, report_key, report)
    return report
