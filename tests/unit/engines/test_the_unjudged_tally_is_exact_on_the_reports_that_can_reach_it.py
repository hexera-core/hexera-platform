# Responsibility: Fix, by measurement, the exact boundary of what `layer_floors_unjudged` asserts
# - which floors had no FIGURE, inferred from the figures - and show that it agrees floor for
# floor with what `layers.shortfalls` really evaluated on every report `conjugate.build` can hand
# over.
# Boundaries: SYNTHETIC logs through the shipped parser and the shipped publisher. Nothing here
# meshes, renders, opens a browser or asserts on a threshold.
#
# THE ASYMMETRY THIS PINS, 2026-11-18. `layers.shortfalls` guards the layers-kept floor on
# `asked and mesh_layers is not None`, where `asked` is `n_layers or row["target_layers"]`.
# `layers.floors_unjudged` - which is what `finalize._layers_published` publishes - sees only the
# measured figures, and `n_layers` travels in no artifact. So on a layer table row asking for 0
# layers the two can part, and the direction matters:
#
#     shortfalls called with   floors it evaluated          the tally claims judged   error
#     n_layers 0               2  coverage, thickness       3                         +1
#     n_layers 3               3  + layers kept             3                          0
#
# ONLY THE SECOND ROW CAN REACH THE PANEL, and that is structural, not a coincidence of this
# log: `conjugate.build` builds a `layer_report` at all only inside `if layers > 0` and then
# passes `n_layers=layers`, so `asked` is positive on every report that gets this far, and a plan
# asking for no layers leaves the report {} - which `_layers_published` returns {} for, above the
# tally. NOTHING IN THIS REPO CALLS `shortfalls` WITHOUT `n_layers`: every call in src/ and in
# tests/ passes it (grepped 2026-11-18).
#
# AND THE OBVIOUS PATCH IS WORSE, which is why this round changed no guard. Requiring a POSITIVE
# `layers_asked` for the layers-kept entry of the roster closes the first row and OPENS the
# second: the tally would name "layers kept" unjudged on a report where `shortfalls` judged it
# and said it kept 0.00 of the 3 asked for. An under-claim on the only shape production emits is
# worse than an over-claim on one it cannot. The asymmetry can only be closed where it is made -
# `shortfalls` reporting which floors IT evaluated - which is layers.py and conjugate.py.
#
# A CHARACTERISATION TEST, SAID SO PLAINLY: nothing here changed behaviour this round, so nothing
# here fails against the pre-change tree. It exists so the next round cannot move this boundary
# by accident, and so the table above has somewhere to be checked.
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import layers as LY
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID

#: SYNTHETIC: the plate's own first extrusion line and face count, with a layer table row that
#: ASKS FOR ZERO and grew zero, no cells added, and the illegal-face line present. This is the
#: only shape in which the roster and `shortfalls` can disagree.
ZERO_ASK_LOG = (
    "Extruding 16122330 out of 16582198 faces (97.2267%). Removed extrusion at 0 faces.\n"
    "Added 0 out of 0 cells (0%).\n"
    "patch   faces   layers  overall thickness   [m]     [%]\n"
    "----    -----   ------  ---------           ---     ---\n"
    "fluid_to_solid 16582198 0 0 0 0\n"
    "***Number of illegal faces 90000\n"
    "Finished meshing in = 100 s.\n"
)

FLOOR_NAMES = tuple(name for name, _keys in LY.LAYER_FLOORS)


def _floors_shortfalls_reached(report: dict, *, n_layers: int, cells: int) -> list[str]:
    """Which of `LAYER_FLOORS` `shortfalls` actually evaluated, read off its own four guards.

    SPELLED OUT HERE ON PURPOSE AND NOWHERE ELSE. This is a probe inside one test file, not a
    second home for the rule: the whole finding is that `shortfalls` does not report this and a
    second reader has to infer it, so the inference is written where it can be seen rather than
    shipped. If `shortfalls` ever reports its own roster, this helper goes and the tests below
    read that instead.
    """
    got = dict(report or {})
    row = (got.get("patches") or {}).get(str(FLUID_TO_SOLID)) or {}
    reached: list[str] = []
    if got.get("coverage_pct") is not None:
        reached.append("coverage")
    if row.get("thickness_pct") is not None:
        reached.append("thickness")
    if int(n_layers or row.get("target_layers") or 0) and row.get("mesh_layers") is not None:
        reached.append("layers kept")
    if got.get("illegal_faces") is not None and int(cells) > 0:
        reached.append("faces in error")
    return reached


def _published(log: str, *, n_layers: int) -> tuple[dict, list[str], list[str]]:
    """The payload `finalize` publishes for `log`, beside what `shortfalls` really reached.

    Exactly `conjugate.build`'s own order: read the log, count its cells, compute the shortfalls
    against those cells, write them back only when non-empty, then publish.
    """
    raw = LY.read_layer_log(log)
    cells = int(raw.get("cells_added") or 0)
    short = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID, n_layers=n_layers,
                               cells=cells))
    if short:
        raw["shortfalls"] = short
    reached = _floors_shortfalls_reached(raw, n_layers=n_layers, cells=cells)
    return F._layers_published(raw), reached, short


def test_a_plan_that_asked_for_no_layers_never_reaches_the_tally_at_all():
    """THE STRUCTURAL REASON THE ASYMMETRY CANNOT BE DRIVEN FROM A PLAN. `conjugate.build` leaves
    its `layer_report` {} unless `layers > 0`, and an empty report publishes nothing."""
    assert F._layers_published({}) == {}
    assert F._layers_published(None) == {}       # the report is untrusted JSON, see the publisher


def test_on_the_report_the_builder_can_emit_the_tally_is_exact_floor_for_floor():
    """THE REACHABLE ROW. `n_layers` is `layers` and `layers > 0`, so the layers-kept floor IS
    evaluated even though the table row asks for 0 - and the tally says so.

    MEASURED, SYNTHETIC, 2026-11-18: three floors reached (coverage, thickness, layers kept), one
    named unjudged (faces in error, because 0 cells were added and that floor is a ratio), two
    shortfall sentences.
    """
    pub, reached, short = _published(ZERO_ASK_LOG, n_layers=3)
    assert reached == ["coverage", "thickness", "layers kept"], reached
    assert pub["layer_floors_unjudged"] == ["faces in error"], pub["layer_floors_unjudged"]
    assert pub["layer_floors_total"] == len(FLOOR_NAMES) == 4
    claimed = len(FLOOR_NAMES) - len(pub["layer_floors_unjudged"])
    assert claimed == len(reached), (
        f"the panel would say {claimed} of {len(FLOOR_NAMES)} floors were judged where "
        f"{len(reached)} were: {reached}")
    assert len(short) == 2 and any("kept 0.00 layers of the 3" in s for s in short), short


def test_the_only_report_the_two_disagree_on_is_one_no_caller_produces():
    """THE UNREACHABLE ROW, pinned so the claim "it cannot happen" has a measurement under it.

    With `n_layers` left at its default the layers-kept floor is skipped for a reason the tally
    cannot see, and the tally over-claims by exactly one floor. Every call to `shortfalls` in
    src/ and tests/ passes `n_layers`, so this is reachable only by a caller that does not exist.
    """
    pub, reached, _short = _published(ZERO_ASK_LOG, n_layers=0)
    assert reached == ["coverage", "thickness"], reached
    claimed = len(FLOOR_NAMES) - len(pub["layer_floors_unjudged"])
    assert claimed - len(reached) == 1, (
        "the over-claim is exactly one floor - the layers-kept one - and no more")
    assert "layers kept" not in pub["layer_floors_unjudged"]


def test_requiring_a_positive_ask_in_the_roster_would_break_the_reachable_row():
    """WHY NO GUARD MOVED. The patch that closes the unreachable row opens the reachable one.

    MEASURED, SYNTHETIC, 2026-11-18. Simulated here rather than applied: the roster entry for
    "layers kept" additionally requires a positive `layers_asked`. On the builder's own report
    that names a floor unjudged which `shortfalls` judged AND reported a shortfall for, so the
    panel would hide a real miss behind "nobody judged it".
    """
    def patched(grown: dict) -> list[str]:
        out = list(LY.floors_unjudged(grown))
        asked = grown.get("layers_asked")
        ok = isinstance(asked, (int, float)) and not isinstance(asked, bool) and float(asked) > 0
        if "layers kept" not in out and not ok:
            out.append("layers kept")
        return [n for n in FLOOR_NAMES if n in out]

    raw = LY.read_layer_log(ZERO_ASK_LOG)
    cells = int(raw.get("cells_added") or 0)
    grown = LY.measured(raw, interface_patch=FLUID_TO_SOLID)
    assert grown.get("layers_asked") == 0, "the premise: the table row asks for zero"
    short = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID, n_layers=3, cells=cells))

    assert "layers kept" not in LY.floors_unjudged(grown), (
        "today the tally agrees with what shortfalls reached on this report")
    assert "layers kept" in patched(grown), "the premise of the simulation"
    assert any("kept 0.00 layers of the 3" in s for s in short), (
        "and shortfalls DID judge that floor and said it missed, so naming it unjudged would "
        "contradict a sentence in the same payload")


@pytest.mark.parametrize("n_layers", [1, 2, 3, 8])
def test_every_positive_ask_gives_the_same_exact_tally(n_layers):
    """The reachable row is not a property of 3. SYNTHETIC."""
    pub, reached, _ = _published(ZERO_ASK_LOG, n_layers=n_layers)
    claimed = len(FLOOR_NAMES) - len(pub["layer_floors_unjudged"])
    assert claimed == len(reached) == 3, (claimed, reached)
