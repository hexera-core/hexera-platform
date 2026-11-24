# Responsibility: The one checker. Verdict assembles the three layers; `score` orders states by it.
# Owns: Declared, Verdict, `verdict`, `cheap_verdict`, `score`.
# Boundaries: no OCCT. Layer 1 is valid.py, layer 2 is the panel (panel.py, Tasks 3/4), layer 3 is
#             faithful.py. Nothing here meshes: members are injected or come from gmsh_members.
"""`verdict` is the search's reward, the delivery gate and the benchmark scoreboard at once.

An infrastructure failure is NOT a verdict. `CheckerUnavailable` (layer 1 or 3 could not be
measured) propagates out of `verdict` rather than becoming an unclean Verdict, so a flaky worker is
never scored as broken CAD. Panel results with status timeout/crashed ride in `panel` and count as
non-passes; they are never cached.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from meshpipeline.cad.repair_v4.verdict.cache import VerdictCache, file_sha256, mesher_versions, panel_config_hash
from meshpipeline.cad.repair_v4.verdict.faithful import DeclaredIdError, FaithfulReport, check_faithful_steps
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, ValidReport, check_valid
from meshpipeline.cad.repair_v4.verdict.wall import TooLateToStart, clip_member_timeout, remaining_s  # noqa: F401
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

logger = logging.getLogger(__name__)
MEMBER_TIMEOUT_S = 300.0
#: lane C16: the doomed cap of BOTH tiers (`panel.run_panel`). Once a member fails the file cannot be clean, so the rest
#: get this budget and no retry. The slowest PASS on any of v4's 150 all-pass (clean) panels was 68.5 s (netgen), and
#: an all-pass panel is never doomed, so this changes no clean verdict; it bounds what a doomed panel can spend.
DOOMED_CAP_S = 60.0
UNMEASURED_SICN = -1.0  # a mesher that produced no mesh has no SICN; it ranks below any real one


#: B23: VERDICT-LEVEL PHASE TIMING, so "the verdict is the cost" can be read per LAYER rather than per file.
#: Seconds and call counts per phase, accumulated process-wide (the search judges states on several threads, so
#: the totals are a sum of concurrent work and are NOT a wall-clock breakdown; `wall_s` on the row is the wall).
#: Diagnostic only: nothing reads these to decide anything.
_PHASES = ("valid", "panel", "panel_cache_hit", "faithful", "faithful_skipped", "key")
_phase_lock = threading.Lock()
_phase_totals: dict[str, float] = {}


def _phase(name: str, seconds: float = 0.0) -> None:
    with _phase_lock:
        _phase_totals[f"{name}_s"] = _phase_totals.get(f"{name}_s", 0.0) + seconds
        _phase_totals[f"{name}_n"] = _phase_totals.get(f"{name}_n", 0.0) + 1


def verdict_phases() -> dict[str, float]:
    """The phase totals so far: `<phase>_s` seconds and `<phase>_n` calls, for `<phase>` in `_PHASES`."""
    with _phase_lock:
        return {k: (round(v, 3) if k.endswith("_s") else int(v)) for k, v in sorted(_phase_totals.items())}


def reset_verdict_phases() -> None:
    """Zero the totals. The eval calls this per (file, arm) so a row's phases are that row's own."""
    with _phase_lock:
        _phase_totals.clear()


class MemberResultLike(Protocol):
    """The part of Task 3's `MemberResult` this module reads."""

    @property
    def name(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def min_sicn(self) -> float | None: ...


class PanelMemberLike(Protocol):
    @property
    def name(self) -> str: ...
    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResultLike: ...



def is_infrastructure_failure(result: Any) -> bool:
    """A timeout/crash the POOL caused (worker replaced, OOM, infrastructure kill), as the panel
    marks it with `error_kind == "infrastructure"`. A mesher that overran its limit or segfaulted
    on the geometry is the file failing the definition ("meshes within the normal time limit"), not
    this.

    The marker is `panel.INFRASTRUCTURE_KIND`, read at call time: importing this module is what
    `verdict/__init__` does first, so a name bound here at import would be bound before anything can
    follow a change to the constant (`test_panel`'s patch-then-import guard)."""
    kind = _panel_module().INFRASTRUCTURE_KIND
    return result.status in ("timeout", "crashed") and getattr(result, "error_kind", None) == kind


class InconclusiveVerdict(RuntimeError):
    """`score` was asked to rank a verdict that says nothing about the file."""


PanelRunner = Callable[..., Sequence[Any]]


@dataclass(frozen=True)
class Declared:
    """Edits the repair claims, as face indices `F<k>`: `added` index the repaired shape, `removed`
    the original (see faithful.py)."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    valid: ValidReport
    panel: list[Any]
    faithful: FaithfulReport | None
    clean: bool
    tier: Literal["cheap", "full"]
    # True when a panel member failed for pool reasons: the verdict says nothing about the file.
    # Not clean, not unclean: the scoreboard must exclude it and `score` refuses it.
    inconclusive: bool = False

    @property
    def counts_for_scoreboard(self) -> bool:
        return not self.inconclusive

    @property
    def out_of_scope(self) -> bool:
        """The input has infinite extent (layer 1 `unbounded_geometry`): MEASURED, and not a failure.

        Not clean (nothing certified it), not inconclusive (the checker measured it, so `counts_for_scoreboard` is
        True and the unmeasured rate does not move), and not a repair failure (no mesh was attempted and no repair
        could ever apply). The scoreboard reads this as its own stratum: such a file is kept out of the failing set
        and out of every arm's denominator, the way `no_solids` is, and is counted separately."""
        return bool(self.valid.unbounded)


def _panel_module() -> Any:
    return importlib.import_module("meshpipeline.cad.repair_v4.verdict.panel")


def default_members(tier: Literal["cheap", "full"]) -> list[Any]:
    """Cheap: GmshDefault and GmshProd. Full: the four-mesher panel the clean-STEP definition names - gmsh prod FEA,
    gmsh defaults, gmsh alt and Netgen-via-OCC. Netgen is the only non-gmsh member, so it is what makes a full verdict
    mesher-agnostic.

    WHY GmshProd IS IN THE CHEAP TIER (lane B8). The cheap tier ranks search states and judges each staged-healer
    transaction. With GmshDefault alone it was blind to the member that failed most: in snapshot v1, 11 of 11 search
    states it passed failed the full panel, every one on gmsh_prod alone (low_quality at min SICN < 0.01, or HXT 3D
    failing outright). Re-judged natively, layer 1 + GmshDefault agreed with the full panel on 6 of 22 states, and
    with GmshProd added on 21 of 22; on v1's 38 inputs, 35 of 38 against 38 of 38. Neither Netgen nor GmshAlt
    would have caught one of them, and no GmshDefault SICN threshold separates them (GmshProd fails where
    GmshDefault reads 0.178 and passes where it reads 0.021). Cost: GmshProd's own meshing, ~1.1 s mean and 2.8 s
    worst on those states, against ~0.3 s for GmshDefault and ~4.7 s for the four members of a full verdict.
    The members are the FULL panel's own, configured identically, so a cheap failure is always a full failure."""
    members = importlib.import_module("meshpipeline.cad.repair_v4.verdict.gmsh_members")
    if tier == "cheap":
        return [members.GmshDefault(), members.GmshProd()]
    netgen = importlib.import_module("meshpipeline.cad.repair_v4.verdict.netgen_member")
    return [members.GmshProd(), members.GmshDefault(), members.GmshAlt(), netgen.NetgenOCC()]


def adaptive_cheap_members(start: Any) -> list[Any]:
    """The cheap tier for a search or staged run that starts from a state with FULL verdict `start`: the static cheap
    members, plus every full-panel member `start` does not pass. A cheap verdict then sees every member already
    failing at the start (lane D7: v1's cheap passes were vacuous - gmsh_default passed at the start, so the cheap
    tier passed before any op acted), and still sees gmsh_prod on a start that passes everything (a stage can
    break it: B5). Members are the full panel's own, configured identically; a name the full panel does not have is
    ignored. `start` None gives the static cheap tier."""
    chosen = list(default_members("cheap"))
    failing = {str(r.name) for r in getattr(start, "panel", None) or () if str(r.status) != "pass"}
    have = {m.name for m in chosen}
    chosen += [m for m in default_members("full") if m.name in failing and m.name not in have]
    return chosen


def clean_of(
    *, tier: str, inconclusive: bool, valid: Any, panel: Sequence[Any], faithful: FaithfulReport | None
) -> bool:
    """The ONE rule for `Verdict.clean`: layer 1 VALID and layer 2 PANEL, and nothing else.

    OWNER DECISION 2026-11-24 (supersedes design section 7 items 1-3): clean = valid + panel. Layer 3 (faithful)
    is still measured and reported on every row - deviation, area and volume ratios, bbox ratio, body counts - and
    it NEVER gates clean and never refuses an edit. `faithful` stays in this signature so every caller keeps
    handing it over and nothing silently stops reporting it; the rule ignores it.

    A cheap verdict ran a partial panel, so it can never certify a file clean; an inconclusive one is neither
    clean nor unclean. A clean output that moved the part a long way is flagged for the owner by report.py
    (`gaming_flags`), not refused here."""
    return (
        tier == "full"
        and not inconclusive
        and bool(valid.ok)
        and bool(panel)
        and all(r.status == "pass" for r in panel)
    )


def _assemble(
    pool: WorkerPool,
    step: str,
    original: str,
    declared: Declared,
    tier: Literal["cheap", "full"],
    members: Sequence[Any] | None,
    cache: VerdictCache | None,
    run_panel: PanelRunner | None,
    original_units: str | None,
) -> Verdict:
    t = time.monotonic()
    valid = check_valid(pool, step, original_units=original_units)
    _phase("valid", time.monotonic() - t)
    if valid.unbounded:  # outside the definition, decided before anything meshes it: no panel, no layer 3, no cache
        return Verdict(valid, [], None, False, tier, False)

    chosen = list(members) if members is not None else default_members(tier)
    runner = run_panel or _panel_module().run_panel
    sha = config = None
    panel: list[Any] | None = None
    # the doomed cap (lane C16), BOTH tiers, for a runner that takes it; a substituted runner (tests) runs as before.
    # The cheap tier too: the staged healer and the search judge with every member the start fails (adaptive), and
    # without the cap one hanging member cost 300 s + its 600 s retry on EVERY stage of a panel already doomed in
    # seconds (695d74ca7fa6: 2,575 of 2,636 staged s). On the stored cheap-only panels the cap loses 0 passes.
    takes_cap = "doomed_cap_s" in inspect.signature(runner).parameters
    cap = {"doomed_cap_s": DOOMED_CAP_S} if takes_cap else {}
    # D23.2: P (parallel panel members) is PER CALL: a full verdict (input, confirm, output) meshes its members at
    # once, up to CRS_V4_PANEL_THREADS; a cheap verdict (the search's inner loop, where candidates already run in
    # parallel) stays serial. P changes only WHEN members run, so it is NOT part of the cache key (`cap` is).
    threads = {"panel_threads": full_panel_threads()} if (
        tier == "full" and "panel_threads" in inspect.signature(runner).parameters) else {}
    # A16: WHAT MAY THIS PANEL ASK FOR? `run_panel`'s worst case is (members + 2) x timeout_s, and a call longer
    # than the wall's remainder cannot be stopped by a check that happens after it (measured: one 929.95 s panel
    # against a 900 s search wall). So the per-member timeout comes from what is LEFT, and a panel with no useful
    # time left raises `TooLateToStart` rather than guaranteeing an overrun.
    try:
        member_timeout, clipped = clip_member_timeout(len(chosen), MEMBER_TIMEOUT_S)
    except TooLateToStart as why:
        # Translated to the exception every caller ALREADY treats as infrastructure rather than a verdict: the
        # search records the candidate inconclusive and stops on its wall, the healer's transaction rolls back,
        # and `repair_v4` reports a file it did not measure. A part must never be called bad because a clock ran
        # out. (Translated here, not raised as CheckerUnavailable from `wall`, so `wall` stays import-free.)
        raise CheckerUnavailable(f"not starting a panel on {step!r}: {why}") from why
    if cache is not None and not clipped:
        t = time.monotonic()
        try:
            sha = file_sha256(step)
        except OSError as exc:  # a STEP that is not there is a broken export, not a file that failed
            raise CheckerUnavailable(f"cannot read {step!r} to key the verdict cache: {exc}") from exc
        config = panel_config_hash(chosen, MEMBER_TIMEOUT_S, mesher_versions(pool),  # fails closed, loudly
                                   extra=cap or None)
        _phase("key", time.monotonic() - t)
        # D23: ONE unit of a wave meshes a given STEP under a given panel; concurrent arms wait for its panel
        # (VerdictCache.single_flight), bounded by the panel's own worst case
        meshed: list[bool] = []

        def mesh() -> list[Any]:
            meshed.append(True)
            t_mesh = time.monotonic()
            try:
                return list(runner(pool, step, chosen, timeout_s=member_timeout, **cap, **threads))
            finally:
                _phase("panel", time.monotonic() - t_mesh)

        try:
            panel = cache.single_flight(sha, config, mesh, lease_s=lease_seconds(len(chosen)))
        except Exception as exc:  # noqa: BLE001 - a STORE failure is a miss, never a verdict and never a crash
            if meshed:
                raise  # the meshing itself failed: once, as without a cache - never meshed a second time
            logger.warning("verdict cache unusable, meshing instead: %s: %s", type(exc).__name__, exc)
            cache.note_error()
            panel = None
        else:
            # B23: a panel the cache (or another unit's single flight) supplied, so this unit meshed nothing
            if not meshed:
                _phase("panel_cache_hit")
    if panel is None:
        # A CLIPPED PANEL IS NEVER STORED (LEAD-3's cache rule). It is a weaker measurement than the key promises:
        # `panel_config_hash` carries MEMBER_TIMEOUT_S, so storing a 40 s-per-member panel under the 300 s key would
        # hand every later reader a fail that is about the clock. Clipped panels therefore skip the cache entirely -
        # no read, no single-flight claim, no store - and the key keeps meaning exactly what it says.
        t_mesh = time.monotonic()
        panel = list(runner(pool, step, chosen, timeout_s=member_timeout, **cap, **threads))
        _phase("panel", time.monotonic() - t_mesh)
        if clipped:
            logger.info("panel on %s ran CLIPPED at %.1f s per member (%.1f s of wall left) and is NOT cached",
                        step, member_timeout, remaining_s() or -1.0)

    # Layer 3 is REPORTED, never a gate (`clean_of`), so it is measured on every row it can be measured on -
    # including one that failed layer 1, where the fidelity numbers still say what the repair did to the part.
    # It can also be unmeasurable (an unreadable or extent-less shape); that is not an error and not a verdict.
    #
    # B23, THE ONE EXCEPTION, and it changes no decision: on the CHEAP tier - the search's inner loop, hundreds
    # of candidates per file - an INVALID state's layer 3 is read by nobody. `score` zeroes keys 3 and 4 while
    # layer 1 fails (see its docstring: that zeroing is lane A's frontier fix), `clean_of` is already False, and
    # no row ever carries a cheap verdict's fidelity numbers - a row carries the delivered state's FULL verdict.
    # So the measurement is computed and discarded. It is skipped here and counted as skipped.
    faithful: FaithfulReport | None = None
    if tier == "cheap" and not valid.ok:
        _phase("faithful_skipped")
    else:
        t = time.monotonic()
        try:
            faithful = check_faithful_steps(
                pool, original, step, declared_added=declared.added, declared_removed=declared.removed
            )
        except (CheckerUnavailable, DeclaredIdError, ValueError) as why:
            logger.info("layer 3 not measured for %s (reported as unmeasured, it gates nothing): %s", step, why)
        _phase("faithful", time.monotonic() - t)

    inconclusive = any(is_infrastructure_failure(r) for r in panel)
    if clipped:
        # A clipped panel cannot certify: a member that did not finish in a clipped slot says nothing about the
        # part, so the verdict is INCONCLUSIVE rather than a fail. `clean_of` already refuses an inconclusive
        # verdict, and `score` raises on one, so nothing ranks or ships on the strength of a clock.
        inconclusive = inconclusive or any(getattr(r, "status", "pass") != "pass" for r in panel)

    clean = clean_of(tier=tier, inconclusive=inconclusive, valid=valid, panel=panel, faithful=faithful)
    return Verdict(valid, panel, faithful, clean, tier, inconclusive)


def full_panel_threads() -> int:
    """CRS_V4_PANEL_THREADS (the cap the wave sizes its pool by: run_arm.pool_size), 1 = the serial panel."""
    import os

    try:
        return max(1, int(os.environ.get("CRS_V4_PANEL_THREADS", "1")))
    except ValueError:
        return 1


def lease_seconds(n_members: int) -> float:
    """How long a single-flight lease lives (D23): the panel's worst case run serially - every member to its
    timeout and its one retry at twice the budget - plus slack. A claimer that dies frees the file after this."""
    return n_members * MEMBER_TIMEOUT_S * 3 + 120.0


def _cache_get(cache: VerdictCache, sha: str, config: str) -> list[Any] | None:
    """A cache that cannot answer (a store outage) is a miss, never a verdict and never a crash."""
    try:
        return cache.get(sha, config)
    except Exception as exc:  # noqa: BLE001 - any store failure
        logger.warning("verdict cache unreadable, meshing instead: %s: %s", type(exc).__name__, exc)
        cache.note_error()
        return None


def _cache_put(cache: VerdictCache, sha: str, config: str, panel: Sequence[Any]) -> None:
    try:
        cache.put(sha, config, panel)
    except Exception as exc:  # noqa: BLE001 - the measurement stands; only its memo is lost
        logger.warning("verdict cache write failed: %s: %s", type(exc).__name__, exc)
        cache.note_error()


def verdict(
    pool: WorkerPool,
    step: str,
    original: str,
    *,
    declared: Declared | None = None,
    members: Sequence[Any] | None = None,
    cache: VerdictCache | None = None,
    run_panel: PanelRunner | None = None,
    original_units: str | None = None,
) -> Verdict:
    """The full verdict on `step`, repaired from `original` (the STEP the product was handed; for an
    injected benchmark file the DAMAGED input, never the clean source)."""
    return _assemble(pool, step, original, declared or Declared(), "full", members, cache, run_panel, original_units)


def cheap_verdict(
    pool: WorkerPool,
    step: str,
    original: str,
    *,
    declared: Declared | None = None,
    members: Sequence[Any] | None = None,
    cache: VerdictCache | None = None,
    run_panel: PanelRunner | None = None,
    original_units: str | None = None,
) -> Verdict:
    """Layer 1, the cheap members (`GmshDefault`, `GmshProd`; `default_members("cheap")`) and layer 3. For the
    search's inner loop and the staged healer's transactions; never `clean`."""
    return _assemble(pool, step, original, declared or Declared(), "cheap", members, cache, run_panel, original_units)


def with_brep_orientation(pool: Any, brep_path: str, v: Verdict) -> Verdict:
    """`v` made INVALID when the BREP it judged holds a solid of negative signed volume (lane C15). Layer 1 judges the
    STEP re-export, whose round trip re-orients an inside-out solid (dev 0051_00510567: -13.46 mm3 read back as
    +13.46), so the finding has to be made on the BREP, before the export hides it. Same code as layer 1's own:
    `non_positive_volume`, with `seen_on: brep`, and layer 1's own threshold (<= 0). A check that could not run (pool
    failure, the 120 s timeout) leaves validity as it was but is NOT silent: soft `orientation_unchecked` with why."""
    from meshpipeline.cad.repair_v4.verdict.valid import Finding

    def unchecked(why: str) -> Verdict:
        return replace(v, valid=replace(v.valid, soft=[*v.valid.soft, Finding("orientation_unchecked", [],
                                                                              {"reason": why[:200]})]))

    try:
        got = pool.call("valid_signed_volumes", {"brep_path": brep_path}, timeout_s=120.0)
    except Exception as exc:  # noqa: BLE001 - a pool that cannot answer says nothing about the part
        return unchecked(f"{type(exc).__name__}: {exc}")
    if not got.ok or got.value is None:
        return unchecked(f"{got.status}: {got.error}")
    neg = [(i, float(x)) for i, x in enumerate(got.value.get("volumes") or [], start=1) if float(x) <= 0.0]
    if not neg:
        return v
    found = [Finding("non_positive_volume", [f"S{i}"], {"volume": x, "seen_on": "brep (before the STEP export)"})
             for i, x in neg]
    return replace(v, valid=replace(v.valid, ok=False, hard=[*v.valid.hard, *found]), clean=False)


def score(v: Verdict) -> tuple[int, int, float, float, int]:
    """(valid, panel passes, min SICN over members, -max deviation mm, -topology churn), compared
    lexicographically.

    KEY 0 IS LAYER 1 ALONE (owner decision 2026-11-24). It used to be "valid AND faithful", which made the
    search refuse an edit that moved the part: an unfaithful state tied with an invalid one. Layer 3 now only
    breaks ties - among states that are equally valid, equally meshable and equally good in SICN, the one that
    moved the part least still wins (keys 3 and 4) - so fidelity is preferred, never required.

    LAYER 3 RANKS ONLY VALID STATES (keys 3 and 4 are 0 while layer 1 fails). It is a TIE-BREAK, and an invalid
    state has nothing to tie: on the cheap tier an unfinished repair scores (0, 0, UNMEASURED) whatever it did, so
    deviation was the only key left and became the whole order - and because every op moves the part, it ranked
    the state that repaired LESS above the state that repaired more. Measured: the three-hole part (14 ops to
    clean, `test_native_repair_v4_waste`) stopped reaching clean at all, because filling the third hole deviates
    1.5 mm against 1.42 mm for leaving it open, so the frontier never expanded the filled state and `make_volume`
    was never proposed. Zeroing both keys while invalid restores the only honest order between unfinished states
    (the order they were proposed in) and keeps fidelity deciding where it has meaning: among finished, valid,
    equally meshable candidates. It also stops layer 3 refusing a staged edit through `stages` keep/rollback,
    which compares the same tuple - the decision says fidelity never refuses an edit.

    Raises `InconclusiveVerdict` for an inconclusive verdict: it must be re-run or dropped, never
    ranked (and never ranked as bad). Check `Verdict.counts_for_scoreboard` first.
    """
    if v.inconclusive:
        raise InconclusiveVerdict("a panel member failed for pool reasons; the verdict says nothing about the file")
    sicns = [r.min_sicn if r.min_sicn is not None else UNMEASURED_SICN for r in v.panel]
    dev = v.faithful.max_dev_mm if v.faithful is not None else 0.0
    churn = len(v.faithful.declared_added) + len(v.faithful.declared_removed) if v.faithful is not None else 0
    valid_ok = int(bool(v.valid.ok))
    return (
        valid_ok,
        sum(1 for r in v.panel if r.status == "pass"),
        min(sicns) if sicns else UNMEASURED_SICN,
        -dev if valid_ok else 0.0,
        -churn if valid_ok else 0,
    )
