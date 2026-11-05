# Responsibility: Verify the Mesh report holds everything the short result screen leaves out - every limit, the cusp and micro-detail coordinates, the plan in words, the full hot-spot list, the per-solver checks, every joined pair with an open one in red, the faces accounted for and the units - and that it is one self-contained page that prints cleanly.
# Boundaries: the FEA block is the server's own (engines/fea_results.assembly_block and fea_solvers.judge) over records shaped as the adapters write them; the report is rendered from it as the bundle and the route render it. No workspace, no browser.
from __future__ import annotations

import re
from html import unescape

from meshpipeline.render.mesh_report import REPORT_FILE, mesh_report_html


def _fea_payload(*, spots: int = 348, open_pair: bool = True) -> dict:
    from meshpipeline.engines.fea_results import assembly_block
    from meshpipeline.engines.fea_solvers import judge

    kinds = [("sharp inside corner", 318), ("fillet", 26), ("hole", 3), ("thin wall", 1)]
    hot = []
    for shape, n in kinds:
        for _ in range(n):
            if len(hot) >= spots:
                break
            k = len(hot) + 1
            hot.append({"id": f"H{k}", "type": shape, "shape": shape, "faces": [f"Face_{k % 40 + 1}"],
                        "at_m": [0.001 * k, 0.002, 0.003], "capped": False, "words": f"{shape}, radius 1 mm",
                        "radius_mm": 1.0})
    pairs = [{"key": "Part_1|Part_2", "parts": ["Part_1", "Part_2"], "kind": "touching", "type": "bonded",
              "default": "bonded", "source": "rule", "why": "they touch: Bonded, the answer for the whole assembly"},
             {"key": "Part_1|Part_3", "parts": ["Part_1", "Part_3"], "kind": "gap", "type": "bonded",
              "default": "bonded", "source": "user", "why": "set to Bonded on the stage, for this pair"}]
    opened = [{"name": "Tie2", "pair": "Interface_2", "parts": ["Part_1", "Part_3"], "reach_m": 1e-6,
               "reach_from": "set", "needed_m": 2.1e-4, "faces": [{"face": "Part_3_Face_4", "nearest_m": 2.0e-4}],
               "tied_share": 0.0}] if open_pair else []
    asm = assembly_block({"connection": "bonded", "mode": "tie",
                          "parts": [{"id": "Part_1", "name": "Base", "elements": 12000},
                                    {"id": "Part_2", "name": "Bracket", "elements": 8000},
                                    {"id": "Part_3", "name": "Boss", "elements": 3000}],
                          "joints": [{"name": "Tie1", "kind": "tie", "slave": "Tie1_Part_2", "master": "Tie1_Part_1",
                                      "parts": [1, 2]}],
                          "pairs": pairs, "bonded_check": {"checked": 2, "closed": 2 - len(opened)},
                          "open_bonded": opened})
    rows = [
        {"key": "fea_inverted", "label": "Inverted elements", "op": "==", "limit": 0, "measured": 0, "ok": True},
        {"key": "fea_min_scaled_jacobian", "label": "Worst scaled Jacobian (1 is a perfect element)", "op": ">=",
         "limit": 0.2, "measured": 0.31, "ok": True},
        {"key": "fea_cusps", "label": "2 element(s) at a cusp in the CAD, set aside from the quality floor",
         "op": "empty", "limit": [], "measured": ["(12.5, -3, 40) mm", "(14.25, -3, 40) mm"], "ok": False},
        {"key": "fea_micro_details", "label": "1 element(s) on CAD details far smaller than the mesh", "op": "empty",
         "limit": [], "measured": ["(1, 2, 3) mm, a round 0.04 mm across"], "ok": False},
    ]
    fea = {"title": "FEA mesh", "summary": "23,000 quadratic tetrahedra (10-node), a solid mesh of 3 parts.",
           "elements": 23000, "units": {"mesh": "m", "words": "Lengths in the files are in metres (the part's own "
                                         "unit, converted). Use consistent units for loads and materials in your "
                                         "solver (for example N, m, Pa)."},
           "check": {"outcome": "concerns", "rows": rows, "solvers": "Limits: the strictest of three solvers."},
           "through_wall": {"ok": True, "min": 2.4, "needed": 2, "short": [], "words": "Yes - at least 2.4 elements."},
           "plan_words": ["The 26 fillets get at least 4 elements round each one.",
                          "41 of 44 CAD faces are named groups; the other 3 are accounted for."],
           "hot_spots": hot, "faces": [{"name": "Face_1", "cad_name": "bore", "kind": "cylinder", "area_mm2": 120.5}],
           "solvers": judge({"fea_inverted": 0, "fea_max_edge_ratio": 4.0, "fea_min_face_angle_deg": 12.0,
                             "fea_max_face_angle_deg": 150.0}),
           "assembly": asm, "skins_left_out": [{"face": "Face_7", "on": "Face_3", "words": "Face_7 lies on Face_3"}],
           "faces_short": "41 of 44 CAD faces are named; 3 skins of no thickness were left out. Every face is "
                          "accounted for.",
           "download": {"contains": [{"file": "mesh.inp", "words": "CalculiX / Abaqus input deck"}]}}
    return {"engine": "gmsh", "mesh_available": True, "surface": {"kind": "stl", "is_mesh": True, "fea": fea},
            "quality": {"cell_count": 23000, "mesh_units": "m"}, "review": {}}


def _text(html: str) -> str:
    body = html.split("</style>", 1)[-1]
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


def test_the_report_holds_everything_the_result_screen_leaves_out():
    html = mesh_report_html(_fea_payload())
    text = _text(html)
    assert REPORT_FILE == "mesh_report.html"
    assert "Passed with notes" in text
    # every limit, with its measured number
    assert "Worst scaled Jacobian (1 is a perfect element)" in text and "0.31" in text and "at least 0.2" in text
    # the cusp and micro-detail coordinates, every one
    for place in ("(12.5, -3, 40) mm", "(14.25, -3, 40) mm", "(1, 2, 3) mm, a round 0.04 mm across"):
        assert place in text, place
    # the plan in words
    assert "The 26 fillets get at least 4 elements round each one." in text
    # the full hot-spot list (348 rows), and a count per kind
    assert len(re.findall(r"<td>H\d+</td>", html)) == 348
    assert "Sharp inside corner" in text and "318" in text
    # every solver's own checks
    for label in ("CalculiX", "Abaqus", "Nastran"):
        assert label in text, label
    # every joined pair, set by whom, and the open one in red
    assert "Part_1 and Part_3" in text and "you, on the stage" in text
    assert 'class="alert"' in html and "A bonded pair is open" in text and "Part_3_Face_4" in text
    # the faces accounted for (MESH-8) and the units
    assert "3 skins of no thickness were left out" in text and "Face_7" in text
    assert "metres" in text and "N, m, Pa" in text


def test_the_report_is_one_self_contained_page_that_prints_cleanly():
    html = mesh_report_html(_fea_payload())
    # nothing to fetch: no script, no link, no remote font or picture
    assert "<script" not in html and "<link" not in html and "http://" not in html and "https://" not in html
    assert "@media print" in html and "page-break-inside:avoid" in html
    # no internals: no code path, no rule file
    assert ".py" not in html and "RULES.md" not in html


def test_the_report_is_the_same_bytes_for_the_same_payload():
    # the Everything bundle is deterministic: the report carries no clock
    assert mesh_report_html(_fea_payload()) == mesh_report_html(_fea_payload())


def test_a_closed_assembly_has_no_red_warning_and_a_cfd_mesh_lists_its_limits():
    assert 'class="alert"' not in mesh_report_html(_fea_payload(open_pair=False))
    cfd = {"engine": "snappy", "surface": {"kind": "polymesh", "is_mesh": True,
                                           "quality": {"cells": 1870000, "max_non_ortho": 64.2, "max_skewness": 3.1}},
           "quality": {"cell_count": 1870000, "mesh_units": "m", "production_grade": True,
                       "criteria": [{"key": "max_non_ortho", "label": "Max non-orthogonality", "gating": True,
                                     "ok_when": "<= 70", "measured": 64.2, "passed": True}]}}
    text = _text(mesh_report_html(cfd))
    assert "Passed" in text and "Max non-orthogonality" in text and "<= 70" in text and "1,870,000" in text
    assert "metres" in text


def test_a_value_from_the_part_is_escaped():
    p = _fea_payload(spots=1)
    p["surface"]["fea"]["faces"] = [{"name": "<img src=x onerror=alert(1)>", "cad_name": "a&b"}]
    html = mesh_report_html(p)
    assert "<img" not in html and "&lt;img" in html and "a&amp;b" in html
