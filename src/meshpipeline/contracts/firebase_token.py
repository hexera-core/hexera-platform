# Responsibility: Declare what a verified sign-in token names in this system, and who is allowed to verify one.
# Owns: the verified-identity value, the single refusal, and the binding to whichever provider does the verifying.
# Boundaries: no provider vocabulary and no outbound call - which claims a token must carry, and where its
# signing keys come from, belong to the adapter for the identity provider that minted it.
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class InvalidToken(Exception):
    """One exception for every way a token fails. The caller answers one refusal either way:
    which part was wrong is only useful to somebody probing which of their guesses is close."""


class NoTokenVerifier(RuntimeError):
    """No verifier is bound. Deliberately NOT an InvalidToken: a deployment that forgot to
    compose its adapters has not been shown a bad token, and answering 401 would report our
    own misconfiguration as the caller's forgery - indistinguishable, in the log, from an
    attack. It surfaces as a 500, which is what it is."""


@dataclass(frozen=True)
class VerifiedToken:
    #: the identity provider's subject - stable across an email change, which is why it, and not
    #: the address, is what `users.firebase_uid` stores
    uid: str
    #: lowercased, because owner_id is the lowercased email everywhere in this schema
    email: str
    email_verified: bool
    name: str


@runtime_checkable
class TokenVerifier(Protocol):
    def __call__(self, raw_token: str, *, project_id: str) -> VerifiedToken: ...


# INJECTED here by runtime composition. api/ calls verify() (this neutral entry point) - it never
# names the identity provider, so replacing the provider is a composition change, not a product one.
_verifier: TokenVerifier | None = None


def set_token_verifier(verifier: TokenVerifier | None) -> None:
    global _verifier
    _verifier = verifier


def verify(raw_token: str, *, project_id: str) -> VerifiedToken:
    if _verifier is None:
        raise NoTokenVerifier(
            "no identity-token verifier configured - runtime composition must call "
            "set_token_verifier()")
    return _verifier(raw_token, project_id=project_id)
