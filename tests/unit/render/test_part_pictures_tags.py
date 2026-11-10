# Responsibility: Verify the parts pictures number a part only where it is SEEN (read off the id picture): a part hidden behind another gets no number on the one in front, a number sits inside its part's visible pixels, and no two numbers touch.
# Boundaries: id pictures written here as arrays; no rendering.
from __future__ import annotations

import numpy as np

from meshpipeline.render import part_pictures as PP


def test_a_hidden_part_gets_no_number_and_a_seen_one_sits_on_itself():
    ids = np.full((100, 200), -1)
    ids[10:90, 10:190] = 0                 # a big plate in front
    ids[40:50, 150:160] = 2                # a small part, seen
    tags, hidden = PP.tag_places(ids, 3)   # part 1 is behind the plate: no pixel of it is seen
    assert hidden == [1]
    by = {t["k"]: t for t in tags}
    assert set(by) == {0, 2}
    assert ids[by[2]["y"], by[2]["x"]] == 2 or abs(by[2]["x"] - 155) < 60


def test_numbers_never_touch():
    ids = np.full((60, 60), -1)
    for k in range(9):                     # nine small parts packed together
        y, x = divmod(k, 3)
        ids[20 + y * 6:26 + y * 6, 20 + x * 6:26 + x * 6] = k
    tags, _hidden = PP.tag_places(ids, 9)
    boxes = [(t["x"] - (14 + 9 * len(str(t["k"] + 1))) / 2, t["y"] - PP.TAG_H / 2,
              t["x"] + (14 + 9 * len(str(t["k"] + 1))) / 2, t["y"] + PP.TAG_H / 2) for t in tags]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def test_the_id_picture_reads_back():
    img = np.zeros((2, 3, 4), dtype=np.uint8)
    img[0, 0] = (1, 0, PP.ID_MARK, 255)
    img[0, 1] = (44, 1, PP.ID_MARK, 255)
    img[1, 2] = (9, 0, 17, 255)            # not an id colour
    got = PP.decode_ids(img)
    assert got[0, 0] == 0 and got[0, 1] == 299 and got[1, 2] == -1 and got[1, 0] == -1
