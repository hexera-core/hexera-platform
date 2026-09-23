# Responsibility: Verify a refusal names whose limitation it is - the file's, or this system's.
# Boundaries: the attribution seam; which combinations are impossible is the admission suite's proof.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.agents.intake.validation import ADMIT_IMPOSSIBLE, preview_admission
from meshpipeline.cad.regions import regions_of

REPO = Path(__file__).resolve().parents[4]
#: Real exports, because the whole point is what a CAD file does and does not carry. One names its
#: parts; one is a single unnamed shell. A synthesised fixture would prove neither.
NAMED = REPO / "BETA" / "cht_concentric_pipe.step"
UNNAMED = REPO / "BETA" / "Naca0012.STEP"

_WALLS = [{"name": n, "type": "wall"} for n in ("fuselage", "wing", "horizontal_tail")] + \
         [{"name": "farfield", "type": "farfield"}]


def _refusal(facts: dict | None) -> str:
    r = preview_admission("snappy", "external_cfd", "body-surface", dimensionality="3D",
                          patches=_WALLS, geometry_facts=facts)
    assert r["verdict"] == ADMIT_IMPOSSIBLE
    return r["safe_user_message"]


# what the CAD file itself carries

@pytest.mark.skipif(not NAMED.is_file(), reason="licensed CAD fixture is local-only")
def test_a_file_that_names_its_parts_is_read_as_having_regions():
    regions = regions_of(NAMED)
    assert regions.count >= 2, regions
    assert regions.source, "a file with components must say how they were found"


@pytest.mark.skipif(not UNNAMED.is_file(), reason="licensed CAD fixture is local-only")
def test_a_single_body_export_is_read_as_having_none():
    assert regions_of(UNNAMED).count == 0


def test_an_undescribable_file_is_not_fatal(tmp_path):
    junk = tmp_path / "broken.step"
    junk.write_text("this is not a STEP file")
    assert regions_of(junk).count == 0


# whose limitation the refusal names

def test_a_structureless_geometry_is_reported_as_the_user_s_own():
    message = _refusal({"region_names": [], "region_count": 0, "region_source": ""})
    assert "your geometry is one region" in message.lower(), message
    assert "itself can keep named regions separate" in message, \
        "the engine's real capability is not stated"


def test_a_structured_geometry_is_measured_against_what_it_carries():
    # Three declared, two carried: the refusal names the shortfall and the regions that do exist,
    # rather than implying the engine is incapable.
    message = _refusal({"region_names": ["fluid", "wall"], "region_count": 2,
                        "region_source": "roots"})
    # The facts, not the phrasing: how many the file carries, how many were asked for, and the
    # names it does offer - so the reader can see the shortfall rather than be told there is one.
    assert "2 regions" in message and "3 wall patches" in message, message
    assert "fluid, wall" in message, "the regions the file does carry are not named back"


def test_unknown_geometry_is_admitted_rather_than_refused_on_silence():
    # The engine can do this, and no facts were supplied. Refusing here would force a capability
    # off on no evidence - the failure this whole seam exists to avoid.
    from meshpipeline.agents.intake.validation import preview_admission
    r = preview_admission("snappy", "external_cfd", "body-surface", dimensionality="3D",
                          patches=_WALLS, geometry_facts=None)
    assert r["verdict"] != ADMIT_IMPOSSIBLE, r.get("safe_user_message")


# the facts actually reach admission

def test_intake_supplies_the_facts_it_can_read():
    # The wiring, not the reader: an executor that silently returned {} would leave every refusal
    # blaming the engine while looking correct in every other test. The facts come off the stored
    # measurement of the customer's own bytes, read once in `node_intake`.
    measured = {"status": "ok", "region_count": 2, "region_names": ["inner", "outer"],
                "region_source": "measured"}
    ex = IntakeToolExecutor(state=IntakeExecutionState(session_id="s1", geometry_reading=measured),
                            job_id="j", implemented_engines=["snappy"], search_tool=None)
    facts = ex._geometry_facts()
    assert facts is measured
    assert facts.get("region_count", 0) >= 2, facts


def test_a_session_with_no_measurement_supplies_NOTHING_rather_than_a_zero():
    """`None`, not `{"region_count": 0}`. The reader this replaced listed a staging directory the
    upload empties, so it answered zero regions for every job ever run, and `_supplies_fewer_regions`
    refused a five-wall-patch request on that zero. A count nobody measured is worse than no count:
    unknown says nothing, and the request goes through to be judged on what is actually known."""
    ex = IntakeToolExecutor(state=IntakeExecutionState(session_id="absent"), job_id="j",
                            implemented_engines=["snappy"], search_tool=None)
    assert ex._geometry_facts() is None
