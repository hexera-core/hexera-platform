# Responsibility: Verify a half model's symmetry plane is detected, clamped, and rendered under the user's patch name.
from __future__ import annotations

from meshpipeline.engines.snappy import snappy_runner as R  # noqa: E402

# a half-model: body flat on y=0, extending to +y (like the clipped half-CRM)
_ANALYSIS = {"bbox_min": [0.0, 0.0, -0.5], "bbox_max": [2.0, 0.8, 0.5], "L": 2.0}
_REC = {"base_cell": 0.1, "surface_level": [4, 4], "afford_level": 4, "feature_level": 5,
        "distance_bands": [(0.2, 3), (0.8, 2)], "resolve_feature_angle": 30}
_SYM = {"axis": 1, "pos": 0.0, "side": "min", "name": "symmetry"}


def test_box_face_map_is_consistent_with_the_farfield_list():
    assert set(R._BOX_FACES.values()) == set(R._ALL_BOX_FACES)
    assert len(R._BOX_FACES) == 6 == len(set(R._ALL_BOX_FACES))


def test_domain_clamps_the_symmetry_face_to_the_plane_keeps_other_margins():
    dmin, dmax = R.domain_from_strategy(_ANALYSIS, {}, _SYM)
    assert dmin[1] == 0.0, "the y-min face must sit ON the symmetry plane (no margin)"
    # every other face keeps its far-field margin
    assert dmin[0] < _ANALYSIS["bbox_min"][0] and dmax[0] > _ANALYSIS["bbox_max"][0]
    assert dmax[1] > _ANALYSIS["bbox_max"][1]
    assert dmin[2] < _ANALYSIS["bbox_min"][2] and dmax[2] > _ANALYSIS["bbox_max"][2]


def test_domain_without_symmetry_is_unchanged():
    a, b = R.domain_from_strategy(_ANALYSIS, {})
    assert a[1] < 0.0   # full far-field margin on the y-min face, no clamp


def _render(tmp_path, symmetry):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    dmin, dmax = R.domain_from_strategy(_ANALYSIS, {}, symmetry)
    return R.render_snappy_case(
        tmp_path, surface_name="aircraft", feature_file="aircraft.eMesh", analysis=_ANALYSIS,
        recommendation=_REC, domain_min=dmin, domain_max=dmax, strategy={},
        dimensionality="3D", symmetry=symmetry)


def test_render_emits_a_symmetryPlane_patch_under_the_declared_name(tmp_path):
    _render(tmp_path, _SYM)
    bm = (tmp_path / "system" / "blockMeshDict").read_text()
    assert "symmetry { type symmetryPlane; faces ((0 1 5 4)); }" in bm  # y-min face
    assert "farfield" in bm


def test_render_removes_the_symmetry_face_from_farfield(tmp_path):
    _render(tmp_path, _SYM)
    bm = (tmp_path / "system" / "blockMeshDict").read_text()
    import re
    ff = re.search(r"farfield \{ type patch; faces \((.*?)\); \}", bm).group(1)
    faces = re.findall(r"\([0-9 ]+\)", ff)
    assert len(faces) == 5, f"farfield must have 5 faces after symmetry is pulled out: {faces}"
    assert "(0 1 5 4)" not in faces   # the y-min face is now the symmetry patch


def test_render_honours_the_users_patch_name(tmp_path):
    sym = {**_SYM, "name": "centreplane"}
    _render(tmp_path, sym)
    bm = (tmp_path / "system" / "blockMeshDict").read_text()
    assert "centreplane { type symmetryPlane;" in bm


def test_3d_without_symmetry_is_the_all_farfield_box(tmp_path):
    _render(tmp_path, None)
    bm = (tmp_path / "system" / "blockMeshDict").read_text()
    assert "symmetryPlane" not in bm
    import re
    ff = re.search(r"farfield \{ type patch; faces \((.*?)\); \}", bm).group(1)
    assert len(re.findall(r"\([0-9 ]+\)", ff)) == 6   # all six faces


def test_locationInMesh_is_on_the_fluid_side_of_the_plane(tmp_path):
    summary = _render(tmp_path, _SYM)
    loc = summary["location_in_mesh"]
    assert loc[1] > 0.0, "the mesh seed point must be in the fluid (y>0), not on/under the plane"


def test_snappy_declares_symmetry_capability():
    from meshpipeline.engines.registry import get_spec
    assert get_spec("snappy").supports_symmetry_plane is True


# plane detection: a half-model sits on the centreline, a full model straddles it
def test_detects_the_plane_for_a_half_model_on_the_positive_side():
    a = {"bbox_min": [0.06, 0.0, -0.019], "bbox_max": [1.76, 0.795, 0.235], "L": 1.74}
    sym = R.detect_symmetry_plane(a, "symmetry")
    assert sym == {"axis": 1, "pos": 0.0, "side": "min", "name": "symmetry"}


def test_detects_the_plane_for_a_half_model_on_the_negative_side():
    a = {"bbox_min": [0.06, -0.795, -0.019], "bbox_max": [1.76, 0.0, 0.235], "L": 1.74}
    sym = R.detect_symmetry_plane(a, "symmetry")
    assert sym == {"axis": 1, "pos": 0.0, "side": "max", "name": "symmetry"}


def test_full_span_body_has_no_symmetry_plane():
    a = {"bbox_min": [0.06, -0.795, -0.02], "bbox_max": [1.8, 0.795, 0.24], "L": 1.74}
    assert R.detect_symmetry_plane(a, "symmetry") is None


def test_a_belly_that_dips_near_zero_is_not_mistaken_for_a_cut():
    a = {"bbox_min": [0.06, 0.0, -0.019], "bbox_max": [1.76, 0.795, 0.235], "L": 1.74}
    assert R.detect_symmetry_plane(a, "symmetry")["axis"] == 1   # y, not z


def test_no_declared_symmetry_name_means_no_plane():
    a = {"bbox_min": [0.0, 0.0, -0.5], "bbox_max": [2.0, 0.8, 0.5], "L": 2.0}
    assert R.detect_symmetry_plane(a, "") is None


def test_detection_picks_up_the_users_patch_name():
    a = {"bbox_min": [0.0, 0.0, -0.5], "bbox_max": [2.0, 0.8, 0.5], "L": 2.0}
    assert R.detect_symmetry_plane(a, "centreplane")["name"] == "centreplane"
