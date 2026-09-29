# Responsibility: Propose, confirm and verify which engine the user chose.
# Boundaries: a user-named engine is honoured only when the user actually named it, and a plain
# yes binds to the engine the application proposed - never to one the model would rather have.
from __future__ import annotations

import re
import time
import uuid

from meshpipeline.agents.intake import consent as _consent
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
# where the idiom is aimed at the refinement), negates a choice verb whose object is the
# engine or a pronoun standing alone for it ("don't want snappyHexMesh", "can't use that",
# "won't go with it" - but not "don't want that ground patch", where the pronoun points at the
# noun after it), or retracts the answer ("forget it"). Every "n't" is read as "not" first, so
# the apostrophe never decides. Two idioms mean the opposite of their words and go first.
_NEGATION = frozenset({"not", "dont", "never", "cancel", "stop"})
_DECLINE_FIRST = _NEGATION | frozenset({"no", "nope", "nah", "wait", "hold"})
#: In their filler-free form ("I don't think so" reads as "not think"), matched only where the
#: answer begins or ends.
_DECLINE_IDIOMS = ("not think", "not really", "rather not", "prefer not", "no way", "not yet",
                   "not now", "changed my mind")
#: A retraction is read from its object - the rest of its own clause - wherever it sits. It takes
#: the engine answer back when that object is empty, the engine, a pronoun or a "the whole
#: thing" ("yes, forget it", "forget about it", "Forget it entirely; ...", "forget all that",
#: "forget snappyHexMesh"), or when it takes back a yes ("forget that I said yes"). An object
#: that names something else leaves the yes standing: "Forget the ground patch; yes, use
#: snappyHexMesh", "yes, but forget that the ground patch exists", "forget that I said yes to
#: the ground patch". The last word wins: a later clause that picks the engine by name ("yes,
#: forget it, but actually use snappyHexMesh") overrides a retraction before it, and a
#: retraction after a choice ("use snappyHexMesh - actually, forget it") overrides the choice.
_RETRACT = "forget"
#: "yes to the ground patch": a yes with an object of its own.
_YES_TO = frozenset({"to", "on", "for", "about"})
#: The only other words a clause that picks the engine again may hold: "but actually use
#: snappyHexMesh instead", "snappyHexMesh after all".
_CHOICE_PLAIN = frozenset({"but", "instead", "after", "all", "anyway", "actually"})
#: What may sit between a negation and the choice verb it negates: "won't ever use", "don't
#: really want", "not even want".
_NEGATED_ADVERBS = frozenset({"ever", "really", "even", "actually", "exactly", "necessarily",
                              "particularly", "honestly"})
_RETRACT_WHOLE = frozenset({"about", "all", "everything", "whole", "thing", "entirely",
                            "completely", "totally", "altogether", "actually", "really",
                            "honestly", "anyway", "anyways"})
#: The words of a yes the user may take back. Narrower than _ASSENT: "forget the right-hand
#: outlet" or "forget the y+ target" takes back no yes.
_YES = frozenset({"yes", "yep", "yeah", "yup", "sure", "ok", "okay", "agree", "agreed",
                  "confirm", "confirmed", "goahead", "affirmative"})
#: Where one clause ends and the next begins; a decimal point is not one.
_CLAUSE_BREAK = re.compile(r"[,;:!?()]|\.(?!\d)|\s[-\u2013\u2014]+\s|[\u2013\u2014]")
_BREAK_WORD = "xbreak"
#: A clause that ends in a question mark: asked, not chosen.
_QUESTION_WORD = "xquestion"
_INTENSIFIERS = frozenset({"definitely", "absolutely", "certainly", "surely"})
_CHOICE = frozenset({"want", "like", "need", "prefer", "choose", "select", "use", "pick", "take",
                     "keep", "go", "fancy", "wish"})
#: What stands for the proposed engine as the object of a negated choice verb.
_ENGINE_WORD = "xengine"
_PRONOUNS = frozenset({"it", "that", "this"})
_IDIOMS = ("no problem", "no worries", "why not")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _tokens(engine: str, message: str, *, keep_engine: bool,
            keep_breaks: bool = False) -> list[str]:
    # The message as bare words. Contractions are opened ("don't" -> "do not"), the proposed
    # engine's own name - both spellings, the registry key and the name the user reads - becomes
    # one token or nothing, "go ahead" becomes one word, and the idioms go. With keep_breaks,
    # each clause boundary is kept as a word of its own.
    text = re.sub(r"n['’]t\b", " not", str(message or "").casefold())
    if keep_breaks:
        text = text.replace("?", f" {_QUESTION_WORD} ")
        text = _CLAUSE_BREAK.sub(f" {_BREAK_WORD} ", text)
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
    # The intake's one consent reader first ("sounds good", "go for it", "yes go ahaed" are a yes
    # to this question exactly as they are to the summary), then this question's own words, which
    # may name the engine it proposed ("yes, snappyHexMesh", "use it").
    if _consent.is_yes(message):
        return True
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


def _takes_back_a_yes(obj: list[str]) -> bool:
    # A yes inside the object is the one being taken back ("forget that I said yes", "... yes
    # to it") unless it has an object of its own ("... yes to the ground patch").
    for j, w in enumerate(obj):
        if w not in _YES:
            continue
        if (j + 1 < len(obj) and obj[j + 1] in _YES_TO
                and any(t not in _FILLER for t in obj[j + 2:])):
            continue
        return True
    return False


def _retracts(obj: list[str]) -> bool:
    # `obj` is what follows "forget" up to the end of its clause. It takes the engine answer back
    # when it names the engine or a yes ("forget snappyHexMesh", "forget that I said yes"), or
    # names nothing of its own ("forget", "forget it then", "forget about it", "forget it
    # entirely", "forget the whole thing") - not when it names something else ("forget the
    # ground patch", "forget that the ground patch exists").
    if _ENGINE_WORD in obj or _takes_back_a_yes(obj):
        return True
    return all(w in _FILLER or w in _RETRACT_WHOLE for w in obj)


def _chooses_the_engine(clause: list[str]) -> bool:
    # The clause is a plain choice of the proposed engine and nothing else: its name, a choice or
    # assent word, and otherwise only fillers ("but actually use snappyHexMesh", "go with
    # snappyHexMesh instead", "snappyHexMesh is fine"). Any other word - a negation however far
    # from its verb ("I won't ever use snappyHexMesh"), a hedge ("maybe use snappyHexMesh"), a
    # qualification ("I'll use snappyHexMesh some other time") - makes it no choice at all.
    if _ENGINE_WORD not in clause:
        return False
    rest = [w for w in clause if w != _ENGINE_WORD and w not in _FILLER]
    return (any(w in _ASSENT or w in _CHOICE for w in rest)
            and all(w in _ASSENT or w in _CHOICE or w in _CHOICE_PLAIN for w in rest))


def _retracts_the_answer(engine: str, message: str) -> bool:
    # Clause by clause, the last word wins: a retraction stands unless a later clause picks the
    # engine by name again. A question picks nothing: "Forget it; snappyHexMesh is right for
    # this?" asks, and the retraction stands.
    words = _tokens(engine, message, keep_engine=True, keep_breaks=True)
    clauses: list[list[str]] = [[]]
    asked: list[bool] = [False]
    for w in words:
        if w in (_BREAK_WORD, _QUESTION_WORD):
            asked[-1] = w == _QUESTION_WORD
            clauses.append([])
            asked.append(False)
        else:
            clauses[-1].append(w)
    retracted = False
    for clause, is_question in zip(clauses, asked):
        if retracted and not is_question and _chooses_the_engine(clause):
            retracted = False
        if any(w == _RETRACT and _retracts(clause[i + 1:]) for i, w in enumerate(clause)):
            retracted = True
    return retracted


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
    if _retracts_the_answer(engine, message):
        return True
    for i, w in enumerate(raw):
        if w not in _NEGATION:
            continue
        if i > 0 and raw[i - 1] in _INTENSIFIERS:
            return True
        v = i + 1
        while v < len(raw) and raw[v] in _NEGATED_ADVERBS:
            v += 1     # "won't ever use", "don't really want": the negation still reaches the verb
        if (v < len(raw) and raw[v] in _CHOICE
                and any(_stands_for_the_engine(raw, j) for j in range(v + 1, min(v + 4, len(raw))))):
            return True
    return False


def names_another_engine(engine: str, message: str) -> bool:
    return bool(_vocab.engines_named_in(message, (engine or "").strip().lower()))


def user_chose(engine: str, user_texts) -> bool:
    """Whether the user's own words already chose `engine`. `user_texts` are the user's messages,
    oldest first. The most recent
    message that names any engine decides - it must name this one and no other, neither ask about
    it nor decline it, and CHOOSE it in plain words: the name alone ("snappyHexMesh", the answer
    to "which toolchain?"), a plain choice ("use snappy", "snappyHexMesh it is") or a choice verb
    before it ("I'll go with snappyHexMesh for the mesh"). A mention is not a choice: "I used
    snappyHexMesh last time" names it and chooses nothing. The user who
    answered "which engine?" with "snappyHexMesh" and was asked a setup question next was then
    asked "Do you want to select snappyHexMesh?" - the model proposed the engine a turn later and
    only the latest message was searched for its name."""
    want = (engine or "").strip().lower()
    for said in reversed(tuple(user_texts or ())):
        said = str(said or "")
        named = _vocab.engines_named_in(said, "")
        if not named:
            # a later "forget it", "actually don't use that" takes the choice back although it names
            # no engine (review on #92): the engine is asked again, never confirmed over it
            if declines(want, said):
                return False
            continue
        if named != [want] or "?" in said or declines(want, said):
            return False
        return _chooses_in(want, said)
    return False


#: What may come before an engine's name to choose it: the choice verbs, and "make it", "switch
#: to", "change to", "try", "go ahead with".
_CHOOSING = _CHOICE | frozenset({"make", "switch", "change", "try", "goahead"})


def _chooses_in(engine: str, message: str) -> bool:
    # a clause that is the engine alone, or a plain choice of it ("use it", "go with X", "X it is"),
    # or a choice verb before the engine within its clause ("I'll go with X for the mesh")
    words = _tokens(engine, message, keep_engine=True, keep_breaks=True)
    clauses: list[list[str]] = [[]]
    for w in words:
        if w in (_BREAK_WORD, _QUESTION_WORD):
            clauses.append([])
        else:
            clauses[-1].append(w)
    for clause in clauses:
        if _ENGINE_WORD not in clause:
            continue
        rest = [w for w in clause if w != _ENGINE_WORD and w not in _FILLER]
        if not rest or _chooses_the_engine(clause):
            return True
        before = clause[:clause.index(_ENGINE_WORD)]
        if any(w in _CHOOSING for w in before) and not any(w in _NEGATION for w in before):
            return True
    return False


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
