# Responsibility: Decide which words reach the user after an admission refusal the model did not repair.
# Owns: the checks the model's own reply must pass, and the fall back to the rendered finding.
# Boundaries: it composes nothing and calls no model - the reply is written in the loop, where the
# model holds the finding as a tool result; this only guards what that reply may say.
from __future__ import annotations

import logging
from dataclasses import dataclass

from meshpipeline.agents.intake import vocabulary as _vocab

logger = logging.getLogger(__name__)

#: The rendered message is ~600 characters. Past this the reply is no longer about the finding.
MAX_CHARS = 1400


def check(text: str, facts: dict) -> list[str]:
    # What must never reach the user, not what must be said. Requiring every declared value be
    # repeated forced the model to print a list it had already been given, which is what made
    # these replies read like a form - and it guarded nothing, because the declared values are
    # held in the record and re-checked at submission, never in this prose. A reply that fails
    # any check here is discarded rather than repaired.
    problems: list[str] = []
    body = (text or "").strip()
    if not body:
        return ["empty"]
    if len(body) > MAX_CHARS:
        problems.append(f"too long ({len(body)} > {MAX_CHARS})")

    lowered = body.lower()
    for other in _vocab.engines_named_in(body, str(facts.get("selected_engine") or "")):
        problems.append(f"names another engine ({_vocab.to_display(_vocab.ENGINE, other)})")

    if "revise" not in lowered and "?" not in body:
        problems.append("asks the user nothing")
    return problems


@dataclass(frozen=True)
class Refusal:

    text: str
    #: "composed" when the model's own reply passed the checks, "rendered" otherwise.
    source: str


def settle(candidate: str, facts: dict) -> Refusal:
    # The model received the finding as a tool result and wrote its reply with it in hand. That
    # reply is what the user reads - unless it names another engine, answers nothing or is empty
    # (the loop ran out of rounds), in which case the rendered finding is delivered instead: it
    # is already correct, so a reply that fails costs the user nothing but the nicer wording.
    rendered = str(facts.get("safe_user_message") or "").strip()
    problems = check(candidate, facts)
    if problems:
        logger.info("Intake: refusal reply rejected (%s) - using the rendered finding",
                    "; ".join(problems))
        return Refusal(rendered, "rendered")
    return Refusal(str(candidate or "").strip(), "composed")


__all__ = ["MAX_CHARS", "Refusal", "check", "settle"]
