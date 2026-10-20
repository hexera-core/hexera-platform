# Responsibility: Catch an FEA reply that invents what the user never said - a load, a support or a
# material - so it goes back for a rewrite before the user reads it.
# Boundaries: pure. Text in, the invented things out; it reads no store and decides nothing about
# the mesh. Version 1 asks one thing of an FEA part (the analysis type) and names every face, so
# supports, loads and materials are the user's to set in their own solver (design section 3).
# Collaborates with: agents/intake/loop_policy.py (sends the reply back once), the fea_path prompt
# block (agents/intake/agent.py), which says the same rule in words.
from __future__ import annotations

import re
from collections.abc import Iterable

#: WHAT AN FEA REPLY NEVER PROPOSES unless the user said it first: (what it is, how it reads).
#: The audit's bracket chat (2026-10-19) proposed "1 kN downward on the opposite end face" and
#: "steel E=210 GPa"; neither was the user's.
INVENTED = (
    ("a force", r"\b\d+(?:[.,]\d+)?\s?(?:k|m)?n\b(?!\w)|\b\d+(?:[.,]\d+)?\s?(?:lbf|kgf)\b"),
    ("a pressure or stress value", r"\b\d+(?:[.,]\d+)?\s?(?:[kmg]?pa|psi|bar)\b"),
    ("a material", r"\b(?:steel|aluminium|aluminum|titanium|stainless|cast iron|brass|copper|abs plastic|"
                   r"young'?s modulus|poisson'?s ratio|e\s?=\s?\d)"),
    ("a support", r"\b(?:fixed|clamped|pinned|encastre|built-in) (?:support|face|end|edge|boundary|at|on)\b|"
                  r"\bfix(?:ed)? the (?:\w+ )?(?:face|end|edge)\b"),
    ("a load", r"\b(?:apply|applied|applying) (?:a |an |the )?(?:\w+ )?(?:load|force|pressure|moment|torque)\b|"
               r"\b(?:load|force|pressure) (?:of|on) (?:the )?\w*\s?(?:face|end|edge|surface)\b|\bdownward\b"),
)


def invented(text: str, user_texts: Iterable[str] = ()) -> list[str]:
    """What `text` proposes that the user never said, each kind once, in INVENTED's order - [] when
    it proposes nothing of the kind. A kind the user's own words already carry ("load it with
    500 N", "it is steel") is theirs, and is not counted."""
    said = str(text or "")
    theirs = "\n".join(str(t or "") for t in user_texts)
    out = []
    for kind, rx in INVENTED:
        if re.search(rx, said, re.IGNORECASE) and not re.search(rx, theirs, re.IGNORECASE):
            out.append(kind)
    return out


__all__ = ["INVENTED", "invented"]
