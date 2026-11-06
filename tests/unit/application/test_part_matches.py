# Responsibility: Verify the parts checklist before approval (application/part_matches, NOTE-4): the card queues one reading per notes and parts, says "reading" until it lands, shows a row per phrase ("frame rail -> part 3 -> 3-4 mm") and a line per phrase it could not place, nothing when the notes name no part; a pick by the user is checked by the same rules and kept for the run; a reading of older notes is never used; nothing here ever raises.
# Boundaries: no model, no pictures, no worker: the store is a dict, the model a stub, the enqueue a list.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.application import geometry_check as gc
from meshpipeline.application import part_matches as PM
from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import geometry_check as GC
from meshpipeline.contracts import model_inference, object_storage

SID = "5e551011-part-matches"
TABLE = [{"id": "Part_1", "name": "Bolt", "diag_mm": 45.8}, {"id": "Part_2", "name": "", "diag_mm": 45.8},
         {"id": "Part_3", "name": "", "diag_mm": 533.0}, {"id": "Part_4", "name": "", "diag_mm": 68.4}]
NOTE = "An element size of around 3 - 4 mm for the frame rail, and 1.5-2 mm for the bolts. Refine the bracket."


class _Store:
    def __init__(self, objects: dict | None = None):
        self.objects = dict(objects or {})

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def download_file(self, *, object_key, destination):
        Path(destination).write_bytes(self.get_bytes(object_key=object_key))

    def create_download_url(self, *, object_key, expires_in, **_):
        return f"https://store/{object_key}"


class _Router:
    def __init__(self, phrases: list[dict]):
        self.phrases = phrases
        self.calls = 0

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="", **_):
        self.calls += 1
        return SimpleNamespace(failure_marker="", tool_calls=[SimpleNamespace(
            name=tools[0]["function"]["name"], arguments=json.dumps({"phrases": self.phrases, "words": ["read"]}))])


def _phrase(**kw) -> dict:
    out = {"parts": ["Part_3"], "everything_else": False, "ask": "size", "min": 3, "max": 4, "unit": "mm",
           "quote": "An element size of around 3 - 4 mm for the frame rail", "why": "the long beam", "confidence": 0.9}
    out.update(kw)
    return out


PHRASES = [_phrase(), _phrase(parts=["Part_1", "Part_2"], min=1.5, max=2, quote="1.5-2 mm for the bolts"),
           _phrase(parts=[], ask="refine", min=0, max=0, quote="Refine the bracket", confidence=0.3,
                   why="two small plates look alike")]


@pytest.fixture
def world(monkeypatch):
    from meshpipeline.cad.fea_measure import FACT_KEY
    check = {"status": "ready", "session_id": SID,
             "facts": {FACT_KEY: {PM.PARTS_KEY: {"route": "fea", "table": TABLE, "pictures": [
                 {"name": "parts_iso", "object_key": gc.check_object_key(SID, "parts/parts_iso.png")},
                 {"name": "card_a", "object_key": gc.check_object_key(SID, "parts/card_a.png")},
                 {"name": "card_a_ids", "object_key": gc.check_object_key(SID, "parts/card_a_ids.png")}],
                 "views": [{"name": "card_a", "w": 10, "h": 8, "tags": [], "hidden": []},
                           {"name": "card_b", "w": 10, "h": 8, "tags": [], "hidden": []}]}}}}
    store = _Store({gc.check_object_key(SID, "scout.json"): json.dumps(check).encode(),
                    gc.check_object_key(SID, "parts/parts_iso.png"): b"png",
                    gc.check_object_key(SID, "parts/card_a.png"): b"png",
                    gc.check_object_key(SID, "parts/card_a_ids.png"): b"png"})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    queued: list[dict] = []
    monkeypatch.setattr(GC, "_part_words_enqueuer", lambda **kw: queued.append(kw))
    router = _Router(PHRASES)
    monkeypatch.setattr(model_inference, "_router", router)
    import meshpipeline.engines.fea_ai as FA
    monkeypatch.setattr(FA, "FEA_AI", True)
    session = SimpleNamespace(id=SID, user_notes=[{"id": "n1", "text": NOTE, "source": "console"}],
                              request_txt="a bracket assembly for stress", owner_id="o1")
    return SimpleNamespace(store=store, queued=queued, router=router, session=session)


def _run_queued(w) -> None:
    kw = w.queued.pop()
    PM.run_reading(**kw)


def test_the_card_queues_one_reading_and_says_so_until_it_lands(world):
    assert PM.view(world.session) == {"status": "reading"}
    assert len(world.queued) == 1 and world.queued[0]["session_id"] == SID
    assert PM.view(world.session) == {"status": "reading"}          # not queued twice
    assert len(world.queued) == 1
    _run_queued(world)
    got = PM.view(world.session)
    assert got["status"] == "done"
    rows = {r["words"]: (r["parts_text"], r["size"]) for r in got["rows"]}
    assert rows["1.5-2 mm for the bolts"] == ("parts 1 and 2", "1.5–2 mm")
    assert ("part 3", "3–4 mm") in rows.values()
    assert [u["text"] for u in got["unsure"]] == ["Couldn't tell which part is “Refine the bracket”"]
    assert [p["n"] for p in got["parts"]] == ["1", "2", "3", "4"] and got["parts"][0]["colour"].startswith("#")
    assert got["views"] == [{"name": "card_a", "w": 10, "h": 8, "tags": [], "hidden": []}]   # nothing hidden: one side
    assert PM.card_picture(SID, "card_a") == b"png" and PM.card_picture(SID, "../scout") is None


def test_changed_notes_read_again_and_an_old_reading_is_never_used(world):
    PM.view(world.session)
    first = world.queued[-1]
    world.session.user_notes = [{"id": "n1", "text": NOTE + " 2 mm on part 4.", "source": "console"}]
    assert PM.view(world.session) == {"status": "reading"}
    second = world.queued[-1]
    assert second["key"] != first["key"]
    assert PM.run_reading(**first)["status"] == "superseded"      # the older notes' reading lands late: dropped
    assert PM.for_run(SID, world.session.user_notes, world.session.request_txt) is None
    PM.run_reading(**second)
    assert PM.for_run(SID, world.session.user_notes, world.session.request_txt)["status"] == "used"


def test_a_pick_fixes_a_row_checked_by_the_rules_and_the_run_takes_it(world):
    PM.view(world.session)
    _run_queued(world)
    got = PM.pick(world.session, 2, ["Part_4", "Part_99"])           # an unknown id is dropped
    refine = [r for r in got["rows"] if r["words"] == "Refine the bracket"]
    assert refine and refine[0]["parts"] == ["Part_4"] and refine[0]["size"] == "finer" and refine[0]["picked"]
    assert not got["unsure"]
    got = PM.pick(world.session, 0, ["Part_4"])                      # 3-4 mm moved from the beam to part 4
    assert any(r["parts"] == ["Part_4"] and r["size"] == "3–4 mm" for r in got["rows"])
    run = PM.for_run(SID, world.session.user_notes, world.session.request_txt)
    sized = PW.per_part(run["entries"])
    assert sized["Part_4"]["max_m"] == pytest.approx(0.004) and "Part_3" not in sized


def test_no_part_picked_puts_the_phrase_back_to_which_parts(world):
    PM.view(world.session)
    _run_queued(world)
    got = PM.pick(world.session, 1, [])
    assert "1.5-2 mm for the bolts" in [u["words"] for u in got["unsure"]]


def test_notes_naming_no_part_show_nothing(world):
    world.router.phrases = []
    PM.view(world.session)
    _run_queued(world)
    assert PM.view(world.session) is None


def test_no_notes_or_no_parts_show_nothing_and_ask_nothing(world, monkeypatch):
    world.session.user_notes, world.session.request_txt = [], ""
    assert PM.view(world.session) is None
    world.session.user_notes = [{"id": "n1", "text": NOTE, "source": "console"}]
    world.store.objects[gc.check_object_key(SID, "scout.json")] = json.dumps({"status": "ready", "facts": {}}).encode()
    assert PM.view(world.session) is None
    world.store.objects[gc.check_object_key(SID, "scout.json")] = json.dumps({"status": "scouted"}).encode()
    assert PM.view(world.session) == {"status": "waiting"}
    assert not world.queued


def test_the_model_down_shows_nothing_and_the_run_reads_again(world, monkeypatch):
    class _Down:
        async def call_reviewer_with_tools(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(model_inference, "_router", _Down())
    PM.view(world.session)
    _run_queued(world)
    assert PM.view(world.session) is None
    assert PM.for_run(SID, world.session.user_notes, world.session.request_txt) is None


def test_nothing_raises_when_the_store_is_gone(monkeypatch):
    def _gone():
        raise RuntimeError("no store")
    monkeypatch.setattr(object_storage, "get_object_store", _gone)
    s = SimpleNamespace(id=SID, user_notes=[{"id": "n1", "text": NOTE, "source": "console"}], request_txt="x")
    assert PM.view(s) is None
    assert PM.for_run(SID, s.user_notes, s.request_txt) is None


def test_the_run_uses_the_stored_reading_on_its_own_parts_only():
    stored = PW.section("used", entries=[{"phrase": 0, "parts": ["Part_3", "Part_9"], "ask": "size", "min_mm": 3,
                                          "max_mm": 4, "quote": "q"}], table=TABLE, key="k")
    got = PW.from_stored(stored, TABLE)
    assert got["entries"][0]["parts"] == ["Part_3"] and got["source"] == "checked before approval"
    assert any("Part_9" in d for d in got["dropped"])
    assert PW.from_stored({"status": "failed"}, TABLE) is None and PW.from_stored(None, TABLE) is None


def test_the_driver_takes_the_stored_reading_without_asking(monkeypatch):
    import asyncio

    class _Never:
        async def call_reviewer_with_tools(self, *a, **k):
            raise AssertionError("asked again")
    monkeypatch.setattr(model_inference, "_router", _Never())
    monkeypatch.setattr(PW, "part_table", lambda record, surface: TABLE)
    stored = PW.section("used", entries=[{"phrase": 0, "parts": ["Part_3"], "ask": "size", "min_mm": 3,
                                          "max_mm": 4, "quote": "q"}], table=TABLE, key="k")
    got = asyncio.run(PW.decide(Path("."), {}, None, {"user_notes": [{"id": "n", "text": "q", "source": "c"}],
                                                       "part_matches": stored}, consumer="fea_part_sizes"))
    assert got["status"] == "used" and PW.per_part(got["entries"])["Part_3"]["min_m"] == pytest.approx(0.003)


def test_only_names_that_read_as_names_are_shown():
    assert [PM.readable(n) for n in ("p5 (3)", "Body1", "Solid_12", "Bolt M8", "Rail", "PART-4", "x")] == [
        "", "", "", "Bolt M8", "Rail", "", ""]


def test_parts_words_are_short():
    assert PM.parts_words(["Part_9"]) == "part 9"
    assert PM.parts_words(["Part_1", "Part_2", "Part_3", "Part_4", "Part_7"]) == "parts 1–4 and 7"
    assert PM.parts_words(["Part_2", "Part_5"]) == "parts 2 and 5"


def test_the_flow_route_reads_with_the_snappy_consumer():
    assert PM._route_ask(PM.ROUTE_CFD)[0] == "snappy_part_levels"
