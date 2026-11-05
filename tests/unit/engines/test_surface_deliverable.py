# Responsibility: Verify the delivered surface is the boundary the run produced, not the CAD it snapped to.
from __future__ import annotations

from pathlib import Path

import numpy as np

from meshpipeline.engines.surface_deliverable import SURFACE_MSH, build_surface_msh


def _boundary_dir(ws: Path) -> Path:
    d = ws / "VTK" / "case_0" / "boundary"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(path: Path, points, faces) -> None:
    import pyvista as pv
    pv.PolyData(np.asarray(points, dtype=float), np.asarray(faces, dtype=np.int64)).save(path)


def _quad_patch(path: Path, n: int) -> None:
    """n disjoint quads - a face count we can assert on exactly."""
    pts, faces = [], []
    for i in range(n):
        b = len(pts)
        pts += [[i, 0, 0], [i + 1, 0, 0], [i + 1, 1, 0], [i, 1, 0]]
        faces += [4, b, b + 1, b + 2, b + 3]
    _save(path, pts, faces)


def _read_groups(msh: Path) -> dict[str, int]:
    """Physical-group name -> element count, read back through gmsh itself."""
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(msh))
        out: dict[str, int] = {}
        for dim, tag in gmsh.model.getPhysicalGroups(2):
            name = gmsh.model.getPhysicalName(dim, tag)
            total = 0
            for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, tag):
                _, tags, _ = gmsh.model.mesh.getElements(2, int(ent))
                total += sum(len(t) for t in tags)
            out[name] = total
        return out
    finally:
        gmsh.finalize()


def test_the_delivered_surface_carries_the_faces_the_run_produced(tmp_path):
    # The bug this exists to prevent: the download carried the CAD the mesher snapped TO, so a
    # 3.3M-cell run shipped the customer's own 232-facet upload instead of the surface it made.
    d = _boundary_dir(tmp_path)
    _quad_patch(d / "airfoil.vtp", 500)
    _quad_patch(d / "farfield.vtp", 12)

    assert build_surface_msh(tmp_path) == SURFACE_MSH
    groups = _read_groups(tmp_path / SURFACE_MSH)
    assert groups == {"airfoil": 500, "farfield": 12}, groups


def test_a_patch_face_with_more_than_four_sides_is_split_rather_than_dropped(tmp_path):
    # Boundary faces can have more than four sides where refinement levels meet. Gmsh has no n-gon
    # element, so they must be split - but a hole in the delivered surface would be far worse.
    d = _boundary_dir(tmp_path)
    _save(d / "wall.vtp",
          [[0, 0, 0], [1, 0, 0], [1.5, 1, 0], [0.5, 2, 0], [-0.5, 1, 0]],
          [5, 0, 1, 2, 3, 4])

    assert build_surface_msh(tmp_path) == SURFACE_MSH
    # a pentagon fans into exactly three triangles; zero would mean the face was silently dropped
    assert _read_groups(tmp_path / SURFACE_MSH) == {"wall": 3}


def test_only_the_latest_time_directory_is_converted(tmp_path):
    # foamToVTK writes VTK/<case>_<time>/boundary/. Globbing the whole tree would collect every
    # step, and since a patch is named by file stem the same patch lands twice under one physical
    # name - a surface delivered doubled on top of itself. Only the newest describes the mesh.
    for step, count in ((0, 7), (10, 3), (9, 5)):
        d = tmp_path / "VTK" / f"case_{step}" / "boundary"
        d.mkdir(parents=True, exist_ok=True)
        _quad_patch(d / "airfoil.vtp", count)

    assert build_surface_msh(tmp_path) == SURFACE_MSH
    groups = _read_groups(tmp_path / SURFACE_MSH)
    # _10 is the latest, and numerically - a string sort would wrongly pick _9
    assert groups == {"airfoil": 3}, groups


def test_no_vtk_boundary_yields_no_file_rather_than_an_empty_one(tmp_path):
    # Non-fatal: the bundle still carries the solver mesh. An empty .msh offered as a download
    # would be worse than offering none.
    assert build_surface_msh(tmp_path) is None
    assert not (tmp_path / SURFACE_MSH).exists()


def test_the_uploader_prefers_the_produced_surface_over_the_snapped_to_cad(tmp_path):
    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.persistence.models import ArtifactType

    ws = build_workspace(tmp_path, "gmsh")
    (ws / "mesh.msh").write_text("THE-CAD-IT-SNAPPED-TO")
    (ws / SURFACE_MSH).write_text("THE-SURFACE-IT-PRODUCED")

    import uuid
    planned = au._build_plan(uuid.uuid4(), ws, [], "gmsh")
    mesh = [p for p in planned if p.artifact_type is ArtifactType.mesh]
    assert mesh, "the surface artifact must still be planned"
    assert mesh[0].local_path.read_text() == "THE-SURFACE-IT-PRODUCED", (
        "the uploader served the input CAD instead of the boundary the run generated")


def test_the_uploader_still_serves_the_review_mesh_when_no_surface_was_produced(tmp_path):
    # cfMesh/gmsh paths that emit no VTK boundary must keep working, not lose the artifact.
    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.persistence.models import ArtifactType

    ws = build_workspace(tmp_path, "gmsh")
    (ws / "mesh.msh").write_text("THE-ONLY-SURFACE-THERE-IS")

    import uuid
    planned = au._build_plan(uuid.uuid4(), ws, [], "gmsh")
    mesh = [p for p in planned if p.artifact_type is ArtifactType.mesh]
    assert mesh and mesh[0].local_path.read_text() == "THE-ONLY-SURFACE-THERE-IS"
