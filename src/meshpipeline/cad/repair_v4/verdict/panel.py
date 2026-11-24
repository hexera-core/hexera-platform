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

import dataclasses

import logging
import signal
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, Protocol

from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool

logger = logging.getLogger(__name__)

#: `MemberResult.error_kind` of a `timeout`/`crashed` result that says nothing about the file: the pool
#: died, a worker could not be had, the process was SIGKILLed, a single timeout. The ONE definition:
#: `assemble`, `gmsh_members`, `netgen_member` all import this one, and must not retype it, not a
#: retyped literal (`test_panel`: a scan for the literal outside this file, and a run that patches this
#: value before importing every verdict module and finds none that kept the old string).
INFRASTRUCTURE_KIND = "infrastructure"

#: `error_kind` of a mesher process that died on the file (segfault, abort): a vote AGAINST the file.
MESHER_CRASH_KIND = "gmsh_crash"
#: `error_kind` of a timeout that happened twice, the second time at twice the budget: a vote against the file.
TIMEOUT_KIND = "timeout"

#: Signals that mean the mesher itself crashed on the geometry. By name: SIGBUS is 7 on Linux and 10 on macOS.
CRASH_SIGNALS = frozenset(
    int(getattr(signal, name)) for name in ("SIGILL", "SIGABRT", "SIGBUS", "SIGFPE", "SIGSEGV")
)

#: Seconds a member leaves the pool's own clock beyond the subprocess's, so the subprocess is stopped
#: first and is never orphaned by the worker's SIGKILL.
POOL_MARGIN_S = 30.0


def inner_timeout_s(timeout_s: float) -> float:
    """The clock a member's mesher subprocess gets under a pool budget of `timeout_s`."""
    return max(1.0, timeout_s - POOL_MARGIN_S) if timeout_s > 2 * POOL_MARGIN_S else timeout_s * 0.8


def timeout_cause(timeouts: int, skipped: bool) -> str:
    """`MemberResult.cause` of a timeout after `timeouts` of them; `skipped` = a retry was due and the
    file's budget had none left. Shared by every member that retries."""
    if timeouts >= 2:
        return "timeout_after_hang" if skipped else "timeout_twice"
    return "timeout_retry_spent" if skipped else "timeout_once"


def sigkill_verdict(kills: int, skipped: bool) -> tuple[str, str]:
    """`(error_kind, cause)` of a SIGKILLed mesher after `kills` of them: one blames nobody, a repeat is the file's."""
    if kills >= 2:
        return MESHER_CRASH_KIND, "sigkill_after_doom" if skipped else "sigkill_twice"
    return INFRASTRUCTURE_KIND, "sigkill_retry_spent" if skipped else "sigkill"


def signal_verdict(signum: int) -> tuple[str, str]:
    """`(error_kind, cause)` for a mesher process killed by signal `signum`. SIGSEGV, SIGABRT, SIGFPE, SIGILL
    and SIGBUS are the mesher crashing on this geometry and vote against the file (a `bad_alloc` inside gmsh
    aborts with SIGABRT, so memory exhaustion that gmsh notices is among them). SIGKILL (the OOM killer, an
    operator) cannot be told from the machine failing on ONE sight, so a single one blames nobody and is
    re-run once (`call_with_timeout_retry`); only a repeat is the file's."""
    if signum in CRASH_SIGNALS:
        return MESHER_CRASH_KIND, f"signal_{signum}"
    return INFRASTRUCTURE_KIND, "sigkill" if signum == signal.SIGKILL else f"signal_{signum}"


@dataclass(frozen=True)
class Located:
    """Where a mesher says it went wrong. `entity` is the face's fingerprint (`identity.fingerprint_of`,
    from the surface's bounding box in mm) or None; `xyz` a point in mm or None; `mesher_tag` the
    mesher's own name for it (a gmsh surface tag, say), which means nothing outside that run."""

    entity: str | None
    xyz: tuple[float, float, float] | None
    mesher_tag: str
    #: the named face's box in mm AS THE KERNEL BOXES IT (gmsh's padding taken off, `mesh_child.box_mm`), or None.
    #: `entity` is that box on the MESHER's grid (`quantum_for` of its own diagonal); the workspace names faces on its
    #: own grid, and the two differ 2x when the diagonals straddle a power of two (lane D17: 2 of 12 dev files, every
    #: fingerprint missing). `sites.find_sites` names this box on the workspace's grid instead.
    box: tuple[float, ...] | None = None


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
    #: `(Mesh.Algorithm, Mesh.Algorithm3D)` as the mesher read them back from gmsh after meshing, or None
    #: when no mesher ran (a pool failure, a timeout, a crash).
    algorithms: tuple[int, int] | None = None


class PanelMember(Protocol):
    name: str
    #: Everything that decides this member's answer, JSON-able and never None: the verdict cache keys
    #: on its hash (`cache.py` fails closed without it), so a changed option must change it.
    config: dict

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult: ...


@dataclass
class RetryBudget:
    """The retries one FILE may spend across the whole panel: a genuine hang costs 3T (T, then 2T), so
    three retrying members would cost 9T. One retry per file; `doomed` is set when it was spent and the
    file failed the same way again, after which a later member's single failure of that kind is already
    the file's (it is not going to mesh) and is not retried either."""

    retries_left: int = 1
    doomed: bool = False


_budget: ContextVar[RetryBudget | None] = ContextVar("panel_retry_budget", default=None)


@contextmanager
def retry_budget(retries: int = 1) -> Iterator[RetryBudget]:
    """Scope one `RetryBudget` over the calls made for one file (`run_panel` does this)."""
    budget = RetryBudget(retries)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


@dataclass(frozen=True)
class RetriedCall:
    """The call that settled, and how many of the (up to two) attempts timed out or were SIGKILLed
    (`kills`). `call.seconds` is the sum. `skipped` says a retry was due and the file's budget had
    none left; the counts then already include the doom (`RetryBudget.doomed`)."""

    call: CallResult
    timeouts: int
    kills: int = 0
    skipped: bool = False


def _pool_timed_out(call: CallResult) -> bool:
    return call.status == "timeout"


def _never(_call: CallResult) -> bool:
    return False


def call_with_timeout_retry(
    pool: WorkerPool,
    fn_name: str,
    payload_for: Callable[[float], dict],
    *,
    timeout_s: float,
    timed_out: Callable[[CallResult], bool] = _pool_timed_out,
    killed: Callable[[CallResult], bool] = _never,
) -> RetriedCall:
    """`pool.call`, and on a timeout once more at twice the budget (`payload_for(2 * timeout_s)`); on a
    SIGKILL (`killed`) once more at the same budget.

    A timeout cannot tell a pathological file from a slow worker (a loaded host, an emulated VM), nor a
    SIGKILL a file that exhausts memory from a machine that did, so one blames nobody: the call is run
    again. Only a second one is the file's (`timeout_kind`). `timed_out` says what a timeout is for a
    member whose mesher has its own clock (a subprocess timeout comes back inside an `ok` call).

    The retry is a per-FILE resource inside `run_panel` (`retry_budget`): one for the whole panel, so a
    file costs at most `(members + 2) * timeout_s`, not `3 * members * timeout_s`. A member that needs a
    retry the file has already spent runs once; if the spent retry ended in a repeat failure the file is
    `doomed` and that single failure is counted as the file's. Outside `run_panel` retries are unlimited."""
    first = pool.call(fn_name, payload_for(timeout_s), timeout_s=timeout_s)
    was_timeout = timed_out(first)
    if not was_timeout and not killed(first):
        return RetriedCall(first, 0)
    budget = _budget.get()
    if budget is not None and budget.retries_left <= 0:
        repeat = 2 if budget.doomed else 1
        return RetriedCall(first, repeat if was_timeout else 0, 0 if was_timeout else repeat, skipped=True)
    if budget is not None:
        budget.retries_left -= 1
    retry_s = 2 * timeout_s if was_timeout else timeout_s
    second = pool.call(fn_name, payload_for(retry_s), timeout_s=retry_s)
    merged = CallResult(
        ok=second.ok,
        value=second.value,
        error=second.error,
        status=second.status,
        seconds=first.seconds + second.seconds,
    )
    timeouts = int(was_timeout) + int(timed_out(second))
    kills = int(not was_timeout) + int(killed(second))
    if budget is not None and (timeouts == 2 or kills == 2):
        budget.doomed = True
    return RetriedCall(merged, timeouts, kills)


def timeout_kind(timeouts: int, genuine_kind: str = TIMEOUT_KIND) -> str:
    """`error_kind` of a timeout result after `timeouts` timeouts: a single one is infrastructure, only
    a repeat (at twice the budget) is `genuine_kind`, a vote against the file."""
    return genuine_kind if timeouts >= 2 else INFRASTRUCTURE_KIND


def classify_after_timeout(result: MemberResult, timeouts: int, genuine_kind: str = TIMEOUT_KIND) -> MemberResult:
    """After a first timeout, a retry that did not pass is ONE outcome: the member's genuine timeout, as if it had
    timed out twice. What the retry hit (another timeout, gmsh stopping with a `fail`, a SIGSEGV) is where the mesher
    was when its clock ran out, so it depends on the machine, not the file: Lane A measured occt/bug32314_corrupted.stp
    (gmsh_alt, native x86, --cpus=1, 8 runs) time out first every run and then end timeout x2, fail/other x3,
    crashed/gmsh_crash x3. A pass after a timeout is still a pass, and an infrastructure result stays one. The
    retry's own outcome is kept in `message` and `cause` for triage."""
    if timeouts < 1 or result.status not in ("fail", "crashed") or result.error_kind == INFRASTRUCTURE_KIND:
        return result
    return MemberResult(
        name=result.name, status="timeout", error_kind=genuine_kind,
        message=f"retry after a timeout ended {result.status} ({result.error_kind}): classified by the timeout. "
                + result.message,
        seconds=result.seconds, cause=f"after_timeout_{result.status}",
    )


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


#: lane C16: `error_kind` of a member that ran AFTER the panel was already doomed (an earlier member did not pass, so
#: the file cannot be clean) and did not finish inside the doomed cap. A fail, never inconclusive, and NOT a defect
#: the mesher found: the triage reads it apart.
CAPPED_KIND = "capped_after_doom"
#: the order a DOOMED-CAPPED panel runs its members in: cheapest and most often failing first (v4's cache: gmsh_default
#: 0.6 s median, fails 78%), so a doomed panel is known within about a second. Results keep the CALLER's order.
DOOM_ORDER = ("gmsh_default", "gmsh_prod", "gmsh_alt", "netgen_occ")


def run_panel(
    pool: WorkerPool, step_path: str, members: Sequence[PanelMember], *, timeout_s: float,
    doomed_cap_s: float | None = None,
) -> list[MemberResult]:
    """Run every member on `step_path`, one result each, in the caller's order. A member that times out or
    crashes is a result like any other; the next member runs on a replaced worker. The file gets ONE
    retry in all (`RetryBudget`), so its worst case is `(len(members) + 2) * timeout_s` of pool time.
    Every result is checked (`check_result`), and each inconclusive one is logged with its cause.

    `doomed_cap_s` (lane C16; the full tier): once a member does not PASS the panel cannot be clean, so every later
    member runs with that budget and NO retry, and one that does not finish (or dies for pool reasons) is a fail of
    kind CAPPED_KIND. An all-pass panel is never doomed, so no clean verdict can change; measured on v4's cache it cut
    full-panel member time 34% before hangs (a doomed hang costs the cap, not 300 s + a 600 s retry), losing 2% of
    passes, all on panels that were not clean."""
    if doomed_cap_s is None:
        with retry_budget():
            results = [check_result(member.run(pool, step_path, timeout_s=timeout_s)) for member in members]
    else:
        rank = {n: i for i, n in enumerate(DOOM_ORDER)}
        order = sorted(range(len(members)), key=lambda i: (rank.get(getattr(members[i], "name", ""), len(rank)), i))
        got: dict[int, MemberResult] = {}
        doomed = False
        with retry_budget():
            for i in order:
                if not doomed:
                    r = check_result(members[i].run(pool, step_path, timeout_s=timeout_s))
                else:
                    with retry_budget(0):
                        r = check_result(members[i].run(pool, step_path, timeout_s=doomed_cap_s))
                    if r.status in ("timeout", "crashed") and (is_inconclusive(r) or r.status == "timeout"):
                        r = dataclasses.replace(r, status="fail", error_kind=CAPPED_KIND, cause=None,
                                                message=f"the panel was already not clean; {r.status} inside the "
                                                        f"{doomed_cap_s:g} s doomed cap ({r.message[:120]})")
                got[i] = r
                doomed = doomed or r.status != "pass"
        results = [got[i] for i in range(len(members))]
    for r in results:
        if is_inconclusive(r):
            logger.warning(
                "panel INCONCLUSIVE %s on %s: %s (%s): %s", r.name, step_path, r.status, r.cause or "unspecified", r.message
            )
    return results
