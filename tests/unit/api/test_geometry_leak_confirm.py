# Responsibility: Verify INS-8 at the confirm route: a part whose check found leaks is confirmed with
# the check's proposed leak size unless the user changed it or switched it off, the declaration
# says so in words, the stored confirmation carries the size, and the run reads it back in metres;
# a part with no leaks, or a flow around the part, carries none.
# Boundaries: the route with the session, the store and the chat turn stood in for (the fixtures of
# test_geometry_check_fails_open); no database.
from __future__ import annotations

import json
import uuid

import pytest
from tests.unit.api.test_geometry_check_fails_open import SID, _confirming, _key, _scouted, served  # noqa: F401

from meshpipeline.api.v1 import geometry as route
from meshpipeline.application.geometry_confirmation import confirmed_leak_closing

pytestmark = pytest.mark.asyncio

LEAKS = {"rule": "INS-8", "found": 3, "close_up_to_mm": 0.66, "cell_mm": 0.2, "radius_cubes": 1,
         "leaks": [{"id": 0, "width_mm": 0.46, "at_mm": [55.1, -9.9, 0.2], "cubes": 3},
                   {"id": 1, "width_mm": 0.38, "at_mm": [75.0, 0.2, 10.1], "cubes": 3},
                   {"id": 2, "width_mm": 0.31, "at_mm": [30.0, 9.9, 0.0], "cubes": 2}], "more": 0}


def _leaky() -> dict:
    s = _scouted()
    s["facts"]["leaks"] = dict(LEAKS)
    s["facts"]["inside"] = {"rule": "INS-1", "has_inside": True, "passages": 1, "mouths_per_passage": [2],
                            "leaks_closed": True}
    s["proposal"]["leaks"] = dict(LEAKS)
    return s


def _ops():
    return [route.ConfirmedOpening(id=1, name="inlet", role="inlet", diameter_mm=20.0, centroid_mm=[0.0, 0.0, 0.0]),
            route.ConfirmedOpening(id=2, name="outlet", role="outlet", diameter_mm=20.0, centroid_mm=[100.0, 0.0, 0.0])]


async def _confirm(served, monkeypatch, scout: dict, **kw):  # noqa: F811
    store = served({"scout.json": scout}, geometry_interpretation_id=uuid.uuid4(), messages=[])
    session = await route._owned_session(SID, "alice", "org-1")
    _confirming(monkeypatch, session, {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm",
                                       "scale_to_metres": 0.001, "basis": "file_declared", "evidence": "SI_UNIT"}, [])
    body = route.ConfirmIn(input_kind="body-surface", openings=_ops(), scale_to_m=0.001, unit="mm",
                           **{"flow": "internal", **kw})
    out = await route.confirm_check(SID, body, "alice", "org-1")
    record = json.loads(store.objects[_key("confirmed.json")])
    return out, record


async def test_the_proposed_leak_size_is_confirmed_by_default_and_said(served, monkeypatch):  # noqa: F811
    out, record = await _confirm(served, monkeypatch, _leaky())
    assert record["close_leaks_mm"] == pytest.approx(0.66)
    assert "Leaks: 3 found in the surface" in out["message"]
    assert "every leak up to 0.66 mm is closed for the mesh" in out["message"] and "never in the file" in out["message"]
    assert confirmed_leak_closing(str(SID)) == {"close_up_to_m": pytest.approx(0.00066), "rule": "INS-8"}


async def test_a_size_the_user_typed_or_the_switch_off_is_theirs(served, monkeypatch):  # noqa: F811
    out, record = await _confirm(served, monkeypatch, _leaky(), close_leaks_mm=1.5)
    assert record["close_leaks_mm"] == pytest.approx(1.5) and "up to 1.5 mm" in out["message"]
    assert confirmed_leak_closing(str(SID))["close_up_to_m"] == pytest.approx(0.0015)
    out, record = await _confirm(served, monkeypatch, _leaky(), close_leaks_mm=0)
    assert record["close_leaks_mm"] == 0.0 and "left open as the user chose" in out["message"]
    assert confirmed_leak_closing(str(SID)) is None


async def test_no_leaks_or_a_flow_around_the_part_carries_none(served, monkeypatch):  # noqa: F811
    out, record = await _confirm(served, monkeypatch, _scouted(), close_leaks_mm=2.0)
    assert record["close_leaks_mm"] is None and "Leaks" not in out["message"]
    assert confirmed_leak_closing(str(SID)) is None
    with pytest.raises(Exception):
        route.ConfirmIn(input_kind="body-surface", flow="internal", close_leaks_mm=-1.0)
