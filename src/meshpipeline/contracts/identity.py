# Responsibility: Declare the one thing a proven credential resolves to, whatever kind of credential it was.
# Boundaries: the identity itself; proving it belongs to api/security.py, storing it to persistence.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum as PyEnum


class Credential(str, PyEnum):
    #: an Authorization: Bearer hx_live_… key, which carries its own owner
    api_key = "api_key"
    #: X-User-Id with a valid X-User-Sig HMAC
    signed_header = "signed_header"
    #: genuinely-local dev, where USER_TOKEN_SECRET is unset and the caller states who it is
    self_asserted = "self_asserted"


@dataclass(frozen=True)
class Principal:
    # THE tenant. Every query downstream scopes on this exact string, so a key resolves to an
    # owner_id and nothing below this line has to know which credential produced it.
    owner_id: str
    # WHICH ORGANISATION that owner acted within, as a string uuid, empty while organisations do
    # not exist. Carried from the start because widening a tenant boundary after data has
    # accumulated is the expensive migration; today nothing filters on it.
    organization_id: str = ""
    # The plan whose limits this caller is held to. Empty means "no plan": settings/plans.py
    # answers with the deployment's own configuration, which is today's behaviour for everyone.
    plan: str = ""
    credential: Credential = Credential.self_asserted
    #: which api_keys row proved this, as a string uuid; empty for a header credential
    key_id: str = ""
