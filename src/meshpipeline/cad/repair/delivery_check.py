# Responsibility: Answer "does the part read back BRepCheck-valid in the form the customer receives
#                 it" - written as STEP in the upload's own unit, read back by the pipeline's reader.
# Owns: the delivery unit (a context variable the repair entry point sets), the bounded read-back
#       child, and the refusal wording.
# Boundaries: it judges one written file; it repairs nothing and decides nothing about a package.
#             The read and the BRepCheck run in a fresh interpreter (never the parent, never a
#             fork), killed past their budget; an unmeasured answer is never read as valid.
# Collaborates with: cad/repair/conservative.py (sets the unit, owns `_write_step`),
#                    cad/repair/session.py (refuses a commit whose delivered file is invalid),
#                    cad/repair/gap_split.py (checks its own split before it is ever kept).
"""Delivered-file validity.

WHY IN-MEMORY VALIDITY IS NOT ENOUGH (0060_00600919, a metre upload): a repaired solid was
BRepCheck-valid in memory and as a millimetre STEP, and read back `BRepCheck_UnorientableShape`
from the metre STEP the customer gets. The session's valid->invalid cap judged the in-memory
shape, so a meshing but invalid part would have been delivered. Written exactly as delivery
writes it - `conservative._write_step`, in the upload's unit - and read back, it is caught.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: THE UNIT THE REPAIRED PART WILL BE DELIVERED IN - the upload's own, as `conservative.
#: repair_step_file` hands `parser_applied_unit(source)` to `_write_step`. Millimetres when unset.
DELIVERY_UNIT: ContextVar[Any] = ContextVar("delivery_unit", default="mm")

#: The wall-clock budget of one read-back, in seconds. Past it the child is killed as a group and
#: the answer is `kernel_timeout` - a refusal, never a pass.
BUDGET_S = 120.0

#: The child, as a list so a test can replace it.
_CHILD_CMD = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.repair.delivery_check"]


@contextmanager
def delivering_in(unit) -> Iterator[None]:
    """Run the block with `unit` as the unit the repaired part will be written in."""
    token = DELIVERY_UNIT.set(unit if unit is not None else "mm")
    try:
        yield
    finally:
        DELIVERY_UNIT.reset(token)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _read_back(path: Path, budget_s: float) -> tuple[bool | None, str]:
    """`(valid, "")`, or `(None, reason)` when the child timed out or did not answer."""
    proc = subprocess.Popen([*_CHILD_CMD, str(path)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True, text=True)
    try:
        out, _ = proc.communicate(timeout=max(float(budget_s), 0.0))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the group is already gone
            pass
        return None, f"kernel_timeout (the read-back did not finish in {float(budget_s):g} s)"
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        return None, f"export_unchecked (the read-back exited {proc.returncode} with no answer)"
    try:
        answer = json.loads(lines[-1])
    except ValueError:
        return None, "export_unchecked (the read-back's answer was not JSON)"
    return bool(answer.get("valid")) if isinstance(answer, dict) else None, ""


def export_check(before, after, *, budget_s: float = BUDGET_S) -> str:
    """"" when `after`, written as the delivery writes it and read back, is BRepCheck-valid - or
    when `before` was not valid to begin with (a part that arrived invalid is not made worse by
    this measure). Otherwise the refusal reason. Never raises.

    `budget_s` IS WHAT THE RUN HAS LEFT, not a fresh allowance: the session hands in
    `oracle.stage_budget_s(BUDGET_S, remaining)` (gap 3), so a read-back late in the run cannot
    outlive the attempt node's clock. With NO TIME LEFT (`budget_s <= 0`) nothing is written and no
    child starts, and the answer is `export_unchecked (no time left ...)` - COULD NOT VERIFY, which
    by this function's contract is a refusal: the caller reverts the package rather than commit a
    delivery nobody read back. That is the safe choice on purpose, and it is taken before even the
    upload's own validity is asked, because that too is kernel time the run no longer has."""
    from meshpipeline.cad.repair.conservative import _write_step
    from meshpipeline.cad.repair.operations import _is_valid

    if not float(budget_s) > 0.0:
        return "export_unchecked (no time left in the run to read the delivered file back)"
    try:
        if not _is_valid(before):
            return ""
        with tempfile.TemporaryDirectory(prefix="delivery-check-") as tmp:
            # WRITTEN HERE, FROM THE IN-MEMORY SHAPE, EXACTLY AS DELIVERY WRITES IT: a BREP
            # hand-off first normalised away the very defect this check exists for.
            source = Path(tmp) / "part.step"
            _write_step(after, source, unit=DELIVERY_UNIT.get())
            valid, failure = _read_back(source, budget_s)
    except Exception as exc:  # noqa: BLE001 - unmeasured is never read as valid
        return f"export_unchecked ({type(exc).__name__})"
    if valid is None:
        return failure
    if not valid:
        return ("export_invalidates (the repaired part reads back from STEP BRepCheck-invalid; "
                "the upload does not)")
    return ""


def _child_main(argv: list[str]) -> int:
    """`python -m meshpipeline.cad.repair.delivery_check <part.step>` -> `{"valid": bool}`."""
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.operations import _is_valid

    valid = bool(_is_valid(_read_shape(Path(argv[0]))))
    sys.stdout.write(json.dumps({"valid": valid}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - the child's own entry point
    raise SystemExit(_child_main(sys.argv[1:]))
