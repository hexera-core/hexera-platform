# Responsibility: Hold the ONE vocabulary intake speaks, and convert it to the keys the system routes on.
# Owns: the choices offered to the model, and the normalization of a tool call's arguments.
# Boundaries: translation only - it holds no words of its own and decides nothing about intent.
# Collaborates with: contracts/display_names.py, and engines/registry.py and purposes.py.
from __future__ import annotations

from typing import Final

#: The argument names this module owns, mapped to the vocabulary each draws from. A tool argument
#: not named here is passed through untouched - dimensionality ("2D"/"3D") and boundary roles
#: ("wall", "inlet") are already the words a user would use and need no translation.
PURPOSE: Final = "purpose"
INPUT_KIND: Final = "input_kind"
#: Values are the vocabulary names in contracts/display_names.VOCABULARIES.
ENGINE: Final = "mesh_engine"

#: Tool arguments that carry an engine, under the several names the schemas use for it.
_ENGINE_ARGS: Final = ("engine", "mesh_engine", "selected_engine")


def _named(vocabulary: str, keys) -> dict[str, str]:
    from meshpipeline.contracts.display_names import display_name
    return {display_name(vocabulary, k): k for k in keys}


def _purposes() -> dict[str, str]:
    from meshpipeline.engines.purposes import purpose_keys
    return _named(PURPOSE, purpose_keys())


def _input_kinds() -> dict[str, str]:
    from meshpipeline.engines.purposes import INPUT_KINDS
    return _named(INPUT_KIND, INPUT_KINDS)


def _engines() -> dict[str, str]:
    from meshpipeline.engines.registry import engine_names
    return _named(ENGINE, engine_names())


def purpose_choices() -> tuple[str, ...]:
    return tuple(_purposes())


def input_kind_choices() -> tuple[str, ...]:
    return tuple(_input_kinds())


def engine_choices() -> tuple[str, ...]:
    return tuple(_engines())


def to_key(vocabulary: str, value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    table = {PURPOSE: _purposes, INPUT_KIND: _input_kinds, ENGINE: _engines}[vocabulary]()
    if raw in table:
        return table[raw]
    lowered = {name.lower(): key for name, key in table.items()}
    return lowered.get(raw.lower(), raw)          # already a key, or unknown - validation decides


def to_display(vocabulary: str, value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    table = {PURPOSE: _purposes, INPUT_KIND: _input_kinds, ENGINE: _engines}[vocabulary]()
    for name, key in table.items():
        if key == raw:
            return name
    return raw                                     # already a display name, or unknown


def humanize(text: str) -> str:
    # The counterpart to to_display for prose. Messages are written with the keys the system routes
    # on, and a key is not a word anyone chose to show a user - "external_cfd" is a routing value,
    # "External CFD" is the name for it. Longest first, and bounded by non-word characters, so
    # "snappy" inside "snappy_multiregion" is left alone.
    import re

    from meshpipeline.contracts.display_names import VOCABULARIES
    out = str(text or "")
    if not out:
        return out
    pairs = [(k, v) for table in VOCABULARIES.values() for k, v in table.items()]
    for key, name in sorted(pairs, key=lambda kv: len(kv[0]), reverse=True):
        out = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", name, out)
    return out


def vocabulary_of(field: str) -> str:
    # Which vocabulary a declared field's value is named in, or "" when it needs no translation.
    # Read from the constants above rather than a second table, so a field name and the vocabulary
    # it draws from cannot drift apart.
    name = str(field or "").strip()
    if name in (PURPOSE, INPUT_KIND):
        return name
    if name in _ENGINE_ARGS:
        return ENGINE
    return ""


def display_of_field(field: str, value) -> str:
    vocabulary = vocabulary_of(field)
    return to_display(vocabulary, value) if vocabulary else str(value or "").strip()


def engines_named_in(text: str, selected: str = "") -> list[str]:
    # Which engines a piece of text names, by registry key, ignoring the selected one.
    #
    # Longest spelling first, consuming each match, because one engine's name can contain
    # another's: "snappyHexMesh multi-region" holds "snappyHexMesh". Matching short-first would
    # read a correct mention of the multi-region engine as a mention of snappyHexMesh, and a
    # mention of multi-region while snappyHexMesh is selected as nothing at all. They are separate
    # engines - one meshes external CFD from a body surface and the other cannot, and only the
    # other does conjugate heat transfer - so confusing them is not a naming quibble.
    from meshpipeline.engines.registry import engine_names

    scan = str(text or "").lower()
    spellings: list[tuple[str, str]] = []
    for key in engine_names():
        spellings.extend((s.lower(), key) for s in {key, to_display(ENGINE, key)})
    found: set[str] = set()
    for spelling, key in sorted(spellings, key=lambda sk: len(sk[0]), reverse=True):
        if spelling and spelling in scan:
            found.add(key)
            scan = scan.replace(spelling, " ")
    return sorted(found - {selected})


def normalize_tool_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return args
    out = dict(args)
    for name in (PURPOSE, INPUT_KIND):
        if out.get(name):
            out[name] = to_key(name, out[name])
    for name in _ENGINE_ARGS:
        if out.get(name):
            out[name] = to_key(ENGINE, out[name])
    return out


def choices_text(vocabulary: str) -> str:
    return ", ".join({PURPOSE: purpose_choices, INPUT_KIND: input_kind_choices,
                      ENGINE: engine_choices}[vocabulary]())
