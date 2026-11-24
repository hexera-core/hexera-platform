# Responsibility: Declare the request and response shapes of the chat endpoints.
# Boundaries: the wire contract; no behaviour.
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, model_validator

#: The most files one message or note carries (NOTE-16): the chat says it in one line when more are attached.
MAX_ATTACHMENTS = 5
#: The most faces picked on the model one message or note carries (NOTE-17).
MAX_FACES = 20


class FaceRef(BaseModel):
    """A face the user picked on the model, as the chip under the box shows it. Carried as sent here; the face pick
    (application/face_pick) gives it its meaning."""
    id: str = Field(pattern=r"^(Part_\d+_)?(Face|Region)_\d+$", max_length=40)
    label: str = Field(default="", max_length=80)


class _Carried(BaseModel):
    """WHAT A MESSAGE OR NOTE MAY CARRY BESIDE ITS WORDS: the files the user attached (ids from
    POST /api/v1/chat/attachments) and the faces they picked on the model. Either lets the words be empty."""
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    faces: list[FaceRef] = Field(default_factory=list, max_length=MAX_FACES)

    def carries(self) -> bool:
        return bool(self.attachments or self.faces)


class ChatMessageIn(_Carried):
    session_id: uuid.UUID
    content: str = ""

    @model_validator(mode="after")
    def _words_or_carried(self) -> ChatMessageIn:
        if not self.content.strip() and not self.carries():
            raise ValueError("The message is empty.")
        return self


class ChatNoteIn(_Carried):
    """The console's notes box: anything specific about the simulation, kept exactly as typed
    (contracts/user_notes). Long enough for a pasted spec sheet."""
    session_id: uuid.UUID
    text: str = Field(default="", max_length=20_000)

    @model_validator(mode="after")
    def _words_or_carried(self) -> ChatNoteIn:
        if not self.text.strip() and not self.carries():
            raise ValueError("The note is empty.")
        return self


class AttachmentOut(BaseModel):
    """One kept attachment, as a message or note carries it by `id` (application/chat_attachments.public)."""
    id: str
    kind: str                       # "image" | "text"
    name: str
    mime: str
    bytes: int
    pages: int | None = None        # a PDF's pages
    chars: int | None = None        # the text read from a text file or PDF
    cut: bool | None = None         # only the first part was read
    width: int | None = None        # a picture as kept
    height: int | None = None


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
    # THE STAGE OPENS AGAIN: the user changed the analysis after the stage (FEA <-> CFD); the console watches
    # the geometry check again and shows the part on the new route's stage (agents/intake/message)
    restage: bool = False
    # THE STAGE'S FORM, SET FROM THE CHAT: the flow the user said while the stage waits on Proceed (the flow-kind
    # question's buttons, FLOW-1) - {"flow", "input_kind"} - which the console sets on the open stage's "The fluid
    # flows" and "The file is" before Proceed (agents/intake/message)
    stage_flow: dict[str, str] | None = None
    # A REQUIREMENT'S REPLY STILL ON ITS WAY (NOTE-19): its reading takes longer than the send waits, so the console
    # keeps "Reading…" and the reply comes with the card's poll of the reading (GET .../check/parts, its `reply`)
    reply_later: bool = False
