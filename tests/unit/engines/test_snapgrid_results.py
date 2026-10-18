# Responsibility: Verify a delivered snap-grid mesh of a thermal model reaches the viewer as an
# electronics-cooling engineer reads it: every part by the file's name with the cells the report
# counted, the cells through its thinnest layer and its volume against the file; the quality in plain
# words; slices across the board and through the hottest parts, cut from the delivered regions; the
# per-part table and the thermal model as files - and no inlet, outlet, sticker, sure, triangle, far
# field or "flows through" anywhere in it.
# Boundaries: real ECXML test models (tests/unit/cad/ecxml_models) meshed for real by the snap-grid
# mesher (no OpenFOAM: the check's numbers are written into the report as checkMesh gives them).
from __future__ import annotations

import base64
import csv
import io
import json

import numpy as np
import pytest
from tests.unit.cad.ecxml_models import ducted_board, set_top_box, tiny_board

from meshpipeline.application.thermal_wording import forbidden_words
from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid import results as R
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml
from meshpipeline.engines.snapgrid.viewer import snapgrid_viewer

#: checkMesh's numbers for a clean case, as native.check records them
_NATIVE = {"ok": True, "problems": [], "whole": {"mesh_ok": True, "cells": 0, "hexahedra": 0,
                                                  "polyhedra": 0, "max_non_ortho": 41.2, "fatal": [],
                                                  "skew_faces": 0}}


def _case(tmp_path, doc, native: dict | None = None):
    src = tmp_path / "model.ecxml"
    src.write_bytes(doc.xml())
    case = tmp_path / "case"
    result = mesh_ecxml(src, case, G.GridPlan(), binary=False)
    report = json.loads((case / R.REPORT_NAME).read_text())
    cells = sum(int(r["cells"]) for r in report["regions"])
    nat = json.loads(json.dumps(native or _NATIVE))
    nat["whole"].update(cells=cells, hexahedra=nat["whole"].get("hexahedra") or cells)
    nat["regions"] = {r["name"]: {"mesh_ok": True, "cells": r["cells"]} for r in report["regions"]}
    report["native"] = nat
    (case / R.REPORT_NAME).write_text(json.dumps(report))
    return case, result, report


def _words(t: dict) -> str:
    """Every word the results panel shows."""
    said = [t["title"], t["summary"], *t["quality"], t["files"]["per_part_csv"].splitlines()[0]]
    for r in t["parts"]:
        said += [r["label"], r["kind"], str(r["material"]), r["volume_match"]]
    for s in t["slices"]:
        said += [s["label"], s["why"]]
    return " ".join(said)


@pytest.fixture(scope="module")
def stb(tmp_path_factory):
    return _case(tmp_path_factory.mktemp("stb"), set_top_box())


def test_every_part_is_listed_by_the_files_name_with_the_reports_numbers(stb):
    case, _result, report = stb
    t = R.thermal_results(case)
    solids = [r for r in report["regions"] if r["type"] == "solid"]
    assert [r["name"] for r in t["parts"]] == [r["name"] for r in solids]
    assert [r["cells"] for r in t["parts"]] == [r["cells"] for r in solids]
    assert {a["name"] for a in t["air"]} == {r["name"] for r in report["regions"] if r["type"] == "fluid"}
    thin = {}
    for row in report["thin_layers"]:
        if row["part"] not in thin or row["thickness_m"] < thin[row["part"]]["thickness_m"]:
            thin[row["part"]] = row
    for r in t["parts"]:
        if r["name"] in thin:
            assert r["thinnest_mm"] == pytest.approx(thin[r["name"]]["thickness_m"] * 1000, rel=1e-6)
            assert r["cells_through"] == thin[r["name"]]["cells_across"]
            assert r["cells_asked"] == thin[r["name"]]["wanted"]
    # a box part is meshed exactly; the label reads as the file wrote it
    pcb = next(r for r in t["parts"] if r["name"] == "PCB")
    assert pcb["volume_match"] == "exact" and pcb["kind"] == "PCB"
    assert next(r for r in t["parts"] if r["name"] == "SoC_heat_sink")["label"] == "SoC heat sink"


def test_the_power_is_where_the_file_puts_it_heat_sources_included(stb):
    case, _r, _rep = stb
    sidecar = json.loads((case / R.SIDECAR_NAME).read_text())
    power = R.power_by_region(sidecar)
    assert sum(power.values()) == pytest.approx(float(sidecar["total_power_W"]), abs=1e-6)
    t = R.thermal_results(case)
    assert t["summary"].endswith(f"{float(sidecar['total_power_W']):g} W")


def test_the_quality_is_said_in_plain_words(stb):
    case, _r, report = stb
    t = R.thermal_results(case)
    q = " ".join(t["quality"])
    assert "checked clean by OpenFOAM" in q and "Every cell is a box aligned with the model" in q
    assert "worst angle 41°" in q and "no bad cells" in q
    thinnest = min((r for r in t["parts"] if r["thinnest_mm"] is not None), key=lambda r: r["thinnest_mm"])
    assert f"Thinnest layer: {thinnest['label']}, {thinnest['thinnest_mm']:g} mm" in q


def test_a_mesh_with_split_cells_and_a_short_layer_says_so(tmp_path):
    native = json.loads(json.dumps(_NATIVE))
    native["whole"].update(hexahedra=1, polyhedra=5, skew_faces=3)
    case, _r, report = _case(tmp_path, tiny_board(), native)
    report["thin_layers"][0].update(cells_across=1, wanted=2, ok=False)
    report["native"]["whole"]["hexahedra"] = report["native"]["whole"]["cells"] - 5
    (case / R.REPORT_NAME).write_text(json.dumps(report))
    q = " ".join(R.thermal_results(case)["quality"])
    assert "are plain boxes aligned with the model; the other 5 sit where the grid steps finer" in q
    assert "3 skewed faces" in q
    assert "got fewer cells through a layer than asked" in q and "(1 of 2)" in q


def test_the_slices_cross_the_board_and_the_hottest_parts(stb):
    case, _r, _rep = stb
    sidecar = json.loads((case / R.SIDECAR_NAME).read_text())
    planes = R.slice_planes(sidecar)
    names = [p["name"] for p in planes]
    assert names[0] == "board_midplane" and planes[0]["normal"] == [0.0, 0.0, 1.0]
    power = R.power_by_region(sidecar)
    hottest = sorted((n for n, w in power.items() if w > 0), key=lambda n: -power[n])[:R.HOT_PARTS]
    assert names[1:] == [f"part_{n}" for n in hottest]
    for p in planes:
        assert p["kind"] == "slice" and len(p["origin"]) == 3 and len(p["clip_box"]) == 6
    # the board's mid-plane lies inside the board
    pcb = next(r for r in sidecar["regions"] if r["name"] == "PCB")
    lo, hi = R._box(pcb)
    assert lo[2] < planes[0]["origin"][2] < hi[2]


def test_a_slice_is_the_delivered_mesh_cut_region_by_region(stb):
    case, _r, report = stb
    t = R.thermal_results(case)
    board = next(s for s in t["slices"] if s["name"] == "board_midplane")
    regions = {p["region"] for p in board["pieces"]}
    assert "PCB" in regions and regions <= {r["name"] for r in report["regions"]}
    pcb = next(p for p in board["pieces"] if p["region"] == "PCB")
    pts = np.frombuffer(base64.b64decode(pcb["points_b64"]), dtype=np.float32).reshape(-1, 3)
    # every point of the cut lies on the plane
    assert np.allclose(pts[:, 2], board["origin"][2], atol=1e-6)
    polys = np.frombuffer(base64.b64decode(pcb["polys_b64"]), dtype=np.uint32)
    n, k = 0, 0
    while k < len(polys):
        n += 1
        k += int(polys[k]) + 1
    assert n == pcb["cells"] and polys.max() < len(pts)


def test_the_per_part_table_and_the_thermal_model_are_files(stb):
    case, _r, _rep = stb
    t = R.thermal_results(case)
    rows = list(csv.DictReader(io.StringIO(t["files"]["per_part_csv"])))
    assert [r["part"] for r in rows] == [p["name"] for p in t["parts"]]
    assert [int(r["cells"]) for r in rows] == [p["cells"] for p in t["parts"]]
    assert "-0" not in t["files"]["per_part_csv"].replace("-0.", "")
    assert json.loads(t["files"]["thermal_model_json"]) == json.loads((case / R.SIDECAR_NAME).read_text())


def test_the_viewer_payload_carries_the_results(stb):
    case, _r, _rep = stb
    resp = snapgrid_viewer(case, roles={}, units="m")
    assert resp["thermal"]["parts"] and resp["thermal"]["slices"]


@pytest.mark.parametrize("model", [set_top_box, ducted_board, tiny_board])
def test_no_generic_cfd_words_reach_the_results(tmp_path, model):
    case, _r, _rep = _case(tmp_path, model())
    t = R.thermal_results(case)
    assert forbidden_words(_words(t)) == [], _words(t)


def test_the_manifest_carries_the_thermal_slices_for_the_review(stb, tmp_path):
    from meshpipeline.engines.manifest import write_manifest

    case, _r, _rep = stb
    planes = R.slice_planes(json.loads((case / R.SIDECAR_NAME).read_text()))
    m = write_manifest(tmp_path, patch_types={}, patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
                       quality={"cells": 1}, body_bbox=((0, 0, 0), (1, 1, 1)), mesh_units="m",
                       mesh_mode="snapgrid", inspection_regions=planes)
    assert [r["name"] for r in m["inspection_regions"]] == [p["name"] for p in planes]
    generic = write_manifest(tmp_path, patch_types={}, patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
                             quality={"cells": 1}, body_bbox=((0, 0, 0), (1, 1, 1)), mesh_units="m",
                             mesh_mode="snapgrid")
    assert {r["name"] for r in generic["inspection_regions"]} >= {"slice_x_centre", "slice_z_centre"}


def test_a_case_without_a_report_has_no_results(tmp_path):
    assert R.thermal_results(tmp_path) is None
