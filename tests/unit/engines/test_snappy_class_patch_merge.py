# Responsibility: Verify the layer policy's synthetic wall regions are folded back into the declared
# wall after meshing, so the delivered boundary is exactly the one the user signed.
#
# Five corpus rotors were rejected at patch_contract with a good mesh: the thin-feature layer
# policy split the wall into body / body_thin / body_razor so each class could carry its own
# layer count, snappyHexMesh made every region its own patch, and the contract - exactly
# `body` and `farfield` - refused the extras. The split is a meshing device; createPatch merges
# the class patches back once the layers are on. Real CAD-named solids are the user's own
# patches and are never merged.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.snappy import snappy_runner as R

_ANALYSIS = {"bbox_min": [0.0, -0.3, -0.3], "bbox_max": [0.1, 0.3, 0.3], "L": 0.1,
             "extent": [0.1, 0.6, 0.6]}
_REC = {"base_cell": 0.02, "surface_level": [3, 3], "feature_level": 4, "afford_level": 5,
        "distance_bands": [(0.04, 3), (0.12, 2)], "resolve_feature_angle": 30.0}


def _render(tmp_path: Path, regions):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True, exist_ok=True)
    summary = R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=_ANALYSIS,
        recommendation=_REC, domain_min=[-1.0, -2.0, -2.0], domain_max=[2.0, 2.0, 2.0],
        strategy={"n_layers": 4}, surface_regions=regions,
        layer_counts={"body": 4, "body_thin": 2, "body_razor": 0} if regions else None)
    return ws, summary


def test_synthetic_class_regions_are_merged_back_into_the_declared_wall(tmp_path):
    ws, summary = _render(tmp_path, ["body", "body_thin", "body_razor"])
    cp = ws / "system" / "createPatchDict"
    assert cp.exists(), "no createPatchDict - the class patches would reach the manifest"
    text = cp.read_text()
    assert "name body;" in text and "patchInfo { type wall; }" in text
    assert "patches (body body_thin body_razor);" in text
    assert "constructFrom patches;" in text
    assert summary["merged_regions"] == ["body_thin", "body_razor"]
    # the meshing dict still carries the split - the layers need it
    snappy = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "body_thin { nSurfaceLayers 2; }" in snappy


def test_real_cad_solids_are_never_merged(tmp_path):
    ws, summary = _render(tmp_path, ["fluid", "wall"])
    assert not (ws / "system" / "createPatchDict").exists()
    assert "merged_regions" not in summary


def test_a_plain_wall_authors_no_merge(tmp_path):
    ws, summary = _render(tmp_path, None)
    assert not (ws / "system" / "createPatchDict").exists()
    assert "merged_regions" not in summary


def test_a_replan_without_a_split_leaves_no_stale_merge_behind(tmp_path):
    ws, _ = _render(tmp_path, ["body", "body_thin"])
    assert (ws / "system" / "createPatchDict").exists()
    _render(tmp_path, None)
    assert not (ws / "system" / "createPatchDict").exists()


def test_the_native_run_merges_only_when_the_dict_exists(tmp_path, monkeypatch):
    from meshpipeline.engines.snappy import native as N

    calls: list[str] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        # the stage command is the last element of ["bash", "-lc", "source ...; <cmd>"]
        line = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        calls.append(line)
        ws = Path(k.get("cwd") or tmp_path)
        (ws / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
        for f in ("points", "faces", "owner", "neighbour", "boundary"):
            (ws / "constant" / "polyMesh" / f).write_text("x\n")
        return _Done()

    monkeypatch.setattr(N, "run_guarded", fake_run)
    monkeypatch.setattr(N, "_foam_version", lambda *a, **k: "v2412")
    monkeypatch.setattr(N, "_rank_count", lambda: 1)          # the serial branch
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)

    N._run_snappy_local(ws)
    assert any("snappyHexMesh" in c for c in calls), calls
    assert not any("createPatch" in c for c in calls), "merged with nothing to merge"

    (ws / "system" / "createPatchDict").write_text("patches ();\n")
    calls.clear()
    N._run_snappy_local(ws)
    order = [c.split()[0] if "createPatch" not in c else "createPatch" for c in calls]
    assert "createPatch" in order
    assert order.index("createPatch") > max(i for i, c in enumerate(calls) if "snappyHexMesh" in c), \
        "createPatch must run AFTER snappyHexMesh - it merges the patches the layers needed"
