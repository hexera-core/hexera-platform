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


#: WHAT AN FEA REPLY NEVER ASKS: settled on the FEA path (version 1 meshes the solid with one
#: mesher and quadratic tetrahedra, sized by the plan's rules). The real-path test of 2026-10-22
#: asked the engine with CFD words ("no reliable near-wall layers", "blade-row passages") and then
#: "second-order, or first-order?" on every part. (what it is, how a question about it reads)
SETTLED = (
    ("the element order", r"\b(?:first|second)[- ]order\b|\belement order\b|\blinear\b[^?]*\bquadratic\b|"
                          r"\bquadratic\b[^?]*\blinear\b|\b(?:tet4|tet10|c3d4|c3d10)\b"),
    ("the engine", r"\b(?:mesher|engine|gmsh|netgen|cfmesh|snappy\w*)\b"),
    ("near-wall layers", r"\bnear[- ]wall\b|\bprism layers?\b|\bboundary layers?\b|\binflation layers?\b|\by\s?\+"),
    ("a detail level", r"\bdetail level\b|\bmesh fidelity\b|\b(?:quick|standard|fine) (?:level|mesh)\b|"
                       r"\bhow (?:fine|coarse)\b|\belement count\b"),
    # THE UNIT IS THE FEA STAGE'S: its unit box and size-based suggestion settle it (Areen's Camaro
    # STL, 2026-10-23: "should I treat the STEP coordinates as millimetres?")
    ("the unit", r"\bunits?\b|\bcoordinates\b|\bmillimet\w*|\bcentimet\w*|\binch(?:es)?\b"),
)
_SENTENCE = re.compile(r"[^.?!\n]*[?]|[^.?!\n]*\bor (?:would you|do you|should i)\b[^.?!\n]*[.!]?", re.IGNORECASE)


#: The approval question ("Shall I proceed with the mesh?") names what is approved; it asks nothing settled.
_APPROVAL = re.compile(r"\b(?:proceed|go ahead|start (?:it|the|meshing)|shall i (?:mesh|run|start)|"
                       r"reply yes|approve)\b", re.IGNORECASE)


def _questions(text: str) -> list[str]:
    """The sentences of `text` that ask something (end in a question mark, or offer 'or would you'),
    leaving out the approval question."""
    return [q for q in (m.group(0).strip() for m in _SENTENCE.finditer(str(text or "")))
            if q and not _APPROVAL.search(q)]


def settled_asks(text: str, user_texts: Iterable[str] = ()) -> list[str]:
    """What settled thing `text` ASKS the user about, each kind once, in SETTLED's order - [] when it
    asks about none. A topic the user raised themselves ("can I have first-order elements?") is
    theirs to discuss, and is not counted."""
    theirs = "\n".join(str(t or "") for t in user_texts)
    asked = _questions(text)
    out = []
    for kind, rx in SETTLED:
        if any(re.search(rx, q, re.IGNORECASE) for q in asked) and not re.search(rx, theirs, re.IGNORECASE):
            out.append(kind)
    return out


def without_settled_asks(text: str, user_texts: Iterable[str] = ()) -> str:
    """`text` with the sentences that ask a settled thing taken out ('' when nothing else is left)."""
    theirs = "\n".join(str(t or "") for t in user_texts)
    out = str(text or "")
    for q in _questions(out):
        if any(re.search(rx, q, re.IGNORECASE) and not re.search(rx, theirs, re.IGNORECASE)
               for _, rx in SETTLED):
            out = out.replace(q, "")
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", out)).strip()


__all__ = ["INVENTED", "SETTLED", "invented", "settled_asks", "without_settled_asks"]
