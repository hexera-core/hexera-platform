# Responsibility: Measure the distance between two sub-shapes WITHOUT RISKING THE PARENT PROCESS -
#                 the kernel call runs in a bounded fresh interpreter and its answer is read back.
# Owns: the child command, the budget, the group kill and the refusal shape.
# Boundaries: no policy. It reports a distance or says it was not measured; what that means for a
#             tool result or a region's clock belongs to the caller.
"""A bounded OCC distance.

WHY THIS EXISTS. `BRepExtrema_DistShapeShape` ran IN THE PARENT and held the GIL, so a wave-5
shard sat inside it with neither the tool clock nor the region wall able to interrupt it (lane B's
py-spy: MainThread in `investigator_tools._distance_between`, reached from `_measure` through the
investigator and the tournament). A measurement that cannot be interrupted is not bounded by any
budget we write around it, and on a worker it hangs the job rather than one tool call.

A FRESH INTERPRETER, NEVER A FORK. The repo rule, and the reason is the bug: a forked child
inherits the parent's OCC state and whatever locks it holds, so a fork can hang the same way and
takes the parent's memory with it. The child is `python -m meshpipeline.cad.repair.occ_distance`
in its own session, killed as a group when the budget runs out.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

#: What could not be measured reads as unmeasured, NEVER as a distance of 0: a zero is "they
#: touch", which is the opposite conclusion and one the investigator acts on.
UNMEASURED: dict[str, Any] = {"min_distance_mm": None, "point_a": None, "point_b": None}

#: The child, as a list so a test can replace it. `-W ignore` keeps a warning off the one JSON line.
_CHILD_CMD = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.repair.occ_distance"]


def _write_brep(shape, path: Path) -> None:
    from OCP.BRepTools import BRepTools

    if not BRepTools.Write_s(shape, str(path)):
        raise OSError(f"could not write {path}")


def _read_brep(path: Path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape, builder = TopoDS_Shape(), BRep_Builder()
    if not BRepTools.Read_s(shape, str(path), builder) or shape.IsNull():
        raise OSError(f"could not read {path}")
    return shape


def _point(value) -> list[float]:
    return [round(float(value.X()), 6), round(float(value.Y()), 6), round(float(value.Z()), 6)]


def _measure_here(a, b) -> dict:
    """The kernel call itself. ONLY EVER RUN IN THE CHILD."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    measured = BRepExtrema_DistShapeShape(a, b)
    if not measured.IsDone() or measured.NbSolution() < 1:
        return dict(UNMEASURED)
    return {"min_distance_mm": float(measured.Value()),
            "point_a": _point(measured.PointOnShape1(1)),
            "point_b": _point(measured.PointOnShape2(1))}


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def distance_between(a, b, *, budget_s: float) -> dict:
    """`{min_distance_mm, point_a, point_b, distance_status}`. NEVER raises, never blocks past
    `budget_s`, and never reports 0 for a measurement it did not take."""
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = 0.0
    if not budget > 0.0:
        return {**UNMEASURED, "distance_status": "unmeasured: no budget"}
    try:
        with tempfile.TemporaryDirectory(prefix="occ-distance-") as tmp:
            first, second = Path(tmp) / "a.brep", Path(tmp) / "b.brep"
            _write_brep(a, first)
            _write_brep(b, second)
            return _run_child(first, second, budget)
    except Exception as exc:  # noqa: BLE001 - a measurement may never break the tool that asked
        return {**UNMEASURED, "distance_status": f"unmeasured: {type(exc).__name__}"}


def _run_child(first: Path, second: Path, budget: float) -> dict:
    proc = subprocess.Popen([*_CHILD_CMD, str(first), str(second)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    try:
        out, _ = proc.communicate(timeout=max(budget, 0.0))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return {**UNMEASURED, "distance_status": "unmeasured: budget_spent"}
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        return {**UNMEASURED, "distance_status": "unmeasured: the measurement did not answer"}
    try:
        answer = json.loads(lines[-1])
    except ValueError:
        return {**UNMEASURED, "distance_status": "unmeasured: the answer was not JSON"}
    if not isinstance(answer, dict) or answer.get("min_distance_mm") is None:
        return {**UNMEASURED, "distance_status": "unmeasured: the kernel gave no distance"}
    return {"min_distance_mm": float(answer["min_distance_mm"]),
            "point_a": answer.get("point_a"), "point_b": answer.get("point_b"),
            "distance_status": "measured"}


def _child_main(argv: list[str]) -> int:
    first, second = Path(argv[0]), Path(argv[1])
    sys.stdout.write(json.dumps(_measure_here(_read_brep(first), _read_brep(second))) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - the child's own entry point
    raise SystemExit(_child_main(sys.argv[1:]))
