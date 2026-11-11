# Responsibility: Verify the FEA stage for a file that is a surface (SURF-1, SURF-4, SURF-6): it is no longer stopped
# as "not a closed solid"; it proceeds as a shell with ONE line that says what it is and its thickness; the thickness
# is the user's words, or none - then said with how to set it, never asked and never a stop; a surface's own open
# edges are not said as a fault; a closed skin with a gap is still stopped, with both ways on; a solid, faces that
# close and a triangle file see the stage they saw before.
# Boundaries: pure over stored facts and messages; an in-memory object store for the settled thickness; the reading is
# measured on small shapes drawn with OpenCASCADE.
from __future__ import annotations

import json

import pytest

from meshpipeline.application import fea_check, shell_check, surface_check
from meshpipeline.cad.fea_measure import FACT_KEY
from meshpipeline.contracts import object_storage

SID = "5e55-5u2f"
OPEN_SHELL = {"clean": False, "kind": "brep", "summary": "The geometry is not clean: the surface is not closed: 1 free "
              "boundary(ies) that no sewing within 0.02 mm closes", "reasons": [
                  {"code": "open_shell", "severity": "error", "message": "the surface is not closed: 1 free boundary(ies)"},
                  {"code": "flat", "severity": "error", "message": "the geometry has no thickness along z: a sheet"}]}


def _facts(reading: dict | None, *, validity: dict | None = None, said: str = "") -> dict:
    record: dict = {"measured": True, "bodies": 1, "thickness": {"min_mm": 12.0, "face": "Face_2"},
                    "hot_spots": [{"id": "H1", "type": "hole"}], "counts": {"hole": 1}}
    if reading is not None:
        record[surface_check.KEY] = reading
    facts = {"source_format": "step", "solids": 0, "scale_to_m": 0.001, "validity": validity or OPEN_SHELL,
             FACT_KEY: record}
    if said:
        facts["mesh_as_words"] = {"value": said, "by": "table", "why": ""}
    return facts


SHEET = {"kind": "surface", "why": "it holds no solid and its faces do not close: the open edges span 74% of the "
         "surface", "faces": 11, "sheets": 1, "open_share": 0.74, "sew_tolerance_mm": 0.019, "free_edges": 14,
         "unmet_edges": 0, "size_mm": 149.0}
GAP = {**SHEET, "kind": "gap", "open_share": 0.002, "why": "its faces nearly close"}


def _user(*words):
    return [{"role": "user", "content": w} for w in words]


def test_a_surface_file_is_not_stopped_and_says_its_one_line_with_the_thickness():
    facts = _facts(SHEET)
    msgs = _user("stress analysis of this bracket, 1.5 mm thick steel")
    assert fea_check.blocker(facts, msgs) == ("", "", "")
    view = fea_check.fea_view(facts, msgs)
    assert view["can_proceed"] is True and view["single_part_only"] == ""
    fold = surface_check.view(facts, msgs)
    assert fold["label"] == "This file is a surface, not a solid. It is meshed as a shell, 1.5 mm thick."
    assert fold["reason"] == "" and fold["thickness_mm"] == 1.5 and fold["mesh_as"] == "shell"
    # drawn by the stage's own shell fold, with nothing of its own: no pick, no measuring, nothing "as you asked"
    assert fold["state"] == "ready" and fold["said_words"] == "" and "options" not in fold


def test_no_thickness_is_no_stop_and_no_question_only_a_line_on_how_to_set_it():
    facts = _facts(SHEET)
    msgs = _user("FEA, static")
    assert fea_check.blocker(facts, msgs) == ("", "", "")
    fold = surface_check.view(facts, msgs)
    assert fold["label"] == "This file is a surface, not a solid. It is meshed as a shell."
    assert fold["reason"] == ('No thickness given yet. Add it to your notes (for example "1.2 mm thick"), or set it '
                              "later in the solver file.")
    assert fold["thickness_mm"] is None
    settled = shell_check.settle(fold)
    assert settled == {"mesh_as": "shell", "picked": "shell", "by": "surface file", "why": fold["label"]}
    said = shell_check.declaration_words(settled)
    assert said.startswith("Meshed as: a shell - plate elements on the surface as given")
    assert "each with its wall thickness" not in said
    assert shell_check.card_mesh_as(said) == "Shell (mid-surface, plate elements)"


def test_the_latest_thickness_said_is_the_one_shown():
    facts = _facts(SHEET)
    fold = surface_check.view(facts, _user("FEA, 1 mm thick", "sorry, thickness 1.2 mm"))
    assert fold["thickness_mm"] == 1.2 and fold["thickness_quote"] == "thickness 1.2 mm"
    # the assistant's own words are never read as the user's
    msgs = [{"role": "user", "content": "FEA"}, {"role": "assistant", "content": "is it 2 mm thick?"}]
    assert surface_check.view(facts, msgs)["thickness_mm"] is None


def test_a_surfaces_open_edges_are_not_said_as_a_fault_and_no_solid_measurement_is_shown():
    view = fea_check.fea_view(_facts(SHEET), _user("FEA"))
    assert view["clean"] is True and view["clean_short"] == "A surface, not a solid" and view["clean_details"] == []
    assert view["bodies_sewn"] == 1
    # no "thinnest wall" and no hot spots to refine are said of a sheet
    assert view["measure"] is None
    assert "assembly" not in view
    # anything else the clean check found is still said, and so are edges that do not meet
    other = {**OPEN_SHELL, "reasons": [*OPEN_SHELL["reasons"],
                                        {"code": "degenerate_faces", "severity": "error", "message": "2 face(s) have zero area"}]}
    view = fea_check.fea_view(_facts({**SHEET, "unmet_edges": 3}, validity=other), _user("FEA"))
    assert view["clean"] is False and view["clean_short"] == "A surface · 3 edges do not meet"
    assert view["clean_details"][0].startswith("3 edges lie along a neighbouring face without meeting it")
    assert view["clean_details"][1] == "2 face(s) have zero area"
    one = surface_check.clean_view(_facts({**SHEET, "unmet_edges": 1}))
    assert one["clean_short"] == "A surface · 1 edge does not meet"
    assert one["clean_details"][0].startswith("1 edge lies along")


def test_a_closed_skin_with_a_gap_is_still_stopped_with_both_ways_on():
    facts = _facts(GAP)
    blocked, kind, way_out = fea_check.blocker(facts, _user("FEA"))
    assert kind == "gap" and blocked == surface_check.GAP_WORDS
    assert "close the gaps in your CAD" in way_out and 'say so (for example "shell, 1.2 mm thick")' in way_out
    assert surface_check.view(facts, _user("FEA")) is None
    assert fea_check.fea_view(facts, _user("FEA"))["can_proceed"] is False
    # ... and the user's words settle it: a shell said, or a thickness given
    for words in ("mesh it as a shell", "it is a sheet, 0.8 mm thick"):
        assert fea_check.blocker(facts, _user(words)) == ("", "", "")
        assert surface_check.view(facts, _user(words))["mesh_as"] == "shell"
    assert fea_check.blocker(_facts(GAP, said="shell"), None) == ("", "", "")


def test_any_other_file_sees_the_stage_it_saw_before():
    # no reading (a measurement from before this, a triangle file): the old stop and words, unchanged
    facts = _facts(None)
    blocked, kind, way_out = fea_check.blocker(facts, _user("FEA, 1 mm thick"))
    assert kind == "surface" and blocked.startswith("This is a surface, not a closed solid") and way_out == fea_check.WAY_OUT_SOLID
    assert surface_check.view(facts, _user("shell")) is None
    # faces that close: read as the solid they bound, as before (MESH-1)
    closed = {**_facts({**SHEET, "kind": "closed"}), "validity": {"clean": True, "kind": "brep", "reasons": []}}
    assert fea_check.blocker(closed, None) == ("", "", "") and surface_check.view(closed, _user("FEA")) is None
    assert fea_check.fea_view(closed)["measure"] is not None
    # a solid: nothing of this is consulted
    solid = {"source_format": "step", "solids": 1, "validity": {"clean": True, "kind": "brep"},
             FACT_KEY: {"measured": True, "bodies": 1}}
    assert not surface_check.is_surface(solid, _user("shell, 2 mm thick")) and not surface_check.is_gap(solid)
    assert fea_check.fea_view(solid, _user("FEA")) == fea_check.fea_view(solid)


def test_the_reading_is_measured_with_the_part_and_kept_small():
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import surface_fixtures as sx

    got = surface_check.measure(sx.bracket(), mm_per_unit=1.0)
    assert got["kind"] == "surface" and got["sheets"] == 1 and got["faces"] == 3 and got["unmet_edges"] == 0
    assert "loops" not in json.dumps(got) and got["size_mm"] > 0
    assert surface_check.measure(sx.skin_with_pinhole(), mm_per_unit=1.0)["kind"] == "gap"
    assert surface_check.measure(sx.closed_skin(), mm_per_unit=1.0)["kind"] == "closed"
    assert surface_check.measure(sx.box_solid(), mm_per_unit=1.0) is None
    assert surface_check.measure(object(), mm_per_unit=1.0) is None        # never raises


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]


def test_the_settled_thickness_is_read_into_the_run(monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    key = f"sessions/{SID}/geometry_check/confirmed.json"
    assert surface_check.confirmed_thickness(SID) is None and surface_check.confirmed_thickness("") is None
    store.objects[key] = json.dumps({"fea_mesh_as": "shell", "fea_shell_thickness_mm": 1.2}).encode()
    assert surface_check.confirmed_thickness(SID) == 1.2
    for none in (None, 0, -1, 5e6, "x"):
        store.objects[key] = json.dumps({"fea_shell_thickness_mm": none}).encode()
        assert surface_check.confirmed_thickness(SID) is None
