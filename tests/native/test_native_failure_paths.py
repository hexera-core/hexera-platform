# Responsibility: Verify a native failure is contained - an unknown engine, a missing spec, a poisoned dict, a crash.
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.engines.dispatch import run_engine_local

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "geometry"


def test_an_unknown_engine_fails_loudly_with_no_cfmesh_fallback(tmp_path, canonical_provenance):
    res = run_engine_local(tmp_path, engine="not_a_real_engine", timeout=30)
    assert res["rc"] == -4 and "unknown engine" in res["log_tail"]
    assert not (tmp_path / "constant" / "polyMesh").exists()


def test_vmtk_without_a_spec_fails_cleanly(tmp_path, canonical_provenance):
    res = run_engine_local(tmp_path, engine="vmtk", timeout=60)
    assert res["rc"] != 0 and not (tmp_path / "mesh.vtu").exists()


def test_snappy_rejects_a_malicious_case_dict_before_executing(tmp_path, canonical_provenance):
    (tmp_path / "system").mkdir(parents=True)
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    (tmp_path / "system" / "blockMeshDict").write_text(
        'FoamFile{ version 2.0; format ascii; class dictionary; object blockMeshDict; }\n'
        'evilcode #codeStream { code #{ system("touch /tmp/pwned"); #}; };\n')
    (tmp_path / "system" / "snappyHexMeshDict").write_text(
        'FoamFile{ version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }\n')
    res = run_engine_local(tmp_path, engine="snappy", timeout=60)
    assert res["rc"] == -2 and "REJECTED" in res["log_tail"]
    assert not Path("/tmp/pwned").exists()  # noqa: S108 - asserting the shell escape did NOT run


def test_multiregion_rejects_a_malicious_case_dict_before_executing(tmp_path, canonical_provenance):
    (tmp_path / "system").mkdir(parents=True)
    (tmp_path / ".regions.json").write_text(json.dumps(
        [{"name": "fluid", "type": "fluid", "solids": [0]}]))
    (tmp_path / "system" / "snappyHexMeshDict").write_text(
        'FoamFile{ version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }\n'
        'x #codeStream { code #{ system("id"); #}; };\n')
    res = run_engine_local(tmp_path, engine="snappy_multiregion", timeout=60)
    assert res["rc"] != 0 and ("rejected" in res["log_tail"].lower()
                               or "REJECTED" in res["log_tail"])


def test_a_crashing_out_of_process_mesher_is_contained_as_a_clean_nonzero(tmp_path, canonical_provenance):
    import subprocess

    from meshpipeline.sandbox.safe_exec import run_guarded
    proc = run_guarded(["python", "-c", "import ctypes; ctypes.string_at(0)"],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0                      # the crash surfaced as a nonzero exit …
    assert proc.returncode < 0 or proc.returncode >= 128   # … carrying the fatal signal
    # and the parent (this test process) is alive to make the assertion - containment held.
    assert subprocess is not None


# a vmtk run that exits 0 having produced no volume
#
# vmtk can exit 0, write a syntactically complete .vtu, and still have skipped the volume fill -
# the runner calls this the swallowed TetGen failure. check_mesh exists to classify exactly that,
# and it used to CRASH on it: VTK's cell-quality filter segfaults on an empty tetrahedral grid,
# and a segfault is not a Python exception, so the best-effort `except` around the quality block
# could not catch it. The validator took the worker down instead of returning its verdict.
#
# check_mesh is therefore called in a SUBPROCESS here. A regression is a SIGSEGV, which no
# in-process assertion could survive to report.
import subprocess
import sys
import textwrap


def _vtu_surface_only(path) -> None:
    import pyvista as pv
    tri = pv.Triangle([[0.0, 0.0, 0.0], [1e-3, 0.0, 0.0], [0.0, 1e-3, 0.0]])
    pv.UnstructuredGrid(tri.cast_to_unstructured_grid()).save(str(path))


def _vtu_with_tetrahedra(path) -> None:
    import numpy as np
    import pyvista as pv
    pts = np.array([[0.0, 0.0, 0.0], [1e-3, 0.0, 0.0], [0.0, 1e-3, 0.0], [0.0, 0.0, 1e-3]])
    grid = pv.UnstructuredGrid({pv.CellType.TETRA: np.array([[0, 1, 2, 3]])}, pts)
    grid.save(str(path))


def _check_mesh_in_subprocess(ws) -> dict:
    src = textwrap.dedent("""
        import json, sys
        import meshpipeline.engines.vmtk.vmtk_runner as R
        print("@@" + json.dumps(R.check_mesh(sys.argv[1])))
    """)
    done = subprocess.run([sys.executable, "-c", src, str(ws)], capture_output=True, text=True)
    assert done.returncode == 0, (
        f"check_mesh terminated with {done.returncode} instead of returning a verdict "
        f"(139 = SIGSEGV on the empty tetrahedral grid):\n{done.stderr[-1500:]}")
    line = [x for x in done.stdout.splitlines() if x.startswith("@@")]
    assert line, done.stdout
    return json.loads(line[0][2:])


def test_a_surface_only_vmtk_artifact_is_rejected_without_crashing(tmp_path):
    _vtu_surface_only(tmp_path / "mesh.vtu")
    q = _check_mesh_in_subprocess(tmp_path)
    assert q["cells"] == 0, q
    assert q["mesh_ok"] is False, q
    assert q["fatal"], "a tet-less mesh produced no fatal diagnosis"
    assert any("no tetrahedra" in f for f in q["fatal"]), q["fatal"]
    assert q["min_quality"] is None, "quality was invented for a mesh with no volume cells"


def test_the_reported_cell_count_is_tetrahedra_not_surface_cells(tmp_path):
    # The artifact carries surface cells; the verdict must count VOLUME cells, or a surface-only
    # result would read as a mesh.
    _vtu_surface_only(tmp_path / "mesh.vtu")
    import pyvista as pv
    assert pv.read(str(tmp_path / "mesh.vtu")).n_cells > 0, "the fixture has no cells at all"
    assert _check_mesh_in_subprocess(tmp_path)["cells"] == 0


def test_a_real_tetrahedral_mesh_still_reaches_ordinary_quality_validation(tmp_path):
    _vtu_with_tetrahedra(tmp_path / "mesh.vtu")
    q = _check_mesh_in_subprocess(tmp_path)
    assert q["cells"] == 1, q
    assert q["min_quality"] is not None, "the quality path was skipped for a valid volume mesh"
    assert not [f for f in q["fatal"] if "no tetrahedra" in f], q["fatal"]


def test_a_truncated_artifact_remains_a_safe_structured_failure(tmp_path):
    _vtu_with_tetrahedra(tmp_path / "mesh.vtu")
    raw = (tmp_path / "mesh.vtu").read_bytes()
    (tmp_path / "mesh.vtu").write_bytes(raw[: len(raw) // 2])
    q = _check_mesh_in_subprocess(tmp_path)
    assert q["cells"] == 0 and q["mesh_ok"] is False, q
    assert q["fatal"], "a truncated artifact produced no diagnosis"


def test_rc_zero_with_no_volume_does_not_finalize_or_deliver(tmp_path, canonical_provenance):
    # The whole point: a zero exit status is not a mesh. Finalization must refuse, and nothing
    # may be left behind that a delivery step would treat as a completed result.
    from meshpipeline.engines.vmtk.vmtk_runner import finalize
    _vtu_surface_only(tmp_path / "mesh.vtu")
    assert _check_mesh_in_subprocess(tmp_path)["mesh_ok"] is False
    out = finalize(str(tmp_path), intake_patches=[], engine="vmtk", domain="tet-less",
                   internal_flow=True)
    assert out.get("success") is not True, out
    manifest = tmp_path / "mesh_manifest.json"
    if manifest.is_file():
        m = json.loads(manifest.read_text())
        # `mesh_written` is a file-existence fact - the surface-only mesh.vtu really is on disk -
        # so it is not the signal that a VOLUME was delivered. The volume claim is the cell count,
        # and it must be zero.
        assert m.get("cell_count") == 0, (
            f"a tet-less run published {m.get('cell_count')} cells: {m}")


def test_the_engine_gate_refuses_the_tet_less_result(tmp_path, canonical_provenance):
    from meshpipeline.engines.gates import GateCtx, run_gates
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.vmtk.vmtk_runner import finalize
    _vtu_surface_only(tmp_path / "mesh.vtu")
    finalize(str(tmp_path), intake_patches=[], engine="vmtk", domain="tet-less", internal_flow=True)
    ok, failed, _ = run_gates(get_spec("vmtk").gates,
                              GateCtx(workspace=tmp_path, engine="vmtk", domain="tet-less",
                                      intake_patches=[], engine_params={}))
    assert not ok, "the vmtk gates accepted a result with no tetrahedral volume"
    assert failed, "no gate named the refusal"


# a fully closed lumen is not vmtk's to mesh
#
# vmtk takes its centerline endpoints from the surface's real boundary loops. A sealed anatomy has
# none, and `pointlist` cannot supply them: it maps a coordinate to the nearest WALL VERTEX, and
# vmtkcenterlines caps only for the open-profile selectors. The engine used to accept such a
# surface, run for minutes and produce a triangle-only artifact. It now says so before starting.


def _closed_sphere_lumen(ws) -> None:
    import pyvista as pv
    pv.Sphere(radius=2.0, theta_resolution=40, phi_resolution=40).triangulate().save(
        str(Path(ws) / "lumen.vtp"))


def _open_tube_lumen(ws) -> None:
    # A tube with both ends cut open: exactly two boundary loops, one connected surface.
    import pyvista as pv
    tube = pv.Cylinder(radius=1.5, height=12.0, direction=(0, 0, 1), capping=False,
                       resolution=48).triangulate()
    tube.extract_surface().save(str(Path(ws) / "lumen.vtp"))


def test_a_closed_lumen_is_refused_before_any_native_stage(tmp_path):
    import meshpipeline.engines.vmtk.vmtk_runner as R
    _closed_sphere_lumen(tmp_path)
    assert R.inspect_stl(tmp_path)["n_open_profiles"] == 0, "the fixture is not actually closed"

    out = R.configure_mesh(tmp_path, strategy={"source_points": [0.0, 0.0, -2.0],
                                               "target_points": [0.0, 0.0, 2.0]})
    assert out.get("code") == "vmtk_requires_open_profiles", out
    assert "open inlet/outlet profiles" in out["error"]
    assert "pype" not in out, "a doomed native command was still constructed"
    assert not (tmp_path / "vmtk_spec.json").exists(), (
        "a spec was written for a surface the engine cannot mesh")
    for produced in ("centerlines.vtp", "mesh.vtu", "mesh_manifest.json"):
        assert not (tmp_path / produced).exists(), f"{produced} was produced for a closed lumen"


def test_the_closed_lumen_refusal_leaks_no_implementation_detail(tmp_path):
    import meshpipeline.engines.vmtk.vmtk_runner as R
    _closed_sphere_lumen(tmp_path)
    msg = R.configure_mesh(tmp_path, strategy={})["error"]
    for leak in ("vmtkcenterlines", "FindClosestPoint", "pointlist", "Traceback", "/usr/",
                 "/opt/", "TetGen", str(tmp_path)):
        assert leak not in msg, f"the public refusal mentions {leak!r}"


def test_the_seeding_contract_no_longer_promises_closed_lumens(tmp_path):
    # The message a user gets when seeds are missing must not advertise a mode the engine refuses.
    import meshpipeline.engines.vmtk.vmtk_runner as R
    try:
        R.build_pype(R.resolve_strategy({}))
    except ValueError as exc:
        text = str(exc)
    else:
        raise AssertionError("seeding without source/target points was accepted")
    assert "closed" not in text.lower(), f"the closed-lumen promise survives: {text}"
    assert "open" in text.lower(), "the message no longer names the open-profile requirement"


def test_an_open_profile_lumen_still_configures(tmp_path):
    import meshpipeline.engines.vmtk.vmtk_runner as R
    _open_tube_lumen(tmp_path)
    info = R.inspect_stl(tmp_path)
    assert info["n_open_profiles"] == 2, info
    out = R.configure_mesh(tmp_path, strategy={"source_ids": [0], "target_ids": [1]})
    assert "pype" in out and out.get("code") is None, out
    assert (tmp_path / "vmtk_spec.json").is_file(), "the accepted path wrote no spec"


def test_topology_is_measured_not_inferred_from_the_filename(tmp_path):
    # The same file name, two topologies, two outcomes - so nothing is being read off the path.
    import meshpipeline.engines.vmtk.vmtk_runner as R
    _closed_sphere_lumen(tmp_path)
    assert R.configure_mesh(tmp_path, strategy={}).get("code") == "vmtk_requires_open_profiles"
    _open_tube_lumen(tmp_path)
    assert R.configure_mesh(tmp_path, strategy={"source_ids": [0], "target_ids": [1]}).get(
        "code") is None
