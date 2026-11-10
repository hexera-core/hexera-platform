# Responsibility: Declare the request and response shapes of the chat endpoints.
# Boundaries: the wire contract; no behaviour.
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    session_id: uuid.UUID
    content: str


class ChatNoteIn(BaseModel):
    """The console's notes box: anything specific about the simulation, kept exactly as typed
    (contracts/user_notes). Long enough for a pasted spec sheet."""
    session_id: uuid.UUID
    text: str = Field(min_length=1, max_length=20_000)


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
    # THE OPEN ENGINE QUESTION, drawn as a choice: the engine proposed and every engine that can
    # take the file, with its measured fit and reason (agents/intake/measured_choice.choice_payload).
    # Present only while the question is open; the user's pick is sent back as an ordinary message.
    engine_choice: dict[str, Any] | None = None
    # THE BUTTONS under an application question (application/purpose_map.choices_for): what the
    # mesh is for, when the words did not say, and an FEA part's analysis type. Each is
    # {label, send, soon}; a click sends `send` as an ordinary message. Empty for any other reply.
    choices: list[dict[str, Any]] = []
    # A YES THAT WAITS FOR THE NOTES TO BE READ (NOTE-5): the console shows the reading on the card and sends the
    # same yes again once it lands (a minute at most); that second yes starts the run.
    yes_after_reading: bool = False
