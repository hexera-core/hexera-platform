# Responsibility: Reproduce m483 round 13's 6b and 6c from the stage readings dev-areen made (tests/fixtures/part_readings_m483_r13.json): a one-body model's place is never dropped as "not clear which parts", and a check answered from an earlier session never hands the parts reader areas an older rule made.
"""m483 round 13 (deploy ef26e05e, 21 Nov).

6b: the reader tied "the vertical leg" to Part_1, the only part, as a portion - at confidence 0.45, so the checks
dropped it ("it was not clear which parts you meant") and the shell row was gone.
6c: the stage's table was answered from an earlier session's check (geometry_reuse): one "thin walls, 3 mm thick"
area for both legs of the bracket, no level / upright split (round 9), so "the base plate" could only be a portion of
Part_1 - the nearest there is."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.cad import part_words as PW

SAVED = json.loads((Path(__file__).parents[2] / "fixtures" / "part_readings_m483_r13.json").read_text(encoding="utf-8"))


def _checked(case: str, *, table=None, phrases=None):
    c = SAVED[case]
    args = json.dumps({"phrases": phrases if phrases is not None else c["phrases"], "words": c["words"]})
    return PW.check(args, table if table is not None else c["table"], c["notes"])


def test_6b_a_one_body_place_the_reader_was_unsure_of_is_the_nearest_never_dropped():
    entries, dropped, _w = _checked("6b")
    assert dropped == [] and len(entries) == 1
    assert entries[0]["parts"] == ["Part_1"] and entries[0]["piece"] is True
    # ... on the table the rules make today too (one plate: no flat face of itself, only its holes)
    fresh = [r for r in SAVED["6b"]["table"] if r["id"] in ("Part_1", "Feature_1")]
    entries, dropped, _w = _checked("6b", table=fresh)
    assert dropped == [] and entries[0]["piece"] is True


def test_6b_a_reader_unsure_which_of_two_parts_is_still_dropped():
    """The rule is about a model of ONE body: with two, an unsure reading of which part stays not applied."""
    table = [*SAVED["6b"]["table"], {"id": "Part_2", "name": "", "size_mm": [10, 10, 10], "diag_mm": 17.3}]
    entries, dropped, _w = _checked("6b", table=table)
    assert entries == [] and dropped and "not clear which parts you meant" in dropped[0]


def test_6c_the_saved_table_reproduces_the_nearest():
    entries, dropped, _w = _checked("6c")
    assert dropped == [] and entries[0]["parts"] == ["Part_1"] and entries[0]["piece"] is True
    walls = [r for r in SAVED["6c"]["table"] if r.get("kind") == "thin wall"]
    assert len(walls) == 1 and not walls[0].get("lie")          # both legs one area: the base plate is no area of it


def test_a_check_is_never_reused_across_a_change_of_the_area_rules(monkeypatch):
    from meshpipeline.application import geometry_reuse as GR
    from meshpipeline.cad import part_features as PF

    now = (GR.unit_key({"scale_to_metres": 0.001}), GR.fea_key("abc", {"scale_to_metres": 0.001}, 0.001, None))
    monkeypatch.setattr(PF, "VERSION", PF.VERSION + 1)
    later = (GR.unit_key({"scale_to_metres": 0.001}), GR.fea_key("abc", {"scale_to_metres": 0.001}, 0.001, None))
    assert now[0] != later[0] and now[1] != later[1]


def test_6c_on_the_areas_the_rules_make_today_the_base_plate_is_an_area_and_not_a_piece(tmp_path):
    """A bent bracket read today: its walls lie apart, each its own area, the level one at the bottom - so "the base
    plate" ties to an area of its own (a real place, judged on its elements), not a portion of the part."""
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf
    from tests.unit.engines.test_shell_local_truth import _stage

    from meshpipeline.cad import part_features as PF
    from meshpipeline.engines import fea_driver as FD

    ws = _stage(sf.bent_bracket(), tmp_path / "bracket")
    record, surface, _d = FD.measure_staged(ws)
    table, _faces_of, _shown = PW.table_of(record, surface)
    level = [r for r in table if r.get("feature") and str(r.get("lie") or "").startswith("level")]
    upright = [r for r in table if r.get("feature") and r.get("lie") == "upright"]
    assert level and upright, [(r["id"], r.get("kind"), r.get("lie")) for r in table]
    assert PF.VERSION >= 4
    phrase = {**SAVED["6c"]["phrases"][0], "parts": [level[0]["id"]], "extent": "whole", "confidence": 0.9}
    entries, dropped, _w = _checked("6c", table=table, phrases=[phrase])
    assert dropped == [] and entries[0]["parts"] == [level[0]["id"]] and "piece" not in entries[0]
