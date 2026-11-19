# Responsibility: RUN one rung of the verification ladder and report what it answered - the
#                 composition `oracle.py` deliberately is not.
# Owns: which probe serves each rung, how a rung's hard cap bounds the budget a caller offers,
#       and how a probe's defects become a three-valued verdict (passed / failed / no answer).
# Boundaries: it decides nothing about WHEN to ask - that is the caller's policy, read out of
#             `oracle`. It never raises, it starts no process the rung's policy does not allow,
#             and it imports no mesher at module level: gmsh lives in `mesh_probe`'s child and
#             fTetWild in `tolerant_probe`'s, never in this process. Tier 3 (the product mesher)
#             is refused here, so the repair loop cannot reach the big machines through it.
# Collaborates with: cad/repair/oracle.py (the tier table and its ceilings), cad/repair/caps.py
#                    (tier 0), cad/repair/mesh_probe.py (tiers 1 and 2),
#                    cad/repair/tolerant_probe.py (tier 2b, when the deployment has it),
#                    cad/repair/investigator_tools.py and cad/repair/session.py (callers).
from __future__ import annotations

import importlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import caps, mesh_probe, oracle
from meshpipeline.cad.repair.regions import Region

logger = logging.getLogger(__name__)

#: The rungs a caller may ask for, in ladder order, TAKEN FROM THE POLICY rather than restated.
#: A tier whose `in_loop` is False (tier 3, the product mesher) is not here and is refused.
RUNGS: tuple[str, ...] = tuple(tier.name for tier in oracle.TIERS if tier.in_loop)
_TIER_BY_NAME = {tier.name: tier for tier in oracle.TIERS}

#: The on-value of the tier-2b posture. Anything else - unset included - reads off, and tier 2b
#: then answers `not_asked` without writing a file or starting a process (fail closed).
_TOLERANT_ON = "live"
_TOLERANT_DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class TierResult:
    """What one rung answered.

    `passed` IS THREE-VALUED, and that is the whole point of the type. True is "this rung is
    satisfied", False is "this rung refused the part", and None is "no answer": the rung was not
    asked, could not be asked, timed out, or died. A None read as a False would mark a part
    unmeshable on the strength of a timeout, which is the one mistake the ladder must not make.
    """

    tier: str
    passed: bool | None
    seconds: float
    defects: tuple = ()
    detail: dict = field(default_factory=dict)


def _capped(budget_s: float, cap_s: float | None) -> float:
    """`budget_s` under a rung's hard cap. A rung with no cap gets what the caller offered."""
    budget = max(0.0, float(budget_s))
    return budget if cap_s is None else min(budget, float(cap_s))


def _verdict(defects: tuple) -> bool | None:
    """A probe's defects as a verdict. Only no-answer codes mean the mesher was never heard
    from, and a mix of a real defect with a timeout is still a real defect."""
    if not defects:
        return True
    codes = {getattr(d, "code", None) for d in defects}
    return None if codes <= set(mesh_probe.NO_ANSWER_CODES) else False


def _write_brep(shape, path: Path) -> None:
    """Write `shape` where a probe child can read it. A SEAM: the ABI rule is that a probe reads
    a file, never an OCP pointer across a process boundary."""
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))


def _tolerant_live() -> bool:
    return str(getattr(rcfg, "CAD_REPAIR_TOLERANT_PROBE", "off") or "off") == _TOLERANT_ON


def _tolerant_timeout_s() -> float:
    return float(getattr(rcfg, "CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S",
                         _TOLERANT_DEFAULT_TIMEOUT_S))


def run_tier(shape, tier: str, *, diagonal_mm: float, within: Region | None = None,
             budget_s: float) -> TierResult:
    """Ask rung `tier` of the ladder about `shape`, within `budget_s` seconds. NEVER RAISES.

    - `"0"` - kernel checks: `caps.measure`. Starts no process and needs no budget.
    - `"1"` - the scoped surface probe. `within` is REQUIRED: a scoped probe with no region would
      silently become a whole-part probe at tier 1's (uncapped) budget, so a None is no answer.
    - `"2"` - the whole part, under tier 2's hard cap. Any `within` the caller held is ignored by
      design: this rung's question is about the part.
    - `"2b"` - the coarse tolerant-mesh pass, behind its posture flag. Off is `not_asked`, and
      nothing is written and nothing spawned.
    - anything else, tier 3 included, is no answer: the product mesher is not the loop's to
      start, and an unknown rung must not be guessed at (the cheapest would be wrong as often as
      the dearest).
    """
    started = time.monotonic()

    def done(passed: bool | None, defects: tuple = (), **detail) -> TierResult:
        return TierResult(str(tier), passed, time.monotonic() - started, tuple(defects), detail)

    rung = _TIER_BY_NAME.get(str(tier))
    if rung is None:
        return done(None, error="unknown tier")
    if not rung.in_loop:
        return done(None, error=f"tier {rung.name} is not in the loop")
    try:
        if rung.name == "0":
            measured = caps.measure(shape)
            return done(bool(measured.valid), **measured.to_dict())
        if rung.name == "1":
            if within is None:
                return done(None, error="tier 1 needs a region")
            defects = tuple(mesh_probe.mesh_defects(
                shape, diagonal_mm=float(diagonal_mm), within=within,
                budget_s=_capped(budget_s, rung.hard_cap_s)))
            return done(_verdict(defects), defects, scope="region",
                        region_index=int(within.index))
        if rung.name == "2":
            defects = tuple(mesh_probe.mesh_defects(
                shape, diagonal_mm=float(diagonal_mm), within=None,
                budget_s=_capped(budget_s, rung.hard_cap_s)))
            # The element cap is a QUALITY signal read after the run, never a failure. The
            # defect-level probe reports no element count, so the honest answer is None - a 0
            # would read as a measured "well under the cap".
            return done(_verdict(defects), defects, scope="part",
                        element_cap=rung.element_cap, over_element_cap=None)
        return _tolerant(shape, diagonal_mm=diagonal_mm, budget_s=budget_s, done=done)
    except Exception as exc:  # noqa: BLE001 - a rung that could not be asked is no answer
        logger.info("ladder: tier %s gave no answer (%s: %s)", tier, type(exc).__name__, exc)
        return done(None, error=type(exc).__name__)


def _tolerant(shape, *, diagonal_mm: float, budget_s: float, done) -> TierResult:
    """Tier 2b: would the tolerant rung eat this? Flag-off answers without spending anything."""
    if not _tolerant_live():
        return done(None, verdict="not_asked")
    try:
        # Imported by NAME, not by `from ... import`: the tolerant probe is Task 4's module and a
        # deployment may not carry it, so "is it installed?" has to be a question, not an import.
        tolerant_probe = importlib.import_module("meshpipeline.cad.repair.tolerant_probe")
    except ImportError:
        # A deployment without the tolerant probe installed is a CAPABILITY answer, not a
        # failure: the routing question simply cannot be asked here.
        logger.info("ladder: tier 2b was asked for but the tolerant probe is not installed")
        return done(None, verdict="unavailable")
    budget = _capped(budget_s, _tolerant_timeout_s())
    with tempfile.TemporaryDirectory(prefix="repair-tier2b-") as scratch:
        path = Path(scratch) / "shape.brep"
        _write_brep(shape, path)
        answer = tolerant_probe.tolerant_probe(path, diagonal_mm=float(diagonal_mm),
                                               budget_s=budget)
    verdict = getattr(answer, "verdict", answer)
    passed = True if verdict == tolerant_probe.EATS else (
        False if verdict == tolerant_probe.REFUSES else None)
    return done(passed, verdict=str(verdict))
