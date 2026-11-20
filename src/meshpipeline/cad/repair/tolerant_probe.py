# Responsibility: TIER 2B - answer, coarsely and in seconds, "would the TOLERANT mesher eat this
#                 part?" for a part gmsh refused. A routing answer, never a mesh deliverable.
# Owns: the five verdicts, the bounded child that runs one coarse fTetWild pass, and the mapping
#       from how that child ended to which verdict it is.
# Boundaries: it NEVER RAISES and never blocks past `budget_s + REAP_S`. fTetWild and the kernel
#             live in a FRESHLY EXEC'D child (never forked from a threaded parent - a forked
#             child's first mesher import can crash or hang: mesh_probe's ABI rule), and the
#             shape travels as a file on disk, never as an OCP pointer. The parent imports
#             `engines/fea_tetwild` lazily and only for `binary()`, so no numpy-heavy path and no
#             mesher machinery is pulled into a worker process. It chooses no machine, meshes
#             nothing for delivery, and sizes nothing: tier 2b cannot escalate.
# Collaborates with: cad/repair/oracle.py (TIER_2B, TIER2B_MAX_ITS, the `routing` budget rule),
#                    cad/repair/goal.py (the one caller: it records the verdict under `routing`),
#                    cad/repair/mesh_probe.py (`_kill_group` - one reaper, so a timed-out tier-2b
#                    child cannot leave fTetWild burning a worker's CPU).
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from meshpipeline.cad.repair import oracle
from meshpipeline.cad.repair.mesh_probe import _kill_group

logger = logging.getLogger(__name__)

#: fTetWild meshed the part: the tolerant rung WOULD eat it, overlap and all.
EATS = "tolerant_rung_eats_it"
#: fTetWild refused it for a reason of its own - a real answer, and a negative one.
REFUSES = "tolerant_rung_refuses"
#: Nobody answered: the clock ran out, or the child died unspeaking. NOT a verdict about the
#: customer's geometry, and never to be counted as one.
NO_ANSWER = "no_answer"
#: There is no fTetWild here, so the question could not be asked at all (a host without the mesh
#: image). Distinct from `NO_ANSWER`: nothing was even attempted.
UNAVAILABLE = "unavailable"
#: The question did not apply to this part - it meshed, or it failed in a way the tolerant rung
#: has no bearing on. The pass is not started.
NOT_ASKED = "not_asked"

#: What a no-budget pass says, so a reader can tell "we had no time" from "it would not mesh".
NO_TIME_LEFT = "no time left"

#: Seconds the parent waits BEYOND the budget before killing the child's whole process group.
#: The child enforces the budget on fTetWild itself; this is only the room to write its answer.
REAP_S = 5.0

#: The ideal edge the coarse pass asks for, as a share of the part's diagonal. Twenty elements
#: across the part: enough to find out whether the envelope closes, far too coarse to deliver.
EDGE_DIAG_SHARE = 0.05

#: How much the envelope is widened over the production rung's. TOLERANCE IS THE POINT: the
#: question is whether a mesher that is allowed to smooth over an overlap gets through, so it is
#: asked with more room than a delivered mesh would be given, not less.
ENVELOPE_WIDENING = 2.0

#: What the child calls its run, in the one temporary directory it owns.
JOB_NAME = "tier2b"

#: THE TWO QUESTIONS THE CHILD CAN BE ASKED. `tier2b` is the routing question above: coarse,
#: widened, a handful of rounds. `delivery` asks whether the part MESHES at the production rung's
#: own envelope and iteration count (`fea_tetwild.envelope_m`, `fea_tetwild.MAX_ITS`), unwidened:
#: an evaluation reading of the tolerant route, never a deliverable either. It is PLAN-FREE - the
#: production rung also clamps the envelope to the thinnest real wall and sizes the edge from the
#: FEA plan, neither of which exists outside the FEA pipeline - so the ideal edge is fTetWild's own
#: default share of the diagonal, and that is recorded with every answer (`DELIVERY_SETTINGS`).
MODE_TIER2B = "tier2b"
MODE_DELIVERY = "delivery"
DELIVERY_SETTINGS = ("plan-free production rung: envelope fea_tetwild.envelope_m(diagonal, no "
                     "wall clamp), max_its fea_tetwild.MAX_ITS, ideal edge 0.05 x diagonal "
                     "(fTetWild's default), 1 thread")


@dataclass(frozen=True, slots=True)
class TolerantResult:
    """One tier-2b answer. `tets` and `seconds` are 0 for every verdict but `EATS`/`REFUSES`.

    `stopped` means fTetWild was asked to finish the mesh it had rather than running to its own
    stopping point - an `EATS` that was stopped still ate it, which is the answer being recorded.
    """

    verdict: str
    tets: int
    seconds: float
    stopped: bool
    message: str


def _binary() -> str | None:
    """Where fTetWild is, or None. THE ONLY THING THE PARENT ASKS `fea_tetwild` FOR.

    Imported here rather than at module level so a parent that never probes never pays for numpy,
    and so nothing of the production rung is resident in a worker.
    """
    from meshpipeline.engines import fea_tetwild

    return fea_tetwild.binary()


# #
# the child: one coarse fTetWild pass, in its own freshly exec'd interpreter
# #

def _read_faces(path: str) -> tuple[Any, Any, int]:
    """The shape, its face map and the face count. RUNS IN THE CHILD ONLY (it imports the kernel).

    A `.brep` is read directly; anything else goes through the repair reader, so a corpus STEP
    file can be routed without being converted first.
    """
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    source = Path(path)
    if source.suffix.lower() == ".brep":
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape

        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(source), BRep_Builder()) or shape.IsNull():
            raise ValueError(f"the tolerant probe could not read {source.name}")
    else:
        from meshpipeline.cad.repair.brep import _read_shape

        shape = _read_shape(source)

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    return shape, fmap, int(fmap.Extent())


def child_answer(path: str, diagonal_mm: float, budget_s: float,
                 mode: str = MODE_TIER2B) -> dict:
    """One coarse tolerant pass, as the JSON line the child prints. RUNS IN THE CHILD.

    THE JOB IS THE POLICY. One thread (worker CPU is the worker's), `oracle.TIER2B_MAX_ITS`
    optimisation rounds instead of fTetWild's eighty, a diagonal-sized ideal edge and a widened
    envelope, and no sizing field - every one of those says "answer the question", not "build the
    best mesh". The soft deadline asks fTetWild to finish what it has; the hard one is the budget.
    """
    from meshpipeline.engines import fea_tetwild

    started = time.monotonic()
    diagonal = float(diagonal_mm)
    delivery = mode == MODE_DELIVERY
    epsilon = (fea_tetwild.envelope_m(diagonal, None) if delivery
               else diagonal * fea_tetwild.EPS_DIAG_SHARE * ENVELOPE_WIDENING)
    _shape, fmap, faces = _read_faces(path)
    xyz, tris, _labels = fea_tetwild.tessellate(
        fmap, list(range(faces)), epsilon * fea_tetwild.TESSELLATION_SHARE)

    with tempfile.TemporaryDirectory(prefix="repair-tier2b-") as tmp:
        result = fea_tetwild.run_part({
            "dir": tmp, "xyz": xyz, "tris": tris,
            "edge": diagonal * EDGE_DIAG_SHARE, "epsilon": epsilon, "bg": None,
            "name": JOB_NAME, "threads": 1,
            "max_its": fea_tetwild.MAX_ITS if delivery else oracle.TIER2B_MAX_ITS,
            "soft_at": started + fea_tetwild.SOFT_SHARE * float(budget_s),
            "hard_at": started + float(budget_s)})

    tets = result.get("tets")
    return {"tets": 0 if tets is None else int(len(tets)),
            "seconds": float(result.get("seconds") or (time.monotonic() - started)),
            "stopped": bool(result.get("stopped")),
            "error": str(result.get("error") or ""),
            "out_of_time": bool(result.get("out_of_time"))}


def _child_main(argv: list[str]) -> int:
    """`python -m meshpipeline.cad.repair.tolerant_probe PATH DIAGONAL BUDGET [MODE]`: one JSON
    line."""
    path, diagonal, budget, *rest = argv
    try:
        answer = child_answer(path, float(diagonal), float(budget),
                              rest[0] if rest else MODE_TIER2B)
    except Exception as exc:  # noqa: BLE001 - said as a refusal the parent can report
        answer = {"tets": 0, "seconds": 0.0, "stopped": False,
                  "error": f"{type(exc).__name__}: {exc}", "out_of_time": False}
    sys.stdout.write("\n" + json.dumps(answer) + "\n")
    sys.stdout.flush()
    return 0


# #
# the parent: start it, bound it, read it
# #

def _run_child(path: str, diagonal_mm: float, budget_s: float,
               mode: str = MODE_TIER2B) -> dict[str, Any] | None | Literal["error"]:
    """The child's JSON answer; None when the PARENT's clock won; "error" when it died unspeaking.

    `start_new_session=True` and the group kill go together: fTetWild is this child's own child,
    so killing the interpreter alone would leave the mesher running on a worker.
    """
    cmd = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.repair.tolerant_probe",
           path, repr(float(diagonal_mm)), repr(float(budget_s)), mode]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    try:
        out, _ = proc.communicate(timeout=max(float(budget_s), 0.0) + REAP_S)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=REAP_S)
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return "error"
    try:
        answer = json.loads(lines[-1])
    except ValueError:
        return "error"
    return answer if isinstance(answer, dict) else "error"


def tolerant_probe(path: Path, *, diagonal_mm: float, budget_s: float,
                   mode: str = MODE_TIER2B) -> TolerantResult:
    """Would the tolerant rung eat the part in `path`? NEVER RAISES, never blocks past
    `budget_s + REAP_S`.

    `UNAVAILABLE` and a zero budget start NOTHING: a host without fTetWild, and a run with no time
    left, are both statements about us rather than about the customer's geometry, and spawning a
    child to discover either would be a cost for no answer.
    """
    if _binary() is None:
        return TolerantResult(UNAVAILABLE, 0, 0.0, False,
                              "fTetWild is not installed in this image")
    if not float(budget_s) > 0.0:
        return TolerantResult(NO_ANSWER, 0, 0.0, False, NO_TIME_LEFT)
    try:
        answer = _run_child(str(path), float(diagonal_mm), float(budget_s), mode)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("tolerant_probe: %s: %s", type(exc).__name__, exc)
        answer = "error"
    return _verdict_for(answer, budget_s=float(budget_s))


def _verdict_for(answer: dict[str, Any] | None | Literal["error"], *,
                 budget_s: float) -> TolerantResult:
    """Which verdict is this ending? Pure, so the mapping is testable without a process.

    THE DISTINCTION THAT MATTERS: a clock running out is `NO_ANSWER`, never `REFUSES`. Recording
    "the tolerant rung would not take this" on the evidence of our own 30-second cap is how a
    corpus pass invents a refusal and a later tournament learns a lie.
    """
    if answer is None:
        return TolerantResult(NO_ANSWER, 0, 0.0, False,
                              f"the tolerant probe ran past its {budget_s:g} s budget")
    if answer == "error":
        return TolerantResult(NO_ANSWER, 0, 0.0, False,
                              "the tolerant probe's child died without answering")
    tets = int(answer.get("tets") or 0)
    seconds = float(answer.get("seconds") or 0.0)
    stopped = bool(answer.get("stopped"))
    message = str(answer.get("error") or "")
    if answer.get("out_of_time"):
        return TolerantResult(NO_ANSWER, 0, seconds, stopped, message)
    if tets > 0:
        return TolerantResult(EATS, tets, seconds, stopped, message)
    if message:
        return TolerantResult(REFUSES, 0, seconds, stopped, message)
    return TolerantResult(REFUSES, 0, seconds, stopped,
                          "fTetWild produced no tetrahedra and said nothing")


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
