# A gmsh mesh delivered for FLOW is judged the way a CFD solver integrates it - over faces - and a
# solid for FEA keeps the 0.1 SICN floor. On the lab's 13 gmsh meshes (2026-10-05) the 0.1 floor
# refused a volute at SICN 0.074 whose worst face was 80.2 degrees (10 faces over 70) while
# passing a wing at 0.110 whose worst face was 81.0 (154 over 70).
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.gmsh import gates as G
from meshpipeline.engines.gmsh.driver import CFD_MAX_NON_ORTHO, CFD_SICN_FLOOR
from meshpipeline.engines.gmsh.face_quality import face_quality
from meshpipeline.engines.gmsh.gmsh_runner import SICN_FLOOR, quality_shortfalls


def _cube_tets(n=3):
    """A unit cube of n^3 sub-cubes, each split into 6 tets (Kuhn): a valid, well-shaped mesh."""
    g = np.linspace(0.0, 1.0, n + 1)
    X = np.array([[x, y, z] for z in g for y in g for x in g])

    def idx(i, j, k):
        return i + (n + 1) * (j + (n + 1) * k)
    kuhn = [(0, 1, 3, 7), (0, 1, 5, 7), (0, 2, 3, 7), (0, 2, 6, 7), (0, 4, 5, 7), (0, 4, 6, 7)]
    T = []
    for k in range(n):
        for j in range(n):
            for i in range(n):
                c = [idx(i + a, j + b, k + cc) for cc in (0, 1) for b in (0, 1) for a in (0, 1)]
                T += [[c[p] for p in t] for t in kuhn]
    return X, np.asarray(T, dtype=np.int64)


def test_a_clean_tet_mesh_measures_as_checkmesh_would():
    X, T = _cube_tets()
    q = face_quality(X, T)
    # each Kuhn cube has 6 tets; every internal face is shared once, every boundary face once
    assert q["internal_faces"] * 2 + q["boundary_faces"] == 4 * len(T)
    assert q["boundary_faces"] == 6 * 9 * 2               # 9 squares per side, 2 triangles each
    assert q["max_non_ortho"] < 60.0 and q["non_ortho_severe_faces"] == 0
    assert q["max_skewness"] < 1.0 and q["skew_faces"] == 0


def test_a_flattened_cell_reads_as_severely_non_orthogonal():
    # two tets on one face; the second is a sliver lying almost in the face's plane, far to its
    # side, so the line between the centres runs nearly along the face
    X = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0.3, 0.3, 1.0], [20.0, 20.0, -0.02]], float)
    q = face_quality(X, np.array([[0, 1, 2, 3], [0, 2, 1, 4]]))
    assert q["internal_faces"] == 1 and q["boundary_faces"] == 6
    assert q["max_non_ortho"] > 85.0 and q["non_ortho_severe_faces"] == 1
    sound = face_quality(X[[0, 1, 2, 3]].tolist() + [[0.3, 0.3, -1.0]],
                         np.array([[0, 1, 2, 3], [0, 2, 1, 4]]))
    assert sound["max_non_ortho"] < 1.0


def test_an_empty_mesh_measures_nothing():
    assert face_quality(np.zeros((0, 3)), np.zeros((0, 4), dtype=np.int64)) == {}


@pytest.mark.parametrize("q,misses", [
    # flow meshes: the lab's volute (in), a sliver at a sharp edge (out), a face past 85 (out)
    ({"quality_bar": "cfd", "min_sicn": 0.0743, "max_non_ortho": 80.19}, []),
    ({"quality_bar": "cfd", "min_sicn": 0.0033, "max_non_ortho": 89.79}, ["min_sicn", "max_non_ortho"]),
    ({"quality_bar": "cfd", "min_sicn": 0.0109, "max_non_ortho": 86.74}, ["max_non_ortho"]),
    # a solid keeps the FEA floor, and a report that does not say is a solid
    ({"quality_bar": "fea", "min_sicn": 0.0743}, ["min_sicn"]),
    ({"min_sicn": 0.0743, "max_non_ortho": 10.0}, ["min_sicn"]),
    ({"min_sicn": 0.2}, []),
])
def test_the_bar_follows_what_the_mesh_is_for(q, misses):
    assert [m["key"] for m in quality_shortfalls(q)] == misses
    assert CFD_SICN_FLOOR < SICN_FLOOR and CFD_MAX_NON_ORTHO == 85.0


def _gate(tmp_path, quality):
    import json
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({"quality": quality}))
    return G._gate_sicn_floor(GateCtx(workspace=tmp_path, engine="gmsh"))


def test_the_gate_lets_a_sound_flow_mesh_through_and_names_the_failing_bar(tmp_path):
    ok, _ = _gate(tmp_path, {"quality_bar": "cfd", "min_sicn": 0.0743, "max_non_ortho": 80.2})
    assert ok
    ok, why = _gate(tmp_path, {"quality_bar": "cfd", "min_sicn": 0.0109, "max_non_ortho": 86.7})
    assert not ok and "non-orthogonality" in why
    ok, why = _gate(tmp_path, {"quality_bar": "fea", "min_sicn": 0.0743})
    assert not ok and "SICN" in why
