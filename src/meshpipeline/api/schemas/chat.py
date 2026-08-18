# Responsibility: Declare the request and response shapes of the chat endpoints.
# Boundaries: the wire contract; no behaviour.
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel


class ChatMessageIn(BaseModel):
    session_id: uuid.UUID
    content: str


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    reply: str
    done: bool = False
    request_txt: str | None = None
    job_id: uuid.UUID | None = None
    awaiting_confirmation: bool = False
    # WHAT WAS ACTUALLY AGREED, from the application's own finalized fields - not the
    # model's prose re-read by the browser. Absent until Intake settles a brief; see
    # application.intake_brief for the shape and for what it must never carry.
    brief: dict[str, Any] | None = None
    # The turn's PUBLIC trace - reasoning activity, tool lifecycle and application
    # rationale for work that happened before a job (and therefore before any Redis
    # channel) existed. Already projected for the deployment's trace mode.
    trace: list[dict[str, Any]] = []
