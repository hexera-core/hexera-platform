# Responsibility: Verify PLAN-cfd-4: a size the user asks for on a part becomes that part's own snappyHexMesh level against the background cell (the coarsest level inside their range), "finer" and "coarser" move one level, a part that is its own region gets its level in the dict, a part merged into one wall is refined near its own surface and never made coarser (said), and unclear words or one part apply nothing.
# Boundaries: the dict this system authors from a reading written here; no model, no mesher.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshpipeline.cad import part_words as PW
from meshpipeline.engines.snappy import part_levels as PL
from meshpipeline.engines.snappy import snappy_runner as R

_ANALYSIS = {"bbox_min": [0.0, -0.3, -0.3], "bbox_max": [0.1, 0.3, 0.3], "L": 0.1,
             "extent": [0.1, 0.6, 0.6]}
_REC = {"base_cell": 0.02, "surface_level": [3, 3], "feature_level": 4, "afford_level": 5,
        "distance_bands": [(0.04, 3), (0.12, 2)], "resolve_feature_angle": 30.0}
TRI = [((0.0, 0.0, 0.0), (0.01, 0.0, 0.0), (0.0, 0.01, 0.0))]


def _reading(*entries) -> dict:
    table = [{"id": "Part_1", "name": "fins"}, {"id": "Part_2", "name": "housing"}, {"id": "Part_3", "name": "duct"}]
    return PW.section("used", entries=list(entries), table=table)


BY_ID = {"Part_1": "fins", "Part_2": "housing", "Part_3": "duct"}


def test_a_size_is_the_coarsest_level_inside_the_range():
    assert PL.level_for(0.002, 0.003, 0.02) == 3          # 20 mm / 8 = 2.5 mm, inside 2-3 mm
    assert PL.level_for(0.001, 0.001, 0.02) == 4          # 1.25 mm is nearest 1 mm (round(log2(20)))
    assert PL.level_for(5.0, 9.0, 0.02) == 0              # coarser than the background: level 0
    assert PL.level_for(1e-9, 1e-9, 0.02) == PL.MAX_LEVEL


def test_own_regions_get_their_levels_and_merged_parts_are_refined_only():
    ps = _reading({"parts": ["Part_1"], "ask": "size", "min_mm": 2, "max_mm": 3, "quote": "2-3 mm on the fins"},
                  {"parts": ["Part_2"], "ask": "coarsen", "quote": "coarser on the housing"},
                  {"parts": ["Part_3"], "ask": "refine", "quote": "refine the duct"})
    lv = PL.plan(ps, BY_ID, base_m=0.02, smin=3, smax=4, regions=["fins", "housing"])
    assert lv["regions"] == {"fins": (3, 3), "housing": (2, 3)}
    assert lv["refine"] == {"duct": 5}
    merged = PL.plan(ps, BY_ID, base_m=0.02, smin=3, smax=4, regions=[])
    assert "housing" not in merged["regions"] and "housing" not in merged["refine"]
    assert any("housing was not made coarser" in w for w in merged["words"])


def test_the_dict_carries_each_parts_level_and_its_own_refinement(tmp_path: Path):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    tri = ws / "constant" / "triSurface"
    tri.mkdir(parents=True)
    with (tri / "partref_duct.stl").open("w") as fh:
        R._write_solid(fh, "partref_duct", TRI)
    ps = _reading({"parts": ["Part_1"], "ask": "size", "min_mm": 2, "max_mm": 3, "quote": "2-3 mm on the fins"},
                  {"parts": ["Part_3"], "ask": "refine", "quote": "refine the duct"})
    summary = R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=_ANALYSIS, recommendation=_REC,
        domain_min=[-1.0, -2.0, -2.0], domain_max=[2.0, 2.0, 2.0], strategy={"n_layers": 0},
        surface_regions=["fins", "housing"], part_sizes={"reading": ps, "by_id": BY_ID})
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    lo, hi = summary["part_levels"]["regions"]["fins"]
    assert f"fins {{ level ({lo} {hi});" in text
    assert "partref_duct.stl { type triSurfaceMesh; name partref_duct; }" in text
    assert "partref_duct { mode distance; levels ((" in text
    assert summary["part_levels"]["words"]


def test_no_reading_leaves_the_dict_as_it_was(tmp_path: Path):
    def render(sizes):
        ws = tmp_path / ("a" if sizes is None else "b")
        (ws / "system").mkdir(parents=True)
        R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=_ANALYSIS,
                             recommendation=_REC, domain_min=[-1.0, -2.0, -2.0], domain_max=[2.0, 2.0, 2.0],
                             strategy={"n_layers": 0}, surface_regions=["fins", "housing"], part_sizes=sizes)
        return (ws / "system" / "snappyHexMeshDict").read_text()
    assert render(None) == render({"reading": PW.section("failed"), "by_id": BY_ID})


def test_one_part_or_no_words_asks_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("asked")
    monkeypatch.setattr(PW, "decide", boom)
    notes = {"user_notes": [{"id": "n", "text": "2 mm on the fins", "source": "console"}]}
    got = asyncio.run(PL.decide(Path("."), {"fins": TRI}, notes))
    assert got["reading"]["status"] == "none"
    got = asyncio.run(PL.decide(Path("."), {"fins": TRI, "duct": TRI}, {}))
    assert got["reading"]["status"] == "none"


@pytest.mark.parametrize("why", ["down", "garbage"])
def test_a_model_that_fails_applies_nothing_and_never_raises(monkeypatch, why):
    async def failing(*a, **k):
        raise RuntimeError(why)
    monkeypatch.setattr(PW, "decide", failing)
    notes = {"user_notes": [{"id": "n", "text": "2 mm on the fins", "source": "console"}]}
    got = asyncio.run(PL.decide(Path("."), {"fins": TRI, "duct": TRI}, notes))
    assert got["reading"]["status"] == "failed"
    assert PL.plan(got["reading"], got["by_id"], base_m=0.02, smin=3, smax=4, regions=["fins"]) == {
        "regions": {}, "refine": {}, "words": []}


def test_the_parts_are_the_files_named_solids_in_order():
    surface, record, by_id = PL.surface_of({"fins": TRI, "empty": [], "duct": TRI})
    assert by_id == {"Part_1": "fins", "Part_2": "duct"}
    assert surface.names == ["Part_1_Face_1", "Part_2_Face_1"] and len(surface.tris) == 2
    assert abs(float(surface.xyz.max()) - 10.0) < 1e-9          # millimetres
    table = PW.part_table(record, surface)
    assert [r["name"] for r in table] == ["fins", "duct"]
