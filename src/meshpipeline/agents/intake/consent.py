# Responsibility: Read a plain "yes" the same way wherever the intake asks for one.
# Owns: the consent vocabulary (phrases, fillers, hedges), the repair of a mistyped consent word,
#       and the whole-message reading every asker uses.
# Boundaries: pure text in, a reading out. What a yes binds to - the summary, the proposed engine,
#       the proposed unit - is each asker's own business (approval, engine_selection,
#       unit_clarification); none of them keeps a word list of its own any more.
"""One reading of consent for the whole intake.

The intake asks for a yes in three places - the summary before a run, the engine it proposes, the
unit it proposes - and each used to keep its own word list. They disagreed: "sounds good" was a
correction to the summary and not an answer to the engine question, "looks good" and "go for it"
threw a summary away, "ok" alone was "ambiguous", and a yes with one typo ("yes go ahaed") was a
change request. Every one of those put a user who had said yes back into another summary.

The reading is WHOLE-MESSAGE, as it always was: a message is consent only when every word in it is
consent vocabulary or filler, so "yes, but make the far field 50 chords" names something no consent
phrase covers and stays a correction; a consent diluted by a hedge ("yes... I think") is a hedge.
What changed is the vocabulary (the ways people actually say yes) and one repair: a word one
keystroke away from a consent word, in a short message, is read as that word - never a negation,
never a number.
"""
from __future__ import annotations

import re

APPROVE = "approve"
HEDGE = "ambiguous"
OTHER = "other"

#: Words that carry nothing on their own inside a longer answer.
FILLER = frozenset({
    "please", "pls", "plz", "ok", "okay", "thanks", "thank", "thx", "ty", "you", "u", "alright", "right",
    "great", "perfect", "then", "now", "just", "lets", "let", "us", "s", "and", "cool", "awesome", "nice",
    "oh", "ah", "well", "hey", "hi", "so", "mate", "sir", "haha", "lol", "indeed", "much", "very",
    "really", "totally", "all", "good", "n",
})

#: Filler words that are a yes when the whole answer is made of them: "ok", "perfect", "great!",
#: "all good". "thanks" alone is not one: it thanks, it does not agree.
STANDALONE = frozenset({"ok", "okay", "alright", "perfect", "great", "right", "cool", "awesome", "nice",
                        "good"})

#: A consent phrase, each read as a whole; a message may chain any of them ("Confirmed. Go ahead!").
PHRASES = frozenset({
    # the words the grammar always took
    "yes", "y", "yep", "yeah", "sure", "yes sure", "yes proceed", "proceed", "yes proceed with mesh generation",
    "proceed with mesh generation", "proceed exactly as shown", "yes proceed exactly as shown",
    "exactly as shown", "approve", "approve this", "approve this configuration", "approved",
    "i approve", "i approve this", "use these requirements", "use these", "confirm", "confirmed",
    "i confirm", "go", "go ahead", "yes go ahead", "start", "start the mesh",
    "start mesh generation", "yes start the mesh", "run it", "yes run it", "do it", "yes do it",
    "correct proceed", "yes correct", "that is correct proceed", "looks good proceed",
    "yes looks good", "yes that is right", "that is right proceed", "yes approve",
    "yes confirmed", "yes confirm", "confirm dispatch", "yes dispatch",
    # the ways people actually say it
    "yea", "ya", "yah", "yup", "yas", "yess", "k", "kk", "okey", "okie", "fine", "correct", "exactly",
    "affirmative", "absolutely", "definitely", "certainly", "of course", "sure thing", "why not",
    "agree", "agreed", "i agree", "accept", "accepted", "i accept", "lgtm", "ready", "i am ready",
    "looks good", "look good", "looks fine", "looks correct", "looks right", "looks ok", "looks great",
    "looks good to me", "sounds good", "sounds fine", "sounds right", "sounds great", "sounds good to me",
    "that sounds", "that looks", "that works", "works for me", "fine by me", "fine with me", "good to go",
    "that is it", "that is correct", "that is right", "that is fine", "that is what i said", "as i said",
    "that's it", "that's correct", "that's right", "that's fine", "that's what i said", "all set",
    "it is fine", "it's fine", "it is correct", "it's correct",
    "go for it", "go on", "carry on", "continue", "go with that", "go with it", "lets go", "do that",
    "go ahead with that", "go ahead with it", "proceed with that", "proceed with it",
    "generate the mesh", "generate it", "mesh generation", "make the mesh", "build the mesh",
    "create the mesh", "run the mesh", "go ahead with the mesh", "proceed with the mesh",
    "go ahead with mesh generation", "start meshing it", "mesh it now",
    "make it so", "start it", "start meshing", "start the run", "run", "mesh it", "ship it", "begin",
    "send it", "kick it off", "yes please", "please do", "please proceed", "proceed please", "as shown",
    "as proposed", "as you suggest", "as you suggested", "whatever you think", "whatever you think is best",
    "your call", "up to you", "you decide", "no problem", "no worries",
})

#: Hesitation: never a yes, never a change - the asker asks once more.
HEDGES = frozenset({
    "maybe", "perhaps", "probably", "possibly", "i think so", "i think", "not sure",
    "i am not sure", "im not sure", "unsure", "hmm", "hm", "i guess", "i suppose",
    "maybe yes", "probably yes", "i dont know", "dont know", "no idea", "idk", "dunno", "not certain",
})

#: Emoji and marks that are a yes.
_YES_MARKS = ("👍", "👌", "✅", "✔", "🆗", "🙆")

#: Never read as a mistyped consent word, whatever they are one keystroke from: a refusal, a
#: condition, a count or a direction is a change, not a slip ("five" is not "fine", "not" is not "now").
PROTECTED = frozenset({
    "no", "not", "nope", "nah", "nay", "never", "dont", "don", "t", "cant", "can", "wont", "won", "isnt",
    "stop", "wait", "hold", "cancel", "abort", "halt", "pause", "later", "yet", "wrong", "change",
    "but", "except", "instead", "without", "again", "back", "redo", "undo", "before", "after", "first",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "twenty", "fifty", "hundred", "thousand", "million", "half", "double", "twice",
    "less", "more", "fewer", "finer", "coarser", "bigger", "smaller", "left", "light", "night", "tight",
    "line", "lines", "mine", "fire", "wire", "sine", "goal", "gold", "cold", "bold", "wood",
    "food", "hood", "wall", "walls", "call", "fall", "role", "rule", "star", "stars", "stat", "part",
    "port", "runs", "rung", "sun", "gun", "ran", "rub", "rust", "ruin", "sore", "cure", "pure", "lure",
    "cell", "cells", "must", "than", "tall", "hall", "ball", "pool", "tool", "pipe", "tube",
    "duct", "flow", "slow", "fast", "size", "mesh", "face", "faces", "edge", "hole", "holes", "gap",
})

#: At most this many repaired words, in a message of at most this many words.
MAX_REPAIRS = 2
MAX_REPAIR_WORDS = 8


def _seqs(vocab) -> list[tuple[str, ...]]:
    out = {tuple(_strip_filler(_raw_words(p))) for p in vocab}
    return sorted(seq for seq in out if seq)


def _raw_words(text: str) -> list[str]:
    t = str(text or "").casefold()
    for mark in _YES_MARKS:
        t = t.replace(mark, " yes ")
    t = re.sub(r"[^0-9a-z]+", " ", t)
    # a held key is one letter too many: "yesss" -> "yess", "gooood" -> "good"
    return [re.sub(r"(.)\1{2,}", r"\1\1", w) for w in t.split()]


def _strip_filler(words) -> list[str]:
    return [w for w in words if w not in FILLER]


_CONSENT_SEQS: list[tuple[str, ...]] = []
_ALL_SEQS: list[tuple[str, ...]] = []
_VOCAB: frozenset[str] = frozenset()
_TARGETS: frozenset[str] = frozenset()


def _build() -> None:
    global _CONSENT_SEQS, _ALL_SEQS, _VOCAB, _TARGETS
    _CONSENT_SEQS = _seqs(PHRASES)
    _ALL_SEQS = _seqs(PHRASES | HEDGES)
    _VOCAB = frozenset(w for seq in _ALL_SEQS for w in seq) | FILLER
    # what a mistyped word may be read as: a consent word or a filler, three letters or more
    # ("plesae", "thnak yuo" are slips too, and one of them left the whole yes unread)
    _TARGETS = (frozenset(w for seq in _CONSENT_SEQS for w in seq if len(w) >= 3) | STANDALONE
                | frozenset(w for w in FILLER if len(w) >= 3))


_build()


def one_slip(word: str, target: str) -> bool:
    """One keystroke apart: a letter dropped, added, changed or two neighbours swapped. A three-letter
    word only by a swap ("yse") or a doubled letter ("yess"); shorter words never."""
    a, b = word, target
    if a == b or min(len(a), len(b)) < 3:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
            return True                                   # two neighbours swapped
        return len(diff) == 1 and min(len(a), len(b)) >= 4
    if abs(len(a) - len(b)) != 1:
        return False
    long, short = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] != short:
            continue
        # a letter dropped from a word of four or more ("tat" for "that"), or one doubled ("yess",
        # "forr"); a three-letter word never grows a new letter into a two-letter one
        if len(short) >= 3 and (len(long) >= 4 or (i > 0 and long[i] == long[i - 1])):
            return True
    return False


def _repairs(words: list[str]) -> list[list[str]]:
    """The readings of a message with its slips repaired: each word outside the vocabulary - and
    not a word of its own that means something else - read as each consent or filler word one
    slip away. Empty when a word has no such reading, or more than MAX_REPAIRS words need one."""
    options: list[list[str]] = []
    fixed = 0
    for w in words:
        if w in _VOCAB or w in PROTECTED or not w.isalpha():
            options.append([w])
            continue
        cands = sorted(t for t in _TARGETS if one_slip(w, t))
        if not cands:
            return []
        options.append(cands)
        fixed += 1
    if not 0 < fixed <= MAX_REPAIRS:
        return []
    out: list[list[str]] = [[]]
    for opt in options:
        out = [prev + [o] for prev in out for o in opt]
    return out


def _parses_as(words: tuple[str, ...], phrases: list[tuple[str, ...]]) -> bool:
    """True when the whole word sequence is a concatenation of vocabulary phrases."""
    n = len(words)
    ok = [False] * (n + 1)
    ok[0] = True
    for i in range(n):
        if not ok[i]:
            continue
        for ph in phrases:
            j = i + len(ph)
            if j <= n and words[i:j] == ph:
                ok[j] = True
    return ok[n]


def _read(words: list[str], asked_back: bool) -> str:
    raw = words
    core = tuple(_strip_filler(raw))
    if not core:
        # only fillers: "ok", "perfect!", "great, thanks" say yes; "thanks" or nothing does not
        return APPROVE if any(w in STANDALONE for w in raw) and not asked_back else HEDGE
    if _parses_as(core, _CONSENT_SEQS):
        return HEDGE if asked_back else APPROVE
    if _parses_as(core, _ALL_SEQS):
        return HEDGE
    return OTHER


def reading(message: str, *, named: tuple[str, ...] = ()) -> str:
    """APPROVE, HEDGE or OTHER for a whole message. A consent with a question mark in it ("looks
    good?", "go ahead? with snappy") is asked back, so it is a hedge; a message one or two slips from consent is consent.
    `named` are words the question itself already stands for - the engine the summary names - so
    "yes, go ahead with snappyHexMesh" to a snappyHexMesh summary is a yes; any other name stays."""
    text = str(message or "")
    names = sorted({str(n or "").strip() for n in named if str(n or "").strip()}, key=len, reverse=True)
    if names:
        # the name dropped, and the name read as "it" ("go ahead with it"): consent either way
        verdicts = {_reading(_without(text, names, " ")), _reading(_without(text, names, " it "))}
        return next((v for v in (APPROVE, HEDGE) if v in verdicts), OTHER)
    return _reading(text)


def _without(text: str, names, instead: str) -> str:
    for name in names:
        text = re.sub(rf"(?<![0-9a-z]){re.escape(name)}(?![0-9a-z])", instead, text, flags=re.IGNORECASE)
    return text


def _reading(text: str) -> str:
    # A QUESTION MARK ANYWHERE is a question: "Go ahead? With snappyHexMesh" asks, and once the
    # engine's name is dropped its "?" is no longer at the end
    asked_back = "?" in text
    words = _raw_words(text)
    first = _read(words, asked_back)
    if first != OTHER or len(words) > MAX_REPAIR_WORDS:
        return first
    # every repair lands on a consent or filler word, so a slip that fits two of them ("youu": you
    # or your) is read both ways; the message is consent when one whole reading is
    readings = {_read(fixed, asked_back) for fixed in _repairs(words)}
    for verdict in (APPROVE, HEDGE):
        if verdict in readings:
            return verdict
    return first


def is_yes(message: str) -> bool:
    """The whole message says yes and nothing else."""
    return reading(message) == APPROVE


__all__ = ["APPROVE", "FILLER", "HEDGE", "HEDGES", "OTHER", "PHRASES", "PROTECTED", "STANDALONE",
           "is_yes", "one_slip", "reading"]
