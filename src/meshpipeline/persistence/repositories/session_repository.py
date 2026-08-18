# Responsibility: Read and write chat sessions, their messages and their intake gate.
# Boundaries: a message and its gate consequence are written together - the two cannot diverge.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import ChatSession


class SessionRepository:
    async def create(self, db: AsyncSession, owner_id: str) -> ChatSession:
        s = ChatSession(owner_id=owner_id, messages=[])
        db.add(s)
        await db.flush()
        return s

    async def get_for_owner(self, db: AsyncSession, session_id: uuid.UUID,
                            owner_id: str) -> ChatSession | None:
        res = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id,
                                      ChatSession.owner_id == owner_id))
        return res.scalar_one_or_none()

    async def get_internal(self, db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
        result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        return result.scalar_one_or_none()

    async def get_by_job_id(self, db: AsyncSession, job_id: uuid.UUID) -> ChatSession | None:
        result = await db.execute(select(ChatSession).where(ChatSession.job_id == job_id))
        return result.scalar_one_or_none()

    async def get_for_update(self, db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def append_message(self, db: AsyncSession, session_id: uuid.UUID,
                              role: str, content: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            msgs = list(s.messages or [])
            msgs.append({"role": role, "content": content})
            s.messages = msgs

    async def set_request_txt(self, db: AsyncSession, session_id: uuid.UUID,
                               request_txt: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.request_txt = request_txt
            s.intake_submitted = bool(request_txt)

    async def set_review_brief_txt(self, db: AsyncSession, session_id: uuid.UUID,
                                    review_brief_txt: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.review_brief_txt = review_brief_txt

    async def set_intake_patches(self, db: AsyncSession, session_id: uuid.UUID,
                                  patches: list | None) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.intake_patches = patches if patches is not None else []

    async def set_dimensionality(self, db: AsyncSession, session_id: uuid.UUID,
                                  dimensionality: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.dimensionality = dimensionality

    async def set_purpose(self, db: AsyncSession, session_id: uuid.UUID,
                          purpose: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.purpose = purpose

    async def set_input_kind(self, db: AsyncSession, session_id: uuid.UUID,
                             input_kind: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.input_kind = input_kind

    async def set_requested_mesh_fidelity(self, db: AsyncSession, session_id: uuid.UUID,
                                          requested_mesh_fidelity: str | None) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.requested_mesh_fidelity = requested_mesh_fidelity or None

    async def set_mesh_engine(self, db: AsyncSession, session_id: uuid.UUID,
                              mesh_engine: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.mesh_engine = mesh_engine

    async def set_task_label(self, db: AsyncSession, session_id: uuid.UUID,
                             domain: str) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.domain = domain

    async def set_engine_params(self, db: AsyncSession, session_id: uuid.UUID,
                                params: dict) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.engine_params = dict(params)

    async def set_intake_gate(self, db: AsyncSession, session_id: uuid.UUID,
                              gate: dict | None) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            # Prune empty sub-keys so a cleared gate stores NULL rather than {"selection": null, …}.
            _g = {k: v for k, v in (gate or {}).items() if v}
            s.intake_gate = _g or None

    async def bind_geometry_interpretation(self, db: AsyncSession, session_id: uuid.UUID,
                                           interpretation_id: uuid.UUID) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.geometry_interpretation_id = interpretation_id

    async def append_intake_event(self, db: AsyncSession, session_id: uuid.UUID,
                                   event: dict) -> None:
        # Buffered for emission into the job's ALWAYS-written runtime sample record
        # at dispatch; the privacy switch gates corpus export, not this record.
        s = await self.get_internal(db, session_id)
        if s:
            events = list(s.llm_metadata or [])
            events.append(event)
            s.llm_metadata = events

    async def link_job(self, db: AsyncSession, session_id: uuid.UUID,
                        job_id: uuid.UUID) -> None:
        s = await self.get_internal(db, session_id)
        if s:
            s.job_id = job_id
