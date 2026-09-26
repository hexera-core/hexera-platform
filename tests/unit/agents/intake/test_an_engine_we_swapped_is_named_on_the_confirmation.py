"""We replaced the customer's engine and the only place that said so was a log line.

`_do_preview_selected_admission` may replace an engine that cannot do the job, when the engine was
OURS and the customer had delegated the choice. That is right: it is what stops three of twelve parts
dead-ending on an impossibility we created. But it was announced only in a `swap` dict handed to the
MODEL as guidance, and this codebase's own lesson is that "an instruction to a model is a request;
anything required must be enforced where we control it".

So a customer who read "Go with cfMesh?" and said go would meet "MESHING WITH Gmsh" on the last screen
before compute, with nothing anywhere saying it had changed or why. The adversarial reviewer found the
label half of this ("an engine the customer said yes to is still recorded chosen_by=us and is silently
replaced"); this pins the half that matters to the person reading it.

The swap was also a LOCAL variable in the turn that made it, and the confirmation is composed on a
later turn, so it could not have been said there even if something had tried.
"""
from __future__ import annotations

from meshpipeline.agents.intake.executor import IntakeExecutionState


def test_the_swap_survives_the_turn_that_made_it():
    st = IntakeExecutionState(owner_id="o", session_id="s")
    assert st.engine_swap == {}, "no swap by default"
    st.engine_swap = {"from": "cfMesh", "to": "Gmsh", "because": "input_kind_incompatible"}
    assert st.engine_swap["from"] == "cfMesh"


def _head(engine_label: str, swap: dict, fidelity: str = "") -> str:
    """The composition in `_do_submit_requirements`, as a function of what it depends on."""
    head = "MESHING WITH " + engine_label
    if fidelity:
        head += f" at {fidelity} fidelity"
    if swap:
        head += " (I changed this from {}: {})".format(
            swap.get("from") or "the engine I first picked",
            swap.get("because") or "it could not mesh this setup")
    return head


def test_the_confirmation_names_what_changed_and_why():
    said = _head("Gmsh", {"from": "cfMesh", "to": "Gmsh",
                          "because": "cfMesh cannot mesh internal flow from a fluid-domain file"})
    assert "MESHING WITH Gmsh" in said
    assert "changed this from cfMesh" in said, "they said go to cfMesh and were never told"
    assert "fluid-domain file" in said, "a change with no reason is not an explanation"


def test_a_run_with_no_swap_reads_exactly_as_before():
    assert _head("Gmsh", {}) == "MESHING WITH Gmsh"
    assert _head("Gmsh", {}, "standard") == "MESHING WITH Gmsh at standard fidelity"


def test_a_swap_with_no_recorded_reason_still_admits_that_it_changed():
    """A fact that lies is worse than a missing one - but silence about a change is the worse half."""
    said = _head("Gmsh", {"from": "cfMesh", "to": "Gmsh", "because": ""})
    assert "changed this from cfMesh" in said
    assert "could not mesh this setup" in said


def test_a_swap_missing_even_the_old_name_says_so_rather_than_naming_nothing():
    said = _head("Gmsh", {"to": "Gmsh"})
    assert "changed this from the engine I first picked" in said
