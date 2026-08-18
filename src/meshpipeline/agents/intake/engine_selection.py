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
