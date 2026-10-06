# Responsibility: Verify a thermal model is planned and checked against its file: the surface level each region needs for two cells across its thinnest layer and its narrow air, a refusal naming Option B when that cannot fit, and a hard failure for any mesh that loses a region, changes a volume, invents or cuts a contact, or puts fewer than two cells across a layer.
# ECXML-TEST (2026-10-06): a 25 um TIM with one cell across came out +79.6% in volume and the die
# under it touched the spreader (a contact the file lacks); a 25 um die attach vanished; a die
# stack with 1-5 um gaps meshed for 53 minutes into nothing. Every check had passed.
from __future__ import annotations

import json
import math

import pytest
from tests.unit.cad.ecxml_models import wirebond_package

from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
from meshpipeline.engines.snappy_multiregion import thermal_fidelity as TF


def _sidecar(doc) -> dict:
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import build

    return build(read_ecxml(doc.xml())).sidecar


@pytest.fixture(scope="module")
def package() -> dict:
    return _sidecar(wirebond_package())


def _plan(side: dict, *, budget: int = 6_000_000):
    mapping = {r["name"]: [r["name"]] for r in side["regions"]}
    kinds = {r["name"]: r["type"] for r in side["regions"]}
    b = side["domain"]["box_m"]
    base = math.dist(b["min"], b["max"]) / TF.BACKGROUND_DIVISIONS
    return TF.plan(side, mapping, kinds, base, (2, 2), {}, budget_cells=budget,
                   timeout_s=3000.0), base


def test_a_layer_too_thin_for_its_level_is_raised_to_three_cells_across(package):
    p, base = _plan(package)
    assert not p.refusal
    # a 14.7 mm model starts from 0.37 mm cells: 3 across 25 um need level 6 (5.7 um)
    assert p.levels["Die_attach"] == [6, 6]
    assert base / 2 ** 6 <= 25e-6 / 3 < base / 2 ** 5
    assert any(r.startswith("Die_attach: surface level 2 -> 6") and "25 um thick" in r
               for r in p.raised)
    assert p.levels["air"] == [2, 2]              # the air follows the solids around it


def test_a_model_whose_layers_need_more_than_the_budget_is_refused_naming_option_b(package):
    p, _ = _plan(package, budget=100_000)
    assert "Die_attach is 25 um thick" in p.refusal
    assert "Option B" in p.refusal and "0.1 M-cell budget" in p.refusal
    # 5 um of air under a capacitor: the smaller of the two faces must reach level 8 (1.4 um
    # cells) - millions of cells on its own, more than this engine can afford
    q, base = _plan(_sidecar(wirebond_package(cap_gap_m=5e-6)))
    assert q.levels["Cap"][0] == TF.level_for(5e-6, base) == 8
    assert "the 5 um of air between Cap and Substrate" in q.refusal
    assert "minutes" in q.refusal and "Option B" in q.refusal


def _mesh(ws, side: dict, *, contacts=None, drop=(), scale=None, coarse=()):
    """A split mesh as the checks read it: boundary files with each region's interfaces, and the
    measured cells and volume per region (three cells across each solid's thinnest layer)."""
    contacts = side["solid_contacts"] if contacts is None else contacts
    (ws / TF.THERMAL_MODEL).write_text(json.dumps(side))
    names = [r["name"] for r in side["regions"] if r["name"] not in drop]
    touching: dict[str, dict[str, int]] = {n: {} for n in names}
    for a, b in contacts:
        if a in touching and b in touching:
            touching[a][b] = touching[b][a] = 100
    for n in names:
        if n != "air":
            touching[n]["air"] = touching["air"][n] = 50
    for n, others in touching.items():
        pm = ws / "constant" / n / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"    {n}_to_{o}\n    {{\n        type mappedWall;\n        nFaces {k};\n"
                         f"        startFace 0;\n    }}" for o, k in others.items())
        (pm / "boundary").write_text(f"{len(others)}\n(\n{body}\n)\n")
    measured = {}
    for r in side["regions"]:
        if r["name"] in drop:
            continue
        vol = r["volume_m3"] * (scale or {}).get(r["name"], 1.0)
        t = r.get("thinnest_m") or vol ** (1 / 3)
        across = 1.0 if r["name"] in coarse else 3.0
        measured[r["name"]] = {"cells": max(1, round(vol / (t / across) ** 3)), "volume_m3": vol}
    rmap = {r["name"]: {"type": r["type"], "solids": []} for r in side["regions"]}
    return rmap, measured


def test_a_mesh_that_is_the_files_model_passes(tmp_path, package):
    rmap, measured = _mesh(tmp_path, package)
    assert TF.failures(tmp_path, rmap, measured, []) == []


def test_every_way_a_mesh_can_miss_the_file_is_a_failure_with_its_reason(tmp_path, package):
    contacts = [c for c in package["solid_contacts"] if sorted(c) != ["Die", "Die_attach"]]
    rmap, measured = _mesh(tmp_path, package, drop=("Mold",), scale={"Die_attach": 1.796},
                           coarse=("Die",), contacts=contacts + [["Die", "Substrate"]])
    got = TF.failures(tmp_path, rmap, measured, [])
    text = "\n".join(got)
    assert "Mold (the file's Mold) is not in the mesh" in text
    assert "Die_attach comes out +79.6% in volume" in text
    assert "Die has about 1.0 cell(s) across its thinnest layer (Die, 300 um" in text
    assert "Die and Substrate touch in the mesh (100 faces) but not in the file" in text
    assert "Die and Die_attach touch in the file but not in the mesh" in text


def test_check_mesh_fails_a_thermal_mesh_that_is_not_the_files_model(tmp_path, monkeypatch,
                                                                      package):
    for bad in (False, True):
        ws = tmp_path / str(bad)
        ws.mkdir()
        rmap, measured = _mesh(ws, package, scale={"Die_attach": 1.796} if bad else None)
        (ws / ".regions.json").write_text(json.dumps(
            [{"name": n, "type": r["type"], "solids": []} for n, r in rmap.items()]))
        monkeypatch.setattr(R, "_region_dirs", lambda ws, names=list(rmap): names)
        monkeypatch.setattr(R, "_single_region_check_mesh", lambda ws, region, m=measured: {
            "cells": m[region]["cells"], "total_volume": m[region]["volume_m3"], "fatal": []})
        q = R.check_mesh(ws)
        assert q["mesh_ok"] is (not bad)
        if bad:
            assert "Option B" in q["fatal"][0]
            assert q["fatal"][1].startswith("thermal model: Die_attach comes out +79.6% in volume")


def test_checkmesh_total_volume_is_read_without_the_sentences_full_stop():
    from meshpipeline.engines.snappy_multiregion.foam_exec import _CM

    line = "    Min volume = 1.2e-12. Max volume = 3.4e-09.  Total volume = 0.00096.  Cell volumes OK."
    assert _CM["total_volume"].search(line).group(1) == "0.00096"
    assert _CM["total_volume"].search("Total volume = 2.5e-05.  ").group(1) == "2.5e-05"
