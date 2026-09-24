# Responsibility: Propose, confirm and verify which engine the user chose.
# Boundaries: a user-named engine is honoured only when the user actually named it.
from __future__ import annotations

import re
import time
import uuid

from meshpipeline.engines.registry import engine_label

PROPOSED_TTL_S = 900     # a proposal the user never answers goes stale
CONFIRMED_TTL_S = 3600   # a confirmed selection survives a long gathering conversation

NO_SELECTION = "no_selection"
PROPOSED = "proposed_selection"
CONFIRMED = "confirmed_selection"

# How the selection was established. `structured_input` is the ONLY path that may skip the
# natural-language confirmation turn, and only when the user directly submitted the typed engine
# field that will be dispatched.
VIA_CONVERSATION = "conversation"
VIA_STRUCTURED_INPUT = "structured_input"


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def state_of(sel: dict | None) -> str:
    if not sel or not isinstance(sel, dict):
        return NO_SELECTION
    if time.time() > sel.get("expires_at", 0):
        return NO_SELECTION
    return str(sel.get("state") or NO_SELECTION)


def propose(engine: str, *, session_id: str, owner_id: str, revision: str,
            user_msg_count: int) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "engine": (engine or "").strip().lower(),
        "state": PROPOSED,
        "via": VIA_CONVERSATION,
        "session": session_id,
        "owner": owner_id,
        "proposed_revision": revision,
        "proposed_msg_count": int(user_msg_count),
        "confirmed_revision": None,
        "expires_at": time.time() + PROPOSED_TTL_S,
    }


def select_from_structured_input(engine: str, *, session_id: str, owner_id: str,
                                 revision: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "engine": (engine or "").strip().lower(),
        "state": CONFIRMED,
        "via": VIA_STRUCTURED_INPUT,
        "session": session_id,
        "owner": owner_id,
        "proposed_revision": revision,
        "confirmed_revision": revision,
        "expires_at": time.time() + CONFIRMED_TTL_S,
    }


def quote_is_from_user(quote: str, latest_user_message: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(latest_user_message)


def user_named_engine(engine: str, quote: str, latest_user_message: str) -> bool:
    if not quote_is_from_user(quote, latest_user_message):
        return False
    q = _norm(quote)
    names = {_norm(engine), _norm(engine_label(engine))}
    return any(n and n in q for n in names)


#: The words that answer the question `render_selection_statement` asks - "Do you want to select X?".
#: They live beside the renderer so the question and the reader of its answer cannot drift apart.
#: Deliberately small: a word that is not here is not an answer, and a message that is not an answer
#: never selects anything.
_AFFIRM = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "yea", "aye", "ok", "okay", "sure", "correct", "right",
    "confirm", "confirmed", "confirming", "proceed", "affirmative", "definitely", "absolutely",
})

#: Refusals and hesitations. Any one of these ANYWHERE in the message stops it counting as an answer,
#: because "yes, no wait" and "yes but a different one" are not selections.
_DENY = frozenset({
    "no", "nope", "nah", "not", "dont", "don't", "never", "stop", "cancel", "wait", "hold",
    "instead", "change", "different", "another", "other", "rather", "actually",
})

#: "yees", "yesss". A real user typed the first one at a question that would not take yes for an
#: answer, which is the whole reason this reader exists.
_YES_TYPO = re.compile(r"^y+e+s+$")


def _words(text) -> list[str]:
    return re.findall(r"[a-z']+", _norm(text))


def affirms(latest_user_message) -> bool:
    """Does this message answer YES to the yes/no question the application just put on screen?

    Not "is it positive in tone". A denial or a hesitation anywhere in the message means there is no
    answer here, and a question with no yes in it is not an answer either - someone replying "what is
    it?" has chosen nothing.
    """
    msg = _norm(latest_user_message)
    if not msg:
        return False
    ws = _words(msg)
    if any(w in _DENY for w in ws):
        return False
    said_yes = any(w in _AFFIRM or _YES_TYPO.match(w) for w in ws)
    return bool(said_yes)


def names_engine(engine: str, latest_user_message) -> bool:
    """The user's OWN message names this engine, with no quote from the model involved."""
    msg = _norm(latest_user_message)
    names = {_norm(engine), _norm(engine_label(engine))}
    return any(n and n in msg for n in names)


def answers_the_selection_question(engine: str, quote: str, latest_user_message,
                                   *, outstanding: bool) -> bool:
    """Has the user chosen this engine, by any proof this application accepts?

    THE PROOF IS ALWAYS THE USER'S OWN WORDS. Three shapes of it:
      - the model quoted them and the quote names the engine - `user_named_engine`, unchanged;
      - their own message names the engine AND answers yes, so no quote is needed to see it;
      - the application has ALREADY asked about this engine and their message answers yes, or simply
        names it back at the question.

    The third shape applies only while that question is outstanding, and that is what keeps "what is
    snappyHexMesh?" from selecting anything: it answers nothing, so `affirms` is False, and a bare
    mention counts only as the answer to a question that was actually asked.

    WHY THE CODE READS THE MESSAGE ITSELF. The quote argument is OPTIONAL, so a model that simply
    omitted it made this application re-ask a question the user had already answered - and since
    re-proposing replaced the pending question, every further "yes" was spent re-asking rather than
    answering. The consent was in hand and went unread. A message this code reads for itself cannot
    be forgotten by a caller.
    """
    if user_named_engine(engine, quote, latest_user_message):
        return True
    named = names_engine(engine, latest_user_message)
    said_yes = affirms(latest_user_message)
    if named and said_yes:
        return True
    if outstanding and (said_yes or (named and "?" not in _norm(latest_user_message))):
        return True
    return False



def confirm(sel: dict | None, *, session_id: str, owner_id: str, revision: str,
            quote: str, latest_user_message: str,
            user_msg_count: int | None = None) -> tuple[dict | None, str]:
    st = state_of(sel)
    if st == CONFIRMED:
        return None, "that engine selection is already confirmed"
    if st != PROPOSED or not sel:
        return None, ("no engine selection is awaiting confirmation - call propose_engine_selection "
                      "first and let the user answer")
    if sel.get("session") != session_id or sel.get("owner") != owner_id:
        return None, "that engine selection belongs to a different session or owner"
    if (user_msg_count is not None and "proposed_msg_count" in sel
            and int(user_msg_count) != int(sel["proposed_msg_count"]) + 1):
        # The user said something else after the question was asked, so this proposal is stale -
        # a NEW user revision invalidates a pending selection rather than silently outliving it.
        return None, ("that engine question is stale - the user has said something else since; "
                      "propose the engine again and let them answer it")
    if not quote_is_from_user(quote, latest_user_message):
        return None, ("the words you quoted are not in the user's latest message - they have not "
                      "confirmed the engine; ask them")
    return {**sel, "state": CONFIRMED, "confirmed_revision": revision,
            "expires_at": time.time() + CONFIRMED_TTL_S}, ""


def verify_confirmed(sel: dict | None, engine: str, *, session_id: str,
                     owner_id: str) -> tuple[bool, str]:
    st = state_of(sel)
    if st == NO_SELECTION:
        return False, ("no engine has been selected and confirmed by the user - an engine that was "
                       "only recommended or only proposed is NOT selected")
    if st == PROPOSED:
        return False, ("that engine is only PROPOSED - the user has not confirmed it yet; ask them "
                       "to confirm before checking admission")
    if not sel or sel.get("session") != session_id or sel.get("owner") != owner_id:
        return False, "that engine selection belongs to a different session or owner"
    want = (engine or "").strip().lower()
    if want != sel.get("engine"):
        return False, (f"the confirmed engine is {sel.get('engine')!r}, not {want!r} - to change it, "
                       "propose the new engine and have the user confirm it")
    return True, ""


def render_selection_statement(engine: str) -> str:
    shown = engine_label(engine)
    return (f"Selected engine: {shown}\n\n"
            "This is a proposal, not a selection - nothing has been selected yet and nothing will "
            f"be meshed. Do you want to select {shown}?")
