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

#: A pair the batch did not need to measure: an earlier candidate is already nearer than this
#: one's own LOWER BOUND, so nothing from here on can win. A PROOF, not ignorance - unlike
#: `budget_spent`, which says the search did not finish and the caller must say so.
PRUNED = "pruned"

#: Statuses that mean THE BATCH ITSELF ANSWERED for this pair - it measured it, it proved nothing
#: from there on could win, or the kernel refused this one piece of geometry. Anything else says
#: the search did not finish, which a caller must report rather than read as "nothing is near".
ANSWERED = ("measured", PRUNED, "unmeasured: the kernel gave no distance")


def answered(result: dict) -> bool:
    """Whether this pair's answer is the batch's word, rather than a search that stopped early."""
    return result.get("distance_status") in ANSWERED


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


def distances(shapes: list, pairs: list[tuple[int, int]], *, budget_s: float,
              bounds: list[float] | None = None, tol: float = 0.0) -> list[dict]:
    """One distance per pair of `shapes` (given as index pairs), measured in ONE bounded child.

    A CHILD PER PAIR WOULD COST MORE THAN IT SAVES: `_nearest_faces` asks about every candidate
    face and `deviation._distance_to` about every sample, so a batch is the unit. Each shape is
    written once however many pairs name it.

    WHAT THE CHILD FINISHED IS KEPT. These callers already act on partial answers - their old
    deadline was checked BETWEEN calls, which at least preserved the ones already measured - so
    the child prints one line per pair and the parent reads what arrived before the kill. A pair
    that never answered is unmeasured, never 0.

    PRUNING TRAVELS WITH THE BATCH. `bounds[n]` is a LOWER BOUND on pair `n`'s distance and the
    pairs are given in increasing order of it; with `bounds`, the child stops at the first pair
    whose bound exceeds the least distance it has measured plus `tol`, and the rest come back
    `PRUNED`. The rule cannot be applied by the parent because it needs the distances measured so
    far, and a batch without it would measure every candidate - more than the in-parent loop it
    replaces. Without `bounds`, every pair is measured.
    """
    wanted = list(pairs)
    if not wanted:
        return []
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = 0.0
    if not budget > 0.0:
        return [{**UNMEASURED, "distance_status": "unmeasured: no budget"} for _ in wanted]
    try:
        with tempfile.TemporaryDirectory(prefix="occ-distance-") as tmp:
            root = Path(tmp)
            needed = sorted({i for pair in wanted for i in pair})
            for i in needed:
                _write_brep(shapes[i], root / f"{i}.brep")
            manifest = root / "pairs.json"
            spec: dict[str, Any] = {"pairs": [[int(a), int(b)] for a, b in wanted]}
            if bounds is not None:
                spec["bounds"] = [float(b) for b in bounds]
                spec["tol"] = float(tol)
            manifest.write_text(json.dumps(spec))
            return _run_batch(root, manifest, len(wanted), budget)
    except Exception as exc:  # noqa: BLE001 - a measurement may never break the tool that asked
        return [{**UNMEASURED, "distance_status": f"unmeasured: {type(exc).__name__}"}
                for _ in wanted]


def _run_batch(root: Path, manifest: Path, count: int, budget: float) -> list[dict]:
    """The child's answers, one line per pair, with whatever arrived before the budget ran out."""
    proc = subprocess.Popen([*_CHILD_CMD, "--batch", str(root), str(manifest)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    lines: list[str] = []
    try:
        out, _ = proc.communicate(timeout=max(budget, 0.0))
        lines = [line for line in (out or "").splitlines() if line.strip()]
    except subprocess.TimeoutExpired:
        # READ WHAT IS THERE, THEN KILL. `communicate` discards the pipe on timeout, so the
        # finished pairs are taken off the stream first - they are measurements already paid for.
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=5)
            lines = [line for line in (out or "").splitlines() if line.strip()]
        except Exception:  # noqa: BLE001
            lines = []
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    stopped = count
    for n, raw in enumerate(lines):
        if '"pruned_after"' in raw:
            stopped = min(stopped, n)
            break
    answers: list[dict] = []
    for i in range(count):
        if i >= stopped:
            answers.append({**UNMEASURED, "distance_status": PRUNED})
            continue
        answers.append(_one_answer(lines[i] if i < len(lines) else ""))
    return answers


def _one_answer(raw: str) -> dict:
    if not raw:
        return {**UNMEASURED, "distance_status": "unmeasured: budget_spent"}
    try:
        answer = json.loads(raw)
    except ValueError:
        return {**UNMEASURED, "distance_status": "unmeasured: the answer was not JSON"}
    if not isinstance(answer, dict) or answer.get("min_distance_mm") is None:
        return {**UNMEASURED, "distance_status": "unmeasured: the kernel gave no distance"}
    return {"min_distance_mm": float(answer["min_distance_mm"]),
            "point_a": answer.get("point_a"), "point_b": answer.get("point_b"),
            "distance_status": "measured"}


def _batch_main(root: Path, manifest: Path) -> int:
    """Measure each pair and print it AS IT IS FINISHED, so a kill keeps what came before. With
    bounds, stop where the parent's own loop would have stopped and say so on the last line."""
    cache: dict[int, Any] = {}

    def shape(i: int):
        if i not in cache:
            cache[i] = _read_brep(root / f"{i}.brep")
        return cache[i]

    spec = json.loads(manifest.read_text())
    if isinstance(spec, list):                       # the plain form: every pair, no pruning
        pairs, bounds, tol = spec, None, 0.0
    else:
        pairs = spec.get("pairs") or []
        bounds = spec.get("bounds")
        tol = float(spec.get("tol") or 0.0)
    least: float | None = None
    for n, (a, b) in enumerate(pairs):
        if bounds is not None and least is not None and float(bounds[n]) > least + tol:
            # EXACTLY THE PARENT'S OLD BREAK: this candidate's lower bound is already further
            # than a measured distance, and the pairs are ordered by that bound.
            sys.stdout.write(json.dumps({"pruned_after": n}) + "\n")
            sys.stdout.flush()
            return 0
        try:
            answer = _measure_here(shape(int(a)), shape(int(b)))
        except Exception:  # noqa: BLE001 - one unmeasurable pair is not the batch's failure
            answer = dict(UNMEASURED)
        if answer.get("min_distance_mm") is not None:
            found = float(answer["min_distance_mm"])
            least = found if least is None else min(least, found)
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()
    return 0


def _child_main(argv: list[str]) -> int:
    if argv and argv[0] == "--batch":
        return _batch_main(Path(argv[1]), Path(argv[2]))
    first, second = Path(argv[0]), Path(argv[1])
    sys.stdout.write(json.dumps(_measure_here(_read_brep(first), _read_brep(second))) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - the child's own entry point
    raise SystemExit(_child_main(sys.argv[1:]))
