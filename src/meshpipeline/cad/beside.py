# Responsibility: Run one piece of the geometry check in a child process BESIDE the rest of it, so the
# independent readings of one upload (the clean check, the fluid inside the parts) use the machine's other
# cores instead of waiting their turn on one - and hand back exactly the answer the same call would have
# given in this process.
# Boundaries: a process seam and nothing else: it starts `python -m <module> <spec> <answer>`, waits for the
# JSON answer, and says None when the child stopped, crashed or ran out of time, so the caller does the
# work itself as before. It decides nothing about geometry.
# Collaborates with: cad/validity.py (the clean check's child entry), application/geometry_check.py (starts
# them as soon as the file is read, collects them where the scout used to compute them).
"""WORK BESIDE THE SCOUT.

Shared dev, 1 Nov: the scout spent 69 s of a landing gear's 82 s (186 s on a worker) on three readings
that do not depend on each other - the clean check (30 s), the fluid inside the parts (28 s) and the
holes of the skin (11 s) - one after the other, on one core of a four-core worker. Each reads only the
file. Started together, the step takes as long as the longest of them.

REPEATABLE BY CONSTRUCTION: the child runs the very function the scout called, on the same file, and its
answer is JSON - which is what the scout stored anyway. A child that fails for any reason is no answer,
and the caller computes it in-process, so the worst case is the old time, never a different result.
"""
from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404 - our own modules, run with our own interpreter
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class Beside:
    """One child run. `result()` waits for it (up to `timeout_s` from its start) and returns its JSON
    answer, or None. `close()` stops a child nobody will wait for. Never raises."""

    def __init__(self, module: str, task: dict, work: Path, *, name: str, timeout_s: float | None = None):
        self.name = name
        self.timeout_s = timeout_s
        self.started = time.monotonic()
        work = Path(work)
        work.mkdir(parents=True, exist_ok=True)
        self.spec, self.out = work / f"{name}_task.json", work / f"{name}_answer.json"
        self.out.unlink(missing_ok=True)
        self.proc: subprocess.Popen | None = None
        self._answer: dict | None = None
        self._done = False
        try:
            self.spec.write_text(json.dumps(task))
            self.proc = subprocess.Popen(  # noqa: S603 - our own module, our own interpreter
                [sys.executable, "-m", module, str(self.spec), str(self.out)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001 - no child: the caller does the work itself
            logger.info("beside: %s could not start (%s) - done in-process", name, type(exc).__name__)
            self.proc = None

    def result(self) -> dict | None:
        if self._done:
            return self._answer
        self._done = True
        if self.proc is None:
            return None
        try:
            left = None if self.timeout_s is None else max(0.0, self.timeout_s - (time.monotonic() - self.started))
            self.proc.wait(timeout=left)
        except subprocess.TimeoutExpired:
            self.close()
            self._answer = {"error": f"took over {self.timeout_s:.0f} s", "timed_out": True}
            return self._answer
        except BaseException:
            # the task itself is being stopped (its soft time limit): the child goes with it
            self.close()
            raise
        try:
            answer = json.loads(self.out.read_text()) if self.out.exists() else None
        except ValueError:
            answer = None
        self._answer = answer if isinstance(answer, dict) else None
        if self._answer is None:
            logger.info("beside: %s stopped without an answer (exit %s) - done in-process", self.name,
                        self.proc.returncode)
        return self._answer

    def close(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001 - best effort
                pass


def child_main(argv: list[str], run) -> int:
    """The child's side: read the spec, answer with `run(task)` (a JSON-able dict)."""
    task = json.loads(Path(argv[0]).read_text())
    answer = run(task)
    tmp = Path(argv[1]).with_suffix(".part")
    tmp.write_text(json.dumps(answer))
    tmp.replace(argv[1])
    return 0


__all__ = ["Beside", "child_main"]
