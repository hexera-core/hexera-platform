# Responsibility: Convert a conversation into the message list a provider's API accepts.
# Boundaries: shape only - it copies each turn and refuses one with no role; it never edits content.
from __future__ import annotations

from typing import Any

from meshpipeline.contracts.model_inference import Conversation


def to_provider_messages(conversation: Conversation) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in conversation:
        if "role" not in msg:
            raise ValueError(f"a conversation turn has no role: {sorted(msg)!r}")
        out.append(dict(msg))
    return out
