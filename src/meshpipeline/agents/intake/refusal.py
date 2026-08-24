# Responsibility: Turn an impossible-admission verdict into the sentences the user actually reads.
# Owns: the paraphrase request, the checks its result must pass, and the fall back to the rendered text.
# Boundaries: it composes only - it holds no tool, changes no state, and can never turn a refusal into a pass.
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from meshpipeline.agents.intake import vocabulary as _vocab

logger = logging.getLogger(__name__)

#: The rendered message is ~600 characters. Past this the reply is no longer a paraphrase.
MAX_CHARS = 1400

_INSTRUCTION = (
    "A user asked for a mesh their selected engine cannot produce. Below is the finding, and the "
    "setup they declared.\n\n"
    "Tell them what the engine cannot do and what that means for what they were trying to get. "
    "Two to four sentences of ordinary prose - what a colleague would say, not a form.\n\n"
    "Do NOT list their declared values back to them. They can see what they asked for, and the "
    "finding already names whatever mattered; repeating it is what makes these replies read like "
    "a receipt.\n\n"
    "Say plainly that nothing was changed, and end by asking which part they want to revise. Do "
    "not add a cause, a workaround or an opinion. Name NO engine other than the one they "
    "selected - if they want alternatives they will ask, and another part of the system answers "
    "that. No preamble, no apology, no restating of these rules."
)


def _declared_lines(declared: dict) -> list[str]:
    # Shown to the model in the words the user was offered. Handing it the keys invites it to
    # repeat them back, and "external_cfd" is a routing value, not a name anyone chose to show.
    out = []
    for field, label in (("engine", "engine"), ("purpose", "purpose"),
                         ("input_kind", "geometry"), ("dimensionality", "dimensionality")):
        value = declared.get(field)
        if not value:
            continue
        out.append(f"{label}: {_vocab.display_of_field(field, value)}")
    ext = declared.get("requested_extents") or {}
    ref = declared.get("reference_length_m")
    if any(x is not None for x in ext.values()) and ref:
        stated = ", ".join(f"{k} {ext[k]:g}L" for k in
                           ("upstream", "downstream", "lateral", "vertical")
                           if ext.get(k) is not None)
        out.append(f"far-field request: {stated} (L = {ref:g} m)")
    if declared.get("requirements_strict"):
        out.append("requirements: STRICT - near-misses are refused, never delivered with a note")
    patches = declared.get("patches") or []
    if patches:
        def _one(p: dict) -> str:
            bits = [f"{p.get('name')} ({p.get('role') or p.get('type')})"]
            if p.get("diameter_mm") is not None:
                bits.append(f"d={p['diameter_mm']}mm")
            elif p.get("area_mm2") is not None:
                bits.append(f"A={p['area_mm2']}mm2")
            elif p.get("width_mm") is not None and p.get("height_mm") is not None:
                bits.append(f"{p['width_mm']}x{p['height_mm']}mm")
            if isinstance(p.get("near_mm"), (list, tuple)):
                bits.append("near " + "/".join(str(c) for c in p["near_mm"]) + "mm")
            if p.get("interchangeable_with"):
                bits.append("interchangeable with " + ", ".join(p["interchangeable_with"]))
            return " ".join(bits)
        named = ", ".join(_one(p) for p in patches)
        out.append(f"patches ({len(patches)}): {named}")
    return out


def request_messages(facts: dict) -> list[dict]:
    declared = facts.get("preserved_declared_values") or {}
    body = "\n".join([
        _INSTRUCTION, "",
        "FINDING:", str(facts.get("capability_reason") or "").strip(), "",
        "DECLARED BY THE USER:", *_declared_lines(declared),
    ])
    return [{"role": "user", "content": body}]


def check(text: str, facts: dict) -> list[str]:
    # What must never reach the user, not what must be said. Requiring every declared value be
    # repeated forced the model to print a list it had already been given, which is what made
    # these replies read like a form - and it guarded nothing, because the declared values are
    # held in the record and re-checked at submission, never in this prose. A paraphrase that
    # fails any check here is discarded rather than repaired.
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
    #: "paraphrased" when the model's text passed the checks, "rendered" otherwise.
    source: str
    # Reported even when the text is discarded - the call was still made and billed.
    input_tokens: int = 0
    output_tokens: int = 0


async def explain(facts: dict, *, provider_call: Any, job_id: str, user_id: str) -> Refusal:
    # The rendered message is the fallback for every failure: it is already correct, so a provider
    # that is down or answers badly costs the user nothing.
    rendered = str(facts.get("safe_user_message") or "").strip()
    try:
        response = await provider_call(messages=request_messages(facts), tools=[],
                                       job_id=job_id, user_id=user_id, tool_choice="none")
    except Exception as exc:
        logger.info("Intake: refusal paraphrase unavailable (%s) - using the rendered message", exc)
        return Refusal(rendered, "rendered")

    spent = {"input_tokens": int(getattr(response, "input_tokens", 0) or 0),
             "output_tokens": int(getattr(response, "output_tokens", 0) or 0)}
    candidate = str(getattr(response, "assistant_text", "") or "").strip()
    problems = check(candidate, facts)
    if problems:
        logger.info("Intake: refusal paraphrase rejected (%s) - using the rendered message",
                    "; ".join(problems))
        return Refusal(rendered, "rendered", **spent)
    return Refusal(candidate, "paraphrased", **spent)


__all__ = ["MAX_CHARS", "Refusal", "check", "explain", "request_messages"]
