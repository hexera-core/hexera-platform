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
from dataclasses import dataclass, field
from pathlib import Path

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
)
SOFT_CODES = ("small_edge", "small_face", "large_tolerance")

CALL_TIMEOUT_S = 300.0


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
    round_trip_ok: bool


def _findings(raw: list[dict]) -> list[Finding]:
    return [Finding(item["code"], list(item["entities"]), dict(item["detail"])) for item in raw]


def _failed(call: str, result: CallResult) -> Finding:
    return Finding("brep_invalid", [], {"call": call, "status": result.status, "error": result.error})


def _check_shape(pool: WorkerPool, brep: str) -> CallResult:
    return pool.call("check_valid", {"brep": brep}, timeout_s=CALL_TIMEOUT_S)


def _round_trip(pool: WorkerPool, doc: dict, work: Path, solids: int, known: set[str]) -> tuple[bool, str | None, dict]:
    """Write `doc` as STEP, read it back, re-check it. Returns (ok, units after, why-not).

    `known` are the hard codes the original already has: a defect that survives the round trip is
    not the round trip's fault, only a NEW hard code is.
    """
    written = pool.call(
        "write_step_xcaf",
        {
            "brep": doc["brep"],
            "labels": doc["labels"],
            "assembly_ids": doc["assembly_ids"],
            "units": doc["units"],
            "path": str(work / "round_trip.step"),
        },
        timeout_s=CALL_TIMEOUT_S,
    )
    if not written.ok:
        return False, None, {"stage": "write", "error": written.error}
    reread = pool.call(
        "read_step_xcaf",
        {"path": str(work / "round_trip.step"), "brep_out": str(work / "round_trip.brep")},
        timeout_s=CALL_TIMEOUT_S,
    )
    if not reread.ok or reread.value is None:
        return False, None, {"stage": "read", "error": reread.error}
    again = reread.value
    problems: dict = {}
    names_before = {key: entry["name"] for key, entry in doc["labels"].items()}
    names_after = {key: entry["name"] for key, entry in again["labels"].items()}
    if names_before != names_after:
        lost = sorted(key for key in names_before if names_before[key] != names_after.get(key))
        problems["labels_changed"] = lost
    checked = _check_shape(pool, again["brep"])
    if not checked.ok or checked.value is None:
        problems["recheck"] = checked.error
    else:
        if checked.value["solids"] != solids:
            problems["solids"] = {"before": solids, "after": checked.value["solids"]}
        broken = sorted({item["code"] for item in checked.value["hard"]} - known)
        if broken:
            problems["hard"] = broken
    return not problems, again["units"], problems


def check_valid(pool: WorkerPool, step_path: str, *, original_units: str | None) -> ValidReport:
    """Judge `step_path`. `original_units` is the unit of the file it was repaired from, if known."""
    work = Path(tempfile.mkdtemp(prefix="valid_"))
    try:
        read = pool.call(
            "read_step_xcaf",
            {"path": str(step_path), "brep_out": str(work / "shape.brep")},
            timeout_s=CALL_TIMEOUT_S,
        )
        if not read.ok or read.value is None:
            return ValidReport(False, [_failed("read_step_xcaf", read)], [], "unknown", 0, False)
        doc = read.value
        units = doc["units"]

        checked = _check_shape(pool, doc["brep"])
        if not checked.ok or checked.value is None:
            return ValidReport(False, [_failed("check_valid", checked)], [], units, 0, False)
        hard = _findings(checked.value["hard"])
        soft = _findings(checked.value["soft"])
        solids = int(checked.value["solids"])

        if original_units is not None and units != original_units.lower():
            hard.append(Finding("units_changed", [], {"original": original_units, "file": units}))

        round_trip_ok, units_after, why = _round_trip(pool, doc, work, solids, {f.code for f in hard})
        if units_after is not None and units_after != units:
            hard.append(Finding("units_changed", [], {"file": units, "after_round_trip": units_after}))
            round_trip_ok = False
        if not round_trip_ok and not any(finding.code != "units_changed" for finding in hard):
            hard.append(Finding("round_trip_invalid", [], why))

        return ValidReport(not hard, hard, soft, units, solids, round_trip_ok)
    finally:
        shutil.rmtree(work, ignore_errors=True)
