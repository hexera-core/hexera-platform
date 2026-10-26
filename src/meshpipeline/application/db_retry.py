# Responsibility: Retry a database step that failed because the database was briefly unavailable -
# a restart, a dropped connection, a full server - with a fresh connection each time.
# Owns: what counts as a transient database failure, and the backoff.
# Boundaries: the caller's step must be safe to run again (an upsert, a rolled-back transaction).
"""dev-areen 25 Oct: kart_chassis built a valid 27-part mesh, passed every check and got a reviewer
PASS at 04:43:35 - then the job failed as dependency_down on "OperationalError: the connection is
closed". The checkpointer's one long-lived connection had died at the 03:50 Cloud SQL restart, and
the next checkpoint write was the first to find out. A database blip must cost a retry, never a
finished mesh."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The waits between tries, seconds: about two minutes in all - longer than a Cloud SQL restart's
#: dropped connections take to come back, short enough that a real outage is still told.
BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)

#: Names of the driver errors that mean "the connection or the server", not "your SQL".
_CONNECTION_ERRORS = ("OperationalError", "InterfaceError", "ConnectionDoesNotExist",
                      "ConnectionFailure", "TooManyConnections", "CannotConnectNow",
                      "AdminShutdown", "PoolTimeout", "ConnectionTimeout")


def is_transient_db_error(exc: BaseException) -> bool:
    """The database was unreachable, restarting, full or dropped the connection - worth a retry."""
    for e in (exc, getattr(exc, "__cause__", None), getattr(exc, "orig", None)):
        if e is None:
            continue
        name = type(e).__name__
        if any(t in name for t in _CONNECTION_ERRORS):
            return True
        text = str(e).lower()
        if any(m in text for m in ("the connection is closed", "server closed the connection",
                                   "connection already closed", "terminating connection",
                                   "remaining connection slots are reserved", "too many clients",
                                   "the database system is starting up",
                                   "the database system is shutting down")):
            return True
    return False


async def retry_transient(step: Callable[[], Awaitable[T]], *, what: str, jlog: Any = logger,
                          backoff: tuple[float, ...] | None = None, sleep=asyncio.sleep) -> T:
    """Run `step`; on a transient database failure wait and run it again, up to len(backoff) more
    times. The last failure is raised. Any other error is raised at once."""
    waits = BACKOFF_S if backoff is None else backoff
    for i, wait in enumerate((*waits, None)):
        try:
            return await step()
        except Exception as exc:  # noqa: BLE001 - narrowed at once
            if wait is None or not is_transient_db_error(exc):
                raise
            jlog.warning("%s: the database was unavailable (%s: %s) - trying again in %.0f s (%d of %d)",
                         what, type(exc).__name__, exc, wait, i + 1, len(waits))
            await sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["BACKOFF_S", "is_transient_db_error", "retry_transient"]
