# Responsibility: Verify a large assembly reaches the builder and configure_mesh - geometry_report fits the tool reply (compact rows, in pages) and regions can be named by a solid's name, a pattern, or one per-solid entry.
# ECXML-TEST (2026-10-06): a 351-part server's geometry_report was 87k characters and a 1,000-part
# board's 241k against the builder's 16k tool cap; the builder never saw a solid, never called
# configure_mesh, and no mesh was made. Any assembly past ~60 solids hit it.
from __future__ import annotations

import json

from meshpipeline.agents.builder.tools import geometry as G
from meshpipeline.engines.snappy_multiregion import authoring as A
from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
from meshpipeline.engines.snappy_multiregion import region_select as S

CAP = 16000


def _solids(n: int) -> list[dict]:
    """An air box and n-1 parts on a grid inside it, named like a board's parts."""
    out = [{"index": 0, "name": "air", "volume": 0.2, "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [1.0, 1.0, 0.2], "centroid": [0.5, 0.5, 0.1]}]
    for i in range(1, n):
        x, y = (i % 45) * 0.02, (i // 45) * 0.02
        out.append({"index": i, "name": f"Cap_{i}" if i % 2 else f"R {i}", "volume": 2e-7,
                    "bbox_min": [x, y, 0.01], "bbox_max": [x + 0.01, y + 0.01, 0.012],
                    "centroid": [x + 0.005, y + 0.005, 0.011]})
    return out


def test_regions_by_name_by_pattern_and_one_per_solid_resolve_to_indices():
    regions, problems = S.resolve_regions(
        [{"name": "air", "type": "fluid", "solids": ["air"]},
         {"name": "caps", "type": "solid", "solids": ["Cap_*"]},
         {"per_solid": True, "type": "solid", "solids": ["*"]}], _solids(7))
    assert problems == []
    assert regions[0] == {"name": "air", "type": "fluid", "solids": [0]}
    assert regions[1] == {"name": "caps", "type": "solid", "solids": [1, 3, 5]}
    # every solid no other entry claims is its own region, named (mesh-safe) after it
    assert [(r["name"], r["type"], r["solids"]) for r in regions[2:]] == [
        ("R_2", "solid", [2]), ("R_4", "solid", [4]), ("R_6", "solid", [6])]


def test_a_name_that_matches_no_solid_is_reported_never_dropped():
    _, problems = S.resolve_regions([{"name": "air", "type": "fluid", "solids": ["Air", 0]}],
                                    _solids(3))
    assert problems == ["region 'air': 'Air' names no solid"]


def test_refinement_keys_may_be_patterns_and_an_exact_name_wins():
    rr, problems = S.expand_refinement({"Cap_*": [4, 4], "Cap_3": [6, 6], "nope": [1, 1]},
                                       ["air", "Cap_1", "Cap_3"])
    assert rr == {"Cap_3": [6, 6], "Cap_1": [4, 4]}
    assert problems == ["region_refinement: 'nope' names no region"]


def test_the_palette_takes_names_patterns_and_a_per_solid_entry():
    diags = A.validate({"regions": [{"name": "air", "type": "fluid", "solids": ["air"]},
                                    {"per_solid": True, "type": "solid", "solids": ["*"]}],
                        "region_refinement": {"Cap_*": [4, 4], "R_7": [5, 5]}})
    assert [x.message for x in diags if x.severity == "error"] == []
    bad = A.validate({"regions": [{"name": "air", "type": "fluid", "solids": [""]}]})
    assert any(x.path == "regions[0].solids" for x in bad)


def test_a_large_assemblys_report_fits_the_tool_reply_page_by_page(tmp_path):
    (tmp_path / "_assembly").mkdir()
    (tmp_path / "_assembly" / "solids.json").write_text(json.dumps(_solids(2000)))
    report = R.inspect_stl(tmp_path)
    assert report["n_solids"] == 2000 and report["solids_format"][:2] == ["index", "name"]
    assert {g["names"]: g["count"] for g in report["solid_groups"]} == {
        "Cap*": 1000, "R*": 999, "air": 1}
    # the air box encloses every other solid: the region map comes ready to send
    assert report["suggested_regions"] == [
        {"name": "air", "type": "fluid", "solids": ["air"]},
        {"per_solid": True, "type": "solid", "solids": ["*"]}]
    first = G._bounded_report(report, CAP)
    pages = first["solids_page"]["pages"]
    assert len(json.dumps(first)) <= CAP and pages > 1
    seen: list[int] = []
    for k in range(1, pages + 1):
        page = G._bounded_report(report, CAP, page=k)
        assert len(json.dumps(page)) <= CAP
        assert page["suggested_regions"] and page["solid_groups"]   # the summary on every page
        seen += [row[0] for row in page["solids"]]
    assert seen == list(range(2000))                               # every solid, once
    assert first["solids_page"]["next"] == "call geometry_report with page=2 for the next rows"


def test_a_small_assemblys_report_is_unchanged(tmp_path):
    (tmp_path / "_assembly").mkdir()
    (tmp_path / "_assembly" / "solids.json").write_text(json.dumps(_solids(5)))
    report = R.inspect_stl(tmp_path)
    assert isinstance(report["solids"][0], dict) and report["per_solid_scale"]
    assert G._bounded_report(report, CAP) == report
