# Responsibility: A SHELL'S ELEMENTS ON THE STAGE (SHSTAGE-8): the shell line's one two-way choice, "Elements: Quads |
# Triangles" - what it starts on (the user's notes or their team's settings when either named an element kind, else
# Quads), what Proceed settles (the user's own click is the last word for this run, recorded with source "stage"), and
# the one sentence the declaration says. Quads is the quad-dominant shell the rules make; Triangles is an all-triangle
# shell at the same sizes.
# Boundaries: pure over plain data (the store read is confirmed()'s alone). It reads no model: where the control STARTS
# is read from the user's own words by a few fixed phrases (the same ones the notes' reader is told are an element
# kind) and from the reading kept with the team's standard (engines/meshing_standard STD-1). What the run MAKES is the
# notes' and settings' reading (engines/mesh_requirements, the typed set's kind.shell) with the stage's pick on top
# (engines/meshing_standard.stage, STD-4) - one field, one path. A control left where it started changes nothing.
# Collaborates with: api/v1/geometry.py (serves the view on the shell line, settles it at Proceed),
# application/pipeline_run.py (reads the settled pick into the run), engines/meshing_standard.py (applies it),
# ui/js/render/fea_form.js (draws it).
from __future__ import annotations

import json
import re
from collections.abc import Mapping

RULE = "SHSTAGE-8"
QUESTION = "Elements"
QUADS, TRIANGLES = "quads", "triangles"
DEFAULT = QUADS
#: the two choices, in the user's words; `words` is the tooltip
OPTIONS = (
    {"key": QUADS, "label": "Quads",
     "words": "Mostly 4-node quads (CQUAD4, S4, SHELL181); a few triangles only where a quad cannot be well shaped."},
    {"key": TRIANGLES, "label": "Triangles",
     "words": "All 3-node triangles (CTRIA3, S3, SHELL181 triangles), at the same element sizes."},
)
#: where the starting value came from, in plain words (shown under the tooltip)
FROM_WORDS = {"notes": "as your notes ask", "settings": "as your settings ask", "default": "the default",
              "stage": "as chosen on the stage"}

#: a share of triangles ("at most 5% trias") is a quad-dominant shell: the control starts on Quads
_SHARE = re.compile(r"\b(?:at\s+most|max(?:imum)?|under|below|less\s+than)\s+\d+(?:\.\d+)?\s*%\s*(?:of\s+)?"
                    r"(?:triangles?|trias?|tris?)\b", re.I)


def pick_of(value) -> str:
    """A pick read strictly: "quads", "triangles", or "" (nothing picked)."""
    v = str(value or "").strip().lower()
    return v if v in (QUADS, TRIANGLES) else ""


def said_in(text: str) -> str:
    """The element kind a piece of the user's words names ("quads" | "triangles" | ""), by the reader's own fixed
    phrases (engines/mesh_requirements NOTE-10): the FIRST one written wins; a share of triangles is Quads."""
    from meshpipeline.engines.mesh_requirements import kind_in_words

    got = kind_in_words(text)
    return got or (QUADS if _SHARE.search(str(text or "")) else "")


def _user_texts(messages) -> list[str]:
    from meshpipeline.contracts.user_notes import system_written

    return [str(m.get("content") or "") for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user" and not system_written(m.get("content"))]


def notes_kind(messages=None, notes=None) -> str:
    """The element kind the user's own words name (their chat messages and any notes), else ""."""
    texts = _user_texts(messages) + [str(n.get("text") or "") for n in notes or [] if isinstance(n, Mapping)]
    for text in texts:
        got = said_in(text)
        if got:
            return got
    return ""


def settings_kind(standard: Mapping | None) -> str:
    """The element kind the team's meshing standard names, read from the reading kept when it was saved (STD-1: no
    model asked), else "". A standard not read yet says nothing here (each run reads it)."""
    from meshpipeline.engines import meshing_standard as MS

    if not isinstance(standard, Mapping):
        return ""
    text = str(standard.get("text") or "")
    kept = MS.kept_reading(standard, text)
    if kept is None:
        return ""
    k = ((MS.checked(kept, text).get("requirements") or {}).get("kind") or {})
    if k.get("shell") == TRIANGLES:
        return TRIANGLES
    return QUADS if k.get("shell") == QUADS or k.get("tria_percent_max") is not None else ""


def start(messages=None, *, standard: Mapping | None = None, notes=None) -> dict:
    """Where the control starts: {"value", "from": "notes" | "settings" | "default"} - the notes over the settings
    (STD-2), else Quads."""
    got = notes_kind(messages, notes)
    if got:
        return {"value": got, "from": "notes"}
    got = settings_kind(standard)
    if got:
        return {"value": got, "from": "settings"}
    return {"value": DEFAULT, "from": "default"}


def view(shell: Mapping | None, messages=None, *, standard: Mapping | None = None) -> dict | None:
    """SHSTAGE-8: the control on the shell line, only when the part will be meshed as a shell (a shell asked for, a
    surface file); None otherwise (a solid, or a shell that cannot be made)."""
    from meshpipeline.application import purpose_map as pm

    if not isinstance(shell, Mapping) or shell.get("mesh_as") != pm.MESH_SHELL:
        return None
    s = start(messages, standard=standard)
    return {"question": QUESTION, "options": [dict(o) for o in OPTIONS], "value": s["value"], "from": s["from"],
            "from_words": FROM_WORDS[s["from"]]}


def settle(sent, started: Mapping | None) -> dict:
    """SHSTAGE-8: WHAT PROCEED SETTLES. The user's click is the last word for this run: a pick that differs from where
    the control started is recorded as the stage's ({"pick": "triangles", "by": "stage"}); a control left where it
    started (or nothing sent) is no pick - the notes and settings decide as the run reads them, and Quads with none.
    {"pick": "" | "quads" | "triangles", "by": "stage" | "notes" | "settings" | "default", "value": in force,
    "words": the declaration's sentence ("" for the default)}."""
    s = dict(started or {"value": DEFAULT, "from": "default"})
    p = pick_of(sent)
    if p and p != s.get("value"):
        return {"pick": p, "by": "stage", "value": p, "words": declaration_words(p, "stage")}
    by = str(s.get("from") or "default")
    value = pick_of(s.get("value")) or DEFAULT
    return {"pick": "", "by": by, "value": value, "words": declaration_words(value, by) if by != "default" else ""}


def declaration_words(value: str, by: str) -> str:
    """The declaration's sentence: "Elements: triangles, as chosen on the stage." ("" with no value)."""
    v = pick_of(value)
    if not v:
        return ""
    return f"Elements: {v}, {FROM_WORDS.get(by, FROM_WORDS['default'])}."


def confirmed(session_id: str) -> str:
    """The stage's own pick as confirmed at Proceed (the stored confirmation's fea_shell_elements): "quads",
    "triangles", or "" - none, an unreadable store, or a run that is not a shell (the run then reads the notes and
    settings alone, exactly as before)."""
    if not session_id:
        return ""
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown is no pick: the notes and settings decide
        return ""
    if not isinstance(record, dict) or record.get("fea_mesh_as") not in ("shell", "mixed"):
        return ""
    return pick_of(record.get("fea_shell_elements"))


__all__ = ["DEFAULT", "FROM_WORDS", "OPTIONS", "QUADS", "QUESTION", "RULE", "TRIANGLES", "confirmed",
           "declaration_words", "notes_kind", "pick_of", "said_in", "settings_kind", "settle", "start", "view"]
