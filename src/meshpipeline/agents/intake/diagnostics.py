# Responsibility: Extend an intake run record with the facts diagnostics need.
# Boundaries: intake's slice of the shared accountability vocabulary.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meshpipeline.contracts.agent_loop import DiagnosticValue


@dataclass(frozen=True)
class IntakeRunExtension:

    missing_fields: tuple[str, ...] = ()      # required-value KEYS only, never their values
    tools_invoked: tuple[str, ...] = ()
    submit_attempts: int = 0
    submit_rejections: int = 0
    authorization_state: str = ""             # "", "unauthorized", "authorized"
    selection_state: str = ""                 # "", "proposed", "confirmed"
    approval_state: str = ""                  # "", "awaiting", "approved"
    recommendation_turn: bool = False
    canonical_revision: str = ""              # the app-computed revision id, not the payload

    def sanitized(self) -> Mapping[str, DiagnosticValue]:
        return {
            "missing_fields": self.missing_fields,
            "tools_invoked": self.tools_invoked,
            "submit_attempts": self.submit_attempts,
            "submit_rejections": self.submit_rejections,
            "authorization_state": self.authorization_state,
            "selection_state": self.selection_state,
            "approval_state": self.approval_state,
            "recommendation_turn": self.recommendation_turn,
            "canonical_revision": self.canonical_revision,
        }


__all__ = ["IntakeRunExtension"]
