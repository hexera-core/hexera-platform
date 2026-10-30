# Responsibility: Verify the bounded region read answers as the in-process one does and reports
# rather than raises when it cannot.
# Boundaries: cad/regions.facts_for_file_bounded and its child entry; no API and no uvicorn here.
"""THE ADMISSION PREVIEW WAS READING THE CUSTOMER'S CAD IN THE API WORKER.

`executor._geometry_facts` reads the uploaded file's regions so that a refusal can say what the
FILE is missing rather than only what the engine cannot do. It did that with the XDE reader, in
process, which is OpenCASCADE, and OCP does not release the GIL. uvicorn's multi-worker supervisor
pings each worker every 0.5 s with a 5 s deadline and SIGKILLs one that cannot answer.

MEASURED on the first customer assembly, 2026-10-30 (ColdPlateAssembly_CLEAN.step, 15,883,539
bytes, 6 solids, 9,226 faces), on the turn that submits the requirements:

    engine confirmed                      00:40:49
    preview_selected_admission answered   00:41:34   <- 45 s of GIL, nine ping deadlines

The caller already treats an absent answer as normal, in its own words: "Never fatal and never
blocking: no file, or a file this cannot describe, simply says nothing and the verdict is what it
always was." That is what makes moving the read into a child safe rather than a behaviour change.
"""
from __future__ import annotations

import json

import pytest

try:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.regions import (
    REGIONS_CHILD_BUDGET_S,
    facts_for_file_bounded,
    regions_for_file,
)


def _step(path):
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 20.0, 30.0).Shape()
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def test_the_child_reads_the_same_regions_as_the_caller_would(tmp_path):
    """Moving the read out of the caller's interpreter must not change the verdict it feeds."""
    p = _step(tmp_path / "box.step")
    here = regions_for_file(p).as_facts()
    there = facts_for_file_bounded(p)
    assert there == here, (there, here)


def test_no_file_still_answers_with_the_zero_facts_and_spawns_nothing(tmp_path):
    """The cheap case must not pay for a process AND must not change the answer.

    I wrote this test asserting `== {}` first, which is what my own shortcut returned, and the
    existing suite caught it: `test_a_session_with_no_upload_supplies_nothing_and_raises_nothing`
    reads `region_count` and expects 0. An empty dict says "nothing was read"; the zero facts say
    "I looked and it names nothing". Those are different statements and only the second is true.
    """
    zero = regions_for_file("").as_facts()
    assert zero["region_count"] == 0
    assert facts_for_file_bounded("") == zero
    assert facts_for_file_bounded(None) == zero


def test_running_out_of_time_says_nothing_and_does_not_raise(tmp_path):
    """The caller's comment allows an empty answer; it does not allow an exception."""
    p = _step(tmp_path / "box.step")
    assert facts_for_file_bounded(p, budget_s=0.001) == {}


def test_a_file_that_is_not_cad_names_no_regions(tmp_path):
    """A file it cannot describe is not an error: `regions_for_file` answers that it found no
    region NAMES while still saying what format the name claimed. The child has to carry that
    answer through rather than turn it into an exception the caller must handle, and rather than
    flatten it to {}, which would lose the difference between "read it, found nothing" and
    "never read it"."""
    junk = tmp_path / "notcad.step"
    junk.write_text("this is not a STEP file at all")
    got = facts_for_file_bounded(junk)
    assert got == regions_for_file(junk).as_facts()
    assert got["region_count"] == 0 and got["region_names"] == []


def test_the_child_entry_writes_an_answer_a_caller_can_read(tmp_path):
    """The child is `python -m meshpipeline.cad.regions <task> <answer>`; this is that contract."""
    from meshpipeline.cad import regions as R

    p = _step(tmp_path / "box.step")
    spec, out = tmp_path / "task.json", tmp_path / "answer.json"
    spec.write_text(json.dumps({"path": str(p)}))
    assert R.main([str(spec), str(out)]) == 0
    answer = json.loads(out.read_text())
    assert "error" not in answer, answer
    assert "region_count" in answer["facts"], answer


def test_the_api_path_uses_the_child(monkeypatch, tmp_path):
    """`_geometry_facts` is the path that runs inside the API, so it must go through the bounded
    read and not the in-process one."""
    from meshpipeline.agents.intake import executor as EX

    seen = {}

    def _spy(path, **kw):
        seen["bounded"] = str(path)
        return {"region_count": 1, "region_names": ["box"], "region_source": "test"}

    monkeypatch.setattr("meshpipeline.cad.regions.facts_for_file_bounded", _spy)
    p = _step(tmp_path / "box.step")

    ex = EX.IntakeToolExecutor.__new__(EX.IntakeToolExecutor)
    monkeypatch.setattr(type(ex), "_staged_upload", lambda _self: str(p), raising=False)
    got = EX.IntakeToolExecutor._geometry_facts(ex)

    assert seen.get("bounded"), "the admission preview read the CAD in the API's own interpreter"
    assert got["region_count"] == 1


def test_the_budget_is_generous_because_nothing_waits_on_it():
    """These facts only improve the WORDS of a refusal; the verdict is the same without them, so
    the child is not racing anybody."""
    assert REGIONS_CHILD_BUDGET_S >= 60.0
