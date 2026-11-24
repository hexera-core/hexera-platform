# Responsibility: Give a model call the pictures the user attached to their notes and chat messages (NOTE-16): where a kept attachment's bytes live, a stored picture as a data URL built at call time, and the newest few placed each beside the note it came with.
# Boundaries: reads the object store only; nothing here keeps a data URL - they exist only in the request being sent. Which route may look at pictures is the route's capability, never decided here. The upload, the checks and the session's index are application/chat_attachments.
# Collaborates with: application/chat_attachments.py (stores the files under attachment_key), contracts/user_notes.py (the notes and their attachment rows), cad/part_words.py (the parts reader), contracts/object_storage.py.
"""THE USER'S OWN PICTURES, FOR A MODEL CALL (NOTE-16).

A user who cannot name a face sends a screenshot with their words ("this cell size on the bearing bore").
A reader that can look at pictures is shown it beside the note it came with, so it can tie the words to a place
on the part. Pictures show WHERE; the words say WHAT: a number seen only in a picture is never applied.
"""
from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from meshpipeline.contracts.user_notes import _where

logger = logging.getLogger(__name__)

#: the rule this module is the code of (docs/RULES.md)
RULE = "NOTE-16"
#: the most of the user's pictures one model call is shown (the newest; already downscaled by the store)
MAX_PICTURES = 4
#: the picture types the store keeps (it re-encodes every picture to one of these)
MIMES = ("image/png", "image/jpeg", "image/webp")
_ID = re.compile(r"^[0-9a-f]{16}$")

#: What the reader is told the pictures are - one plain sentence, sent only when there is a picture.
TOLD = ("The user also attached pictures of their own, each below after the note it came with: a screenshot "
        "they took to show which place on the part their words mean - match it to the part's areas by what it "
        "shows, and if you cannot tell which place is meant, say so (confidence below 0.6), never guess.")


def attachment_key(session_id: Any, attachment_id: str, part: str) -> str:
    """Where one attachment's bytes ("file") or read text ("text.txt") are kept."""
    return f"sessions/{session_id}/attachments/{attachment_id}/{part}"


def _read(key: str) -> bytes:
    from meshpipeline.contracts.object_storage import get_object_store
    return get_object_store().get_bytes(object_key=key)


def data_url(session_id: Any, row: Mapping, *, read: Callable[[str], bytes] | None = None) -> str | None:
    """A stored picture as a data URL for a model call (built at call time, never kept), or None when it cannot be
    read now. `read(key)` gives the bytes (the object store by default). Blocking (the store)."""
    if row.get("kind") != "image" or not _ID.match(str(row.get("id") or "")) or not str(session_id or ""):
        return None
    mime = str(row.get("mime") or "image/png")
    if mime not in MIMES:
        return None
    try:
        data = (read or _read)(attachment_key(session_id, str(row["id"]), "file"))
    except Exception:  # noqa: BLE001 - the call goes on; the picture is named instead
        logger.warning("user pictures: picture %s could not be read", row.get("id"))
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def picked(notes: Iterable[Mapping], *, limit: int = MAX_PICTURES) -> list[tuple[Mapping, Mapping]]:
    """(note, picture row) for the newest `limit` pictures the notes carry, in the notes' own order. `notes` as the
    reader is shown them (user_notes.own_notes: numbered n1, n2...)."""
    rows = [n for n in notes or () if isinstance(n, Mapping)]
    chosen: list[tuple[int, int]] = []
    for i in range(len(rows) - 1, -1, -1):
        for j, a in enumerate(rows[i].get("attachments") or ()):
            if isinstance(a, Mapping) and a.get("kind") == "image" and a.get("id") and len(chosen) < limit:
                chosen.append((i, j))
    return [(rows[i], rows[i]["attachments"][j]) for i, j in sorted(chosen)]


def content_parts(notes: Iterable[Mapping], session_id: Any, *, limit: int = MAX_PICTURES,
                  load: Callable[[Any, Mapping], str | None] | None = None) -> list[dict]:
    """The content parts that show the reader the user's pictures: TOLD once, then for each picture a line naming
    the note it came with and the picture itself. [] with no picture, or none readable now. Blocking (the store)."""
    load = load or data_url
    out: list[dict] = []
    for note, row in picked(notes, limit=limit):
        url = load(session_id, row)
        if not url:
            continue
        out.append({"type": "text", "text": (f"The user's picture \"{row.get('name') or 'picture'}\", attached to "
                                             f"note [{note.get('id') or '?'}] ({_where(note)}).")})
        out.append({"type": "image_url", "image_url": {"url": url}})
    return [{"type": "text", "text": TOLD}, *out] if out else []
