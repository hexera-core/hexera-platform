# Responsibility: Layer 1 of the CRS v4 checker - is this STEP file a valid, unit-faithful solid
#                 model that survives a write and re-read?
# Owns: Finding, ValidReport, `check_valid` and the hard/soft code sets.
# Boundaries: no OCCT here. Every kernel call goes through a WorkerPool; the geometry rules are in
#             `valid_kernel`, the STEP I/O in `xcaf_io`.
"""Layer 1 (valid) of the one checker `verdict` that scores, gates and benchmarks every repair.

Hard codes fail a file; soft codes are reported and never fail it. Small entities and loose
tolerances are soft because real damage from them shows up in layer 2 (meshing); failing them here
would fill the failing set with files that mesh fine.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S
from meshpipeline.cad.repair_v4.units import WRITER_CODES, canonical_unit
from meshpipeline.cad.repair_v4.verdict import memo
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool

HARD_CODES = (
    "brep_invalid",
    "open_shell",
    "non_manifold",
    "non_positive_volume",
    "self_intersection",
    "body_overlap",
    "units_changed",
    "round_trip_invalid",
    "no_solids",
    "unbounded_geometry",
)
#: The one hard code that is not a defect: the input has infinite extent, so it is outside what the definition can
#: judge. `check_valid` stops at it (no round trip), `verdict` does not mesh it, and the scoreboard reports it as its own
#: stratum rather than a failure (`Verdict.out_of_scope`).
UNBOUNDED = "unbounded_geometry"
#: `orientation_unchecked` (lane C15 follow-up): the BREP's signed-volume check on an op-made state could not run (pool
#: failure, timeout); reported so a skipped check shows in the row, never a failure
SOFT_CODES = ("small_edge", "small_face", "large_tolerance", "orientation_unchecked")

#: The limit on ONE of this layer's kernel calls. Defined once in `repair_v4.limits` (the per-file wall that
#: bounds their SUM is evalspec.DEFAULT_FILE_TIMEOUT_S); re-exported under this name because faithful.py and
#: every call site below already read it from here.
CALL_TIMEOUT_S = CHECKER_CALL_TIMEOUT_S


class CheckerUnavailable(RuntimeError):
    """The checker could not reach a verdict: a worker timed out or crashed, or a kernel call failed
    for a reason that is not the file's. It is NOT a finding about the file, so callers must not
    score it as invalid; retry, or treat it as an infrastructure failure.
    """


@dataclass(frozen=True)
class Finding:
    code: str
    entities: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ValidReport:
    ok: bool
    hard: list[Finding]
    soft: list[Finding]
    units: str
    solids: int
    round_trip_ok: bool  # False, and not attempted, when the input is unbounded

    @property
    def unbounded(self) -> bool:
        """True when the input has infinite extent (`unbounded_geometry`): not a defect, outside the definition."""
        return any(f.code == UNBOUNDED for f in self.hard)


def _findings(raw: list[dict]) -> list[Finding]:
    return [Finding(item["code"], list(item["entities"]), dict(item["detail"])) for item in raw]


def _unavailable(call: str, result: CallResult) -> CheckerUnavailable:
    return CheckerUnavailable(f"{call}: {result.status}: {result.error}")


def _is_file_error(result: CallResult) -> bool:
    """A kernel call that raised ValueError is reporting about the file (unreadable STEP/BREP)."""
    return result.status == "error" and (result.error or "").startswith("ValueError:")


#: The hard codes found only by layer 1's two Boolean checks (per-solid self-intersection, pairwise overlap): about
#: 98% of a check's kernel time (C16, measured).
BOOLEAN_CODES = frozenset({"self_intersection", "body_overlap"})


def _check_shape(pool: WorkerPool, brep: str, *, booleans: bool = True) -> CallResult:
    return pool.call("check_valid", {"brep": brep, "booleans": booleans}, timeout_s=CALL_TIMEOUT_S)


class _ReadInvalid:
    """Set once the input's own check has a hard finding. The round trip READS it, never waits for it: a round-trip
    check that starts first runs in full, so no overlap of the two checks is ever lost to this."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def __bool__(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class _RoundTripCalls:
    """The kernel calls of one round trip, made without judging them. A later call is None when an
    earlier one failed, exactly where the judging stops."""

    written: CallResult
    reread: CallResult | None = None
    checked: CallResult | None = None


def _round_trip_calls(
    pool: WorkerPool, doc: dict, work: Path, written_units: str, read_invalid: _ReadInvalid | None = None
) -> _RoundTripCalls:
    """Write `doc` as STEP in `written_units`, read it back and check the re-read shape. Nothing here
    needs the input's own check, so it can run while that check does (`check_valid`).

    The re-read's Booleans are skipped when the input's check already has a hard finding (`read_invalid`): the
    verdict is then invalid whatever they find, and `_judge_round_trip` ignores Boolean codes in exactly that case,
    so skipping them or not gives the same report (C16 lever b')."""
    written = pool.call(
        "write_step_xcaf",
        {
            "brep": doc["brep"],
            "labels": doc["labels"],
            "assembly_ids": doc["assembly_ids"],
            "units": written_units,
            "path": str(work / "round_trip.step"),
        },
        timeout_s=CALL_TIMEOUT_S,
    )
    if not written.ok:
        return _RoundTripCalls(written)
    reread = pool.call(
        "read_step_xcaf",
        {"path": str(work / "round_trip.step"), "brep_out": str(work / "round_trip.brep")},
        timeout_s=CALL_TIMEOUT_S,
    )
    if not reread.ok or reread.value is None:
        return _RoundTripCalls(written, reread)
    booleans = not (read_invalid is not None and read_invalid)
    return _RoundTripCalls(written, reread, _check_shape(pool, reread.value["brep"], booleans=booleans))


def _written_units(doc: dict) -> str:
    """A unit OCCT cannot write (a yard file, say) is a legitimate file, not a broken one: the BREP is
    always millimetres, so it is written back in mm and the caller compares against that. The unit
    the file really has is still what the report carries."""
    return doc["units"] if doc["units"] in WRITER_CODES else "mm"


def _judge_round_trip(calls: _RoundTripCalls, doc: dict, solids: int, known: set[str]) -> tuple[bool, str | None, dict]:
    """Returns (ok, units after, why-not). Raises `CheckerUnavailable` where a call failed for a reason
    that is not the file's.

    `known` are the hard codes the original already has: a defect that survives the round trip is
    not the round trip's fault, only a NEW hard code is. When `known` is not empty the file is already invalid,
    and a NEW Boolean code is not counted either: the round trip's Booleans may have been skipped then (lever b'),
    and counting them only when they ran would make `round_trip_ok` depend on timing. Validity cannot change: a
    round trip only adds `round_trip_invalid` to a file with no other hard code.
    """
    written = calls.written
    if not written.ok and not _is_file_error(written):
        raise _unavailable("write_step_xcaf", written)
    if not written.ok:
        return False, None, {"stage": "write", "error": written.error}
    reread = calls.reread
    assert reread is not None  # a successful write is always followed by the re-read
    if not reread.ok and not _is_file_error(reread):
        raise _unavailable("read_step_xcaf", reread)
    if not reread.ok or reread.value is None:
        return False, None, {"stage": "read", "error": reread.error}
    again = reread.value
    problems: dict = {}
    names_before = {key: entry["name"] for key, entry in doc["labels"].items()}
    names_after = {key: entry["name"] for key, entry in again["labels"].items()}
    lost = sorted(key for key in names_before if names_before[key] != names_after.get(key))
    if lost:  # extra keys after the re-read are not a loss
        problems["labels_changed"] = lost
    checked = calls.checked
    assert checked is not None  # a successful re-read is always checked
    if not checked.ok or checked.value is None:
        raise _unavailable("check_valid", checked)
    if checked.value["solids"] != solids:
        problems["solids"] = {"before": solids, "after": checked.value["solids"]}
    found = {item["code"] for item in checked.value["hard"]}
    if known:
        found -= BOOLEAN_CODES
    broken = sorted(found - known)
    if broken:
        problems["hard"] = broken
    return not problems, again["units"], problems


def _bounded(pool: WorkerPool, doc: dict) -> bool:
    """True only when the kernel says the shape is bounded. Anything else (unbounded, or a call that failed)
    is False, and `check_valid` then keeps the plain order: check first, round trip only if it may."""
    extent = pool.call("valid_extent", {"brep": doc["brep"]}, timeout_s=CALL_TIMEOUT_S)
    return bool(extent.ok and extent.value is not None and extent.value.get("unbounded") is False)


def check_valid(pool: WorkerPool, step_path: str, *, original_units: str | None) -> ValidReport:
    """Judge `step_path`. `original_units` is the unit of the file it was repaired from, if known.

    Raises `CheckerUnavailable` when no verdict can be reached (worker timeout or crash, or a kernel
    failure that is not about the file); that is never reported as a finding against the file.

    The report is memoised per (pool, path, bytes, original unit): the cheap and the full tier, and the
    search's repeated states, ask the same question of the same file. A raise is never memoised.
    """
    key = memo.content_key(step_path)
    if key is not None:
        hit = memo.recall(pool, ("valid", key, original_units))
        if hit is not None:
            return cast(ValidReport, hit)
    report = _check_valid(pool, step_path, key, original_units)
    if key is not None:
        memo.remember(pool, ("valid", key, original_units), report)
    return report


def _check_valid(
    pool: WorkerPool, step_path: str, key: tuple[str, str] | None, original_units: str | None
) -> ValidReport:
    """The measurement. Every decision is taken in the order of the original sequential code, on the
    same call results; only WHEN the round trip's calls run changed (alongside the input's check, on a
    second worker, once the shape is known to be bounded)."""
    work = Path(tempfile.mkdtemp(prefix="valid_"))
    speculative: Future[_RoundTripCalls] | None = None
    executor: ThreadPoolExecutor | None = None
    read_invalid = _ReadInvalid()
    try:
        read = memo.read_step(pool, step_path, key, str(work / "shape.brep"))
        if not read.ok and _is_file_error(read):  # the file cannot be read as STEP: it is invalid
            finding = Finding("brep_invalid", [], {"call": "read_step_xcaf", "error": read.error})
            return ValidReport(False, [finding], [], "unknown", 0, False)
        if not read.ok or read.value is None:
            raise _unavailable("read_step_xcaf", read)
        doc = read.value
        units = doc["units"]
        written_units = _written_units(doc)

        if _bounded(pool, doc):
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="valid_round_trip")
            speculative = executor.submit(_round_trip_calls, pool, doc, work, written_units, read_invalid)

        checked = _check_shape(pool, doc["brep"])
        if not checked.ok or checked.value is None:
            raise _unavailable("check_valid", checked)
        hard = _findings(checked.value["hard"])
        soft = _findings(checked.value["soft"])
        solids = int(checked.value["solids"])
        if any(f.code == UNBOUNDED for f in hard):  # nothing else about an infinite part is measured, or written back
            return ValidReport(False, hard, soft, units, solids, False)

        if original_units is not None and units != canonical_unit(original_units):
            hard.append(Finding("units_changed", [], {"original": original_units, "file": units}))

        if hard:
            read_invalid.set()
        calls = (
            speculative.result()
            if speculative is not None
            else _round_trip_calls(pool, doc, work, written_units, read_invalid)
        )
        round_trip_ok, units_after, why = _judge_round_trip(calls, doc, solids, {f.code for f in hard})
        if units_after is not None and units_after != written_units:
            hard.append(Finding("units_changed", [], {"file": units, "after_round_trip": units_after}))
            round_trip_ok = False
        if not round_trip_ok and not any(finding.code != "units_changed" for finding in hard):
            hard.append(Finding("round_trip_invalid", [], why))

        return ValidReport(not hard, hard, soft, units, solids, round_trip_ok)
    finally:
        if speculative is None or executor is None:
            shutil.rmtree(work, ignore_errors=True)
        else:  # its calls write into `work`, so `work` goes when they are done. Never wait for them here: a
            # raise (a file wall-clock, an unavailable check) must not be held up by up to three more calls.
            speculative.add_done_callback(lambda _done: shutil.rmtree(work, ignore_errors=True))
            executor.shutdown(wait=False)
