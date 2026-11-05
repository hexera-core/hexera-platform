# Responsibility: Verify a Google Identity Platform ID token against Google's published signing certificates.
# Owns: the certificate endpoint, the cached key set, and the claims an Identity Platform token must carry.
# Boundaries: the token only - what the identity it names may then do belongs to the application.
# Collaborates with: contracts/firebase_token.py, whose VerifiedToken and InvalidToken this produces.
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx
from google.auth import jwt as google_jwt

from meshpipeline.contracts.firebase_token import InvalidToken, VerifiedToken

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
        # Bound to a LOCAL under the lock, then returned: reading the global twice would let a
        # concurrent refresh swap it between the freshness test and the return, and it is also
        # what lets a type checker see that the returned value is not None.
        cached = _certs_cache
        if cached is not None and _now_monotonic() - _certs_fetched_at < CERT_TTL_SECONDS:
            return cached
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

    # Fetch certs OUTSIDE the try: a cert-endpoint outage or a bug inside google_certs() is an
    # infrastructure/programming fault, not evidence of a forged token, and must not be relabelled
    # as one. Collapsing the two would mean an operator watching this boundary could never tell
    # "Google's cert endpoint is down" from "someone is sending us forged tokens" - every sign-in
    # would fail identically either way, hiding the real cause permanently.
    certs = (certs_provider or google_certs)()
    try:
        claims = _decode(token, certs=certs, audience=project_id)
    except Exception as exc:
        # Signature, expiry and audience failures from google.auth.jwt.decode all land here, and
        # all become the same refusal - that collapse is intended. The cause is preserved via
        # `from exc` for anyone debugging with the traceback, but it is deliberately NOT
        # interpolated into the message: some of the decoder's own pre-signature-check messages
        # (e.g. "Certificate for key id {} not found", "Unsupported signature algorithm {}") echo
        # the token's own header fields verbatim, and those fields are attacker-controlled. Putting
        # them in a message that may reach a log is a log-injection vector from untrusted input.
        raise InvalidToken("token did not verify") from exc

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
