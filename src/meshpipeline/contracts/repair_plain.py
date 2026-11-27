# Responsibility: The CAD repair's user-facing vocabulary - the plain name of every problem class,
#                 the plain words of every status and stage - and THE HARD RULE that nothing a
#                 customer reads names the repair's internals.
# Owns: PROBLEM_TYPES (class key -> plain words), the per-status fallback lines, and `leaks` /
#       `plain`, the one check every repair string a customer sees passes through.
# Boundaries: words and a regex. No I/O, no imports beyond the narration guard it extends.
# Collaborates with: contracts/narration.py (the general leak guard this one extends),
#                    pipeline/repair_progress.py (the live lane), pipeline/repair_result.py (the
#                    job API's repair section), render/repair_report.py (the HTML report).
#
# THE RULE (CRS v5 beta spec, "User-facing language"). The UI never shows internal architecture:
# no "tournament", "entrant", "heat", "rule seed", "fallback", "conformal pass", class identifiers,
# op names, or exception text. Admin views keep the full technical trace; this module is what keeps
# the customer's from ever growing one.
from __future__ import annotations

import re
from typing import Final

from meshpipeline.contracts.narration import leaks as _narration_leaks

#: The plain name of every CRS v5 problem class, as the spec's table spells it. A class this table
#: does not know is said as `unknown`'s words - never by its key.
PROBLEM_TYPES: Final[dict[str, str]] = {
    "close_faces": "thin gap",
    "overlap_self_intersection": "overlapping parts",
    "surface_crossing": "intersecting surfaces",
    "facet_crossing_local": "folded surface",
    "low_quality": "tiny or sliver feature",
    "open_shell_volume": "hole or open surface",
    "invalid_brep_tolerance": "damaged geometry",
    "closed_periodic_face": "seam problem",
    "unknown": "problem we couldn't identify",
}
UNKNOWN_TYPE: Final = "unknown"

#: The engine's own short keys for the same classes (cad/repair_v5/result.PROBLEM_TYPES: "thin_gap",
#: "overlapping_parts", ...), read back to the class key the job API and the console use.
ENGINE_KEYS: Final[dict[str, str]] = {
    "thin_gap": "close_faces",
    "overlapping_parts": "overlap_self_intersection",
    "intersecting_surfaces": "surface_crossing",
    "folded_surface": "facet_crossing_local",
    "tiny_feature": "low_quality",
    "hole_or_open_surface": "open_shell_volume",
    "damaged_geometry": "invalid_brep_tolerance",
    "seam_problem": "closed_periodic_face",
    "unidentified": "unknown",
}

#: The engine's flag keys (cad/repair_v5/result.flags_of) in the customer's words.
FLAG_WORDS: Final[dict[str, str]] = {
    "volume_changed": "The part's volume changed noticeably",
    "size_shrank": "The part got smaller",
}

#: A problem's three endings (spec "Contract": fixed | not_fixed | left_as_is).
STATUSES: Final = ("fixed", "not_fixed", "left_as_is")
#: What a problem's line says when the engine gave no plain reason of its own (or one that leaked).
STATUS_WORDS: Final[dict[str, str]] = {
    "fixed": "Fixed",
    "not_fixed": "Couldn't fix this one",
    "left_as_is": "Couldn't fix this one - left as is",
}

#: The repair's four outcomes (spec "Contract").
OUTCOMES: Final = ("repaired", "partly_repaired", "unchanged", "unrepairable")

# THE REPAIR'S OWN LEAKS, on top of the narration guard (paths, snake_case keys, meshers, models).
# Whole words, any case: the spec's banned vocabulary, exception text, class identifiers in
# CamelCase, and the engine's own names for itself.
_REPAIR_LEAKS: Final = [
    re.compile(r"\b(?:tournaments?|entrants?|heats?|seeds?|seeded|fallbacks?|fall\s+back|"
               r"conformal|panel|rounds?|kernel|occt?|ocp|opencascade|brep|shapefix|"
               r"crs|v[345])\b", re.I),
    re.compile(r"\b\w*(?:Error|Exception|Refused|Unavailable|Warning)\b"),     # exception classes
    re.compile(r"\btraceback\b|\bline \d+\b|\bFile \"", re.I),                # exception text
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b"),                     # CamelCase identifiers
    re.compile(r"\b[a-z]+[A-Z]\w*\b"),                                        # camelCase identifiers
    re.compile(r"\w\(|\)\w|::|->|=>"),                                        # calls, scopes, arrows
]


def leaks(text: str) -> bool:
    """Whether `text` says something a customer must never read about the repair."""
    line = str(text or "")
    return _narration_leaks(line) or any(rx.findall(line) for rx in _REPAIR_LEAKS)


def plain(text: object, fallback: str) -> str:
    """`text` as one tidy line when it passes the guard, else `fallback` (which must itself pass)."""
    line = " ".join(str(text or "").split())
    if line and len(line) <= 240 and not leaks(line):
        return line
    return fallback


def type_key(value: object) -> str:
    """A problem class as a key this table knows (the engine's short key read back to its class);
    anything else is `unknown`."""
    key = str(value or "").strip().lower()
    key = ENGINE_KEYS.get(key, key)
    return key if key in PROBLEM_TYPES else UNKNOWN_TYPE


def type_words(value: object) -> str:
    """The plain words for a problem class (spec table), never its key."""
    return PROBLEM_TYPES[type_key(value)]


#: The plain names that are plural ("Repairing overlapping parts", never "an overlapping parts").
_PLURAL: Final = frozenset({"overlapping parts", "intersecting surfaces"})


def a_or_an(words: str) -> str:
    if words in _PLURAL:
        return words
    return ("an " if words[:1].lower() in "aeiou" else "a ") + words


__all__ = ["ENGINE_KEYS", "FLAG_WORDS", "OUTCOMES", "PROBLEM_TYPES", "STATUSES", "STATUS_WORDS",
           "UNKNOWN_TYPE", "a_or_an",
           "leaks", "plain", "type_key", "type_words"]
