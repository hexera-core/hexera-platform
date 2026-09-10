# Responsibility: Decide whether a presented Identity Platform ID token is genuine, current and addressed to this deployment.
# Owns: the claims a token must carry to name an identity here, and the cached copy of Google's signing certificates.
# Boundaries: the token only - what the identity it names may then do belongs to the application.
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from google.auth import jwt as google_jwt

#: Where Google publishes the certificates Identity Platform signs ID tokens with. This is the
#: securetoken issuer's key set specifically - NOT the general OAuth2 certificate URL, which
#: signs a different family of tokens and would verify nothing we care about here.
CERTS_URL = ("https://www.googleapis.com/robot/v1/metadata/x509/"
             "securetoken@system.gserviceaccount.com")

#: How long a fetched key set is reused. Google rotates these on the order of days, so an hour is
#: comfortably fresh while making a sign-in cost no outbound request in the ordinary case.
CERT_TTL_SECONDS = 3600

#: The issuer a token from THIS project must name. Checked separately from `aud` because the two
#: are different assertions: `aud` says who the token is for, `iss` says who minted it, and only
#: checking one of them accepts a token from the wrong side of that pair.
_ISSUER_TEMPLATE = "https://securetoken.google.com/{project_id}"

#: Indirected so tests can stand in for the one operation that needs a real key.
_decode = google_jwt.decode

_certs_lock = threading.Lock()
_certs_cache: dict | None = None
_certs_fetched_at: float = 0.0


class InvalidToken(Exception):
    """One exception for every way a token fails. The caller answers one refusal either way:
    which part was wrong is only useful to somebody probing which of their guesses is close."""


@dataclass(frozen=True)
class VerifiedToken:
    #: the Identity Platform subject - stable across an email change, which is why it, and not
    #: the address, is what `users.firebase_uid` stores
    uid: str
    #: lowercased, because owner_id is the lowercased email everywhere in this schema
    email: str
    email_verified: bool
    name: str


def _now_monotonic() -> float:
    # Monotonic, not wall clock: a TTL measured against a clock that can step backwards over NTP
    # would pin a stale key set for as long as the step.
    return time.monotonic()


def reset_cert_cache() -> None:
    """Test seam: forget the cached key set."""
    global _certs_cache, _certs_fetched_at
    with _certs_lock:
        _certs_cache = None
        _certs_fetched_at = 0.0


def google_certs() -> dict:
    global _certs_cache, _certs_fetched_at
    with _certs_lock:
        fresh = (_certs_cache is not None
                 and _now_monotonic() - _certs_fetched_at < CERT_TTL_SECONDS)
        if fresh:
            return _certs_cache
    # httpx, not requests: requests reaches this project only as a transitive dependency of
    # google-cloud-storage, and a runtime that imports it directly depends on that accident.
    response = httpx.get(CERTS_URL, timeout=10.0)
    response.raise_for_status()
    certs = response.json()
    with _certs_lock:
        _certs_cache = certs
        _certs_fetched_at = _now_monotonic()
    return certs


def verify(raw_token: str, *, project_id: str,
           certs_provider: Callable[[], dict] | None = None) -> VerifiedToken:
    token = (raw_token or "").strip()
    # Shape and configuration first, and refuse on either alone - neither costs a network call
    # or a signature check. An unset project_id is the dangerous case: without this, `aud` would
    # be compared against "" and the decoder would be asked to accept anything.
    if not token:
        raise InvalidToken("no token presented")
    if not project_id:
        raise InvalidToken("no Identity Platform project is configured")

    provider = certs_provider or google_certs
    try:
        claims = _decode(token, certs=provider(), audience=project_id)
    except Exception as exc:
        # Signature, expiry and audience all land here, and all become the same refusal.
        raise InvalidToken(f"token did not verify: {exc}") from exc

    if claims.get("iss") != _ISSUER_TEMPLATE.format(project_id=project_id):
        raise InvalidToken("token was not minted for this project")

    uid = str(claims.get("user_id") or claims.get("sub") or "").strip()
    if not uid:
        raise InvalidToken("token names no subject")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        # owner_id IS the email. A token without one names no tenant here, however genuine it is.
        raise InvalidToken("token carries no email address")

    return VerifiedToken(uid=uid, email=email,
                         email_verified=bool(claims.get("email_verified")),
                         name=str(claims.get("name") or "").strip())
