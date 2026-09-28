# Responsibility: Propose, confirm and verify which engine the user chose.
# Boundaries: a user-named engine is honoured only when the user actually named it, and a plain
# yes binds to the engine the application proposed - never to one the model would rather have.
from __future__ import annotations

import re
import time
import uuid

from meshpipeline.agents.intake import vocabulary as _vocab
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

# A PLAIN YES to the application's own question. The question names the engine ("Do you want to
# select snappyHexMesh?"), so "yes", "ok", "sure" or "go with that" is a complete answer that binds
# to that engine and needs no quote of the model's. Whole-message, as the approval grammar is: every
# word must be assent or filler, so "yes, but use cfMesh" is not assent - it names another engine,
# and that is refused below on its own. A hedge ("maybe", "I think so") has no assent word and is
# not assent either; the model may still quote a hesitant user's words, and the quote is checked.
_ASSENT = frozenset({
    "yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "fine", "go", "ahead", "goahead",
    "agree", "agreed", "confirm", "confirmed", "correct", "right", "proceed", "select", "use",
    "keep", "choose", "pick", "alright", "absolutely", "definitely", "certainly", "affirmative",
})
_FILLER = frozenset({
    "please", "thanks", "thank", "you", "then", "now", "just", "lets", "let", "us", "s", "t",
    "d", "ll", "ve", "re", "m", "and", "that", "this", "it", "one", "the", "with", "is", "be",
    "good", "great", "perfect", "sounds", "works", "me", "for", "i", "we", "do", "so", "engine",
    "mesher", "as", "proposed", "suggested",
})
# A REFUSAL, read from where the negation sits rather than from its presence: "yes, use
# snappyHexMesh, but don't worry about mesh density" is a yes with a negation aimed at another
# clause, while "yes, but I don't want snappyHexMesh" is a no. A negation refuses when it opens
# the message ("no, don't use snappyHexMesh"), ends it ("definitely not", "I'd rather not"),
# follows an intensifier ("absolutely not, ..."), opens or closes the answer as a refusal idiom
# ("yes, I don't think so", "hmm, not really" - but not "yes, but not yet on the refinement",
# where the idiom is aimed at the refinement), or negates a choice verb whose object is the
# engine or a pronoun standing alone for it ("don't want snappyHexMesh", "can't use that",
# "won't go with it" - but not "don't want that ground patch", where the pronoun points at the
# noun after it). Every "n't" is read as "not" first, so the apostrophe never decides. Two
# idioms mean the opposite of their words and go first.
_NEGATION = frozenset({"not", "dont", "never", "cancel", "stop"})
_DECLINE_FIRST = _NEGATION | frozenset({"no", "nope", "nah", "wait", "hold"})
#: In their filler-free form ("I don't think so" reads as "not think", "forget it" as "forget"),
#: matched only where the answer begins or ends: "yes, forget it" retracts, "yes, but forget the
#: ground patch" does not.
_DECLINE_IDIOMS = ("not think", "not really", "rather not", "prefer not", "no way", "not yet",
                   "not now", "changed my mind", "forget")
_INTENSIFIERS = frozenset({"definitely", "absolutely", "certainly", "surely"})
_CHOICE = frozenset({"want", "like", "need", "prefer", "choose", "select", "use", "pick", "take",
                     "keep", "go", "fancy", "wish"})
#: What stands for the proposed engine as the object of a negated choice verb.
_ENGINE_WORD = "xengine"
_PRONOUNS = frozenset({"it", "that", "this"})
_IDIOMS = ("no problem", "no worries", "why not")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _tokens(engine: str, message: str, *, keep_engine: bool) -> list[str]:
    # The message as bare words. Contractions are opened ("don't" -> "do not"), the proposed
    # engine's own name - both spellings, the registry key and the name the user reads - becomes
    # one token or nothing, "go ahead" becomes one word, and the idioms go.
    text = re.sub(r"n['’]t\b", " not", str(message or "").casefold())
    text = re.sub(r"[^0-9a-z]+", " ", text)
    for spelling in {engine, engine_label(engine)}:
        bare = re.sub(r"[^0-9a-z]+", " ", str(spelling or "").casefold()).strip()
        if bare:
            text = re.sub(rf"(?<![0-9a-z]){re.escape(bare)}(?![0-9a-z])",
                          f" {_ENGINE_WORD} " if keep_engine else " ", text)
    text = f" {text} "
    for idiom in _IDIOMS:
        text = text.replace(f" {idiom} ", " ")
    text = text.replace(" go ahead ", " goahead ")
    return text.split()


def plain_assent(engine: str, message: str) -> bool:
    words = [w for w in _tokens(engine, message, keep_engine=False) if w not in _FILLER]
    return bool(words) and all(w in _ASSENT for w in words)


def _stands_for_the_engine(raw: list[str], j: int) -> bool:
    # The object of a negated choice verb: the engine itself, or a pronoun standing alone for it
    # ("don't want it", "don't want that one", "don't want this engine") - not a pronoun that
    # points at the noun after it ("don't want that ground patch").
    if raw[j] == _ENGINE_WORD:
        return True
    if raw[j] not in _PRONOUNS:
        return False
    return j + 1 >= len(raw) or raw[j + 1] in _FILLER


def declines(engine: str, message: str) -> bool:
    raw = _tokens(engine, message, keep_engine=True)
    words = [w for w in raw if w not in _FILLER and w != _ENGINE_WORD]
    if not words:
        return False
    if words[0] in _DECLINE_FIRST or words[-1] in _NEGATION:
        return True
    phrase = " ".join(words)
    if any(phrase == idiom or phrase.startswith(f"{idiom} ") or phrase.endswith(f" {idiom}")
           for idiom in _DECLINE_IDIOMS):
        return True
    for i, w in enumerate(raw):
        if w not in _NEGATION:
            continue
        if i > 0 and raw[i - 1] in _INTENSIFIERS:
            return True
        if (i + 1 < len(raw) and raw[i + 1] in _CHOICE
                and any(_stands_for_the_engine(raw, j) for j in range(i + 2, min(i + 5, len(raw))))):
            return True
    return False


def names_another_engine(engine: str, message: str) -> bool:
    return bool(_vocab.engines_named_in(message, (engine or "").strip().lower()))


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


def _confirmed(sel: dict, revision: str) -> dict:
    return {**sel, "state": CONFIRMED, "confirmed_revision": revision,
            "expires_at": time.time() + CONFIRMED_TTL_S}


def _answerable(sel: dict | None, *, session_id: str, owner_id: str,
                user_msg_count: int | None) -> tuple[bool, str]:
    st = state_of(sel)
    if st == CONFIRMED:
        return False, "that engine selection is already confirmed"
    if st != PROPOSED or not sel:
        return False, ("no engine selection is awaiting confirmation - call "
                       "propose_engine_selection first and let the user answer")
    if sel.get("session") != session_id or sel.get("owner") != owner_id:
        return False, "that engine selection belongs to a different session or owner"
    if (user_msg_count is not None and "proposed_msg_count" in sel
            and int(user_msg_count) != int(sel["proposed_msg_count"]) + 1):
        # The user said something else after the question was asked, so this proposal is stale -
        # a NEW user revision invalidates a pending selection rather than silently outliving it.
        return False, ("that engine question is stale - the user has said something else since; "
                       "propose the engine again and let them answer it")
    return True, ""


def confirm(sel: dict | None, *, session_id: str, owner_id: str, revision: str,
            quote: str, latest_user_message: str,
            user_msg_count: int | None = None) -> tuple[dict | None, str]:
    ok, why = _answerable(sel, session_id=session_id, owner_id=owner_id,
                          user_msg_count=user_msg_count)
    if not ok or sel is None:
        return None, why
    engine = str(sel.get("engine") or "")
    if names_another_engine(engine, latest_user_message):
        # "yes, but use cfMesh" quotes as "yes" and is not a yes to snappyHexMesh. A message that
        # names another engine is never a confirmation of the one asked about, whatever else it
        # says: the user is choosing again, and that goes through a proposal of THEIR engine.
        return None, (f"the user named a different engine in their latest message, which is not "
                      f"a confirmation of {engine_label(engine)} - call propose_engine_selection "
                      "for the engine they named")
    if declines(engine, latest_user_message):
        return None, ("the user declined - they have not confirmed the engine; ask what they want "
                      "instead")
    if plain_assent(engine, latest_user_message):
        # The question named the engine; a plain yes to it needs no quote.
        return _confirmed(sel, revision), ""
    if not quote_is_from_user(quote, latest_user_message):
        return None, ("the words you quoted are not in the user's latest message - they have not "
                      "confirmed the engine; ask them")
    return _confirmed(sel, revision), ""


def confirm_by_assent(sel: dict | None, *, session_id: str, owner_id: str, revision: str,
                      latest_user_message: str, user_msg_count: int) -> dict | None:
    # The application reads a plain yes to its own question ITSELF, before the model runs. The
    # model used to have to quote the user's words to confirm, and a "yes" it paraphrased
    # ("yes, select snappyHexMesh") was refused as words the user never wrote - after which it
    # asked the same question again. The user answered; the answer is honoured here.
    ok, _ = _answerable(sel, session_id=session_id, owner_id=owner_id,
                        user_msg_count=user_msg_count)
    if not ok or sel is None:
        return None
    engine = str(sel.get("engine") or "")
    if names_another_engine(engine, latest_user_message) or declines(engine, latest_user_message):
        return None
    if not plain_assent(engine, latest_user_message):
        return None
    return _confirmed(sel, revision)


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
