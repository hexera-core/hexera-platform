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

        `state` is what `application/geometry_survey.py` keeps: `schema`, `facts_sha256`, `stage`, `survey`,
        `asking`, `composed_for`, `planner_block`, `asked`, `answers`, `look_queued` and `agent_git_sha`,
        and, when the geometry agent's step ran, `geometry_step` and `late`. The digest is the one thing
        that must not move: a different sha256 is a different upload.

        EVERY KEY THE STATE CARRIES IS NAMED HERE, and a key that is not named is silently dropped. That
        is not a hypothetical, and it has now happened twice. The step's plan and its third-intake question
        were written into the state and lost on the way to the table, so the plan was gone by the time the
        builder read the row and the question could not be answered in the next turn. Then `asking` and
        `look_queued`: the question finder's own decisions and the look queue's answer, both written onto
        the state and both dropped, so the customer's answer to the fluid-side question and to the budget
        trade was accepted in memory and refused after a round trip, and a look that was still running read
        back as one that had never been taken. `tests/unit/persistence` pins the round trip for exactly
        that reason, and it pins it against a survey composed by the real chain rather than a fixture,
        because the fixture it used to pin it against did not carry either of the two dropped keys.
        """
        existing = await self.for_source(db, owner_id=owner_id, geometry_source_id=geometry_source_id)
        row = existing if existing is not None else GeometrySurvey(
            geometry_source_id=geometry_source_id, owner_id=owner_id, sha256=sha256)
        row.sha256 = sha256
        if session_id is not None:
            row.session_id = session_id
        row.facts_sha256 = str(state.get("facts_sha256") or "")[:64]
        row.state_schema = str(state.get("schema") or "")[:64]
        row.stage = str(state.get("stage") or "surveyed")[:32]
        row.survey = dict(state.get("survey") or {})
        # THE FINDER'S OWN DECISIONS, and the row is useless without them: the machine values the two
        # questions the chain exists to ask are answered from live in `asking.record`, the cap and the
        # ranking in `asking.put`, and the sentence a customer reads in `asking.text`.
        row.asking = dict(state.get("asking") or {})
        row.composed_for = dict(state.get("composed_for") or {})
        block = state.get("planner_block")
        row.planner_block = dict(block) if isinstance(block, dict) else None
        row.asked = list(state.get("asked") or [])
        row.answers = list(state.get("answers") or [])
        # WHAT THE LOOK'S QUEUE ANSWERED. The only thing on this row that can tell a survey waiting for
        # eyes from one that will never have any, because the stored document carries no look until the
        # worker writes one and those two are the same bytes there.
        row.look_queued = str(state.get("look_queued") or "")[:16]
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

    `asking` and `look_queued` are always here, empty where the row has nothing, because neither has that
    distinction to make: `asking_of` already reads `{}` as a row composed before the finder was wired, and
    `look_state` already reads an empty queue answer as a look nobody put on a queue. A key that is always
    present is also a key a total round-trip check cannot miss, which is the other half of why they were
    lost for as long as they were.
    """
    if row is None:
        return None
    state = {"schema": str(row.state_schema or ""),
             "sha256": str(row.sha256 or ""), "facts_sha256": str(row.facts_sha256 or ""),
             "stage": str(row.stage or "surveyed"), "survey": dict(row.survey or {}),
             "asking": dict(row.asking or {}),
             "composed_for": dict(row.composed_for or {}),
             "planner_block": dict(row.planner_block) if isinstance(row.planner_block, dict) else None,
             "asked": list(row.asked or []), "answers": list(row.answers or []),
             "look_queued": str(row.look_queued or ""),
             "agent_git_sha": str(row.agent_git_sha or ""),
             "session_id": str(row.session_id) if row.session_id else ""}
    if isinstance(row.geometry_step, dict):
        state["geometry_step"] = dict(row.geometry_step)
    if isinstance(row.late, dict):
        state["late"] = dict(row.late)
    return state
