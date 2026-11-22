# Responsibility: Verify that what checkMesh v2412 calls invalid is invalid in every OpenFOAM-family engine (VER-1, SW-1):
# zero-area faces and non-orthogonality errors (the two the engines missed on delivered board meshes) and the rest of
# checkMesh's validity failures land in quality["fatal"]; skewness and other quality failures do not; "... OK." lines never do.
# No OpenFOAM runs: checkMesh's own text from the delivered manifold mesh (board batch 3) is fed to each engine's reader.
from __future__ import annotations

import importlib

import pytest

from meshpipeline.engines.checkmesh_validity import failed_lines, fatal_defects

#: checkMesh v2412 on the cfMesh manifold the board's batch 3 got delivered (1,059,052 cells), its own lines
_INVALID = """Check mesh...
Mesh stats
    points:           1170376
    faces:            3287547
    internal faces:   3088767
    cells:            1059052
    faces per cell:   6.02078
Checking topology...
    Boundary definition OK.
    Cell to face addressing OK.
    Point usage OK.
    Upper triangular ordering OK.
    Face vertices OK.
    Number of regions: 1 (OK).
Checking geometry...
    Overall domain bounding box (-0.018023 -0.018032 -0.0030048) (0.0180366 0.0180327 0.126504)
    Boundary openness (2.31398e-16 4.64219e-16 -2.38329e-16) OK.
    Max cell openness = 1.23229e-14 OK.
    Max aspect ratio = 47.6714 OK.
 ***Zero or negative face area detected.  Minimum area: 0
  <<Writing 2 zero area faces to set zeroAreaFaces
    Min volume = 1.19852e-19. Max volume = 1.42593e-10.  Total volume = 3.77688e-05.  Cell volumes OK.
    Mesh non-orthogonality Max: 118.682 average: 8.2824
   *Number of severely non-orthogonal (> 70 degrees) faces: 109.
 ***Number of non-orthogonality errors: 2.
  <<Writing 111 non-orthogonal faces to set nonOrthoFaces
    Face pyramids OK.
 ***Max skewness = 6.08027, 2 highly skew faces detected which may impair the quality of the results
  <<Writing 2 skew faces to set skewFaces
    Coupled point location match (average 0) OK.

Failed 3 mesh checks.

End
"""

#: the same mesh with only the quality failure left: skewness is quality, never validity
_SKEW_ONLY = "\n".join(ln for ln in _INVALID.splitlines()
                       if "Zero or negative face area" not in ln and "non-orthogonality errors" not in ln)


def test_the_delivered_manifold_is_invalid_by_checkmesh_s_own_lines():
    assert fatal_defects(_INVALID) == ["zero-area faces", "faces 90 degrees or more out of square"]
    assert len(failed_lines(_INVALID)) == 3


def test_skewness_alone_is_quality_and_ok_lines_are_never_defects():
    # "Upper triangular ordering OK.", "Point usage OK.", "Face pyramids OK." name checks that passed
    assert fatal_defects(_SKEW_ONLY) == []


@pytest.mark.parametrize("line, label", [
    (" ***Zero or negative cell volume detected.  Minimum negative volume: -1e-15, Number of negative volume "
     "cells: 3", "negative-volume cells"),
    (" ***Error in face pyramids: 4 faces are incorrectly oriented.", "incorrectly oriented faces"),
    (" ***Open cells found, max cell openness: 1, number of open cells 2", "open cells"),
    (" ***Faces not in upper triangular order.", "faces out of upper-triangular order"),
    (" ***Unused points found in the mesh, number unused by faces: 6 number unused by cells: 6",
     "unused points"),
    (" ***Zero or negative face area detected.  Minimum area: 0", "zero-area faces"),
    (" ***Number of non-orthogonality errors: 5.", "faces 90 degrees or more out of square"),
])
def test_every_checkmesh_validity_failure_is_fatal(line, label):
    assert label in fatal_defects("Checking geometry...\n" + line + "\nFailed 1 mesh checks.\n")


@pytest.mark.parametrize("line", [
    " ***Max skewness = 51.1603, 6 highly skew faces detected which may impair the quality of the results",
    " ***High aspect ratio cells found, Max aspect ratio: 1520, number of cells 4",
    " ***Cells with small determinant (< 0.001) found, number of cells: 12",
    " ***Faces with small interpolation weight (< 0.05) found, number of faces: 3",
])
def test_checkmesh_quality_failures_are_not_validity(line):
    assert fatal_defects("Checking geometry...\n" + line + "\nFailed 1 mesh checks.\n") == []


class _Proc:
    def __init__(self, out: str):
        self.stdout, self.stderr, self.returncode = out, "", 0


@pytest.mark.parametrize("engine", ["snappy", "cfmesh", "netgen", "snappy_multiregion"])
def test_each_openfoam_engine_reads_the_delivered_manifold_as_invalid(engine, tmp_path, monkeypatch):
    fx = importlib.import_module(f"meshpipeline.engines.{engine}.foam_exec")
    monkeypatch.setattr(fx, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(fx, "run_guarded", lambda *a, **k: _Proc(_INVALID))
    q = fx.check_mesh(tmp_path)
    assert q["cells"] == 1059052 and q["max_non_ortho"] == 118.682
    assert q["fatal"] == ["zero-area faces", "faces 90 degrees or more out of square"]


def test_snapgrid_reads_the_delivered_manifold_as_invalid():
    from meshpipeline.engines.snapgrid.native import parse_check_mesh
    assert parse_check_mesh(_INVALID)["fatal"] == ["zero-area faces", "faces 90 degrees or more out of square"]


def test_the_cfmesh_rescue_never_keeps_such_a_mesh_as_valid():
    # the manifold's rescue kept this mesh over two broken ones as "no fatal defect"
    from meshpipeline.engines.cfmesh import rescue as RS
    as_read = {"cells": 1059052, "max_non_ortho": 118.682, "fatal": fatal_defects(_INVALID)}
    assert RS.near_edge(as_read).startswith("checkMesh found zero-area faces")
    # ranked by the number of fatal defects (ea3caae9): behind any valid mesh, however skewed
    assert RS.rank(as_read)[0] == 2
    assert RS.rank(as_read) > RS.rank({"cells": 1059052, "max_non_ortho": 89.0, "fatal": []})
