# Responsibility: Verify NOTE-17 on the parts checklist before approval (application/part_matches.with_faces): a row read from words sent with a picked face shows that face as its place (picked), even one the reader tied to a named area, an unplaced phrase is placed by the pick, and words for the whole mesh stay as read.
from __future__ import annotations

from meshpipeline.application import part_matches as PM

FACE = {"id": "Face_12", "label": "Face 12 · round Ø15 mm", "sig": {"area_m2": 0.0004, "centroid_m": [0, 0, 0]}}


def _got(rows=(), unsure=(), inside=None) -> dict:
    out = {"status": "done", "rows": list(rows), "unsure": list(unsure), "parts": [], "order": []}
    if inside:
        out["inside"] = inside
    return out


def _row(quote, parts=("Part_1",), size="2 mm", phrase=0) -> dict:
    return {"phrase": phrase, "words": quote, "quote": quote, "parts": list(parts), "parts_text": "the whole part",
            "size": size, "picked": False, "lit": [*parts, "Feature_1"]}


def test_a_row_for_words_sent_with_a_face_shows_the_face():
    notes = [{"text": "2 mm here", "source": "console", "faces": [FACE]}]
    (row,) = PM.with_faces(_got([_row("2 mm here")]), notes)["rows"]
    assert row["parts"] == [] and "lit" not in row
    assert row["parts_text"] == "the face you picked (Face 12 · round Ø15 mm)" and row["size"] == "2 mm"
    assert row["faces"] == [{"id": "Face_12", "label": "Face 12 · round Ø15 mm"}]       # no signature on the card


def test_a_phrase_the_reader_could_not_place_is_placed_by_the_pick():
    notes = [{"text": "1 mm on this", "source": "console", "faces": [FACE]}]
    unsure = {"phrase": 0, "words": "1 mm on this", "quote": "1 mm on this", "text": "Couldn't tell", "size": "1 mm"}
    out = PM.with_faces(_got(unsure=[unsure]), notes)
    assert out["unsure"] == [] and out["rows"][0]["faces"][0]["id"] == "Face_12" and "text" not in out["rows"][0]


def test_whole_mesh_words_and_notes_without_faces_stay_as_read():
    notes = [{"text": "2 mm overall", "source": "console", "faces": [FACE]},
             {"text": "3 mm on part 2", "source": "console"}]
    rows = [_row("2 mm overall"), _row("3 mm on part 2", parts=("Part_2",), phrase=1)]
    got = _got(rows)
    assert PM.with_faces(got, notes)["rows"] == rows
    assert PM.with_faces(got, [{"text": "2 mm overall", "source": "console"}]) is got
    assert PM.with_faces(None, notes) is None


def test_a_row_the_reader_tied_to_a_named_area_shows_the_pick_as_the_run_applies_it():
    # the live fault: "2 mm elements here" with a hole's bore picked showed the reader's "a hole, 22 mm across"
    # (Feature_4, picked false) while the run sized the picked face. One rule now (mesh_requirements.picked_for).
    notes = [{"text": "2 mm elements here", "source": "console", "faces": [FACE]}]
    row = {**_row("2 mm elements here", parts=("Feature_4",)), "parts_text": "a hole, 22 mm across"}
    (out,) = PM.with_faces(_got([row], inside={"Feature_4": "Part_1"}), notes)["rows"]
    assert out["parts"] == [] and out["picked"] is True and out["faces"][0]["id"] == "Face_12"
    assert out["parts_text"] == "the face you picked (Face 12 · round Ø15 mm)" and out["size"] == "2 mm"
