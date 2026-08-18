# Responsibility: Verify the API key is required outside dev, and identity falls back predictably.
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.security import owner_dep


async def _call(
    x_api_key: str | None = None,
    x_user_id: str | None = None,
    product_api_key: str  = "test-key",
) -> str:
    with patch.object(polcfg, "MESH_API_KEY", product_api_key):
        return await owner_dep(x_api_key=x_api_key, x_user_id=x_user_id)



async def test_valid_api_key_accepted():
    result = await _call(x_api_key="test-key")
    assert result == "test-key"


async def test_invalid_api_key_raises_401():
    with pytest.raises(HTTPException) as exc:
        await _call(x_api_key="wrong-key")
    assert exc.value.status_code == 401


async def test_missing_api_key_raises_401():
    with pytest.raises(HTTPException) as exc:
        await _call(x_api_key=None)
    assert exc.value.status_code == 401


async def test_empty_string_api_key_raises_401():
    with pytest.raises(HTTPException) as exc:
        await _call(x_api_key="")
    assert exc.value.status_code == 401



async def test_x_user_id_returned_as_identity():
    result = await _call(x_api_key="test-key", x_user_id="user-abc")
    assert result == "user-abc"


async def test_x_user_id_whitespace_stripped():
    result = await _call(x_api_key="test-key", x_user_id="  user-abc  ")
    assert result == "user-abc"


async def test_falls_back_to_api_key_when_user_id_absent():
    result = await _call(x_api_key="test-key", x_user_id=None)
    assert result == "test-key"


async def test_falls_back_to_api_key_when_user_id_empty_string():
    result = await _call(x_api_key="test-key", x_user_id="")
    assert result == "test-key"


async def test_falls_back_to_api_key_when_user_id_only_whitespace():
    result = await _call(x_api_key="test-key", x_user_id="   ")
    assert result == "test-key"



async def test_dev_mode_no_key_required():
    result = await _call(x_api_key=None, x_user_id=None, product_api_key="")
    assert result == "dev-user"


async def test_dev_mode_uses_provided_user_id():
    result = await _call(x_api_key=None, x_user_id="engineer-1", product_api_key="")
    assert result == "engineer-1"


async def test_dev_mode_uses_api_key_as_identity_when_provided():
    result = await _call(x_api_key="local-key", x_user_id=None, product_api_key="")
    assert result == "local-key"


async def test_dev_mode_prefers_user_id_over_api_key():
    result = await _call(x_api_key="local-key", x_user_id="me", product_api_key="")
    assert result == "me"



async def test_401_detail_mentions_api_key():
    with pytest.raises(HTTPException) as exc:
        await _call(x_api_key="bad")
    assert "API-Key" in exc.value.detail or "api" in exc.value.detail.lower()
