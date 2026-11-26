# Responsibility: Verify RUN-11 - an FEA solid run's steps are logged once each on the stage file's own clock, summed per step (a comparison mesh and a refill held as steps of their own), carried into the result block and the report's "Where the time went" table, and said in one closing line only when one step took most of the run.
# Boundaries: plain files in a temp workspace and the pure report renderer; no mesher, no model.
from __future__ import annotations

import json

from meshpipeline.engines import fea_times as T
from meshpipeline.engines.gmsh import fea_adapter as A
from meshpipeline.render.mesh_report import mesh_report_html


def _rows(*steps):
    return [{"key": k, "at": float(at), **extra} for k, at, extra in steps]


def test_each_step_is_logged_once_on_the_stage_files_clock(tmp_path):
    T.start(tmp_path)
    A.note_stage(tmp_path, "volume", "filling the solid with elements")
    A.note_stage(tmp_path, "volume", "filling the solid with elements")        # the ticker's re-note
    A.note_stage(tmp_path, "optimising", "improving the elements")
    rows = T.read(tmp_path)
    assert [r["key"] for r in rows] == ["measuring", "volume", "optimising"]
    stage = json.loads((tmp_path / A.STAGE_FILE).read_text())
    assert rows[-1]["at"] == stage["at"]                                        # the same clock, not a second one


def test_a_new_run_starts_the_log_again(tmp_path):
    T.start(tmp_path)
    A.note_stage(tmp_path, "volume", "filling")
    T.start(tmp_path)
    assert [r["key"] for r in T.read(tmp_path)] == ["measuring"]


def test_steps_are_summed_and_a_comparison_mesh_is_a_step_of_its_own():
    rows = _rows(("measuring", 0, {}), ("planning", 10, {}), ("pass", 30, {}), ("sizing", 31, {}),
                 ("surface", 32, {}), ("volume", 60, {}), ("detail", 61, {"fill": {"algorithm": "hxt", "threads": 16}}),
                 ("optimising", 200, {}), ("quality_pass", 260, {}), ("curving", 1700, {}), ("writing", 1720, {}),
                 ("comparison", 1760, {}), ("sizing", 1761, {}), ("volume", 1800, {}), ("delivering", 1900, {}))
    s = T.summary(rows, end_at=1960)
    got = {r["key"]: r["seconds"] for r in s["stages"]}
    assert got == {"measuring": 10, "planning": 20, "sizing": 1, "surface": 28, "volume": 140, "optimising": 60,
                   "quality_pass": 1440, "curving": 20, "writing": 40, "comparison": 140, "delivering": 60}
    assert s["total_s"] == 1960 and s["passes"] == 1 and s["fill"] == {"algorithm": "hxt", "threads": 16}
    assert T.most_words(s) == "Most of the time, 24 of 33 min, went to improving the elements (the quality pass)."


def test_no_closing_line_when_no_step_took_most_of_the_run():
    s = T.summary(_rows(("measuring", 0, {}), ("volume", 50, {}), ("writing", 100, {})), end_at=150)
    assert T.most_words(s) == ""
    assert T.summary([], end_at=10) is None and T.most_words(None) == ""


def test_the_report_says_where_the_time_went_in_plain_words():
    s = T.summary(_rows(("measuring", 0, {}), ("volume", 10, {}),
                        ("detail", 11, {"fill": {"algorithm": "hxt", "threads": 8}}),
                        ("optimising", 70, {}), ("quality_pass", 90, {}),
                        ("detail", 91, {"improving": [{"stage": "quality_pass", "name": "the quality pass",
                                                       "changed": "113 past your limit before, 111 after"}]})),
                  end_at=400)
    rows = T.table_rows(s)
    assert rows[1] == ["Filling the solid with elements (the fast fill, on 8 threads)", "1 min 00 s"]
    assert rows[3] == ["Improving the elements (the quality pass): 113 past your limit before, 111 after",
                       "5 min 10 s"]
    assert rows[-1] == ["The whole run", "6 min 40 s"]
    html = mesh_report_html({"surface": {"fea": {"summary": "x", "times": {**s, "rows": rows}}}})
    assert "Where the time went" in html and "the fast fill, on 8 threads" in html
    # no mesher's name reaches the page
    assert "hxt" not in html.lower() and "mmg" not in html.lower()


def test_a_report_without_times_has_no_such_section():
    assert "Where the time went" not in mesh_report_html({"surface": {"fea": {"summary": "x"}}})
