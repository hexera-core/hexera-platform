# Responsibility: Verify a note or chat turn keeps the files attached and the faces picked with it (NOTE-16, NOTE-17): made, cleaned, merged, rendered and captured with them, and a picture-only note kept.
from __future__ import annotations

from meshpipeline.contracts import user_notes as un

PIC = {"id": "a1b2c3", "kind": "image", "name": "bore.png", "mime": "image/png", "bytes": 1234,
       "width": 800, "height": 600}
SPEC = {"id": "d4e5f6", "kind": "text", "name": "spec.pdf", "mime": "application/pdf", "bytes": 9000, "pages": 3}
FACE = {"id": "Face_12", "label": "Face 12 · round Ø20 mm"}


def test_a_plain_note_is_as_before():
    note = un.make("2 mm on the bore", source=un.SOURCE_CONSOLE, at="t")
    assert note == {"text": "2 mm on the bore", "source": "console", "turn": None, "at": "t"}
    assert un.clean([note]) == [{"id": "n1", **note}]


def test_make_and_clean_keep_attachments_and_faces():
    note = un.make("this cell size here", source=un.SOURCE_CONSOLE, attachments=[PIC], faces=[FACE], at="t")
    assert note["attachments"] == [PIC] and note["faces"] == [FACE]
    (kept,) = un.clean([note])
    assert kept["attachments"] == [PIC] and kept["faces"] == [FACE]


def test_a_note_with_only_a_picture_is_kept_with_no_words():
    (kept,) = un.clean([un.make("", source=un.SOURCE_CONSOLE, attachments=[PIC])])
    assert kept["text"] == "" and kept["attachments"] == [PIC]
    assert un.clean([{"text": "", "source": "console"}]) == [], "an empty note with nothing attached is no note"


def test_clean_keeps_only_the_declared_keys_never_bytes():
    dirty = {**PIC, "data_url": "data:image/png;base64,AAAA", "object_key": "sessions/x/attachments/a1b2c3/file"}
    (kept,) = un.clean([{"text": "", "source": "console", "attachments": [dirty, {"name": "no id"}]}])
    assert kept["attachments"] == [PIC]


def test_merge_keeps_two_picture_notes_apart():
    a = un.make("", source=un.SOURCE_CONSOLE, attachments=[PIC])
    b = un.make("", source=un.SOURCE_CONSOLE, attachments=[SPEC])
    merged = un.merge([a], [b])
    assert [n["attachments"][0]["id"] for n in merged] == ["a1b2c3", "d4e5f6"]
    assert len(un.merge(merged, [a])) == 2, "the same note again is not added twice"


def test_render_names_the_files_and_faces_under_the_words():
    text = un.render([un.make("2 mm here", source=un.SOURCE_CONSOLE, attachments=[PIC, SPEC], faces=[FACE])])
    assert "2 mm here\n>>>\n[attached: bore.png]\n[attached: spec.pdf]" in text
    assert "[picked on the model: Face_12 (Face 12 · round Ø20 mm)]" in text


def test_capture_keeps_a_turn_that_carries_a_file_with_its_rows():
    messages = [{"role": "user", "content": "mesh this for CFD"},
                {"role": "assistant", "content": "Sure."},
                {"role": "user", "content": "this face needs finer cells", "attachments": [PIC], "faces": [FACE]},
                {"role": "user", "content": "", "attachments": [SPEC]}]
    notes, unmatched = un.capture(messages)
    assert unmatched == []
    assert [(n["turn"], n["text"]) for n in notes] == [(2, "this face needs finer cells"), (3, "")]
    assert notes[0]["attachments"] == [PIC] and notes[0]["faces"] == [FACE]
    assert notes[1]["attachments"] == [SPEC]


def test_capture_by_quote_keeps_the_turns_rows():
    messages = [{"role": "user", "content": "2 mm on this bore", "faces": [FACE]}]
    notes, _ = un.capture(messages, quotes=["2 mm on this bore"])
    assert len(notes) == 1 and notes[0]["faces"] == [FACE]
