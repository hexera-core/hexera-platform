# Responsibility: Verify the closing message's asked-against-built remark is short and whole: the count line names
#       only how many asks had each outcome, and every ask follows it with where it came from, its outcome and what
#       was built - kept whole in the workspace, never cut mid-sentence at the remark limit (live, 10 asks: the line was
#       cut at "an edge longer tha").
from __future__ import annotations

from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines.meshing_standard import remark as led

WHY = ("the quality pass tried the 137 elements past it and could not make them better without moving the surface "
       "mesh, which stays as meshed")
ROWS = [
    {"id": "r1", "field": "global", "asked": "global element size 3 mm", "from": "settings", "met": True,
     "delivered": "elements are about 3 mm"},
    {"id": "r2", "field": "limit:jacobian", "asked": "Jacobian ratio over 0.3", "from": "settings", "met": True},
    {"id": "r3", "field": "limit:skewness", "asked": "skewness under 0.9", "from": "notes", "met": True},
    {"id": "r4", "field": "limit:aspect_ratio", "asked": "aspect ratio under 5", "from": "notes", "met": False,
     "delivered": f"0.0163% of elements (137) are over 5; the worst is 21.75; {WHY}"},
    {"id": "r5", "field": "limit:min_angle", "asked": "smallest angle over 20°", "from": "notes", "met": False,
     "delivered": "0.4% of elements (3,310) have an angle under 20°; the smallest is 6.1°"},
    {"id": "r6", "field": "min_edge", "asked": "shortest element edge over 1 mm", "from": "settings", "met": None,
     "target": True, "outside": 120, "outside_pct": 0.014,
     "delivered": "a target - 0.014% of elements (120) have an edge shorter than 1 mm (the shortest is 0.61 mm)"},
    {"id": "r7", "field": "max_edge", "asked": "longest element edge under 4 mm", "from": "settings", "met": None,
     "target": True, "outside": 1610, "outside_pct": 0.19,
     "delivered": "a target - 0.19% of elements (1,610) have an edge longer than 4 mm (the longest is 6.2 mm)"},
    {"id": "r8", "field": "local", "asked": "1 mm on the bore", "from": "notes", "met": None, "nearest": True,
     "applied": True, "delivered": "set on the whole part, the nearest there is"},
    {"id": "r9", "field": "order", "asked": "second-order elements", "from": "settings", "met": None,
     "applied": True, "delivered": "every element is second order"},
    {"id": "r10", "field": "limit:warpage", "asked": "warpage under 10°", "from": "notes", "met": None,
     "applied": False, "delivered": "not applied - warpage is a measure of four-sided elements"},
]
REP = {"rows": ROWS}
SEC = {"standard": {"from": {"global": "settings", "skewness": "notes"}}}


def test_the_count_line_is_counts_only_and_short():
    said = MR.summary(REP, kind="solid")
    assert said == ("Of the 10 things you asked for: 3 met, 2 not fully met; 2 edge-length targets; 1 applied as a "
                    "setting; 1 nearest there is; 1 could not be applied to a solid mesh.")
    # short enough that the result shows it whole as its paragraph's lead (ui/js/render/outcome.js `lead`: 230)
    assert "aspect ratio" not in said and len(led(SEC, said)) <= 230


def test_every_ask_follows_with_its_source_outcome_and_what_was_built():
    whole = MR.closing(led(SEC, MR.summary(REP, kind="solid")), REP)
    assert whole.startswith("From your settings and notes: Of the 10 things you asked for: 3 met,")
    for r in ROWS:
        assert MR.line(r).split(":")[0] in whole, r["asked"]
    assert f"Aspect ratio under 5 (from your notes): not fully met - 0.0163% of elements (137) are over 5; the worst " \
           f"is 21.75; {WHY}." in whole
    assert ("Longest element edge under 4 mm (from your settings): target, not met - 0.19% of elements (1,610) have "
            "an edge longer than 4 mm (the longest is 6.2 mm).") in whole
    assert "1 mm on the bore (from your notes): nearest there is - set on the whole part" in whole
    assert "Second-order elements (from your settings): applied (a setting) - every element is second order." in whole
    assert "Warpage under 10° (from your notes): not applied - warpage is a measure" in whole
    assert whole.endswith("four-sided elements.")


def test_the_remark_is_kept_whole_past_the_remark_limit(tmp_path):
    whole = MR.closing(led(SEC, MR.summary(REP, kind="solid")), REP)
    assert len(whole) > UN.MAX_REMARK_CHARS
    UN.record_remark(tmp_path, whole, by="mesh_requirements", whole=True)
    assert UN.read_remarks(tmp_path) == [whole]
    # any other remark is cut at the limit as before
    UN.record_remark(tmp_path, "x " * 400, by="builder")
    assert len(UN.read_remarks(tmp_path)[-1]) == UN.MAX_REMARK_CHARS


def test_the_closing_message_carries_every_ask_whole(tmp_path):
    # the result page shows this paragraph's lead (the count line) and the whole of it under Details
    from meshpipeline.application import final_result as fr
    whole = MR.closing(led(SEC, MR.summary(REP, kind="solid")), REP)
    UN.record_remark(tmp_path, whole, by="mesh_requirements", whole=True)
    base = fr.build_final_result(
        job_id="j", owner_id="u", status=fr.TerminalStatus.succeeded, engine="gmsh",
        purpose="fea_solid", dimensionality="3D", approved_snapshot_id="", executor_success=True,
        reviewer_verdict="PASS", failed_gate="", api_failure="", attempts=1, attempts_max=3,
        required_ready=True, delivered_types=["mesh"], optional_warnings=[])
    msg = fr.render_message(fr.with_notes_remarks(base, UN.read_remarks(tmp_path)))
    assert whole in msg and msg.count("Of the 10 things you asked for") == 1


def test_nothing_asked_says_nothing():
    assert MR.closing("", REP) == "" and MR.asks_said(None) == "" and MR.closing("a line.", {"rows": []}) == "a line."
