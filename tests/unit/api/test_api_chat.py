# Responsibility: Verify the chat route reads history for a known session and never classifies consent itself.
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.chat as chat_mod
from meshpipeline.api.v1.chat import router as chat_router

_app = FastAPI()
_app.include_router(chat_router, prefix="/api/v1/chat")

_SESSION_ID = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")


@asynccontextmanager
async def _mock_get_db():
    yield AsyncMock()


# the keyword consent classifier is gone

def test_no_keyword_consent_classifier_survives():
    for dead in ("_regex_consent_class", "_llm_consent_class", "_classify_consent",
                 "_CONSENT_RE", "_REJECTION_RE"):
        assert not hasattr(chat_mod, dead), f"{dead} is a keyword consent classifier"


def test_the_application_owns_dispatch_not_the_model():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    names = {t["function"]["name"] for t in INTAKE_TOOLS}
    assert "confirm_dispatch" not in names
    assert names == {"web_search", "recommend_compatible_engines", "propose_engine_selection",
                     "confirm_engine_selection", "preview_selected_admission",
                     "submit_requirements"}



async def test_history_unknown_session_returns_404():
    mock_repo = MagicMock()
    mock_repo.get_internal = AsyncMock(return_value=None)
    mock_repo.get_for_owner = AsyncMock(return_value=None)
    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.persistence.repositories.session_repository.SessionRepository", return_value=mock_repo),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/chat/history/{_SESSION_ID}")
    assert resp.status_code == 404



async def test_history_known_session_returns_messages():
    mock_session = MagicMock()
    mock_session.owner_id = "dev-user"
    mock_session.messages = [{"role": "assistant", "content": "Hello!"}]
    mock_session.request_txt = ""

    mock_repo = MagicMock()
    mock_repo.get_internal = AsyncMock(return_value=mock_session)
    mock_repo.get_for_owner = AsyncMock(return_value=mock_session)

    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.persistence.repositories.session_repository.SessionRepository", return_value=mock_repo),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/chat/history/{_SESSION_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == str(_SESSION_ID)
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content"] == "Hello!"
    assert data["awaiting_confirmation"] is False
