# Responsibility: Recommend engines compatible with what the user described.
# Boundaries: advice derived from declared capabilities; a recommendation is never a selection.
from __future__ import annotations

import re

from meshpipeline.agents.intake.validation import ADMIT_IMPOSSIBLE, ADMIT_SUPPORTED, preview_admission

# Explicit ways a user asks which engine to use. Deliberately phrase-level (never a bare word like
# "engine" or "which"), and deliberately incomplete - a false negative asks a clarifying question,
# a false positive volunteers alternatives the user never asked for.
_REQUEST_PHRASES = (
    "which engine", "which engines", "what engine", "what engines",
    "which mesher", "which meshers", "what mesher", "what meshers",
    "which solver can mesh", "which tool can mesh",
    "recommend an engine", "recommend a engine", "recommend engine", "recommend engines",
    "recommend a mesher", "recommend an mesher", "recommend mesher",
    "recommend one", "recommend something", "recommendation", "recommendations",
    "compatible engine", "compatible engines", "compatible mesher", "compatible meshers",
    "compare engine", "compare engines", "compare the engine", "compare the engines",
    "compare mesher", "compare meshers",
    "what alternative", "what alternatives", "which alternative", "which alternatives",
    "any alternative", "any alternatives", "other engine", "other engines",
    "other mesher", "other meshers",
    "list the compatible", "list compatible", "engine options", "meshing options",
    "what else could", "what else can", "which else can",
    "suggest an engine", "suggest a mesher", "suggest an alternative",
)


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold())).strip()


#: HANDING THE CHOICE BACK IS ASKING FOR ONE. A customer who says "you decide" has asked which engine
#: to use as plainly as one who says "which engine" - more plainly, because they have also said they
#: do not want to be asked again. Without these, `recommendation_requested` was False for every one
#: of them, `rec_authorized` was False, and UNAUTHORIZED_GUIDANCE told the model to reply "would you
#: like engine recommendations?" - bouncing the question back at the one customer who had already
#: said they did not want it. A real conversation said "you decide", "u decide best case" and "not
#: sure" and was asked to choose anyway, every time.
#:
#: DEFERRAL OF A CHOICE, NOT IGNORANCE OF A FACT. "I don't know" and "no idea" are deliberately NOT
#: here: they are how someone answers "what velocity?", and reading them as "pick an engine for me"
#: would volunteer alternatives nobody asked for - the false positive this module's own header warns
#: about. Every phrase below hands the DECISION over.
_DEFER_PHRASES = (
    "you decide", "u decide", "you choose", "u choose", "you pick", "u pick",
    "you tell me", "u tell me", "your call", "ur call", "up to you", "up to u",
    "you go ahead and pick", "whatever you think", "whatever u think",
    "whatever is best", "whatever's best", "whichever is best", "whichever you",
    "whichever u", "best one", "best option", "best case", "best fit", "best choice",
    "you know best", "u know best", "doesn t matter", "does not matter", "no preference",
    "i don t mind", "dont mind", "surprise me", "your choice", "ur choice",
    "pick for me", "choose for me", "decide for me", "you suggest", "u suggest",
)


def choice_deferred(latest_user_message: str) -> bool:
    """The customer has handed the decision to us in this message.

    Separate from `recommendation_requested` because the two authorize different things: asking
    which engines are compatible authorizes a COMPARISON, while handing the choice over also means
    the customer does not want to be asked again, which is what lets the same turn go on to propose
    one. Neither ever SELECTS - the application still shows its own question and the customer still
    answers it in their own words.
    """
    return any(p in _norm(latest_user_message) for p in _DEFER_PHRASES)


def recommendation_requested(latest_user_message: str) -> bool:
    t = _norm(latest_user_message)
    return any(p in t for p in _REQUEST_PHRASES) or choice_deferred(latest_user_message)


UNAUTHORIZED_GUIDANCE = (
    "Recommendation mode is NOT authorized for this turn: the user's latest message did not ask "
    "which engines are compatible. Do not name or list any alternative engine. Ask them plainly "
    "whether they would like engine recommendations, and stop there."
)


def recommend_compatible_engines(purpose: str, input_kind: str, dimensionality: str | None = None,
                                 patches=None, engine_params=None, geometry_facts=None,
                                 *, authorized: bool) -> dict:
    if not authorized:
        return {"recommendation_authorized": False, "candidates": [], "compatible_engines": [],
                "authorizes_selection": False, "authorizes_submission": False,
                "guidance": UNAUTHORIZED_GUIDANCE}

    from meshpipeline.engines.registry import engine_label, engine_names

    candidates = []
    needs_other_input = []
    for name in engine_names():
        r = preview_admission(name, purpose, input_kind, dimensionality=dimensionality,
                              patches=patches, engine_params=engine_params,
                              geometry_facts=geometry_facts)
        verdict = r["verdict"]
        code = str(r.get("blocking_rule_code") or "")
        # An engine the catalog calls physically incapable of this PURPOSE is not an option and no
        # geometry change makes it one. It is dropped here rather than reported as a rejected row,
        # so it cannot be listed, compared, or offered as an alternative.
        if verdict == ADMIT_IMPOSSIBLE and code == "purpose_incompatible":
            continue
        candidates.append({
            # ONE name, the one intake speaks and will send back; the executor boundary
            # converts it to the registry key (agents/intake/vocabulary.py).
            "engine": engine_label(name),
            "supported": verdict == ADMIT_SUPPORTED,
            "verdict": verdict,
            "blocking_rule_codes": r.get("blocking_rule_codes", []) if verdict == ADMIT_IMPOSSIBLE else [],
            # Catalog/engine-authored text only - the model must not invent compatibility reasons.
            "reason": r.get("capability_reason") or r.get("safe_user_message", ""),
        })
        # CAN do this purpose, but not from the geometry as declared - and the catalog's reason
        # already names the geometry that would work. That is the compromise to put to the user.
        if verdict == ADMIT_IMPOSSIBLE and code == "input_kind_incompatible":
            needs_other_input.append(name)

    compatible = [c["engine"] for c in candidates if c["supported"]]
    _NO_SELECTION = (
        "You have NOT selected anything: do not say an engine 'is selected', do not ask the user to "
        "confirm one particular engine, and do not call propose_engine_selection, "
        "preview_selected_admission, submit_requirements or confirm_dispatch in this turn."
    )
    if compatible:
        guidance = (
            "Offer these as engines that LOOK LIKE they suit this case - a suggestion you are "
            "making, not a complete menu of everything that exists. Say so in those terms ('a "
            "couple of engines that should suit this', not 'the options' or 'all engines "
            "available'), and invite the user to name a different one if they have a preference. "
            "Use ONLY these rows and these reasons - do not add engines, invent reasons, or rank "
            f"by your own knowledge. {_NO_SELECTION} Ask which one the user wants and wait for "
            "their next message, even if exactly one engine is compatible."
        )
    elif needs_other_input:
        guidance = (
            "NO engine can mesh the geometry as it is currently declared. Every engine listed here "
            "CAN do this purpose but needs a different geometry, and each row's reason names exactly "
            "which. Put that to the user as the compromise it is: state what they declared, list "
            "each engine with the geometry it would need, and ask which they want to pursue - "
            f"supplying different geometry, or changing the purpose. {_NO_SELECTION} Report only "
            "these reasons; do not invent a workaround the catalog did not state."
        )
    else:
        guidance = (
            "No registered engine can do this purpose at all, and no change of geometry would help. "
            f"Say so plainly and ask what the user wants to do instead. {_NO_SELECTION}"
        )
    return {
        "recommendation_authorized": True,
        "candidates": candidates,
        "compatible_engines": compatible,
        "needs_other_input": needs_other_input,
        "authorizes_selection": False,
        "authorizes_submission": False,
        "guidance": guidance,
    }
