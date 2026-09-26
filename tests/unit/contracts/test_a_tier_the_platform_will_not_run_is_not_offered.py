"""The customer was offered a mesh size this platform refuses, and the refusal was silent.

The measurement forecasts a cell count per tier and sizes it from the part, so on a part with fine
features `max` comes out well past `CELL_HARD_LIMIT`. Two readers quoted that forecast without
knowing the ceiling existed: the intake panel, which offered the tier by name, and the geometry
agent, which composed against `cell_cap=None` whenever the customer had confirmed no budget. The
clamp that actually holds the mesh down lives in the snappy driver - downstream of both, and
downstream of the customer saying yes. A confirmation that will not be honoured is the same defect
as a default that passes for a confirmation, entered from the other side.
"""
from __future__ import annotations

from tests._surveyor_package import require

import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.intake.geometry_brief import surveyor_panel
from meshpipeline.contracts.geometry_agent_block import (
    cell_cap_for_composition,
    platform_cell_ceiling,
    runnable_cell_estimates,
)

CEILING = polcfg.CELL_HARD_LIMIT


def _document(*tiers: tuple[str, int]) -> dict:
    """The least a document needs for the panel to render: a good measurement and a landed look."""
    return {"status": "ok",
            "look": {"status": "ok", "seconds": 31.0,
                     "impression": {"looks_like": "a bracket", "confidence": 0.7}},
            "bbox": {"extent_m": [0.12, 0.08, 0.04]},
            "unit": {"declared": "mm"},
            "openings": [],
            "facts": {"cell_estimates": [{"tier": t, "cells": c} for t, c in tiers]}}


def test_the_ceiling_is_the_policy_number_and_not_a_second_copy_of_it():
    assert platform_cell_ceiling() == CEILING


def test_a_tier_over_the_ceiling_is_not_offered_as_a_choice():
    runnable, beyond = runnable_cell_estimates(
        [{"tier": "draft", "cells": 400_000},
         {"tier": "standard", "cells": 1_500_000},
         {"tier": "max", "cells": 34_000_000}])
    assert [r["tier"] for r in runnable] == ["draft", "standard"]
    assert [r["tier"] for r in beyond] == ["max"], "a tier the clamp would cut is not a choice"


def test_the_dropped_tier_is_returned_rather_than_discarded():
    """A tier dropped in silence is one the customer may still ask for by name."""
    _runnable, beyond = runnable_cell_estimates([{"tier": "max", "cells": CEILING + 1}])
    assert beyond and beyond[0]["tier"] == "max", (
        "the reader that drops a tier needs something to answer with when it is asked for")


def test_a_tier_with_no_forecast_is_runnable_because_absence_is_not_evidence():
    """A fact that lies is worse than a missing one: no number is not a large number."""
    for missing in (None, 0, -1, "lots", True):
        runnable, beyond = runnable_cell_estimates([{"tier": "standard", "cells": missing}])
        assert not beyond, f"cells={missing!r} was read as over the ceiling"
        assert len(runnable) == 1


def test_a_row_that_is_not_a_tier_is_in_neither_half():
    runnable, beyond = runnable_cell_estimates(["max", None, 7, {"tier": "draft", "cells": 10}])
    assert [r["tier"] for r in runnable] == ["draft"]
    assert beyond == []


def test_the_panel_offers_only_what_it_will_run():
    panel = surveyor_panel(_document(("draft", 400_000), ("standard", 1_500_000), ("max", 34_000_000)), None)
    assert "1,500,000 cells at standard" in panel
    assert "draft about 400,000" in panel
    assert "34,000,000" not in panel, "the panel offered a tier the driver would clamp away"
    assert "say draft to change it" in panel, "it must not offer the word `max` either"


def test_when_every_tier_is_over_the_ceiling_the_panel_says_so_rather_than_nothing():
    panel = surveyor_panel(_document(("draft", CEILING * 2), ("standard", CEILING * 9)), None)
    assert "mesh size" in panel, (
        "with no runnable tier the panel used to fall silent, leaving the customer to learn the "
        "size of their mesh from the result")
    assert f"{CEILING:,}" in panel
    assert f"{CEILING * 9:,}" not in panel


# THE COMPOSITION CAP. `cell_cap=None` read as "no limit" to the geometry agent's catalog rule, whose
# tier filter is `[tier for tier where cells <= cell_cap]` - so with nothing confirmed it filtered
# against nothing and could plan a fidelity the builder then clamped.

def test_composing_with_no_confirmed_budget_uses_the_ceiling_not_nothing():
    assert cell_cap_for_composition(None) == CEILING


def test_a_confirmed_budget_under_the_ceiling_is_the_customer_s_own_number():
    assert cell_cap_for_composition(750_000) == 750_000


def test_a_confirmed_budget_over_the_ceiling_is_held_to_the_ceiling():
    assert cell_cap_for_composition(CEILING * 4) == CEILING, (
        "a budget the platform will not honour must not be composed against")


def test_a_nonsense_budget_falls_back_to_the_ceiling_rather_than_through_it():
    for junk in (0, -1, True, False, "big", 1.5, None):
        assert cell_cap_for_composition(junk) == CEILING, f"{junk!r} did not fall back"


# THE READING THAT DISAGREED WITH ITSELF. On ahmed_variant_001 the look's prose said "solid material
# throughout, with no visible internal flow passage" and "the labelled openings do not resolve as
# visible mouths"; its `openings_seen` came back empty. The panel read only the structured field, so
# it said nothing, and the customer's next question was which of the four mouths is the inlet.

def _looked_at(openings: list[dict], seen: list[dict] | None, purpose: str = "internal_cfd") -> str:
    return surveyor_panel(
        {"status": "ok", "unit": {"declared": "mm"}, "bbox": {"extent_m": [1.0, 0.4, 0.3]},
         "openings": openings,
         "look": {"status": "ok", "seconds": 18.0,
                  "impression": {"looks_like": "a solid chamfered rectangular block",
                                 "openings_seen": seen if seen is not None else []}},
         "facts": {"cell_estimates": [{"tier": "standard", "cells": 1_324_507}]}},
        {"composed_for": {"purpose": purpose}})


_FOUR = [{"id": f"o{n}"} for n in (1, 2, 3, 4)]


def test_an_empty_reading_of_the_openings_is_said_rather_than_passed_over():
    panel = _looked_at(_FOUR, [])
    assert "could not confirm any of the 4 opening(s)" in panel, (
        "the panel said nothing at all, and four ports were then proposed on a solid block")


def test_it_says_unconfirmed_and_never_not_a_port():
    """An empty field is not a denial. Spending it as one would be a fact that lies."""
    panel = _looked_at(_FOUR, [])
    assert "do NOT look like ports" not in panel
    assert "could not confirm" in panel


def test_a_reading_that_did_come_back_is_still_the_one_that_speaks():
    panel = _looked_at(_FOUR, [{"id": f"o{n}", "likely_role": "not a port"} for n in (1, 2, 3, 4)])
    assert "do NOT look like ports" in panel
    assert "could not confirm" not in panel, "the weaker sentence must not displace the real reading"


def test_a_part_with_no_openings_is_not_told_its_openings_are_unconfirmed():
    assert "could not confirm" not in _looked_at([], [])


def test_only_a_flow_job_is_asked_about_flow():
    assert "the flow really does go through" in _looked_at(_FOUR, [])
    structural = _looked_at(_FOUR, [], purpose="structural")
    assert "could not confirm any of the 4 opening(s)" in structural, (
        "what the openings are is worth saying whatever the job is")
    assert "flow" not in structural, "a structural job has no flow in it"


# THE TWO CEILINGS AGREE BY COINCIDENCE. `hexera.budget` does
# `cap = int(cell_cap) if cell_cap else MAX_CELLS_CAP`, so composing with None never meant unbounded -
# it meant the package's own constant. Both are 8,000,000 today, in two independently pinned
# distributions, and only the platform's is env-overridable. Nothing tied them together.

def test_the_package_s_own_ceiling_has_not_drifted_from_the_platform_s():
    """If this fails, the agent is planning against one number and the driver clamping to another.

    The import goes through `require` rather than being written bare: a bare one raises ImportError
    where the package is absent, and an ERROR inside a test is read as a broken test rather than as
    the stated absence of the Surveyor. `require` is the one door, and it never skips silently.
    """
    MAX_CELLS_CAP = require("geometry_agent.agent.hexera",
                            needs="the agent's own cell ceiling").MAX_CELLS_CAP
    assert MAX_CELLS_CAP == CEILING, (
        f"the geometry agent plans against {MAX_CELLS_CAP:,} and this platform meshes at most "
        f"{CEILING:,}. Whichever is wrong, the customer is told one and gets the other, and the "
        f"clamp that resolves it is silent and downstream of their confirmation.")


# AND THE CEILING MUST NOT BE PUT IN THE COMPOSITION'S CAP, which is where I first put it.
#
# `compose`'s cap becomes `planner_block.customer_cell_cap`, and `contract.deliver.
# check_builder_handoff` refuses any handoff whose `customer_cell_cap` is not the RESOLVED BUDGET:
#
#     the builder's cap is 8000000 and the resolved budget is None [customer_cell_cap]
#
# `given.budget` is None whenever nobody stated a budget, which is most jobs. So a ceiling written
# there makes `geometry_step.builder_handoff` raise on every one of them, `planner_inputs_for_state`
# falls back to the no-plan block, and the job runs with no plan at all - the whole of step 5 lost to
# a change meant to make one number honest. Reverted in 57324d2.
#
# A cap the customer never confirmed cannot be recorded as the cap the builder was given. That is the
# second house law one field along, and it is why `cell_cap_kind` is computed from `cell_cap` and
# `stated_cap` and never from a ceiling.
#
# The drift test above is the guard that survives: an env-overridden CELL_HARD_LIMIT makes it fail
# rather than letting the agent plan against one number while the driver clamps to another. A failing
# test is the outcome wanted here - not a value threaded into a contract that checks it for equality
# against something else.

def test_the_composition_does_not_record_a_ceiling_as_the_customer_s_cap():
    """Pins the revert. Re-adding the ceiling here costs every unstated-budget job its plan."""
    import inspect

    from meshpipeline.application import geometry_survey as gs

    src = inspect.getsource(gs.composition)
    assert "cell_cap_for_composition" not in src, (
        "the platform ceiling is back in `compose`'s cap. It becomes "
        "planner_block.customer_cell_cap, check_builder_handoff refuses it against a resolved budget "
        "of None, and every job with no stated budget silently loses step 5. See 57324d2.")
