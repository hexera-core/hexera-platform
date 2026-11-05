"""Symmetry resolution: a swept slab has two ends; a bbox touching zero is not a cut plane."""
import pytest

from meshpipeline.engines.snappy import snappy_runner as R


def _analysis(*, bbox_min, bbox_max, caps):
    ext = [bbox_max[i] - bbox_min[i] for i in range(3)]
    return {"bbox_min": list(bbox_min), "bbox_max": list(bbox_max),
            "extent": ext, "L": max(ext), "diag": sum(e * e for e in ext) ** 0.5,
            "axis_caps": [{"min": c[0], "max": c[1]} for c in caps]}


# a NACA 0012 swept along Z: real flat caps at both Z ends, a tangency band on Y, nothing on X
_SLAB = _analysis(bbox_min=(0.0011, -0.0598, -2.0), bbox_max=(1.0011, 0.0598, 0.0),
                  caps=[(0.001, 0.001), (0.0188, 0.0167), (0.0193, 0.0193)])


def test_slab_symmetry_picks_the_swept_axis():
    s = R.detect_slab_symmetry(_SLAB, "symmetry_front", "symmetry_back")
    assert s is not None and s["slab"] is True
    assert s["axis"] == 2, "the swept axis is Z - its two caps are equal, Y's only graze"
    assert (s["lo_name"], s["hi_name"]) == ("symmetry_front", "symmetry_back")


def test_slab_domain_is_not_padded_along_the_sweep():
    s = R.detect_slab_symmetry(_SLAB, "front", "back")
    dmin, dmax = R.domain_from_strategy(_SLAB, {}, s)
    assert (dmin[2], dmax[2]) == (-2.0, 0.0), "symmetry planes must sit ON the slab ends"
    for ax in (0, 1):
        assert dmin[ax] < _SLAB["bbox_min"][ax] and dmax[ax] > _SLAB["bbox_max"][ax]


def test_a_bbox_touching_zero_is_not_a_half_model():
    """The aerofoil starts at x=0.0011 with L=2, so abs(lo) < tol on X - but there is no face
    there. Calling it a cut plane clamped the domain to x>=0 and removed the upstream far field
    entirely, producing a clean mesh of a useless domain."""
    half = R.detect_symmetry_plane(_SLAB, "symmetry")
    assert half is None or half["axis"] != 0, "X has no cap - it cannot be a symmetry cut"


def test_a_real_cut_face_is_still_detected():
    cut = _analysis(bbox_min=(0.0, -1.0, -1.0), bbox_max=(2.0, 1.0, 1.0),
                    caps=[(0.20, 0.0), (0.0, 0.0), (0.0, 0.0)])
    half = R.detect_symmetry_plane(cut, "sym")
    assert half is not None and half["axis"] == 0 and half["side"] == "min"


def test_no_caps_means_no_slab():
    ball = _analysis(bbox_min=(-1, -1, -1), bbox_max=(1, 1, 1),
                     caps=[(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)])
    assert R.detect_slab_symmetry(ball, "a", "b") is None


def test_one_cap_is_not_a_slab():
    """A half-model has a single cut face. Two symmetry patches cannot be placed on it."""
    half_body = _analysis(bbox_min=(0.0, -1.0, -1.0), bbox_max=(2.0, 1.0, 1.0),
                          caps=[(0.20, 0.0), (0.0, 0.0), (0.0, 0.0)])
    assert R.detect_slab_symmetry(half_body, "a", "b") is None


# --- far-field margins land on the axes they are named for -------------------------------------

_BRIEF = {"domain_margin": {"up": 20.0, "down": 30.0, "side": 1.0, "vert": 20.0}}


def test_axis_roles_follow_the_geometry_not_the_index():
    """Chord on X, THICKNESS on Y, span on Z - the orientation that broke the old fixed mapping."""
    assert R.axis_roles(_SLAB, R.detect_slab_symmetry(_SLAB, "a", "b")) == (0, 1, 2)


def test_the_vertical_margin_reaches_the_vertical_axis():
    """A 20-chord brief used to leave 2 chords above and below: `vert` was applied to the span
    (where the slab clamp discarded it) and `side` to the vertical. Clean mesh, wrong lift."""
    slab = R.detect_slab_symmetry(_SLAB, "a", "b")
    dmin, dmax = R.domain_from_strategy(_SLAB, _BRIEF, slab)
    chord = _SLAB["extent"][0]
    below = (_SLAB["bbox_min"][1] - dmin[1]) / chord
    above = (dmax[1] - _SLAB["bbox_max"][1]) / chord
    assert below == pytest.approx(20.0, abs=0.1)
    assert above == pytest.approx(20.0, abs=0.1)


def test_margins_are_measured_in_chords_not_max_bbox_extent():
    """L = max(extent) is the SPAN here (2 m on a 1 m chord), which silently doubled every
    margin - 20 chords upstream became 40, and the background cells were billed for it."""
    slab = R.detect_slab_symmetry(_SLAB, "a", "b")
    dmin, dmax = R.domain_from_strategy(_SLAB, _BRIEF, slab)
    chord = _SLAB["extent"][0]
    assert (_SLAB["bbox_min"][0] - dmin[0]) / chord == pytest.approx(20.0, abs=0.1)
    assert (dmax[0] - _SLAB["bbox_max"][0]) / chord == pytest.approx(30.0, abs=0.1)


def test_the_slab_is_still_unpadded_along_its_sweep():
    slab = R.detect_slab_symmetry(_SLAB, "a", "b")
    dmin, dmax = R.domain_from_strategy(_SLAB, _BRIEF, slab)
    assert (dmin[2], dmax[2]) == (-2.0, 0.0)


def test_without_symmetry_the_historical_mapping_is_untouched():
    """The roles are only knowable when something else fixes the span axis. For a bare body the
    thinnest axis is as likely to be a wing's thickness as its span, so nothing is guessed."""
    body = _analysis(bbox_min=(0.0, 0.0, 0.0), bbox_max=(2.0, 1.0, 1.0),
                     caps=[(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)])
    assert R.axis_roles(body, None) is None
    dmin, dmax = R.domain_from_strategy(body, _BRIEF, None)
    L = body["L"]
    assert dmin[0] == pytest.approx(0.0 - 20.0 * L)
    assert dmax[0] == pytest.approx(2.0 + 30.0 * L)
