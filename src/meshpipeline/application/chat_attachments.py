# Responsibility: Own the files a user attaches in the chat box or the notes box (NOTE-16): check each by its bytes, re-encode pictures, read a text file's or a PDF's text, keep them per session, and answer who may read them back.
# Owns: the limits and the plain lines said when one is hit, the session's attachment index (chat_sessions.attachments), the object keys under sessions/<sid>/attachments/, and the public row a message or note carries.
# Boundaries: no HTTP (api/v1/chat.py carries it) and no model call (agents/intake and the readers ask for pictures and text here). A stored row never holds a file's bytes or a data URL.
# Collaborates with: contracts/object_storage (the bytes), persistence/repositories/session_repository (the index, under the session row lock), contracts/user_notes (the rows notes keep), application/privacy_service (erasure reads object_keys).
"""THE USER'S ATTACHMENTS (docs/RULES.md NOTE-16).

A user pastes a screenshot ("I want this cell size on the bearing bore") or attaches a spec sheet. Each file:

* is checked by its BYTES, never its name or the browser's type: a PNG, JPEG or WebP picture, a PDF, or UTF-8 text
  (TXT, MD, CSV). Anything else - SVG, HTML, an executable, an archive - is refused with one plain line;
* a picture is decoded under a pixel cap, turned upright, downscaled to LONG_SIDE and RE-ENCODED (PNG when it has
  transparency, else JPEG): only the re-encoding is kept, so no metadata (EXIF, GPS, text chunks) survives;
* a PDF's text is read with page, character and time caps (an encrypted PDF, or one with no text - a scan - is told);
* is kept under sessions/<sid>/attachments/<id>/ and indexed on the session, so erasure finds every key by name.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

KIND_IMAGE = "image"
KIND_TEXT = "text"

#: The most bytes one picture may have as sent, and one text file or PDF.
IMAGE_MAX_BYTES = 10 * 1024 * 1024
TEXT_MAX_BYTES = 20 * 1024 * 1024
#: The most pixels a picture may decode to (a "decompression bomb" is refused before it is decoded).
MAX_PIXELS_IN = 50_000_000
#: The long side a picture is kept at (what the vision models are shown anyway).
LONG_SIDE = 1568
#: A PDF: the most pages read, and the longest it may take.
PDF_MAX_PAGES = 50
PDF_SECONDS = 15.0
#: The most text kept from one file (the rest is told as cut).
MAX_CHARS = 40_000
#: The most files one message or note carries (api/schemas/chat MAX_ATTACHMENTS; a test holds them equal) and one
#: conversation holds.
MAX_PER_MESSAGE = 5
MAX_PER_SESSION = 30

IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

# the same lines the browser says before it sends (ui/js/shell/attach.js)
TOO_BIG_IMAGE = "That picture is too large: 10 MB is the most."
TOO_BIG_FILE = "That file is too large: 20 MB is the most."
NOT_ACCEPTED = "That kind of file cannot be attached. Pictures, PDF and text files can."
EMPTY = "That file is empty."
TOO_MANY_PIXELS = "That picture is too large to read. Attach a smaller screenshot."
BAD_IMAGE = "That picture could not be read. Attach it again as a PNG or JPG."
BAD_PDF = "That PDF could not be read."
LOCKED_PDF = "That PDF is locked with a password. Attach an unlocked copy."
SCANNED_PDF = "This PDF has no text we can read - it looks scanned. Attach a screenshot of the page instead."
SLOW_PDF = "That PDF took too long to read. Attach the pages you need, or a screenshot."
TOO_MANY_HERE = f"This conversation already holds {MAX_PER_SESSION} files, the most it can hold."
RUN_STARTED = ("The run has already started, so a new file cannot reach it. "
               "Attach it before you approve the next run.")


class Refused(Exception):
    """A file not kept, with the one plain line the user is shown and the HTTP status the route answers."""

    def __init__(self, line: str, status: int = 422):
        super().__init__(line)
        self.line = line
        self.status = status


@dataclass(frozen=True)
class Read:
    """A file as it will be kept: the bytes stored, their type, and what was read from them."""
    kind: str
    mime: str
    data: bytes
    text: str = ""
    pages: int | None = None
    cut: bool = False
    width: int | None = None
    height: int | None = None


# #
# checking by bytes
# #

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_MARKUP = re.compile(rb"^\s*<(?:!doctype|html|svg|\?xml|script|head|body|iframe)", re.IGNORECASE)


def sniff(data: bytes) -> str | None:
    """What the bytes are - "png" / "jpeg" / "webp" / "pdf" / "text" - or None. Never the name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    if b"\x00" in data or _MARKUP.match(data[:2048].lstrip(b"\xef\xbb\xbf")):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # text is words: a control character other than a tab, a line end or a page break means binary (an archive's
    # "PK" and two control bytes, an executable's header) whatever it decodes to
    if _CONTROL.search(text):
        return None
    return "text"


def clean_name(name: Any, fallback: str = "pasted.png") -> str:
    """The name the user sees: the last part of what the browser sent, no control characters, at most 120 long."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:120] or fallback


# #
# reading
# #

def read_image(data: bytes) -> Read:
    """A picture decoded under the pixel cap, turned upright, downscaled and re-encoded with no metadata."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            w, h = probe.size
            if fmt not in IMAGE_MIMES:
                raise Refused(NOT_ACCEPTED, 415)
            if w * h > MAX_PIXELS_IN:
                raise Refused(TOO_MANY_PIXELS, 413)
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            upright = ImageOps.exif_transpose(img) or img
            alpha = upright.mode in ("RGBA", "LA", "PA") or (upright.mode == "P" and "transparency" in upright.info)
            # a NEW image from the pixels alone: nothing of the file's metadata is carried
            pixels = upright.convert("RGBA" if alpha else "RGB")
    except Refused:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise Refused(BAD_IMAGE, 422) from exc
    pixels.thumbnail((LONG_SIDE, LONG_SIDE))
    out = io.BytesIO()
    if alpha:
        pixels.save(out, format="PNG", optimize=True)
        mime = "image/png"
    else:
        pixels.save(out, format="JPEG", quality=88)
        mime = "image/jpeg"
    return Read(kind=KIND_IMAGE, mime=mime, data=out.getvalue(), width=pixels.width, height=pixels.height)


def read_pdf(data: bytes, *, seconds: float = PDF_SECONDS) -> Read:
    """A PDF's text: at most PDF_MAX_PAGES pages and MAX_CHARS characters, within `seconds` (checked page by page).
    Only text is read - never its scripts, forms or embedded files."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    deadline = time.monotonic() + seconds
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Refused(LOCKED_PDF, 422)
        pages = len(reader.pages)
        parts, size, cut = [], 0, pages > PDF_MAX_PAGES
        for i, page in enumerate(reader.pages):
            if i >= PDF_MAX_PAGES:
                break
            if time.monotonic() > deadline:
                raise Refused(SLOW_PDF, 422)
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(f"--- page {i + 1} ---\n{text}")
                size += len(text)
            if size >= MAX_CHARS:
                cut = cut or i + 1 < min(pages, PDF_MAX_PAGES) or size > MAX_CHARS
                break
    except Refused:
        raise
    except (PyPdfError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise Refused(BAD_PDF, 422) from exc
    text = "\n\n".join(parts)
    if not text.strip():
        raise Refused(SCANNED_PDF, 422)
    if len(text) > MAX_CHARS:
        text, cut = text[:MAX_CHARS], True
    return Read(kind=KIND_TEXT, mime="application/pdf", data=data, text=text, pages=pages, cut=cut)


def read_text(data: bytes) -> Read:
    text = data.decode("utf-8-sig")
    if not text.strip():
        raise Refused(EMPTY, 422)
    cut = len(text) > MAX_CHARS
    return Read(kind=KIND_TEXT, mime="text/plain", data=data, text=text[:MAX_CHARS], cut=cut)


def read(data: bytes) -> Read:
    """Check one file by its bytes and read it, or refuse it with its plain line."""
    if not data:
        raise Refused(EMPTY, 422)
    what = sniff(data)
    if what in ("png", "jpeg", "webp"):
        if len(data) > IMAGE_MAX_BYTES:
            raise Refused(TOO_BIG_IMAGE, 413)
        return read_image(data)
    if len(data) > TEXT_MAX_BYTES:
        raise Refused(TOO_BIG_FILE, 413)
    if what == "pdf":
        return read_pdf(data)
    if what == "text":
        return read_text(data)
    raise Refused(NOT_ACCEPTED, 415)


# #
# keeping
# #

def object_key(session_id: Any, attachment_id: str, part: str) -> str:
    """Where one attachment's bytes ("file") or read text ("text.txt") are kept (contracts/user_pictures owns it)."""
    from meshpipeline.contracts.user_pictures import attachment_key
    return attachment_key(session_id, attachment_id, part)


def new_id() -> str:
    return secrets.token_hex(8)


_ID = re.compile(r"^[0-9a-f]{16}$")

#: The row a message or note carries and the browser is sent: never an object key, a hash or the bytes.
PUBLIC_KEYS = ("id", "kind", "name", "mime", "bytes", "pages", "chars", "cut", "width", "height")


def entry_for(attachment_id: str, name: str, got: Read, *, session_id: Any) -> dict:
    """The session index's entry for one kept file."""
    entry: dict[str, Any] = {
        "kind": got.kind, "name": name, "mime": got.mime, "bytes": len(got.data),
        "object_key": object_key(session_id, attachment_id, "file"),
        "sha256": hashlib.sha256(got.data).hexdigest(), "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if got.kind == KIND_TEXT:
        entry.update(text_key=object_key(session_id, attachment_id, "text.txt"), chars=len(got.text), cut=got.cut)
        if got.pages is not None:
            entry["pages"] = got.pages
    else:
        entry.update(width=got.width, height=got.height)
    return entry


def public(attachment_id: str, entry: Mapping) -> dict:
    return {"id": attachment_id, **{k: entry[k] for k in PUBLIC_KEYS[1:] if entry.get(k) is not None}}


def index_of(session: Any) -> dict:
    raw = getattr(session, "attachments", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def resolve(session: Any, ids: Iterable[Any]) -> list[dict]:
    """The public rows of the ids this session holds, in the order sent, each once; an id it does not hold is
    dropped (never another session's file)."""
    index, out, seen = index_of(session), [], set()
    for raw in ids or ():
        aid = str(raw.get("id") if isinstance(raw, Mapping) else raw or "")
        if aid in index and aid not in seen and _ID.match(aid):
            seen.add(aid)
            out.append(public(aid, index[aid]))
    return out


def with_text(session: Any, rows: Iterable[Mapping]) -> list[dict]:
    """The rows with each text file's read text on it (capped at MAX_CHARS): what the notes readers see as the
    user's own words. A text the store cannot give now is left off - the row is kept. Blocking (the store)."""
    index, out = index_of(session), []
    for row in rows or ():
        row = dict(row)
        entry = index.get(str(row.get("id") or ""))
        if row.get("kind") == KIND_TEXT and isinstance(entry, Mapping) and entry.get("text_key"):
            try:
                text = get_bytes(str(entry["text_key"])).decode("utf-8", "replace")[:MAX_CHARS]
            except Exception:  # noqa: BLE001 - the words still go; the file's text is read again on the next note
                logger.warning("chat attachment: text of %s could not be read", row.get("id"))
                text = ""
            if text.strip():
                row["text"] = text
        out.append(row)
    return out


def data_url(session_id: Any, row: Mapping) -> str | None:
    """A stored picture as a data URL for a model call (built at call time, never kept), or None when it cannot be
    read now. Blocking (the store). One definition: contracts/user_pictures."""
    from meshpipeline.contracts.user_pictures import data_url as url

    return url(session_id, row, read=get_bytes)


def object_keys(session: Any) -> list[str]:
    """Every object key this session's attachments are kept under (erasure deletes them by name)."""
    keys = []
    for entry in index_of(session).values():
        if isinstance(entry, Mapping):
            keys += [str(entry[k]) for k in ("object_key", "text_key") if entry.get(k)]
    return keys


#: what an erased account's index keeps of each file: only where its bytes are, for the sweep to delete them
_ERASED_KEEPS = ("object_key", "text_key")


def erased_index(index: Any) -> dict | None:
    """The index as the erasure request leaves it (application/privacy_service): each entry only its object keys -
    the names the user gave their files go with everything else they wrote; the keys stay until the sweep
    deletes the bytes by them (the object store cannot list a session's keys). None with no files."""
    if not isinstance(index, Mapping) or not index:
        return None
    return {str(aid): {k: e[k] for k in _ERASED_KEEPS if e.get(k)} for aid, e in index.items()
            if isinstance(e, Mapping)}


def exported(index: Any) -> list[dict]:
    """The files of one session's index as the account export lists them: the public row and when it was
    attached - never the bytes or the read text (each file downloads from its own route while the account is
    active)."""
    if not isinstance(index, Mapping):
        return []
    return [{**public(str(aid), e), **({"at": e["at"]} if e.get("at") else {})} for aid, e in index.items()
            if isinstance(e, Mapping) and _ID.match(str(aid))]


def stored(session: Any, attachment_id: str) -> dict | None:
    """The index entry of one attachment this session holds, or None."""
    entry = index_of(session).get(str(attachment_id or ""))
    return dict(entry) if isinstance(entry, Mapping) and _ID.match(str(attachment_id)) else None


def _put(key: str, data: bytes, mime: str) -> None:
    import tempfile
    from pathlib import Path

    from meshpipeline.contracts.object_storage import get_object_store

    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        get_object_store().upload_file(local_path=tmp, object_key=key, content_type=mime)
    finally:
        tmp.unlink(missing_ok=True)


def _drop(keys: Iterable[str]) -> None:
    from meshpipeline.contracts.object_storage import get_object_store

    store = get_object_store()
    for key in keys:
        try:
            store.delete_object(object_key=key)
        except Exception:  # noqa: BLE001 - best effort: the bytes of a refused file are swept with the session
            pass


def get_bytes(key: str) -> bytes:
    from meshpipeline.contracts.object_storage import get_object_store

    return get_object_store().get_bytes(object_key=key)


async def accept(session_id: Any, owner_id: str, *, name: Any, data: bytes, session_repo, db_factory,
                 organization_id: str = "") -> dict:
    """Keep one attached file for the session: read and checked, its bytes (and read text) stored, its entry
    indexed under the session row lock. Returns the public row. Raises Refused with the user's line, or LookupError
    when the session is not the caller's."""
    import asyncio

    async with db_factory() as db:
        session = await session_repo.get_for_owner(db, session_id, owner_id, organization_id=organization_id)
    if session is None:
        raise LookupError("session not found")
    if getattr(session, "job_id", None):
        raise Refused(RUN_STARTED, 409)
    if len(index_of(session)) >= MAX_PER_SESSION:
        raise Refused(TOO_MANY_HERE, 422)

    got = await asyncio.to_thread(read, data)
    aid = new_id()
    shown = clean_name(name, "pasted.png" if got.kind == KIND_IMAGE else "file.txt")
    entry = entry_for(aid, shown, got, session_id=session_id)
    await asyncio.to_thread(_put, entry["object_key"], got.data, got.mime)
    if entry.get("text_key"):
        await asyncio.to_thread(_put, entry["text_key"], got.text.encode("utf-8"), "text/plain; charset=utf-8")

    # the index entry under the session row lock, in the repository: the run may have started, or another file
    # filled the session, while this one was read - then its bytes are dropped
    async with db_factory() as db:
        status = await session_repo.add_attachment(db, session_id, owner_id, aid, entry, most=MAX_PER_SESSION)
        if status == "added":
            await db.commit()
    if status != "added":
        await asyncio.to_thread(_drop, [k for k in (entry["object_key"], entry.get("text_key")) if k])
        if status == "not_found":
            raise LookupError("session not found")
        raise Refused(RUN_STARTED, 409) if status == "running" else Refused(TOO_MANY_HERE, 422)
    return public(aid, entry)


#: What a kept file is served as: its own picture type, plain text, or a PDF - never anything a browser runs.
_SERVED = {"image/png", "image/jpeg", "image/webp", "application/pdf"}


def served_type(entry: Mapping) -> str:
    mime = str(entry.get("mime") or "")
    return mime if mime in _SERVED else "text/plain; charset=utf-8"


def served_headers(entry: Mapping) -> dict[str, str]:
    """Never sniffed, never scripted (a sandboxed page with no sources), kept privately for an hour; a PDF opens as a
    download so no viewer runs what it holds."""
    shown = re.sub(r'[^A-Za-z0-9._ -]', "_", str(entry.get("name") or "file"))[:120]
    how = "attachment" if served_type(entry) == "application/pdf" else "inline"
    return {"X-Content-Type-Options": "nosniff", "Content-Disposition": f'{how}; filename="{shown}"',
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "private, max-age=3600"}


async def open_for_owner(session_id: Any, attachment_id: str, owner_id: str, *, session_repo, db_factory,
                         organization_id: str = "") -> tuple[bytes, dict] | None:
    """One attachment's stored bytes and its entry, for its owner only (None for anyone else or no such file)."""
    import asyncio

    from meshpipeline.contracts.object_storage import ObjectNotFound

    async with db_factory() as db:
        session = await session_repo.get_for_owner(db, session_id, owner_id, organization_id=organization_id)
    entry = stored(session, attachment_id) if session is not None else None
    if entry is None:
        return None
    try:
        data = await asyncio.to_thread(get_bytes, entry["object_key"])
    except ObjectNotFound:
        return None
    return data, entry


__all__ = ["IMAGE_MAX_BYTES", "KIND_IMAGE", "KIND_TEXT", "MAX_CHARS", "MAX_PER_MESSAGE", "MAX_PER_SESSION",
           "PDF_MAX_PAGES", "Read", "Refused", "TEXT_MAX_BYTES", "accept", "clean_name", "entry_for",
           "index_of", "object_key", "object_keys", "open_for_owner", "public", "read", "read_image", "read_pdf",
           "read_text", "resolve", "served_headers", "served_type", "sniff", "stored"]
