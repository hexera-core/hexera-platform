# Responsibility: Run the snappy stages that may execute in parallel, and prove the case is complete afterwards.
# Boundaries: completeness is checked against this attempt's own output, never a previous attempt's directory.
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# The reconstructed volume mesh the deliverable is built from. A complete OpenFOAM polyMesh has
# all of these; a partial/aborted reconstruction is missing one or more.
REQUIRED_POLYMESH = ("points", "faces", "owner", "neighbour", "boundary")


@dataclass(frozen=True)
class StageResult:
    name: str
    command: str
    log: str
    rc: int
    timed_out: bool
    dt_s: float
    authoritative: bool = True
    expected_outputs: tuple[str, ...] = ()
    outputs_present: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {"name": self.name, "command": self.command, "log": self.log, "rc": self.rc,
                "timed_out": self.timed_out, "dt_s": self.dt_s, "ok": self.ok,
                "authoritative": self.authoritative,
                "expected_outputs": list(self.expected_outputs),
                "outputs_present": dict(self.outputs_present)}


@dataclass(frozen=True)
class BenignCondition:
    stage: str
    code: int
    reason: str
    foam_versions: tuple[str, ...]
    pattern: re.Pattern

    def matches(self, stage: StageResult, log_text: str, foam_version: str) -> bool:
        return (stage.name == self.stage and stage.rc == self.code
                and foam_version in self.foam_versions
                and bool(self.pattern.search(log_text or "")))


# EMPTY until a specific benign condition is reproduced in-image with a complete, checkMesh-clean
# mesh. Strict fail-closed is the default; adding an entry is a deliberate, evidence-backed act.
_BENIGN_PARALLEL_CONDITIONS: tuple[BenignCondition, ...] = ()


def polymesh_complete(ws: Path, *, since: float | None = None) -> tuple[bool, list[str]]:
    poly = Path(ws) / "constant" / "polyMesh"
    problems: list[str] = []
    for name in REQUIRED_POLYMESH:
        f = poly / name
        # `is_file`, not `exists`: a directory named `points` satisfies `exists()` and reports a
        # nonzero `st_size`, so it would pass both checks below and ship as a mesh component.
        if not f.is_file():
            problems.append(f"missing {name}")
        elif f.stat().st_size == 0:
            problems.append(f"empty {name}")
        elif since is not None and f.stat().st_mtime + 1e-6 < since:
            problems.append(f"stale {name} (older than this attempt)")
    return (not problems), problems


def processor_residue(ws: Path) -> list[str]:
    return sorted(p.name for p in Path(ws).glob("processor*") if p.is_dir())


def classify(
    ws: Path,
    stages: list[StageResult],
    *,
    foam_version: str = "",
    attempt_started: float | None = None,
    checkmesh_ok: Callable[[], bool] | None = None,
    logs: dict[str, str] | None = None,
) -> dict:
    logs = logs or {}
    residue = processor_residue(ws)
    poly_ok, poly_problems = polymesh_complete(ws, since=attempt_started)
    stage_dicts = [s.to_dict() for s in stages]

    def _result(rc: int, timed_out: bool, failing: str | None, benign: dict | None,
                note: str) -> dict:
        return {"rc": rc, "timed_out": timed_out, "failing_stage": failing, "benign": benign,
                "stages": stage_dicts, "residue": residue,
                "polymesh": {"complete": poly_ok, "problems": poly_problems}, "note": note}

    # 1) a timeout is always a failure (and usually leaves processor residue) - fail closed.
    for s in stages:
        if s.timed_out:
            return _result(-1, True, s.name, None,
                           f"{s.name} timed out; parallel run incomplete")

    # 2) walk stages in order; the FIRST authoritative nonzero is the candidate failure.
    for s in stages:
        if s.ok:
            continue
        if not s.authoritative:
            # a non-authoritative (e.g. cleanup) stage failed: represent it, but it does not by
            # itself invalidate a complete mesh - carry on evaluating authoritative stages.
            continue
        # a benign reclassification requires: a matched named condition, a COMPLETE current-attempt
        # polyMesh, NO processor residue, and (if supplied) a clean checkMesh. Fail closed otherwise.
        cond = next((c for c in _BENIGN_PARALLEL_CONDITIONS
                     if c.matches(s, logs.get(s.name, ""), foam_version)), None)
        if cond and poly_ok and not residue and (checkmesh_ok is None or checkmesh_ok()):
            benign = {"stage": s.name, "rc": s.rc, "reason": cond.reason,
                      "foam_version": foam_version}
            return _result(0, False, None, benign,
                           f"{s.name} exited {s.rc} (benign: {cond.reason}); mesh complete "
                           "and checkMesh-clean")
        # not benign - the run failed at this stage.
        why = "unrecognised nonzero"
        if cond and not poly_ok:
            why = f"nonzero and polyMesh incomplete ({', '.join(poly_problems)})"
        elif cond and residue:
            why = f"nonzero with processor residue {residue}"
        elif cond:
            why = "matched benign signature but checkMesh did not pass"
        return _result(s.rc, False, s.name, None, f"{s.name} exited {s.rc}: {why} - failing closed")

    # 3) every authoritative stage exited zero. Still require a complete current-attempt mesh, no
    #    residue, and (when the caller supplies it) a clean checkMesh - a 'success' with a missing
    #    output, leftover processor dirs, or an invalid mesh is not a success.
    if not poly_ok:
        return _result(-3, False, "reconstruction", None,
                       f"all stages exited zero but polyMesh is incomplete: {', '.join(poly_problems)}")
    if residue:
        return _result(-3, False, "cleanup", None,
                       f"all stages exited zero but processor residue remains: {residue}")
    if checkmesh_ok is not None and not checkmesh_ok():
        return _result(-3, False, "checkMesh", None,
                       "all stages exited zero and polyMesh is complete, but checkMesh did not "
                       "pass the acceptance policy - the mesh is invalid")
    return _result(0, False, None, None, "all stages exited zero; mesh complete")
