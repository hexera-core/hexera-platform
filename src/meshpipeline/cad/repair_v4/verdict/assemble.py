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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from meshpipeline.cad.repair_v4.verdict.cache import VerdictCache, file_sha256, panel_config_hash
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport, check_faithful_steps
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport, check_valid
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

MEMBER_TIMEOUT_S = 300.0
SICN_FLOOR = 0.01
UNMEASURED_SICN = -1.0  # a mesher that produced no mesh has no SICN; it ranks below any real one


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


def _panel_module() -> Any:
    return importlib.import_module("meshpipeline.cad.repair_v4.verdict.panel")


def default_members(tier: Literal["cheap", "full"]) -> list[Any]:
    """Cheap: GmshDefault only. Full: every member the panel defines (Task 4 adds Netgen here)."""
    members = importlib.import_module("meshpipeline.cad.repair_v4.verdict.gmsh_members")
    if tier == "cheap":
        return [members.GmshDefault()]
    return [members.GmshProd(), members.GmshDefault(), members.GmshAlt()]


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
    valid = check_valid(pool, step, original_units=original_units)

    chosen = list(members) if members is not None else default_members(tier)
    runner = run_panel or _panel_module().run_panel
    sha = config = None
    panel: list[Any] | None = None
    if cache is not None:
        sha, config = file_sha256(step), panel_config_hash(chosen, MEMBER_TIMEOUT_S)
        panel = cache.get(sha, config)
    if panel is None:
        panel = list(runner(pool, step, chosen, timeout_s=MEMBER_TIMEOUT_S))
        if cache is not None and sha is not None and config is not None:
            cache.put(sha, config, panel)

    faithful: FaithfulReport | None = None
    if valid.ok:  # an unreadable or solid-less repaired file has nothing to compare
        faithful = check_faithful_steps(
            pool, original, step, declared_added=declared.added, declared_removed=declared.removed
        )

    # A cheap verdict ran a partial panel, so it can never certify a file clean.
    clean = (
        tier == "full"
        and valid.ok
        and bool(panel)
        and all(r.status == "pass" for r in panel)
        and faithful is not None
        and faithful.ok
    )
    return Verdict(valid, panel, faithful, clean, tier)


def verdict(
    pool: WorkerPool,
    step: str,
    original: str,
    *,
    declared: Declared = Declared(),
    members: Sequence[Any] | None = None,
    cache: VerdictCache | None = None,
    run_panel: PanelRunner | None = None,
    original_units: str | None = None,
) -> Verdict:
    """The full verdict on `step`, repaired from `original` (the STEP the product was handed; for an
    injected benchmark file the DAMAGED input, never the clean source)."""
    return _assemble(pool, step, original, declared, "full", members, cache, run_panel, original_units)


def cheap_verdict(
    pool: WorkerPool,
    step: str,
    original: str,
    *,
    declared: Declared = Declared(),
    members: Sequence[Any] | None = None,
    cache: VerdictCache | None = None,
    run_panel: PanelRunner | None = None,
    original_units: str | None = None,
) -> Verdict:
    """Layer 1, `GmshDefault` only, and layer 3. For the search's inner loop; never `clean`."""
    return _assemble(pool, step, original, declared, "cheap", members, cache, run_panel, original_units)


def score(v: Verdict) -> tuple[int, int, float, float, int]:
    """(valid, panel passes, min SICN over members, -max deviation mm, -topology churn), compared
    lexicographically: validity outranks any amount of meshing, meshing outranks fidelity, and
    churn (the declared edits) breaks the last tie."""
    sicns = [r.min_sicn if r.min_sicn is not None else UNMEASURED_SICN for r in v.panel]
    dev = v.faithful.max_dev_mm if v.faithful is not None else 0.0
    churn = len(v.faithful.declared_added) + len(v.faithful.declared_removed) if v.faithful is not None else 0
    return (
        int(v.valid.ok),
        sum(1 for r in v.panel if r.status == "pass"),
        min(sicns) if sicns else UNMEASURED_SICN,
        -dev,
        -churn,
    )
