# Responsibility: Keep a running job's lease while its worker is busy in one long step that the in-process beat cannot interrupt.
# Owns: the keeper process beside a running job, when it renews on the job's behalf, and when it lets the lease lapse.
# Boundaries: it renews the lease and nothing else - never the deadline, never the job's state; the claim, the fence and the beat stay persistence/lease.py's and pipeline_run's.
# Collaborates with: application/pipeline_run.py (starts it, marks the beat), persistence/lease.py (the renewal), sandbox/liveness.py.
"""A job is never lost to its own long step.

WHAT HAPPENED. dev-areen, 25 Oct, a 72-case FEA assembly run: helmholtz_coil, odri_biped,
ox_mc_laser and ender3_full stopped logging inside the builder's "preparing a metre-normalised
surface" while the worker process stayed alive. The lease beat is a task on the run's event loop,
and that step ran ON the loop: CAD import and tessellation of a big assembly, minutes of native
work that also holds the GIL. Nothing renewed the lease, it lapsed, the reaper re-ran the job, the
re-run met the same step, and the job ended as "worker lost" - or, for ender3_full, as a poison job
after five deliveries. No mesher had started.

THE KEEPER. A small process started beside every leased run. It cannot be starved: it has its own
interpreter, its own GIL and its own database connection. While the in-process beat runs, the
keeper only watches (the beat marks a file every tick). When the beat has been silent for longer
than two ticks, the keeper looks at the worker's process tree (sandbox/liveness): while it is still
burning CPU, changing its memory, or its children are, the job is working, and the keeper renews
the lease on its behalf. A worker that shows no sign of life for the stall window is stuck, and the
keeper lets the lease lapse exactly as before - so a hung worker is still taken over, and a busy one
never is. The keeper renews ONLY the lease: the pipeline deadline still moves on progress alone
(application/pipeline_budget), so a step that never ends still meets it.

It ends when the run ends (its stdin is closed), when the worker process is gone (stdin reaches EOF
and the parent changes), or when the lease is no longer this worker's.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: named, not __name__: the keeper runs as __main__ (python -m), and its lines must read as its own
logger = logging.getLogger("meshpipeline.application.lease_keeper")

#: Unit tests that fake the lease turn the keeper off with it (tests/unit/conftest.py).
ENABLED = True
#: How long the worker may show no sign of life before the keeper stops renewing for it - the same
#: window that marks a native run as stuck (engines/gmsh/gmsh_runner.FEA_STALL_WINDOW_S).
STALL_WINDOW_S = 900.0
#: The renewal the keeper calls, as "module:function" - overridable for its own tests.
DEFAULT_RENEW = "meshpipeline.application.lease_keeper:renew_in_database"


def fresh_window(interval_s: float) -> float:
    """How old the in-process beat's mark may be while the beat still counts as running: two ticks
    and a margin, so one slow tick (a database blip) never sends the keeper in."""
    return 2.0 * float(interval_s) + 15.0


def decide(*, beat_age_s: float | None, life_age_s: float, fresh_s: float, stall_s: float) -> str:
    """What the keeper does at one look:
    * "watch" - the in-process beat is running; nothing to do;
    * "renew" - the beat is silent but the worker is working (a sign of life within the window);
    * "hold"  - the beat is silent and the worker has shown no life for the window: it is stuck,
      and the lease is left to lapse."""
    if beat_age_s is not None and beat_age_s <= fresh_s:
        return "watch"
    if life_age_s <= stall_s:
        return "renew"
    return "hold"


@dataclass
class Keeper:
    """The parent's handle on a running keeper."""
    proc: subprocess.Popen
    beat_path: Path
    workdir: Path

    def mark_alive(self) -> None:
        """The in-process beat ran just now."""
        try:
            self.beat_path.touch()
        except OSError:
            pass

    def stop(self, *, wait_s: float = 5.0) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=wait_s)
            except subprocess.TimeoutExpired:
                pass
        for p in (self.beat_path,):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.workdir.rmdir()
        except OSError:
            pass


def _ownership_dict(ownership) -> dict:
    deadline = getattr(ownership, "pipeline_deadline_at", None)
    return {"job_id": str(ownership.job_id),
            "execution_generation": int(ownership.execution_generation),
            "worker_token": str(ownership.worker_token),
            "backend": str(getattr(ownership, "backend", "") or ""),
            "pipeline_deadline_at": deadline.isoformat() if deadline is not None else None,
            "claim_epoch": int(getattr(ownership, "claim_epoch", 0) or 0),
            "fence_ttl_seconds": int(getattr(ownership, "fence_ttl_seconds", 0) or 0)}


def start(ownership, *, interval_s: float, stall_s: float = STALL_WINDOW_S,
          fresh_s: float | None = None, renew: str = DEFAULT_RENEW,
          env: Mapping[str, str] | None = None) -> Keeper | None:
    """Start the keeper beside the running job. Never raises: without a keeper the in-process beat
    still renews the lease as it always did, and the failure is logged."""
    if not ENABLED or ownership is None:
        return None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="lease-keeper-"))
        beat = workdir / "beat"
        beat.touch()
        cfg = {"ownership": _ownership_dict(ownership), "watch_pid": os.getpid(),
               "interval_s": float(interval_s), "stall_s": float(stall_s),
               "fresh_s": float(fresh_s if fresh_s is not None else fresh_window(interval_s)),
               "beat_path": str(beat), "renew": renew}
        # the worker token travels on stdin, never on the command line where `ps` would show it
        proc = subprocess.Popen([sys.executable, "-m", "meshpipeline.application.lease_keeper"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                env=dict(env) if env is not None else None, close_fds=True)
        if proc.stdin is None:
            raise RuntimeError("the keeper's stdin did not open")
        proc.stdin.write((json.dumps(cfg) + "\n").encode())
        proc.stdin.flush()
        return Keeper(proc=proc, beat_path=beat, workdir=workdir)
    except Exception as exc:  # noqa: BLE001 - the in-process beat still holds the lease
        logger.warning("lease keeper could not start for job %s (%s) - the in-process beat alone "
                       "renews the lease", getattr(ownership, "job_id", "?"), exc)
        return None


# #
# the keeper process
# #

def _resolve(target: str) -> Callable[[Mapping], bool]:
    import importlib
    mod, _, fn = target.partition(":")
    return getattr(importlib.import_module(mod), fn)


def renew_in_database(cfg: Mapping) -> str:
    """Renew the lease once, as its owner (persistence/lease.heartbeat_reason), on a connection of
    the keeper's own.

    RETURNS THE REASON, not a bool, because the caller has to tell two kinds of refusal apart: a
    rotated token or a finished job can never come back, but a passed pipeline deadline can - the
    very next beat may carry an extension earned by fresh progress. Treating all refusals alike is
    what made the keeper exit for good on a run that was still working. `lease.ends_renewal` is
    the predicate; nothing is extended here, the beat owns that.

    A substitute `renew` that still returns a bool is accepted by `run` for compatibility.
    """
    import asyncio
    return asyncio.run(_renew_in_database(cfg))


async def _renew_in_database(cfg: Mapping) -> str:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    import meshpipeline.settings.providers as provcfg
    from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository
    o = cfg["ownership"]
    own = ExecutionOwnership(
        job_id=uuid.UUID(o["job_id"]), execution_generation=int(o["execution_generation"]),
        worker_token=uuid.UUID(o["worker_token"]), backend=str(o.get("backend") or ""),
        pipeline_deadline_at=(datetime.fromisoformat(o["pipeline_deadline_at"])
                              if o.get("pipeline_deadline_at") else None),
        claim_epoch=int(o.get("claim_epoch") or 0),
        fence_ttl_seconds=int(o.get("fence_ttl_seconds") or 0))
    engine = create_async_engine(provcfg.POSTGRES_DSN, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            # THE REASON, not a bare bool: a deadline refusal can reverse on the next beat and
            # must not end this keeper, where a rotated token must.
            why = await LeaseRepository().heartbeat_reason(db, own)
            await db.commit()
        return why
    finally:
        await engine.dispose()


def _mark(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def run(cfg: Mapping, stop: threading.Event, *, clock: Callable[[], float] = time.monotonic) -> str:
    """The keeper's loop; returns why it ended ("stopped", "parent_gone", "lost")."""
    from meshpipeline.sandbox.liveness import LivenessWatch
    job = str(cfg["ownership"]["job_id"])
    watch_pid = int(cfg["watch_pid"])
    interval, stall, fresh = float(cfg["interval_s"]), float(cfg["stall_s"]), float(cfg["fresh_s"])
    beat = Path(cfg["beat_path"])
    renew = _resolve(str(cfg.get("renew") or DEFAULT_RENEW))
    watch = LivenessWatch(watch_pid, (), exclude=(os.getpid(),))
    last_life = last_beat = clock()
    # the beat's age is measured on this process's monotonic clock, from when its mark last
    # CHANGED - never wall time against a file time, which a clock step (a VM's, WSL's) would skew
    seen_mark = _mark(beat)
    said = ""
    while not stop.wait(interval):
        if os.getppid() != watch_pid:
            return "parent_gone"
        now = clock()
        if watch.poll() is not None:
            last_life = now
        mark = _mark(beat)
        if mark is not None and mark != seen_mark:
            seen_mark, last_beat = mark, now
        action = decide(beat_age_s=now - last_beat, life_age_s=now - last_life, fresh_s=fresh,
                        stall_s=stall)
        if action == "renew":
            try:
                owned = renew(cfg)
            except Exception as exc:  # noqa: BLE001 - a blip is tried again next look
                logger.warning("lease keeper: renewal for job %s failed (%s) - trying again", job, exc)
                continue
            # `renew` is pluggable and its old contract was a bool, so both shapes are accepted:
            # a reason string from `renew_in_database`, or True/False from a substitute.
            from meshpipeline.persistence.lease import RENEWED, SUPERSEDED, ends_renewal
            why = owned if isinstance(owned, str) else (RENEWED if owned else SUPERSEDED)
            if why != RENEWED:
                if ends_renewal(why):
                    logger.error("lease keeper: the lease of job %s was refused (%s - the job is "
                                 "over, or a newer worker owns it) - stopping", job, why)
                    return "lost"
                # PAST THE DEADLINE, WHICH CAN REVERSE. Keep watching: the worker may yet show
                # progress that moves the deadline on, and a keeper that exited here left the run
                # with one renewal path instead of two for the rest of its life.
                if said != "deadline":
                    logger.warning("lease keeper: job %s is past its pipeline deadline with no "
                                   "recent progress - still watching, because progress may move "
                                   "the deadline on", job)
                    said = "deadline"
                continue
            if said != "renew":
                logger.warning("lease keeper: job %s - the worker is busy in one long step (its own "
                               "beat silent for %.0f s, its processes still working): renewing its "
                               "lease for it", job, now - last_beat)
        elif action == "hold" and said != "hold":
            logger.error("lease keeper: job %s - no sign of life from the worker for %.0f s (no CPU, "
                         "no memory change) and its beat is silent: the lease is left to lapse",
                         job, now - last_life)
        said = action
    return "stopped"


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    line = sys.stdin.buffer.readline()
    if not line:
        return 0
    cfg = json.loads(line)
    stop = threading.Event()

    def _until_eof() -> None:
        # the parent closes stdin to stop the keeper; a parent that dies closes it too
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except OSError:
            pass
        stop.set()

    threading.Thread(target=_until_eof, name="lease-keeper-stdin", daemon=True).start()
    why = run(cfg, stop)
    logging.shutdown()
    # at once, not through interpreter shutdown: the stdin reader may still be blocked in its read,
    # holding stdin's lock, and a normal exit would wait on it
    os._exit(0 if why in ("stopped", "parent_gone", "lost") else 1)


__all__ = ["DEFAULT_RENEW", "ENABLED", "STALL_WINDOW_S", "Keeper", "decide", "fresh_window",
           "renew_in_database", "run", "start"]


if __name__ == "__main__":
    raise SystemExit(main())
