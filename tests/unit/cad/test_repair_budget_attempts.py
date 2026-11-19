# Responsibility: Pin the per-part attempt allowance - the tier table, its fail-closed default, the
#                 explicit override, the flag-off guarantee, and that the outer bounds still bind.
# Boundaries: no kernel. The session, the author and the clock are stubs, so what is asserted here
#             is the ARITHMETIC and the posture, over tables of values rather than single points.
from __future__ import annotations

from dataclasses import dataclass

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import grind, region_loop
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.mesh_probe import (
    OPEN_LOOP,
    OVERLAPPING_FACETS,
    STAGE_2D,
    STAGE_3D,
    TIMEOUT,
)

# --- the table itself ------------------------------------------------------------------------


def test_the_attempts_table_is_the_three_tiers_and_nothing_else():
    assert set(grind.ATTEMPTS) == {grind.CHEAP, grind.STANDARD, grind.HEAVY}
    # PINNED LITERALLY, both the values and their order, so a mutated constant is caught here and
    # not only through a loop that might be reading the same mutation.
    assert grind.ATTEMPTS[grind.CHEAP] == 3
    assert grind.ATTEMPTS[grind.STANDARD] == 6
    assert grind.ATTEMPTS[grind.HEAVY] == 10
    assert (grind.ATTEMPTS[grind.CHEAP] < grind.ATTEMPTS[grind.STANDARD]
            < grind.ATTEMPTS[grind.HEAVY])
    # CHEAP IS THE FLOOR, which is what makes `attempts_for`'s fallback fail-closed rather than
    # merely arbitrary. Without this, reordering the table would silently make UNKNOWN generous.
    assert grind.ATTEMPTS[grind.CHEAP] == min(grind.ATTEMPTS.values())
    # CHEAP KEEPS THE COMMITTED FLAT DEFAULT, so the cheapest tier changes nothing.
    assert grind.ATTEMPTS[grind.CHEAP] == int(rcfg.CAD_REPAIR_REGION_ATTEMPTS)


@pytest.mark.parametrize(("tier", "expected"), [
    (grind.CHEAP, 3),
    (grind.STANDARD, 6),
    (grind.HEAVY, 10),
    # FAIL-CLOSED, over everything that is not a tier: the smallest budget, never a guess.
    (grind.UNKNOWN, 3),
    ("", 3),
    ("Heavy", 3),
    ("gigantic", 3),
    (None, 3),
    (10, 3),
    (True, 3),
    (object(), 3),
])
def test_attempts_for_is_a_table_and_fails_closed(tier, expected):
    assert grind.attempts_for(tier) == expected


# --- the tier a part's defects buy ------------------------------------------------------------


@dataclass(frozen=True)
class _Defect:
    code: object
    severity: object
    measurements: dict
    fingerprint: str = "f1"


def _mesh_defect(kind: str, stage: str, severity=DefectSeverity.error) -> _Defect:
    from meshpipeline.cad.repair.mesh_probe import MESH_CODES

    return _Defect(code=sorted(MESH_CODES, key=str)[0], severity=severity,
                   measurements={"mesh_kind": kind, "stage": stage})


@pytest.mark.parametrize(("kind", "stage", "faces", "tier", "attempts"), [
    # A VOLUME-STAGE OVERLAP IS STANDARD: 6.
    (OVERLAPPING_FACETS, STAGE_3D, 139, grind.STANDARD, 6),
    (OVERLAPPING_FACETS, STAGE_3D, 1, grind.STANDARD, 6),
    # A SURFACE-STAGE OPEN LOOP IS CHEAP: 3.
    (OPEN_LOOP, STAGE_2D, 138, grind.CHEAP, 3),
    (OPEN_LOOP, STAGE_2D, 999, grind.CHEAP, 3),
    # NEVER ANSWERED, OR VERY LARGE, IS HEAVY: 10. Two-sided around HEAVY_FACES.
    (TIMEOUT, STAGE_3D, 10, grind.HEAVY, 10),
    (OVERLAPPING_FACETS, STAGE_3D, grind.HEAVY_FACES, grind.STANDARD, 6),
    (OVERLAPPING_FACETS, STAGE_3D, grind.HEAVY_FACES + 1, grind.HEAVY, 10),
    (OPEN_LOOP, STAGE_2D, grind.HEAVY_FACES, grind.CHEAP, 3),
    (OPEN_LOOP, STAGE_2D, grind.HEAVY_FACES + 1, grind.HEAVY, 10),
    # NO FACE COUNT IS UNKNOWN, AND UNKNOWN READS AS THE SMALLEST: 3.
    (OVERLAPPING_FACETS, STAGE_3D, None, grind.UNKNOWN, 3),
])
def test_attempts_follow_the_grind_tier(monkeypatch, kind, stage, faces, tier, attempts):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    record = region_loop.attempt_budget([_mesh_defect(kind, stage)], solids=1, faces=faces)
    assert record == {"tier": tier, "attempts": attempts,
                      "rule": region_loop.BUDGET_RULE_TIER}


def test_unknown_tier_reads_cheap(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    record = region_loop.attempt_budget([_mesh_defect(OVERLAPPING_FACETS, STAGE_3D)],
                                        solids=1, faces=None)
    assert record["tier"] == grind.UNKNOWN
    assert record["attempts"] == grind.ATTEMPTS[grind.CHEAP] == 3
    # AND IT IS THE SMALLEST OF THE THREE, not merely equal to a number.
    assert record["attempts"] == min(grind.ATTEMPTS.values())


@pytest.mark.parametrize("faces", [
    float("inf"), float("-inf"), float("nan"), -1, -1.0, "139", object(), True, False,
])
def test_a_non_finite_or_nonsense_face_count_is_unknown_not_a_number(monkeypatch, faces):
    """A `-inf`/`NaN` sailing through a threshold comparison would answer HEAVY or STANDARD for a
    part nobody measured. It must read as UNKNOWN, which is the cheapest budget."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    record = region_loop.attempt_budget([_mesh_defect(OVERLAPPING_FACETS, STAGE_3D)],
                                        solids=1, faces=faces)
    assert record == {"tier": grind.UNKNOWN, "attempts": 3,
                      "rule": region_loop.BUDGET_RULE_TIER}


@pytest.mark.parametrize(("given", "expected"), [
    # THE COUNTS THAT ARE COUNTS, two-sided around zero.
    (0, 0), (1, 1), (139, 139), (1000, 1000), (1001, 1001), (12.0, 12), (12.9, 12),
    # ... AND EVERYTHING THAT IS NOT ONE. None is what `grind` reads as UNKNOWN.
    (None, None), (-1, None), (-1.0, None), (-0.5, None),
    (float("inf"), None), (float("-inf"), None), (float("nan"), None),
    ("139", None), (object(), None),
    # A BOOL IS NOT A FACE COUNT, however much `isinstance(True, int)` says it is.
    (True, None), (False, None),
])
def test_the_count_guard_is_two_sided_and_rejects_the_non_finite(given, expected):
    """ASSERTED ON THE GUARD ITSELF and not only through the tier, because `grind.predict` fails
    closed on its own: routing the table through `attempt_budget` alone passes with this guard
    deleted, which is a vacuous safety test. `_count` is the layer that must not hand a `NaN` or
    an `inf` to a threshold comparison, so it is the layer asserted."""
    assert region_loop._count(given) == expected  # noqa: SLF001 - the guard under test


def test_a_predictor_that_throws_still_yields_the_cheapest(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setattr(region_loop.grind, "tier_of",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    record = region_loop.attempt_budget([], solids=1, faces=10)
    assert record == {"tier": grind.UNKNOWN, "attempts": 3,
                      "rule": region_loop.BUDGET_RULE_TIER}


# --- the posture and the explicit override ----------------------------------------------------


@pytest.mark.parametrize("posture", ["off", "", "Live", "live ", "on", "true", "shadow", None])
def test_off_yields_no_record_at_all(monkeypatch, posture):
    """FAIL-CLOSED over a table: only the exact string `live` turns the allowance on, and off
    means NO KEY, which is the whole of the byte-identical-report guarantee."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", posture)
    assert region_loop.budget_attempts_live() is False
    assert region_loop.attempt_budget([_mesh_defect(TIMEOUT, STAGE_3D)],
                                      solids=1, faces=10) == {}


def test_live_is_the_one_string_that_turns_it_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    assert region_loop.budget_attempts_live() is True


@pytest.mark.parametrize(("given", "expected"), [(1, 1), (2, 2), (5, 5), (0, 1), (-7, 1)])
def test_explicit_attempts_still_win(monkeypatch, given, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    record = region_loop.attempt_budget([_mesh_defect(TIMEOUT, STAGE_3D)], solids=1, faces=10,
                                        attempts=given)
    assert record == {"tier": "", "attempts": expected,
                      "rule": region_loop.BUDGET_RULE_EXPLICIT}
    # ... and the tier would have said otherwise, so this really is an override.
    assert region_loop.attempt_budget([_mesh_defect(TIMEOUT, STAGE_3D)],
                                      solids=1, faces=10)["attempts"] == 10


def test_an_allowance_is_never_zero(monkeypatch):
    """A zero would walk every region, author for nothing and report it as exhausted."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setitem(grind.ATTEMPTS, grind.CHEAP, 0)
    assert region_loop.attempt_budget([], solids=1, faces=10)["attempts"] == 1
