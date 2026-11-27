# Responsibility: Verify NOTE-16 and NOTE-17 together: a note that carries a picture AND a picked face keeps both - the reader is shown the face under the words, the pick decides the place in code, and the picture still goes to the parts reader.
# Boundaries: no model and no store: the model's answer is written here, the picture's bytes are a stub loader.
from __future__ import annotations

import json

from meshpipeline.contracts import user_notes as UN
from meshpipeline.contracts import user_pictures as UP
from meshpipeline.engines import mesh_requirements as MR

BORE = {"id": "Face_12", "label": "Face 12 · round Ø15 mm",
        "sig": {"area_m2": 0.0004, "centroid_m": [0.0, 0.0, 0.0], "normal": None}}
PIC = {"id": "00000000000000a1", "kind": "image", "name": "bore.png", "mime": "image/png"}
NOTE = {"text": "2 mm here", "source": "console", "attachments": [PIC], "faces": [BORE]}


def _answer() -> str:
    return json.dumps({"stated_unit": "none", "sizes": [{"what": "global", "value": 2, "unit": "mm",
                                                          "quote": "2 mm here"}],
                       "counts": [], "layers": [], "local": [], "physics": [], "element_kind": [], "limits": [],
                       "other": [], "controls": []})


def test_the_pick_decides_the_place_when_the_note_also_carries_a_picture():
    kept = UN.own_notes([NOTE])
    shown = UN.render(kept)
    assert "Face 12 · round Ø15 mm" in shown and "bore.png" in shown
    req, _dropped, _na = MR.check(_answer(), shown, "fea")
    req = MR.bind_picks(req, kept)
    assert req["sizes"]["global_mm"] is None
    (loc,) = req["local"]
    assert loc["faces"] == ["Face_12"] and loc["mm"] == 2.0 and loc["rule"] == "NOTE-17"


def test_the_picture_of_a_note_with_a_face_still_goes_to_the_parts_reader():
    kept = UN.own_notes([NOTE])
    parts = UP.content_parts(kept, "sess-1", load=lambda _sid, row: "data:image/png;base64,AAAA")
    assert parts[0]["text"] == UP.TOLD
    assert parts[1]["text"].startswith('The user\'s picture "bore.png", attached to note [')
    assert parts[2] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert kept[0]["faces"][0]["id"] == "Face_12"            # the face rides with the same note
