# Responsibility: Hold every reply about a COOLED SOLID - a cold plate, a water block, a cooled
# package - to the words that case is actually made of: the part, its passages, the coolant, the
# ports, the metal and the coolant as the two halves of one mesh. No far field, no freestream, no
# air box, no room air, no cooling air, nothing "around the part".
# Boundaries: pure. It takes a text and the words that are not ours to police, and returns which
# forbidden words the text used; it reads no store, decides nothing about the mesh and asks no
# model.
# Collaborates with: agents/intake/loop_policy.py (the reply guard, where a reply that breaks this
# goes back once for a rewrite) and agents/intake/turn.py (the send-back the model reads).
#
# THE THERMAL MODEL'S MECHANISM, ON THE OTHER CASE. application/thermal_wording.FORBIDDEN is this
# list for a whole ECXML thermal model, read at the same place in the same loop, and nothing about
# the shape here is new: the same `forbidden_words(text, names)`, the same letter-boundary reading,
# the same "once, then the reply is delivered" rule in the policy. The LIST is the difference,
# because the two cases are opposites - a thermal model IS an air box and says so, and a cold
# plate has nothing built around it at all.
#
# WHY IT IS A CHECK AND NOT A PROMPT LINE. agents/intake/agent._block_cooled_solid already tells
# the model: "Never write far field, freestream, air box, room air, cooling air or 'the fluid
# around the part' in a reply, in request_txt or in review_brief_txt." That sentence is the whole
# of this list, and it was the whole of the enforcement too. A prompt block is a hope; the thermal
# half of the same instruction has had a deterministic send-back behind it since the thermal split.
from __future__ import annotations

import re
from collections.abc import Iterable

#: WORDS A COOLED SOLID'S TEXT NEVER USES, outside the user's own. Each is a thing built AROUND a
#: part, and nothing is built around this one: the coolant is the space inside it. A cold-plate
#: owner reads any of these as a tool that meshed the wrong case - an air box where their metal
#: should be - which is the reading the conjugate path exists to avoid.
#: Read at letter boundaries, so a name part counts ("far_field_1") and a longer word does not
#: (the "airbox" in "airboxes" is caught on purpose; "fairground" is not).
FORBIDDEN: tuple[tuple[str, str], ...] = (
    # one entry, both spellings: `far[ _-]?field` matches "far field", "far-field" and "farfield",
    # so a second "farfield" row would only make the log name the same word twice
    ("far field", r"(?<![a-z])far[ _-]?fields?(?![a-z])"),
    ("freestream", r"(?<![a-z])free[ _-]?stream(s|ing)?(?![a-z])"),
    ("air box", r"(?<![a-z])air[ _-]?box(es)?(?![a-z])"),
    ("room air", r"(?<![a-z])room air(?![a-z])"),
    ("cooling air", r"(?<![a-z])cooling air(?![a-z])"),
    # THE PART, AND NOT ANY NOUN. A cold plate's coolant really does flow AROUND things - the pin
    # fins and the channel ribs are inside the passages - so this is the prompt block's own phrase
    # and the obvious variants of it, never a bare "around". "the coolant around the pin fins" is
    # a cooled solid's own sentence and is left alone.
    ("the fluid around the part",
     r"(?<![a-z])(fluid|air|coolant|water)\s+(flowing\s+|moving\s+|that flows\s+)?around\s+"
     r"(the|this)\s+(part|plate|body|solid|metal|component)s?(?![a-z])"),
)


def forbidden_words(text: str, names: Iterable[str] = ()) -> list[str]:
    """The words of another kind of job `text` uses, each once, in FORBIDDEN's order - [] when it
    speaks a cooled solid's language.

    `names` are the words that are not ours to police, taken out before the text is read. The
    thermal half of this reads the FILE's own names there (an ECXML model names its own vents),
    and that is the shape this argument is for: a short string that appears in the reply verbatim.

    A COOLED SOLID HAS NO SUCH FILE, and the intake passes the user's own MESSAGES, which are
    sentences rather than names. Measured 2026-11-12: with "what about the far field around it?"
    as the one name, "You asked about the far field; there is no far field here" still returns
    ["far field"], because a whole message does not appear inside a reply. So in production this
    argument only exempts a reply that quotes a user message word for word, and a user who raises
    one of these words themselves still gets the answer sent back once. That is recorded in
    agents/intake/loop_policy.py beside the call, and pinned by a test that runs the real policy.
    """
    said = str(text or "")
    for name in sorted({str(n) for n in names if str(n).strip()}, key=len, reverse=True):
        said = re.sub(re.escape(name), " ", said, flags=re.IGNORECASE)
    return [word for word, rx in FORBIDDEN if re.search(rx, said, re.IGNORECASE)]


__all__ = ["FORBIDDEN", "forbidden_words"]
