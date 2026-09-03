# Responsibility: Declare the public API key format, and the only comparison that may accept a secret.
# Boundaries: bytes and strings; where a key is stored and when it is valid belongs to persistence and application.
from __future__ import annotations

import hmac
import secrets
import string
from dataclasses import dataclass
from hashlib import sha256

#: The literal every live key starts with. It is deliberately visible in the credential itself so a
#: leaked key can be recognised as one - by a secret scanner, by a log filter, by a person reading a
#: paste - without the finder having to try it against the API.
LIVE_PREFIX = "hx_live"

#: The secret half, in bytes of entropy handed to secrets.token_urlsafe.
SECRET_ENTROPY_BYTES = 32

#: The public half. Base62 ONLY: the presented key is split on the first separator after
#: LIVE_PREFIX, so an identifier containing that separator would silently move bytes across the
#: boundary and make two different keys parse to the same prefix.
_IDENTIFIER_ALPHABET = string.ascii_letters + string.digits
_IDENTIFIER_LENGTH = 12
_SEPARATOR = "_"

#: A well-formed hash of a secret nobody holds. Presented to `secret_matches` when a prefix
#: resolves to no row, so an unknown prefix costs the same comparison a known one does - without
#: it, response time answers "does this key exist?" for free.
DECOY_HASH = sha256(secrets.token_bytes(32)).hexdigest()


@dataclass(frozen=True)
class MintedKey:
    #: the public, stored, indexed half - "hx_live_<identifier>"
    key_prefix: str
    #: SHA-256 of the secret; this is what the row keeps
    key_hash: str
    #: the secret half. Held only long enough to hand back to the issuer, never stored.
    secret: str
    #: what the holder presents: "<key_prefix>_<secret>". Shown once, at creation.
    presented: str


@dataclass(frozen=True)
class PresentedKey:
    key_prefix: str
    secret: str


def hash_secret(secret: str) -> str:
    return sha256((secret or "").encode()).hexdigest()


def mint() -> MintedKey:
    identifier = "".join(secrets.choice(_IDENTIFIER_ALPHABET) for _ in range(_IDENTIFIER_LENGTH))
    key_prefix = f"{LIVE_PREFIX}{_SEPARATOR}{identifier}"
    secret = secrets.token_urlsafe(SECRET_ENTROPY_BYTES)
    return MintedKey(key_prefix=key_prefix, key_hash=hash_secret(secret), secret=secret,
                     presented=f"{key_prefix}{_SEPARATOR}{secret}")


def parse(presented: str | None) -> PresentedKey | None:
    # Shape only. A credential this refuses is not a key of ours, so it never becomes a query.
    candidate = (presented or "").strip()
    head = f"{LIVE_PREFIX}{_SEPARATOR}"
    if not candidate.startswith(head):
        return None
    identifier, sep, secret = candidate[len(head):].partition(_SEPARATOR)
    if not sep or not secret or not identifier:
        return None
    if any(c not in _IDENTIFIER_ALPHABET for c in identifier):
        return None
    return PresentedKey(key_prefix=f"{head}{identifier}", secret=secret)


def secret_matches(secret: str, key_hash: str) -> bool:
    # compare_digest, never ==: a byte-by-byte comparison that returns early leaks how much of a
    # forged hash was right, which is enough to construct the rest. Same discipline as
    # api/security.py's signature check.
    return hmac.compare_digest(hash_secret(secret), (key_hash or "").lower())
