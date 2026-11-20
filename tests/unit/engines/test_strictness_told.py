# Responsibility: Verify NOTE-14's telling (m484x round 12): a shell's plan says its limits are flexible as a solid's does (A3), and a note on how strict the limits are that names no limit gets one fixed plain line - never a model's sentence, never "not applied", never a count (A6).
"""m484x A3 and A6, round 12."""
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline.contracts.user_notes import read_remarks, result_lines
from meshpipeline.engines import mesh_requirements as MR

NO_LIMIT_NOTE = "These limits are flexible."


def _answer(**fields) -> dict:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": [], "strictness": []}
    base.update(fields)
    return base


def _no_limit_section() -> dict:
    answer = _answer(strictness=[{"target": "all", "strictness": "flexible", "quote": NO_LIMIT_NOTE}])
    req, dropped, na = MR.check(json.dumps(answer), NO_LIMIT_NOTE)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model")


def test_a_note_that_names_no_limit_gets_one_fixed_line_never_a_count():
    sec = _no_limit_section()
    assert MR.strictness_unnamed(sec)
    assert MR.words(sec) == [MR.NO_LIMIT_WORDS]
    assert MR.not_applied_words(sec) == [] and not sec["dropped"]
    assert "could not be applied" not in MR.summary(None, not_applied=0)


def test_no_model_remark_is_kept_beside_a_strictness(tmp_path):
    """A6: the FEA AI's "Treated the flexible note as guidance; all geometry remains present ..." reached the result.
    A strictness is the rules': the reading is kept, so no AI step's remark speaks for it."""
    sec = _no_limit_section()
    assert not MR.ai_remark_kept_for(sec) and not MR.ai_remark_kept_for(sec, skip=MR.COUNT_FIELDS)
    assert MR.write(tmp_path, sec) and not MR.ai_remark_kept(tmp_path)
    assert any("how strict the limits are" in a for a in MR.decided_asks(sec))


def test_the_fea_reading_records_the_fixed_line_once(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import meshing_standard as MS

    async def _decide(*_a, **_k):
        return _no_limit_section()

    monkeypatch.setattr(MS, "decide", _decide)
    asyncio.run(FD.read_requirements(tmp_path, {}, job_id="j", say=False))
    asyncio.run(FD.read_requirements(tmp_path, {}, job_id="j", say=False))      # read again: still one line
    remarks = read_remarks(tmp_path)
    assert remarks == [MR.NO_LIMIT_WORDS]
    assert result_lines(remarks) == ["From your notes: your note set no limit, so nothing in the mesh changed because "
                                     "of it."]


def test_a_note_that_names_its_limits_says_how_strict_they_are_and_no_fixed_line():
    notes = "Aspect ratio below 4. These are guidelines."
    answer = _answer(limits=[{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 4,
                              "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "Aspect ratio below 4"}],
                     strictness=[{"target": "all", "strictness": "flexible", "quote": "These are guidelines."}])
    req, dropped, na = MR.check(json.dumps(answer), notes)
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model")
    assert not MR.strictness_unnamed(sec)
    said = MR.strictness_words(req)
    assert len(said) == 1 and "flexible - a target" in said[0] and MR.NO_LIMIT_WORDS not in said


def test_a_shell_plan_says_its_limits_are_flexible_as_a_solid_plan_does(tmp_path):
    """A3: Val4 as a shell, three limits the notes call guidelines - read flexible and held so, but no line before
    or with the mesh said it (the solid's plan does: "... - flexible - a target: ...")."""
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf
    from tests.unit.engines import test_shell_requirements as TSR

    from meshpipeline.engines import shell_delivery as SD
    from meshpipeline.engines import shell_run

    notes = "Aspect ratio below 4. These are guidelines."
    answer = _answer(limits=[{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 4,
                              "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "Aspect ratio below 4"}],
                     strictness=[{"target": "all", "strictness": "flexible", "quote": "These are guidelines."}])
    req, dropped, na = MR.check(json.dumps(answer), notes)
    ws = TSR._stage(sf.plate_with_hole(), tmp_path / "plate")
    MR.write(ws, MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model"))
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged"]) == 0
    assert SD.deliver(ws, shell_run.read(ws))
    words = json.loads((ws / "fea_plan.json").read_text())["words"]
    assert any("aspect ratio under 4 - flexible - a target" in w for w in words), words
