# Responsibility: Run one allow-listed CAD kernel analysis in a FRESH INTERPRETER under a wall
#                 clock, and hand back its JSON answer or nothing.
# Owns: the kernel allow-list, the job file, the shape operands' BREP, the wire encoding, the child
#       command, the group kill, and the child's own entry point.
# Boundaries: no policy. What an analysis means, and what its absence means, belongs to the caller
#             (`localize._bounded` is the one public door; every kernel caller goes through it).
"""A bounded kernel analysis, in a fresh interpreter - never a fork.

WHY NOT A FORK. Production repair runs in a THREADED worker (Celery, plus model-call threads). A
forked child inherits every other thread's locks in whatever state they were in - the allocator's
included, which OCC takes at once - with no thread left to release them, so the child can deadlock
before it starts and the evidence is silently lost. The repo rule is "never fork a threaded
process"; `occ_distance` and `mesh_probe` set the pattern this module follows.

WHY A CHILD AT ALL. OCP's bindings HOLD THE GIL through a kernel call, and OCP exposes no user-break
progress indicator, so a daemon thread cannot be abandoned and a call cannot be asked to stop. A
child process can be killed: on expiry its whole session is SIGKILLed and reaped. A kernel crash on
a pathological part kills the child, not the worker.

THE JOB. The parent writes, into a private temp directory:
  - `operands.brep`: every shape argument, in order, as the children of ONE compound. One file, not
    one per shape, so sub-shapes they share keep their identity (`IsSame`) in the child - a face
    passed beside the solid that owns it is still a face of that solid. BREP keeps the face map's
    order, so a 1-based face index the child reports names the same face in the parent.
  - `job.json`: `{"kernel": <key>, "args": [{"shape": i} | {"value": <json>}], ...}`. THE KERNEL IS
    A KEY INTO `KERNELS`, a fixed allow-list in this file. The child never imports a path it read
    from data; an unknown key is refused before anything is imported.
The child writes `result.json`, `{"ok": true, "value": ...}` or `{"ok": false, "error": ...}`. JSON
carries every return type the kernels have: lists, dicts, numbers and None; bytes (the rendered
PNGs) ride as `{"__b64__": ...}` and tuples come back as lists. No pickle crosses the boundary.

THE COST. One child is a Python start plus `import OCP` - OCP is one monolithic extension, about
1.1-1.5 s on a developer laptop - plus writing the operands. That is paid out of the caller's
budget, so a budget shorter than the start-up is an honest None, never a wait past it. The operand
write is the one step the wall cannot stop (no child exists yet, and OCP's progress indicator has
no constructor to break it): it is linear serialisation, not a kernel search, so it is kept to the
B-rep alone and a write that used the budget starts no child.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODULE = "meshpipeline.cad.repair.bounded_child"

#: THE ALLOW-LIST: the only functions a child will run, by a fixed key. A key maps to a module and
#: a function name written here, never to anything read from a job. The `probe.*` kernels are this
#: module's own: inert, used by its tests and to measure a child's start-up.
KERNELS: dict[str, tuple[str, str]] = {
    "localize.kernel_face_pairs": ("meshpipeline.cad.repair.localize", "_kernel_face_pairs"),
    "operations.crossing_pairs": ("meshpipeline.cad.repair.operations", "_crossing_pairs"),
    "operations.common_volume": ("meshpipeline.cad.repair.operations", "_common_volume"),
    "operations.face_distances": ("meshpipeline.cad.repair.operations", "_distances_to"),
    "operations.general_fuse": ("meshpipeline.cad.repair.operations", "_fuse_kernel"),
    "probe.contact_extrema": ("meshpipeline.cad.repair.mesh_probe", "_contact_extrema"),
    "probe.nearest_face_positions": ("meshpipeline.cad.repair.mesh_probe",
                                     "_nearest_face_positions"),
    "investigator.thickness": ("meshpipeline.cad.repair.investigator_tools", "_thickness_kernel"),
    "investigator.render": ("meshpipeline.cad.repair.investigator_tools", "_render_kernel"),
    "mesh_hints.face_table": ("meshpipeline.cad.repair.mesh_hints", "_face_table"),
    "probe.echo": (_MODULE, "_probe_echo"),
    "probe.wrap": (_MODULE, "_probe_wrap"),
    "probe.slow_shape": (_MODULE, "_probe_slow_shape"),
    "probe.sleep": (_MODULE, "_probe_sleep"),
    "probe.raise": (_MODULE, "_probe_raise"),
    "probe.die": (_MODULE, "_probe_die"),
}

#: How long a killed child's reap may take before the parent stops waiting for it. SIGKILL cannot
#: be ignored, so this is only ever reached by a process stuck in the kernel (uninterruptible IO).
_REAP_S = 5.0


class _NotEncodable(TypeError):
    """An argument or answer JSON cannot carry."""


# --- the wire ----------------------------------------------------------------------------------

#: A shape in an ANSWER, as a reference into the answer's own BREP compound. Geometry cannot be
#: JSON, and a kernel that BUILDS geometry - the general fuse's pieces - had no way home at all
#: while the wire was JSON only. Operands already cross as a compound; answers cross the same way.
_SHAPE_KEY = "__shape__"


def _encode(value: Any, shapes: list | None = None) -> Any:
    """`value` as JSON. With `shapes`, a shape is appended there and replaced by a reference.

    WITHOUT `shapes` A SHAPE IS STILL REFUSED, which is deliberate: on the ARGUMENT side the
    operands are collected by `_run_in` and a shape nested somewhere it cannot reach must fail
    loudly rather than be dropped.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"__float__": repr(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if shapes is not None and _is_shape(value):
        shapes.append(value)
        return {_SHAPE_KEY: len(shapes) - 1}
    if isinstance(value, (list, tuple)):
        return [_encode(v, shapes) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise _NotEncodable("a dict key that is not a string")
        return {k: _encode(v, shapes) for k, v in value.items()}
    raise _NotEncodable(f"{type(value).__name__} cannot cross to the child as JSON")


def _decode(value: Any, shapes: list | None = None) -> Any:
    """The child's JSON back as Python. A shape reference resolves against `shapes`; one that
    names a shape the answer did not carry RAISES, because a corrupt or truncated answer must
    read as no answer rather than as some other index's geometry."""
    if isinstance(value, list):
        return [_decode(v, shapes) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__b64__"}:
            return base64.b64decode(value["__b64__"])
        if set(value) == {"__float__"}:
            return float(value["__float__"])
        if set(value) == {_SHAPE_KEY}:
            index = int(value[_SHAPE_KEY])
            if shapes is None or not 0 <= index < len(shapes):
                raise _NotEncodable(
                    f"the answer names shape {index}, which it did not carry")
            return shapes[index]
        return {k: _decode(v, shapes) for k, v in value.items()}
    return value


def _is_shape(value: Any) -> bool:
    """An OCP `TopoDS_Shape` (or subclass), told apart without importing OCP for plain values."""
    if not type(value).__module__.startswith("OCP"):
        return False
    from OCP.TopoDS import TopoDS_Shape

    return isinstance(value, TopoDS_Shape)


def _write_operands(shapes: list, path: Path) -> None:
    """The operands as ONE compound's BREP, WITHOUT TRIANGULATION. This write runs in the parent,
    before any child exists, so the child's wall cannot stop it: it must carry only what a kernel
    reads. A meshed part's triangles are most of its BREP (5x on a corpus hinge), and no kernel
    reads the parent's mesh - the render kernel tessellates in the child for its own view."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_FormatVersion

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    if not BRepTools.Write_s(compound, str(path), False, False,
                             TopTools_FormatVersion.TopTools_FormatVersion_VERSION_1):
        raise OSError(f"could not write the operands to {path}")


def _read_operands(path: Path) -> list:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Iterator, TopoDS_Shape

    compound = TopoDS_Shape()
    if not BRepTools.Read_s(compound, str(path), BRep_Builder()) or compound.IsNull():
        raise OSError(f"could not read the operands from {path}")
    out = []
    children = TopoDS_Iterator(compound)
    while children.More():
        out.append(children.Value())
        children.Next()
    return out


# --- the parent --------------------------------------------------------------------------------

def key_for(fn) -> str | None:
    """The allow-list key of `fn`, or None when it is not a registered kernel."""
    where = (getattr(fn, "__module__", None), getattr(fn, "__name__", None))
    for key, registered in KERNELS.items():
        if registered == where:
            return key
    return None


def _child_cmd() -> list[str]:
    """The fresh interpreter. `-W ignore` keeps a warning out of the child's stderr tail."""
    return [sys.executable, "-W", "ignore", "-m", _MODULE]


def _child_env() -> dict[str, str]:
    """The parent's environment, with THIS checkout's package root first on the child's path when
    the parent runs from a source tree: otherwise the child could import a different
    `meshpipeline` (an editable install of another checkout) than the parent is running."""
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[3])
    if Path(root).name not in ("site-packages", "dist-packages"):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _tail(path: Path) -> str:
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


def run(fn, args: tuple, budget_s: float) -> Any:
    """`fn(*args)` computed in a fresh interpreter, or None. NEVER RAISES, NEVER BLOCKS PAST
    `budget_s` (bar the reap of a child already SIGKILLed); every failure is logged at info."""
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = 0.0
    if not math.isfinite(budget) or budget <= 0:
        return None
    started = time.monotonic()
    key = key_for(fn)
    if key is None:
        logger.info("bounded kernel: %r is not on the allow-list - not run", fn)
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="bounded-kernel-") as tmp:
            return _run_in(Path(tmp), key, args, started + budget, budget)
    except Exception as exc:  # noqa: BLE001 - optional evidence; a failure here is silence
        logger.info("bounded kernel %s gave no answer (%s: %s)", key, type(exc).__name__, exc)
        return None


def _run_in(tmp: Path, key: str, args: tuple, deadline: float, budget: float) -> Any:
    shapes: list = []
    wired: list[dict] = []
    for arg in args:
        if _is_shape(arg):
            wired.append({"shape": len(shapes)})
            shapes.append(arg)
        else:
            wired.append({"value": _encode(arg)})
    if shapes:
        _write_operands(shapes, tmp / "operands.brep")
    job = tmp / "job.json"
    job.write_text(json.dumps({"kernel": key, "args": wired,
                               "operands": "operands.brep" if shapes else None,
                               "result": "result.json"}))
    left = deadline - time.monotonic()
    if left <= 0:
        logger.info("bounded kernel %s: its %.2fs budget went on writing the operands", key, budget)
        return None
    stderr_path = tmp / "stderr.txt"
    with open(stderr_path, "wb") as stderr:
        proc = subprocess.Popen([*_child_cmd(), str(job)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=stderr,
                                start_new_session=True, env=_child_env(), cwd=str(tmp))
    finished = False
    try:
        proc.wait(timeout=left)
        finished = True
    except subprocess.TimeoutExpired:
        logger.info("bounded kernel %s passed its %.2fs budget and was stopped", key, budget)
        return None
    finally:
        # KILLED UNLESS IT FINISHED, not only on expiry: a KeyboardInterrupt can land mid-wait,
        # and the child must not outlive the call that started it.
        if not finished:
            _kill_group(proc)
            try:
                proc.wait(timeout=_REAP_S)
            except subprocess.TimeoutExpired:
                logger.info("bounded kernel %s: the killed child was not reaped in %.0fs",
                            key, _REAP_S)
    result = tmp / "result.json"
    if not result.exists():
        logger.info("bounded kernel %s ended without an answer (the child died, exit %s): %s",
                    key, proc.returncode, _tail(stderr_path))
        return None
    answer = json.loads(result.read_text())
    if not answer.get("ok"):
        logger.info("bounded kernel %s raised (%s)", key, answer.get("error"))
        return None
    carried = _read_operands(tmp / str(answer["shapes"])) if answer.get("shapes") else []
    return _decode(answer.get("value"), carried)


# --- the child ---------------------------------------------------------------------------------

def _resolve(key: str):
    """The registered function for `key`. Raises KeyError for anything off the allow-list."""
    import importlib

    if key not in KERNELS:
        raise KeyError(f"{key!r} is not on the bounded-kernel allow-list")
    module, name = KERNELS[key]
    return getattr(importlib.import_module(module), name)


def _write_result(path: Path, answer: dict) -> None:
    """Written whole, then renamed: a child killed mid-write leaves no half an answer."""
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(answer))
    os.replace(partial, path)


def _child_main(argv: list[str]) -> int:
    job_path = Path(argv[0])
    job = json.loads(job_path.read_text())
    result = job_path.parent / str(job.get("result") or "result.json")
    try:
        fn = _resolve(str(job.get("kernel")))
        shapes = _read_operands(job_path.parent / job["operands"]) if job.get("operands") else []
        args = [shapes[int(a["shape"])] if "shape" in a else _decode(a["value"])
                for a in job.get("args") or []]
        value = fn(*args)
        built: list = []
        encoded = _encode(value, built)
        answer: dict = {"ok": True, "value": encoded}
        if built:
            # THE GEOMETRY FIRST, THE ANSWER SECOND. `_write_result` renames the JSON into place,
            # so a child killed while writing the compound leaves no result at all - never a
            # result whose shapes are missing or half-written.
            _write_operands(built, job_path.parent / "answer.brep")
            answer["shapes"] = "answer.brep"
        _write_result(result, answer)
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent, never raised past here
        _write_result(result, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


# --- the probes: inert kernels for this module's tests and start-up measurement ----------------

def _probe_echo(*args):
    return list(args)


def _probe_sleep(seconds: float):
    time.sleep(float(seconds))
    return "woke"


def _probe_wrap(shape):
    """An answer with shapes NESTED in it, which is the shape of the fuse's own answer."""
    return {"piece": shape, "faces": 6, "both": (shape, shape)}


def _probe_slow_shape(seconds: float):
    """Slow, and only then answers with geometry: the window a kill has to land in."""
    time.sleep(float(seconds))
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape()


def _probe_raise():
    raise RuntimeError("probe kernel raised")


def _probe_die():
    os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":  # pragma: no cover - the child's own entry point
    raise SystemExit(_child_main(sys.argv[1:]))
