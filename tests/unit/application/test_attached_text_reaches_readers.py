# Responsibility: Verify an attached text file reaches the notes readers as the user's own words (NOTE-16): its read
#   text is kept on the note's row, a size written only in the file passes the quote rule and is applied, and the
#   read text never goes back to the browser (the card, the notes reply, the chat history).
# Boundaries: pure code with a fake object store; no database, no model.
from __future__ import annotations

import json

import pytest

from meshpipeline.application import chat_attachments as ca
from meshpipeline.contracts import user_notes as un
from meshpipeline.engines import mesh_requirements as MR

SPEC = "Bearing bore element size 0.5 mm"


def _pdf(text: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    page = w.add_blank_page(300, 200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    import io
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


class _Session:
    def __init__(self, index):
        self.attachments = index


def _stored(monkeypatch, files: dict[str, bytes]):
    """Each file read as the store reads it, its text kept under its own key; the session's index names them."""
    store, index = {}, {}
    for i, (name, data) in enumerate(files.items()):
        got = ca.read(data)
        aid = f"{i + 1:016x}"
        index[aid] = {"kind": got.kind, "name": name, "mime": got.mime, "bytes": len(data),
                      "object_key": f"k/{aid}/file", "text_key": f"k/{aid}/text.txt"}
        store[f"k/{aid}/text.txt"] = got.text.encode()
    monkeypatch.setattr(ca, "get_bytes", lambda key: store[key])
    session = _Session(index)
    return session, ca.with_text(session, ca.resolve(session, list(index)))


def _size_answer(quote: str) -> str:
    return json.dumps({"stated_unit": "none", "element_kind": [], "limits": [], "other": [],
                       "sizes": [{"what": "global", "value": 0.5, "unit": "mm", "quote": quote}]})


@pytest.mark.parametrize("name,data", [("spec.pdf", _pdf(SPEC)), ("spec.txt", (SPEC + "\n").encode())])
def test_a_size_written_only_in_an_attached_file_is_applied(monkeypatch, name, data):
    _session, rows = _stored(monkeypatch, {name: data})
    assert SPEC in rows[0]["text"]
    note = un.make("", source=un.SOURCE_CONSOLE, attachments=rows)
    notes = MR.notes_text({"user_notes": [note]})
    assert SPEC in notes and f"[attached: {name}]" in notes
    req, dropped, _na = MR.check(_size_answer(SPEC), notes)
    assert dropped == [] and req["sizes"]["global_mm"] == pytest.approx(0.5)
    # the same answer without the file's text: not the user's words, not applied
    bare = MR.notes_text({"user_notes": [un.make("", source=un.SOURCE_CONSOLE,
                                                 attachments=[{k: v for k, v in rows[0].items() if k != "text"}])]})
    req, dropped, _na = MR.check(_size_answer(SPEC), bare)
    assert MR.is_empty(req) and dropped


def test_the_browser_never_receives_the_read_text(monkeypatch):
    from meshpipeline.application.intake_brief import build_brief

    _session, rows = _stored(monkeypatch, {"spec.txt": (SPEC + "\n").encode()})
    note = un.make("see the spec", source=un.SOURCE_CONSOLE, attachments=rows,
                   faces=[{"id": "Face_12", "label": "Face 12 · round"}])
    assert note["attachments"][0]["text"].strip() == SPEC
    shown = un.shown(note)
    assert shown["attachments"] == [{k: v for k, v in rows[0].items() if k != "text"}]
    assert shown["faces"] == [{"id": "Face_12", "label": "Face 12 · round"}]
    assert "text" in note["attachments"][0], "shown() changed the stored note"
    msg = {"role": "user", "content": "see the spec", **un.carried(note)}
    assert SPEC not in json.dumps(un.for_browser(msg)) and un.for_browser(msg)["attachments"][0]["name"] == "spec.txt"
    assert un.for_browser({"role": "assistant", "content": "ok"}) == {"role": "assistant", "content": "ok"}

    class _S:
        request_txt = "A bracket."
        purpose = "structural"
        mesh_engine = "gmsh"
        intake_submitted = True
        user_notes = [note]
        messages = []

    brief = build_brief(_S())
    row = brief["user_notes"][0]
    assert row["attachments"][0]["name"] == "spec.txt" and SPEC not in json.dumps(brief), row


def test_a_file_whose_text_cannot_be_read_now_keeps_its_row(monkeypatch):
    session, _rows = _stored(monkeypatch, {"spec.txt": b"bore 2 mm\n"})

    def broken(key):
        raise OSError("store down")
    monkeypatch.setattr(ca, "get_bytes", broken)
    rows = ca.with_text(session, ca.resolve(session, list(session.attachments)))
    assert rows[0]["name"] == "spec.txt" and "text" not in rows[0]


def test_the_rows_cap_is_the_stores_cap():
    assert un.ATTACHED_TEXT_MAX == ca.MAX_CHARS
    row = un.carried({"attachments": [{"id": "a" * 16, "kind": "text", "text": "x" * (ca.MAX_CHARS + 50)}]})
    assert len(row["attachments"][0]["text"]) == ca.MAX_CHARS
