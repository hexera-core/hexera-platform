# Responsibility: The shared shapes of layer 2 of the CRS v4 checker - what one mesher on the panel
#                 reports - and the runner that puts a file in front of every member.
# Owns: Located, MemberResult, the PanelMember protocol, `run_panel`, and the rules every member shares:
#       the infrastructure marker, the timeout retry, the crash kinds and the inconclusive tally.
# Boundaries: no gmsh, no OCCT, no process handling. A member talks to the WorkerPool; which meshers
#             exist is `gmsh_members` (Task 4 adds Netgen). It judges nothing across members.
"""Layer 2 (meshable on a mesher panel). "Clean" has to hold in whatever tool the customer uses, so a
file is put in front of several unlike meshers and each answers for itself.

One bad mesher must not take down the panel: a member that segfaults or hangs is recorded `crashed`
or `timeout` (the pool kills and replaces its worker) and the members after it still run. An
INFRASTRUCTURE failure - a worker that cannot start, a kernel function that errors for a reason
that is not the file's - is not a mesher finding and raises `CheckerUnavailable`, as layer 1 does.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool

logger = logging.getLogger(__name__)

#: `MemberResult.error_kind` of a `timeout`/`crashed` result that says nothing about the file: the pool
#: died, a worker could not be had, the process was SIGKILLed, a single timeout. The ONE definition:
#: `assemble.INFRASTRUCTURE_KIND` and `netgen_member.INFRASTRUCTURE` must be this object, not a copy
#: (`test_panel.test_one_infrastructure_constant` fails if they are not).
INFRASTRUCTURE_KIND = "infrastructure"

#: `error_kind` of a mesher process that died on the file (segfault, abort): a vote AGAINST the file.
MESHER_CRASH_KIND = "gmsh_crash"
#: `error_kind` of a timeout that happened twice, the second time at twice the budget: a vote against the file.
TIMEOUT_KIND = "timeout"

#: Signals that mean the mesher itself crashed on the geometry: SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGSEGV.
_CRASH_SIGNALS = frozenset({4, 6, 7, 8, 11})

#: Seconds a member leaves the pool's own clock beyond the subprocess's, so the subprocess is stopped
#: first and is never orphaned by the worker's SIGKILL.
POOL_MARGIN_S = 30.0


def inner_timeout_s(timeout_s: float) -> float:
    """The clock a member's mesher subprocess gets under a pool budget of `timeout_s`."""
    return max(1.0, timeout_s - POOL_MARGIN_S) if timeout_s > 2 * POOL_MARGIN_S else timeout_s * 0.8


def signal_verdict(signum: int) -> tuple[str, str]:
    """`(error_kind, cause)` for a mesher process killed by signal `signum`. SIGSEGV, SIGABRT, SIGFPE, SIGILL
    and SIGBUS are the mesher crashing on this geometry and vote against the file. SIGKILL (the OOM killer,
    an operator) and anything else cannot be told from the machine failing, so they do not blame the file."""
    if signum in _CRASH_SIGNALS:
        return MESHER_CRASH_KIND, f"signal_{signum}"
    return INFRASTRUCTURE_KIND, "sigkill" if signum == 9 else f"signal_{signum}"


@dataclass(frozen=True)
class Located:
    """Where a mesher says it went wrong. `entity` is the face's fingerprint (`identity.fingerprint_of`,
    from the surface's bounding box in mm) or None; `xyz` a point in mm or None; `mesher_tag` the
    mesher's own name for it (a gmsh surface tag, say), which means nothing outside that run."""

    entity: str | None
    xyz: tuple[float, float, float] | None
    mesher_tag: str


@dataclass(frozen=True)
class MemberResult:
    """One mesher's answer. `pass` means a valid tet mesh with no inverted element and
    `min_sicn >= 0.01`; `fail` names why in `error_kind`; `timeout` and `crashed` are the mesher
    hanging or dying on this file, a finding about the file, UNLESS `error_kind` is
    `INFRASTRUCTURE_KIND`, which says nothing about it. `cause` says why, for the tally of
    inconclusive files (`inconclusive_tally`); it is not part of the verdict."""

    name: str
    status: Literal["pass", "fail", "timeout", "crashed"]
    error_kind: str | None = None
    message: str = ""
    located: list[Located] = field(default_factory=list)
    min_sicn: float | None = None
    elements: int = 0
    seconds: float = 0.0
    cause: str | None = None


class PanelMember(Protocol):
    name: str
    #: Everything that decides this member's answer, JSON-able and never None: the verdict cache keys
    #: on its hash (`cache.py` fails closed without it), so a changed option must change it.
    config: dict

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult: ...


@dataclass(frozen=True)
class RetriedCall:
    """The call that settled, and how many of the (up to two) attempts timed out. `call.seconds` is the sum."""

    call: CallResult
    timeouts: int


def _pool_timed_out(call: CallResult) -> bool:
    return call.status == "timeout"


def call_with_timeout_retry(
    pool: WorkerPool,
    fn_name: str,
    payload_for: Callable[[float], dict],
    *,
    timeout_s: float,
    timed_out: Callable[[CallResult], bool] = _pool_timed_out,
) -> RetriedCall:
    """`pool.call`, and on a timeout once more at twice the budget (`payload_for(2 * timeout_s)`).

    A timeout cannot tell a pathological file from a slow worker (a loaded host, an emulated VM), so
    one timeout blames nobody: the call is run again with twice the time. Only a second timeout is the
    file's (`timeout_kind`). `timed_out` says what a timeout is for a member whose mesher has its own
    clock (a subprocess timeout comes back inside an `ok` call)."""
    first = pool.call(fn_name, payload_for(timeout_s), timeout_s=timeout_s)
    if not timed_out(first):
        return RetriedCall(first, 0)
    second = pool.call(fn_name, payload_for(2 * timeout_s), timeout_s=2 * timeout_s)
    merged = CallResult(
        ok=second.ok,
        value=second.value,
        error=second.error,
        status=second.status,
        seconds=first.seconds + second.seconds,
    )
    return RetriedCall(merged, 2 if timed_out(second) else 1)


def timeout_kind(timeouts: int, genuine_kind: str = TIMEOUT_KIND) -> str:
    """`error_kind` of a timeout result after `timeouts` timeouts: a single one is infrastructure, only
    a repeat (at twice the budget) is `genuine_kind`, a vote against the file."""
    return genuine_kind if timeouts >= 2 else INFRASTRUCTURE_KIND


class PanelContractError(RuntimeError):
    """A member returned a `timeout`/`crashed` result that does not say whose fault it is."""


def check_result(result: MemberResult) -> MemberResult:
    """A `timeout` or `crashed` result must name its kind: the verdict reads `INFRASTRUCTURE_KIND` to decide
    between 'the file failed' and 'nothing is known', and a missing kind would be read as the former."""
    if result.status in ("timeout", "crashed") and not result.error_kind:
        raise PanelContractError(f"{result.name}: a {result.status!r} result has no error_kind")
    return result


def is_inconclusive(result: MemberResult) -> bool:
    return result.status in ("timeout", "crashed") and result.error_kind == INFRASTRUCTURE_KIND


def inconclusive_tally(results: Sequence[MemberResult]) -> dict[str, int]:
    """Inconclusive members by `member:cause`, e.g. `{"gmsh_prod:sigkill": 1}`. A file with a non-empty
    tally is excluded from the scoreboard; this is the visible count of what was excluded and why."""
    return dict(Counter(f"{r.name}:{r.cause or 'unspecified'}" for r in results if is_inconclusive(r)))


def run_panel(
    pool: WorkerPool, step_path: str, members: Sequence[PanelMember], *, timeout_s: float
) -> list[MemberResult]:
    """Run every member on `step_path`, in order, one result each. A member that times out or
    crashes is a result like any other; the next member runs on a replaced worker. Every result is
    checked (`check_result`), and each inconclusive one is logged with its cause."""
    results = [check_result(member.run(pool, step_path, timeout_s=timeout_s)) for member in members]
    for r in results:
        if is_inconclusive(r):
            logger.warning(
                "panel INCONCLUSIVE %s on %s: %s (%s): %s", r.name, step_path, r.status, r.cause or "unspecified", r.message
            )
    return results
