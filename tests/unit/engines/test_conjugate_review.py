"""A conjugate mesh opens in the reviewer, with the metal and the coolant as two named bodies.

Every engine in the catalog must declare a rendered artifact that is required, so there is no
metric-only route to a delivered mesh: a run passes the executor gates and then has to be LOOKED
at. Nothing in this bundle wrote a per-region surface, and the shared manifest writer hardcodes
mesh_paths.surface to <workspace>/mesh.msh - so a conjugate run would have passed every gate and
then raised EVIDENCE_MISSING before any renderer started. A live two-solid multi-region run did
exactly that, and snappy_multiregion's own comment records both the failure and the fix.

MEASURED IN THE MESH IMAGE on a real two-region unit_cell at 4 cells across:

    export_volume_vtk(all_regions=True, region="fluid")  -> VTK/fluid/review_0/internal.vtu
    build_region_msh                                     -> {fluid: [1,2,3], solid: [4..9]}
    bounding box                                          0.2 x 32.5 x 1.3 mm, the block exactly
    mesh.msh                                              35,655,089 bytes in 3.3 s
    read_mesh, the reviewer's own reader                  OK, physical groups
                                                          [(2,1,'fluid',(1,2,3)),
                                                           (2,2,'solid',(4,5,6,7,8,9))]

Two things that run taught, both of which had the builders returning empty on a mesh that was
there. foamToVTK names a time directory "<case>_<n>" and not "<n>", so parsing the whole name as a
number found no time directory at all. And a split case has MORE than one, so taking the first
alphabetically rather than the newest made the volume lookup and the export disagree about which
file described the delivered mesh.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import foam_exec
from meshpipeline.engines.cfmesh_conjugate._shared import (
    _INSPECTION_TARGETS,
    _RENDER_ARTIFACTS,
)
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION
from meshpipeline.engines.cfmesh_conjugate.review_surface import (
    REVIEW_MSH,
    _time_index,
    build_region_msh,
    review_volume,
)


def _square(path: Path, z: float = 0.0) -> None:
    """One two-triangle patch as a .vtp, written with pyvista so the reader is the real one."""
    import numpy as np
    import pyvista as pv
    pts = np.array([[0.0, 0.0, z], [1.0, 0.0, z], [1.0, 1.0, z], [0.0, 1.0, z]])
    faces = np.hstack([[3, 0, 1, 2], [3, 0, 2, 3]])
    path.parent.mkdir(parents=True, exist_ok=True)
    pv.PolyData(pts, faces).save(str(path))


def _quad(path: Path, z: float = 0.0) -> None:
    """One QUADRANGLE patch, which is what a real boundary mostly is."""
    import numpy as np
    import pyvista as pv
    pts = np.array([[0.0, 0.0, z], [1.0, 0.0, z], [1.0, 1.0, z], [0.0, 1.0, z]])
    path.parent.mkdir(parents=True, exist_ok=True)
    pv.PolyData(pts, np.hstack([[4, 0, 1, 2, 3]])).save(str(path))


@pytest.fixture
def case(tmp_path: Path) -> Path:
    """A workspace with both regions' VTK boundaries, in foamToVTK's own layout."""
    pytest.importorskip("pyvista", reason="pyvista is not available")
    for region, z in ((FLUID_REGION, 0.0), (SOLID_REGION, 1.0)):
        base = tmp_path / "VTK" / region / f"{tmp_path.name}_0" / "boundary"
        _square(base / "coolant_in.vtp", z)
        _quad(base / "wall.vtp", z)
    _square(tmp_path / "VTK" / FLUID_REGION / f"{tmp_path.name}_0" / "boundary"
            / "fluid_to_solid.vtp")
    _square(tmp_path / "VTK" / SOLID_REGION / f"{tmp_path.name}_0" / "boundary"
            / "solid_to_fluid.vtp")
    return tmp_path


# #
# which time directory is the delivered one
# #
@pytest.mark.parametrize(("name", "want"), [
    ("review_0", 0), ("review_1", 1), ("review_10", 10), ("case_9", 9),
    ("0", 0), ("constant", -1), ("", -1),  # a bare numeric name is a valid index too
])
def test_a_time_directory_is_named_case_then_index(name, want):
    # Parsing the whole name as a number is what found no time directory at all on a real run.
    assert _time_index(name) == want


def test_ten_sorts_after_nine():
    assert _time_index("review_10") > _time_index("review_9")


# #
# the msh both regions open from
# #
def test_both_regions_become_one_named_group_each(case):
    groups, box = build_region_msh(case)
    assert set(groups) == {FLUID_REGION, SOLID_REGION}, groups
    assert box is not None and len(box) == 6


def test_the_reviewers_own_reader_opens_it(case):
    """The assertion that matters: not that a file was written, but that the reader the review
    scene calls can load it and see the two bodies."""
    from meshpipeline.sandbox.mesh_reader import read_mesh
    build_region_msh(case)
    msh = case / REVIEW_MSH
    assert msh.is_file() and msh.stat().st_size > 0
    loaded = read_mesh(str(msh), "m")
    named = {g[2] for g in loaded.physical_groups}
    assert named == {FLUID_REGION, SOLID_REGION}, loaded.physical_groups


def test_a_quadrangle_is_split_rather_than_written_as_one(case):
    """Not aesthetic. sandbox/mesh_reader prefixes each face block with a corner count and then
    vstacks the blocks, so one group holding both quads and triangles raises ValueError and the
    review never opens. Measured: the mixed file is 28,781,401 bytes and FAILS to read; all
    triangles is 35,655,089 and loads in 3.3 s."""
    from meshpipeline.sandbox.mesh_reader import read_mesh
    build_region_msh(case)
    read_mesh(str(case / REVIEW_MSH), "m")       # the quad patch is in the fixture


def test_the_solid_side_of_the_interface_is_not_carried_twice(case):
    """It is the same surface as the fluid side - measured at 93,600 faces on both - so carrying
    both doubles the interface geometry for a view the reviewer already has."""
    from meshpipeline.engines.cfmesh_conjugate.review_surface import _patch_files
    solid = [p.stem for p in _patch_files(case, SOLID_REGION)]
    assert "solid_to_fluid" not in solid, solid
    fluid = [p.stem for p in _patch_files(case, FLUID_REGION)]
    assert "fluid_to_solid" in fluid, "the coolant's side of the interface IS carried"


def test_no_vtk_at_all_is_not_a_failure(tmp_path):
    # A review aid never fails a mesh.
    assert build_region_msh(tmp_path) == ({}, None)
    assert not (tmp_path / REVIEW_MSH).exists()


# #
# the coolant's volume, never the merged block
# #
def test_the_volume_is_the_coolant_regions(case):
    vol = case / "VTK" / FLUID_REGION / f"{case.name}_0" / "internal.vtu"
    vol.write_bytes(b"x")
    assert review_volume(case) == f"VTK/{FLUID_REGION}/{case.name}_0/internal.vtu"


def test_a_merged_export_is_never_returned(case):
    """VTK/case_0/internal.vtu is the whole block as ONE body, which is the very thing a conjugate
    review exists to tell a real split apart from."""
    (case / "VTK" / "case_0").mkdir(parents=True, exist_ok=True)
    (case / "VTK" / "case_0" / "internal.vtu").write_bytes(b"x")
    assert review_volume(case) is None, "no fluid export yet, so nothing may be offered"
    vol = case / "VTK" / FLUID_REGION / f"{case.name}_0" / "internal.vtu"
    vol.write_bytes(b"x")
    got = review_volume(case)
    assert got is not None and "case_0" not in got, got


def test_the_newest_export_wins_not_the_first(case):
    for n in (0, 1):
        d = case / "VTK" / FLUID_REGION / f"{case.name}_{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "internal.vtu").write_bytes(b"x")
    assert review_volume(case).endswith(f"{case.name}_1/internal.vtu")


def test_the_export_and_the_lookup_agree_on_which_file(case, monkeypatch):
    """They did not: the export returned review_0 while the lookup returned review_1, so the two
    disagreed about which file described the delivered mesh."""
    for n in (0, 1):
        d = case / "VTK" / FLUID_REGION / f"{case.name}_{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "internal.vtu").write_bytes(b"x")
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(foam_exec, "run_guarded", lambda *a, **k: None)
    exported = foam_exec.export_volume_vtk(case, all_regions=True, region=FLUID_REGION)
    assert exported == review_volume(case), (exported, review_volume(case))


def test_an_export_that_fails_is_none_not_a_raise(case, monkeypatch):
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")

    def boom(*a, **k):
        raise RuntimeError("foamToVTK is not here")

    monkeypatch.setattr(foam_exec, "run_guarded", boom)
    assert foam_exec.export_volume_vtk(case, all_regions=True, region=FLUID_REGION) is None


def test_a_refused_case_is_not_exported(case, monkeypatch):
    ran: list = []
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "a dict names a file outside")
    monkeypatch.setattr(foam_exec, "run_guarded", lambda *a, **k: ran.append(1))
    assert foam_exec.export_volume_vtk(case, all_regions=True) is None
    assert ran == [], "the guard runs before foamToVTK, here as everywhere"


# #
# what the engine declares it needs looked at
# #
def test_the_surface_is_required_and_the_volume_is_not():
    by_key = {a.artifact_key: a for a in _RENDER_ARTIFACTS}
    assert by_key["mesh_paths.surface"].required is True
    assert by_key["mesh_paths.volume"].required is False, (
        "required would end the review before the model was asked, on a mesh that passed every "
        "gate - the defect base.py's own comment exists to prevent")


def test_both_patches_and_region_slices_must_be_walked():
    by_id = {t.target_id: t for t in _INSPECTION_TARGETS}
    assert by_id["patch:*"].required is True
    assert by_id["region:*"].required is True


def test_the_renderer_asks_for_exactly_what_the_engine_declares():
    from meshpipeline.engines.cfmesh_conjugate.review_renderer import RENDERER
    assert tuple(RENDERER.required_artifacts()) == _RENDER_ARTIFACTS
    assert [a.artifact_key for a in RENDERER.required_artifacts()][0] == "mesh_paths.surface", (
        "the surface comes first: it is the one that must render")
