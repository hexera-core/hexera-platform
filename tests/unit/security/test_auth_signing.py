# Responsibility: Verify a signed identity is accepted, and a forged or unsigned one is not.
import pytest
from fastapi import HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import security as auth  # noqa: E402


def test_self_asserted_when_no_secret(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "")
    assert auth.verify_identity(None, "alice", None) == "alice"


def test_signed_identity_accepts_valid_sig(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "key")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "shh")
    sig = auth.expected_user_sig("alice")
    assert auth.verify_identity("key", "alice", sig) == "alice"


def test_signed_identity_rejects_forged_id(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "key")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "shh")
    # attacker presents victim id with alice's signature → rejected
    alice_sig = auth.expected_user_sig("alice")
    with pytest.raises(HTTPException) as exc:
        auth.verify_identity("key", "victim", alice_sig)
    assert exc.value.status_code == 401


def test_signed_identity_rejects_missing_sig(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "key")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "shh")
    with pytest.raises(HTTPException):
        auth.verify_identity("key", "alice", None)


def test_bad_api_key_rejected(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "key")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        auth.verify_identity("wrong", "alice", None)
    assert exc.value.status_code == 401
