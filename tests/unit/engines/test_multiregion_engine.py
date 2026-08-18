# Responsibility: Verify the multi-region case configures exact region coverage, splits, and checks each region.
def _write_region_mesh(ws, name: str, *, complete: bool = True) -> None:
    from pathlib import Path as _P

    from meshpipeline.engines.snappy_multiregion.regions import POLYMESH_COMPONENTS
    poly = _P(ws) / "constant" / name / "polyMesh"
    poly.mkdir(parents=True, exist_ok=True)
    for component in (POLYMESH_COMPONENTS if complete else ("owner",)):
        # never clobber a component the test wrote itself - a real `boundary` carries the
        # patch table these suites parse.
        if not (poly / component).exists():
            (poly / component).write_text("x")



# per-region checkMesh is region-aware (live tube-reactor smoke found it)

def test_per_region_checkmesh_runs_from_the_case_root_with_region_flag(monkeypatch, tmp_path):
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    calls = []
    def _fake_check(ws, region=""):
        calls.append((str(ws), region))
        return {"cells": 7, "fatal": [], "skew_fraction": 0.0, "max_non_ortho": 10.0}
    monkeypatch.setattr(R, "_single_region_check_mesh", _fake_check)
    monkeypatch.setattr(R, "check_interfaces",
                        lambda ws, rmap: {"interface_ok": True, "interfaces": []})
    (tmp_path / ".regions.json").write_text(
        '[{"name": "gas", "type": "fluid"}, {"name": "shape", "type": "solid"}]')
    for name in ("gas", "shape"):
        _write_region_mesh(tmp_path, name)
    q = R.check_mesh(tmp_path)
    assert calls == [(str(tmp_path), "gas"), (str(tmp_path), "shape")], \
        "checkMesh must run at the CASE ROOT with region=<name>, once per region"
    assert q["cells"] == 14 and q["mesh_ok"] is True


def test_shared_check_mesh_appends_the_region_flag(monkeypatch, tmp_path):
    from meshpipeline.engines.cfmesh import foam_exec
    seen = {}
    class _P:
        stdout = "Mesh OK\n cells: 5\n faces: 10\n"
        stderr = ""
    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd[-1]
        return _P()
    monkeypatch.setattr(foam_exec, "run_guarded", _fake_run)
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: None)
    foam_exec.check_mesh(tmp_path, region="gas")
    assert "checkMesh -constant -region gas" in seen["cmd"]
    foam_exec.check_mesh(tmp_path)
    assert seen["cmd"].endswith("checkMesh -constant")


def test_every_rendered_dict_carries_the_foamfile_header(tmp_path, monkeypatch):
    import json as _json

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = tmp_path
    (ws / "_assembly").mkdir()
    tri = "solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid s\n"
    solids = []
    for i in range(2):
        p = ws / "_assembly" / f"solid_{i}.stl"
        p.write_text(tri)
        solids.append({"index": i, "stl": str(p), "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1]})
    (ws / "_assembly" / "solids.json").write_text(_json.dumps(solids))
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: (0.0, 0.0, 0.0))
    R.configure_mesh(ws, strategy={"regions": [
        {"name": "air", "type": "fluid", "solids": [0]},
        {"name": "core", "type": "solid", "solids": [1]}]})
    for dic in ("blockMeshDict", "snappyHexMeshDict", "surfaceFeatureExtractDict"):
        txt = (ws / "system" / dic).read_text()
        assert txt.lstrip().startswith("FoamFile"), f"{dic} lacks the FoamFile header"


# coverage is a SCHEMA rule (forensic: 19-solid run showed prompt-only rules fail)

def _cov_ws(tmp_path, n=3):
    import json as _json
    (tmp_path / "_assembly").mkdir()
    tri = "solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid s\n"
    solids = []
    for i in range(n):
        p = tmp_path / "_assembly" / f"solid_{i}.stl"; p.write_text(tri)
        solids.append({"index": i, "stl": str(p), "bbox_min": [0,0,0], "bbox_max": [1,1,1]})
    (tmp_path / "_assembly" / "solids.json").write_text(_json.dumps(solids))
    return tmp_path


def test_configure_rejects_missing_unknown_and_duplicate_indices(tmp_path):
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = _cov_ws(tmp_path, n=3)
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "air", "type": "fluid", "solids": [0, 0, 99]},
        {"name": "core", "type": "solid", "solids": [1]}]})
    assert out["success"] is False
    assert out["code"] == "multiregion_region_coverage_invalid"
    assert out["missing_indices"] == [2]
    assert out["unknown_indices"] == [99]
    assert out["duplicate_indices"] == [0]
    assert out["expected_inventory_count"] == 3


def test_configure_accepts_exact_coverage(tmp_path, monkeypatch):
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: (0.0, 0.0, 0.0))
    ws = _cov_ws(tmp_path, n=2)
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "air", "type": "fluid", "solids": [0]},
        {"name": "core", "type": "solid", "solids": [1]}]})
    assert out.get("code") != "multiregion_region_coverage_invalid"
    assert out["regions"] == ["air", "core"]


def test_finalize_writes_the_review_surface_for_the_visual_reviewer(tmp_path, monkeypatch):
    import json as _json

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = tmp_path
    (ws / "constant" / "triSurface").mkdir(parents=True)
    (ws / "constant" / "regionProperties").write_text("regions ( fluid (air) solid (plate) );")
    tri = ("solid air\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
           "vertex 0 1 0\nendloop\nendfacet\nendsolid air\n")
    for n in ("air", "plate"):
        _write_region_mesh(ws, n)
        (ws / "constant" / n / "polyMesh" / "boundary").write_text(
            "1 ( outer { type patch; nFaces 1; startFace 0; } )")
        (ws / "constant" / "triSurface" / f"{n}.stl").write_text(tri)
    (ws / ".regions.json").write_text(
        '[{"name":"air","type":"fluid","solids":[0]},{"name":"plate","type":"solid","solids":[1]}]')
    monkeypatch.setattr(R, "_single_region_check_mesh",
                        lambda w, region="": {"cells": 5, "fatal": [], "skew_fraction": 0.0,
                                              "max_non_ortho": 10.0})
    monkeypatch.setattr(R, "check_interfaces", lambda w, r: {"interface_ok": True, "interfaces": []})
    captured = {}
    def _fake_build(w, tris):
        captured["names"] = sorted(tris)
        (Path(w) / "mesh.msh").write_text("msh")
        return ({n: [1] for n in tris}, (0,)*6)
    from pathlib import Path
    monkeypatch.setattr(R, "build_review_msh", _fake_build)
    out = R.finalize(str(ws), [], "snappy_multiregion", "cht", False, {}, "")
    assert out["success"] is True
    assert captured["names"] == ["air", "plate"], "review surface must cover every region"
    m = _json.loads((ws / "mesh_manifest.json").read_text())
    assert m["patches"].get("air") == [1]   # write_manifest stores patch_entities as "patches"


# semantic hardening: actual regions must reconcile EXACTLY with the declared plan

def test_check_mesh_reports_undeclared_regions_and_fails_mesh_ok(tmp_path, monkeypatch):

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = tmp_path
    (ws / ".regions.json").write_text(
        '[{"name":"air","type":"fluid","solids":[0]},{"name":"plate","type":"solid","solids":[1]}]')
    for n in ("air", "plate", "domain0"):
        _write_region_mesh(ws, n)
    monkeypatch.setattr(R, "_single_region_check_mesh",
                        lambda w, region="": {"cells": 9, "fatal": [], "skew_fraction": 0.0,
                                              "max_non_ortho": 5.0})
    monkeypatch.setattr(R, "check_interfaces", lambda w, r: {"interface_ok": True, "interfaces": []})
    q = R.check_mesh(ws)
    assert q["regions_undeclared"] == ["domain0"]
    assert q["mesh_ok"] is False


def test_regions_split_gate_rejects_undeclared_regions():
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.snappy_multiregion.gates import _gate_regions_split
    class _Ctx(GateCtx):
        pass
    ctx = GateCtx(workspace=None, engine="snappy_multiregion", domain="", intake_patches=[],
                  engine_params={})
    # gates read the manifest quality block via _q - monkey-free: call with a fake _q via ctx?
    # simplest: patch the module's _q
    import meshpipeline.engines.snappy_multiregion.gates as G
    real_q = G._q
    try:
        G._q = lambda c: {"regions_missing": [], "regions": [{"name": "air", "cells": 5}],
                          "regions_undeclared": ["domain0"]}
        ok, fb = _gate_regions_split(ctx)
        assert ok is False and "undeclared region" in fb and "domain0" in fb
    finally:
        G._q = real_q


def test_configure_persists_the_assembly_plan_and_seeds_regions(tmp_path, monkeypatch):
    import json as _json

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = _cov_ws(tmp_path, n=2)
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: (0.1, 0.2, 0.3))
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "air", "type": "fluid", "solids": [0]},
        {"name": "core", "type": "solid", "solids": [1]}]})
    assert out.get("success") is not False
    plan = _json.loads((ws / ".assembly_plan.json").read_text())
    assert plan["expected_regions"] == ["air", "core"]
    assert plan["body_to_region"] == {"0": "air", "1": "core"}
    assert plan["allow_inert_background"] is False
    dict_txt = (ws / "system" / "snappyHexMeshDict").read_text()
    # SINGLE locationInMesh at the VERIFIED fluid point (the multi-point locationsInMesh
    # form loses the cellZones in this workflow - proven live, 78 connectivity fragments)
    assert "locationsInMesh" not in dict_txt
    assert "locationInMesh (0.1 0.2 0.3);" in dict_txt
    # enclosing fluid (its bbox spans the assembly) => ZERO background padding: the
    # background box coincides with the fluid's outer faces, so no unzoned shell exists
    bm = (ws / "system" / "blockMeshDict").read_text()
    assert "(0 0 0)" in bm.replace("    (", "(")   # unpadded min corner


def test_unseedable_region_is_a_structured_failure(tmp_path, monkeypatch):
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = _cov_ws(tmp_path, n=2)
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: None)
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "air", "type": "fluid", "solids": [0]},
        {"name": "core", "type": "solid", "solids": [1]}]})
    assert out["success"] is False and out["code"] == "multiregion_region_unseedable"


# scale-aware planning (19-solid stress lesson: one global level cannot resolve
# mm fasteners in a 0.3 m box)

def test_geometry_report_flags_tiny_solids_and_needed_levels(tmp_path, monkeypatch):
    import json as _json

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    ws = tmp_path; (ws / "_assembly").mkdir()
    solids = [
        {"index": 0, "volume": 1.0, "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "centroid": [.5]*3},
        {"index": 1, "volume": 1e-9, "bbox_min": [0.4, 0.4, 0.4],
         "bbox_max": [0.401, 0.401, 0.401], "centroid": [.4]*3},
    ]
    (ws / "_assembly" / "solids.json").write_text(_json.dumps(solids))
    rep = R.inspect_stl(ws)
    assert rep["small_solids"] == [1]
    assert rep["scale_ratio_warnings"] and "region_refinement" in rep["scale_ratio_warnings"][0]
    lv = {p["index"]: p["needed_level"] for p in rep["per_solid_scale"]}
    assert lv[0] == 0 and lv[1] >= 5


def test_region_refinement_overrides_the_global_level(tmp_path):
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    rmap = R.region_map([{"name": "air", "type": "fluid", "solids": [0]},
                         {"name": "fasteners", "type": "solid", "solids": [1]}])
    block = R.render_refinement_surfaces(rmap, (2, 2), 1, {"fasteners": [5, 6]})
    assert "level (2 2);" in block            # air keeps the global level
    assert "level (5 7);" in block            # fasteners: 6 + interface_refinement 1


def test_region_refinement_schema_validates_names_and_bounds():
    from meshpipeline.engines.snappy_multiregion.authoring import validate
    base = {"regions": [{"name": "air", "type": "fluid", "solids": [0]},
                        {"name": "core", "type": "solid", "solids": [1]}]}
    assert not [d for d in validate({**base, "region_refinement": {"core": [4, 5]}})
                if d.severity == "error"]
    errs = [d.message for d in validate({**base, "region_refinement": {"bogus": [4, 5]}})]
    assert any("unknown region" in m for m in errs)
    errs = [d.message for d in validate({**base, "region_refinement": {"core": [9, 2]}})]
    assert errs
