# Responsibility: Verify triage splits a report per defect, and that review off leaves today's routing untouched.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.triage import (
    ROUTE_CONSERVATIVE_REPAIR,
    ROUTE_MANUAL_CLEANUP,
    recommend,
    split,
)


def _entity(code, severity, kind, index, fingerprint="") -> dict:
    return {"code": code, "severity": severity, "entity": f"{kind}:{index}",
            "entity_type": kind, "entity_index": index, "message": f"a {code}",
            "region": "unknown", "measurements": {}, "location": {}, "fingerprint": fingerprint}


def _report(*entities) -> dict:
    return {"report": {"summary": "inspected",
                       "defects": [{"code": e["code"], "severity": e["severity"]} for e in entities],
                       "entities": list(entities)}}


def _mixed() -> dict:
    return _report(_entity("small_face", "warning", "face", 3, "fa"),
                   _entity("open_shell", "error", "edge", 8, "eb"))


@pytest.fixture
def review(monkeypatch):
    def _set(value: bool):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", value)
    return _set


def test_with_review_off_triage_is_unchanged(review):
    review(False)
    advice = recommend(repair_status="repairable", report=_mixed())
    assert advice.route == ROUTE_MANUAL_CLEANUP


def test_with_review_on_a_mixed_part_is_repaired_for_what_it_can_be(review):
    review(True)
    advice = recommend(repair_status="repairable", report=_mixed())
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert [t["code"] for t in advice.targets] == ["open_shell"]


def test_with_review_on_judgement_alone_still_reaches_the_loop(review):
    review(True)
    report = _report(_entity("small_face", "warning", "face", 3, "fa"))
    assert recommend(repair_status="repairable", report=report).route == ROUTE_CONSERVATIVE_REPAIR


def test_split_puts_each_defect_where_a_person_or_the_catalogue_answers_it():
    out = split(_report(_entity("small_face", "warning", "face", 3, "fa"),
                        _entity("invalid_brep", "error", "shape", 1, "fb"),
                        _entity("open_shell", "error", "edge", 8, "ec"),
                        _entity("self_intersection", "error", "face", 2, "fd")))
    assert [d.code.value for d in out.repairable] == ["open_shell"]
    assert {d.code.value for d, _ in out.judgement} == {"small_face", "invalid_brep",
                                                        "self_intersection"}
    assert all(reason for _, reason in out.judgement)
    small = next(r for d, r in out.judgement if d.code.value == "small_face")
    assert "a person decides" in small


def test_split_reads_a_bare_report_and_a_report_with_no_entities():
    assert split({}).repairable == () and split({}).judgement == ()
    bare = _report(_entity("open_shell", "error", "edge", 8, "ec"))["report"]
    assert len(split(bare).repairable) == 1
