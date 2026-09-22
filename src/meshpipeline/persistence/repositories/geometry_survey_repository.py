# Responsibility: Read and record the survey composed for an upload, and the answers given to it.
# Boundaries: one row per source, tenant-scoped; it decides nothing about what the survey means.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import GeometrySurvey


class GeometrySurveyRepository:
    """The one reader and writer of `geometry_surveys`.

    Every read is tenant-scoped, like every geometry read: absent and foreign are the same answer.
    """

    async def for_source(self, db: AsyncSession, *, owner_id: str,
                         geometry_source_id: uuid.UUID) -> GeometrySurvey | None:
        res = await db.execute(
            select(GeometrySurvey).where(
                GeometrySurvey.geometry_source_id == geometry_source_id,
                GeometrySurvey.owner_id == owner_id))
        return res.scalar_one_or_none()

    async def for_sha(self, db: AsyncSession, *, owner_id: str, sha256: str) -> list[GeometrySurvey]:
        """Every survey this tenant holds for these bytes. What an answer is bound to later."""
        res = await db.execute(
            select(GeometrySurvey).where(GeometrySurvey.owner_id == owner_id,
                                         GeometrySurvey.sha256 == sha256))
        return list(res.scalars().all())

    async def record(self, db: AsyncSession, *, owner_id: str, geometry_source_id: uuid.UUID,
                     sha256: str, state: dict, session_id: uuid.UUID | None = None) -> GeometrySurvey:
        """Write the survey state, or replace the one already there for this upload.

        `state` is what `application/geometry_survey.py` keeps: `facts_sha256`, `stage`, `survey`,
        `composed_for`, `planner_block`, `asked`, `answers` and `agent_git_sha`, and, when the geometry
        agent's step ran, `geometry_step` and `late`. The digest is the one thing that must not move: a
        different sha256 is a different upload.

        EVERY KEY THE STATE CARRIES IS NAMED HERE, and a key that is not named is silently dropped. That
        is not a hypothetical: the step's plan and its third-intake question were written into the state
        and lost on the way to the table, so the plan was gone by the time the builder read the row and
        the question could not be answered in the next turn. `tests/unit/persistence` pins the round
        trip for exactly that reason.
        """
        existing = await self.for_source(db, owner_id=owner_id, geometry_source_id=geometry_source_id)
        row = existing if existing is not None else GeometrySurvey(
            geometry_source_id=geometry_source_id, owner_id=owner_id, sha256=sha256)
        row.sha256 = sha256
        if session_id is not None:
            row.session_id = session_id
        row.facts_sha256 = str(state.get("facts_sha256") or "")[:64]
        row.stage = str(state.get("stage") or "surveyed")[:32]
        row.survey = dict(state.get("survey") or {})
        row.composed_for = dict(state.get("composed_for") or {})
        block = state.get("planner_block")
        row.planner_block = dict(block) if isinstance(block, dict) else None
        row.asked = list(state.get("asked") or [])
        row.answers = list(state.get("answers") or [])
        step = state.get("geometry_step")
        row.geometry_step = dict(step) if isinstance(step, dict) else None
        late = state.get("late")
        row.late = dict(late) if isinstance(late, dict) else None
        row.agent_git_sha = str(state.get("agent_git_sha") or "")[:64]
        if existing is None:
            db.add(row)
        await db.flush()
        return row


def state_of(row: GeometrySurvey | None) -> dict | None:
    """The row as the plain dict the application layer works on, or None.

    `geometry_step` and `late` are present only when the step wrote them, because a `late` key on a row
    that has none is a question, and the readers tell "no third question" from "one that is open" by
    whether the key is there at all.
    """
    if row is None:
        return None
    state = {"sha256": str(row.sha256 or ""), "facts_sha256": str(row.facts_sha256 or ""),
             "stage": str(row.stage or "surveyed"), "survey": dict(row.survey or {}),
             "composed_for": dict(row.composed_for or {}),
             "planner_block": dict(row.planner_block) if isinstance(row.planner_block, dict) else None,
             "asked": list(row.asked or []), "answers": list(row.answers or []),
             "agent_git_sha": str(row.agent_git_sha or ""),
             "session_id": str(row.session_id) if row.session_id else ""}
    if isinstance(row.geometry_step, dict):
        state["geometry_step"] = dict(row.geometry_step)
    if isinstance(row.late, dict):
        state["late"] = dict(row.late)
    return state
