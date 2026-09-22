# Responsibility: Verify that every key the survey state carries survives the trip to the row and back, the geometry agent's step included.
# Boundaries: the repository against a stand-in session; a real database is the integration tier's parity gate.
#
# WHY THIS FILE EXISTS. `record` writes a NAMED LIST of keys, and a key that is not named is dropped
# without a word. The geometry agent's step wrote its plan and its third-intake question into the state
# and both were lost on the way to the table: the plan was made inside the submission turn and gone by
# the time the builder read the row, so the builder fell back on every job while the step reported
# success, and the third intake's question could not be answered in the next turn because the row it was
# raised on no longer had it. Nothing above the repository could see it - every test and every harness
# that stands in for the database keeps whatever it is handed.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.persistence.models import GeometrySurvey
from meshpipeline.persistence.repositories.geometry_survey_repository import (
    GeometrySurveyRepository,
    state_of,
)

SHA = "a" * 64


class _NoRowYet:
    """A session with nothing in it: `record` builds a new row and never touches a database."""

    def __init__(self):
        self.added: list = []

    async def execute(self, _statement):
        return self

    def scalar_one_or_none(self):
        return None

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _state(**extra) -> dict:
    return {"sha256": SHA, "facts_sha256": "b" * 64, "stage": "settled",
            "survey": {"schema_name": "geometry_agent.contract.survey.v1"},
            "composed_for": {"purpose": "internal_cfd", "cell_cap": 2_000_000},
            "planner_block": {"schema": "geometry_agent.planner_block.v1"},
            "asked": ["role_inlet"],
            "answers": [{"question_id": "role_inlet", "subject": "o1", "value": "inlet",
                         "answered_by": "customer"}],
            "agent_git_sha": "c" * 40, **extra}


async def _written(state: dict) -> GeometrySurvey:
    db = _NoRowYet()
    return await GeometrySurveyRepository().record(
        db, owner_id="owner-7f3a", geometry_source_id=uuid.uuid4(), sha256=SHA, state=state)


@pytest.mark.asyncio
async def test_the_geometry_agents_plan_and_its_late_question_survive_the_row():
    step = {"schema": "meshpipeline.geometry_step.v1", "status": "planned", "for": "d" * 32,
            "fidelity": "standard", "envelope": {"cells_high": 987_828, "cap": 750_000},
            "flow_patches": {"o1": {"role": "inlet", "kind": "confirmed"}},
            "plan": {"unit": {"assumed": "mm"}},
            "ledger": {"meta": {"key": "k"}, "events": [{"event": "plan"}]}}
    late = {"schema": "meshpipeline.geometry_step.late.v1", "id": "u_budget_planned",
            "options": ["hold 750,000", "raise to about 987,828"], "default": "hold 750,000"}
    row = await _written(_state(geometry_step=step, late=late))
    assert row.geometry_step == step, "the plan was dropped on the way to the row"
    assert row.late == late, "the question only a plan can raise was dropped on the way to the row"
    back = state_of(row)
    assert back["geometry_step"] == step and back["late"] == late


@pytest.mark.asyncio
async def test_a_row_the_step_never_touched_carries_neither_key_at_all():
    """Not null values under the keys: NO keys. `late_view` and `builder_handoff` tell "there is no
    third question" from "there is one and it is open" by whether the key is there."""
    row = await _written(_state())
    assert row.geometry_step is None and row.late is None
    back = state_of(row)
    assert "geometry_step" not in back and "late" not in back


@pytest.mark.asyncio
async def test_every_key_the_state_carries_is_a_key_the_row_keeps():
    """The list of names in `record` is the whole contract, so it is checked as a list rather than one
    key at a time: a key added to the state and not to `record` is a key that vanishes in production."""
    state = _state(geometry_step={"status": "failed"}, late={"id": "x"})
    back = state_of(await _written(state))
    assert set(state) - set(back) == set(), f"dropped: {sorted(set(state) - set(back))}"
    for key, value in state.items():
        if key in ("stage", "agent_git_sha", "facts_sha256"):
            continue                                # stored truncated, checked by the column's own width
        assert back[key] == value, key
