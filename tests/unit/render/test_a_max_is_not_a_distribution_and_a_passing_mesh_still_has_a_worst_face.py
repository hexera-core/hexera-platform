"""A max is not a distribution, and a mesh that passes still has a worst face.

Every metric on every conjugate mesh delivered so far clears its bar - non-orthogonality 48.39
against 65, skewness 1.6455 against 20, aspect ratio 3.76 against 1000 - so three maxima say "fine"
three times and a reader learns nothing about the mesh they were sent. Two measured examples from
the delivered plate, same verdict and two different meshes:

    fluid non_ortho   median 0   p99.9 41.73     max 46.91    max is 1.1x the 99.9th
    chip  non_ortho   median 0   p99.9 3.85e-05  max 0.0024   max is 62.7x the 99.9th

On the coolant the worst face stands in a population of sixteen thousand like it, the voxel
staircase; on the chip it is one stray. `max / p99.9` is what tells those apart, and it is the
judgement a mesh engineer makes before deciding whether a mesh needs work.

The second half of this file is the hotspots. `_sidecar_hotspots` took the faces over the bar and
returned an empty list when there were none, which is every metric of every mesh above - so the
viewer's "Worst" button, whose only job is to fly a reader to the worst place, had nowhere to go on
a good mesh, and the path on a bad one was never once exercised in a browser. A face under the bar
is still the worst face there is.

Nothing here re-measures the mesh. These numbers all ride on the one pass
`quality_fields_from_sidecar` already makes per metric to take its max.
"""
from __future__ import annotations

import numpy as np
import pytest
from tests.foam_fixtures import write_quality_fields, write_row_of_hexes

from meshpipeline.render import boundary_index as B
from meshpipeline.render import boundary_quality as Q

#: The fixture mesh: three unit hexes, 14 boundary faces as inlet(1) + outlet(1) + wall(12).
N_BOUNDARY = 14

#: A distinct value per cell, so a wrong owner lookup cannot coincide with a right one. Both
#: metrics below are the OWNER CELL's reading, so every boundary face carries one of these three.
CELL_NON_ORTHO = [11.0, 22.0, 33.0]
CELL_ASPECT = [1.5, 2.5, 3.5]
#: Skewness is the one metric checkMesh writes per boundary face, so these fourteen are the
#: fourteen values, and their order on paper is what the percentiles below are taken from.
SKEW_INLET = 0.11
SKEW_OUTLET = 0.22
SKEW_WALL = [round(0.3 + i / 100, 4) for i in range(12)]
SKEW_ALL = sorted([SKEW_INLET, SKEW_OUTLET, *SKEW_WALL])


@pytest.fixture
def region(tmp_path):
    """A region directory shaped the way a delivered conjugate one is: the fields beside the
    polyMesh, which is where checkMesh writes them."""
    pm = tmp_path / "constant" / "fluid" / "polyMesh"
    write_row_of_hexes(pm)
    B.write_index(pm, region="fluid")
    write_quality_fields(
        pm.parent,
        cells=CELL_NON_ORTHO,
        per_face={"skewness": {"inlet": [SKEW_INLET], "outlet": [SKEW_OUTLET],
                               "wall": list(SKEW_WALL)}},
        uniform={"nonOrthoAngle": {"inlet": 0.0, "outlet": 0.0, "wall": 0.0}},
        zero_gradient=("aspectRatio",),
    )
    write_quality_fields(pm.parent, cells=CELL_ASPECT, names=("aspectRatio",),
                         zero_gradient=("aspectRatio",))
    Q.write_quality(pm, region="fluid")
    return pm


def _fields(pm):
    return Q.quality_fields_from_sidecar(pm, non_ortho_limit=65.0)


def _mine(pm, metric):
    """One region's own block for one metric, which is where the distribution lives."""
    f = _fields(pm)
    return f["regions"]["fluid"]["metrics"][metric]


# --------------------------------------------------------------------------- the distribution

def test_the_distribution_is_published_under_the_name_checkmesh_wrote_not_the_directorys(region):
    """A conjugate response carries one quality block for every region, so each one's numbers
    have to say whose they are. The name is taken from the quality sidecar's own meta, which is
    what checkMesh was run against, rather than from the directory it happens to sit in."""
    f = _fields(region)
    assert list(f["regions"]) == ["fluid"]
    assert f["regions"]["fluid"]["n_faces"] == N_BOUNDARY


def test_every_metric_carries_a_histogram_over_its_own_observed_range(region):
    """Not over the bar. The bar for aspect ratio is 1000 against a mesh that reaches 3.5, and a
    histogram binned to the bar would put every face in the first bin and draw nothing."""
    for metric, lo, hi in (("non_ortho", 11.0, 33.0),
                           ("aspect_ratio", 1.5, 3.5),
                           ("skewness", SKEW_ALL[0], SKEW_ALL[-1])):
        h = _mine(region, metric)["histogram"]
        assert len(h["counts"]) == Q.HISTOGRAM_BINS
        assert len(h["edges"]) == Q.HISTOGRAM_BINS + 1
        assert h["edges"][0] == pytest.approx(lo)
        assert h["edges"][-1] == pytest.approx(hi)


def test_the_counts_are_the_faces_and_none_of_them_falls_between_two_bins(region):
    """A histogram a reader can add up. `np.histogram`'s last bin is closed on the right, so the
    worst face lands in it rather than outside the range it defined."""
    for metric in ("non_ortho", "skewness", "aspect_ratio"):
        h = _mine(region, metric)["histogram"]
        assert sum(h["counts"]) == N_BOUNDARY
        assert h["max_count"] == max(h["counts"])


def test_the_percentiles_are_the_values_and_not_the_bin_they_landed_in(region):
    """Taken off the array, so a median is a value some face actually has a claim to rather than
    a bin's midpoint. Fourteen skewness values, so the median is the mean of the seventh and the
    eighth: (0.34 + 0.35) / 2."""
    m = _mine(region, "skewness")
    assert m["min"] == pytest.approx(SKEW_ALL[0])
    assert m["max"] == pytest.approx(SKEW_ALL[-1])
    assert m["p50"] == pytest.approx((SKEW_ALL[6] + SKEW_ALL[7]) / 2, rel=1e-6)
    assert m["mean"] == pytest.approx(sum(SKEW_ALL) / len(SKEW_ALL), rel=1e-6)
    assert m["p50"] <= m["p90"] <= m["p99"] <= m["p999"] <= m["max"]


def test_the_top_level_metric_still_carries_everything_it_carried_before(region):
    """`min` and `mean` are added to it and nothing is taken away, because the merge across
    regions and the viewer both read this block and neither was changed to suit the new one."""
    md = _fields(region)["metrics"]["non_ortho"]
    for key in ("label", "unit", "limit", "max", "n_over", "n_faces", "scope", "basis", "source"):
        assert key in md, key
    assert md["min"] == pytest.approx(11.0)
    assert md["max"] == pytest.approx(33.0)
    # the distribution itself does NOT go here: a histogram cannot be merged across regions and
    # two medians do not average, so only the two figures that merge honestly are carried up
    for key in ("histogram", "p50", "p999", "tail_ratio"):
        assert key not in md, key


def test_every_metric_says_where_its_colours_should_span(region):
    """The viewer's ramp already spans a metric's own range when the bar sits far above anything
    the mesh has, and it switches on `scale_to`. Only aspect ratio ever carried one, so the
    delivered plate's legend ran to 84.5 degrees against a worst face of 48.39 and skewness ran
    to 26 against 1.65 - six per cent of the colours doing all the work."""
    mt = _fields(region)["metrics"]
    assert mt["aspect_ratio"]["floor"] == 1.0
    assert mt["aspect_ratio"]["scale_to"] == pytest.approx(max(3.5, 2.0))
    for metric, top in (("non_ortho", 33.0), ("skewness", SKEW_ALL[-1])):
        assert mt[metric]["floor"] == 0.0
        assert mt[metric]["scale_to"] == pytest.approx(top)
        # and the ramp's own switch - scale_to below the bar - is what this is for
        assert mt[metric]["scale_to"] < mt[metric]["limit"]


# ------------------------------------------------------ the tail, on arrays built to say one thing

def test_the_tail_ratio_tells_a_population_from_a_stray():
    """The number that makes two passing meshes readable apart. A thousand faces at the top is a
    feature of the mesh; one face at the top is a face."""
    population = np.concatenate([np.full(9_000, 1.0, np.float32),
                                 np.full(1_000, 40.0, np.float32)])
    stray = np.concatenate([np.full(9_999, 1.0, np.float32),
                            np.array([40.0], np.float32)])
    assert Q._distribution(population)["tail_ratio"] == pytest.approx(1.0, rel=1e-3)
    assert Q._distribution(stray)["tail_ratio"] > 20


def test_a_tail_of_nothing_is_said_as_nothing_rather_than_as_a_symbol():
    """A region whose metric is zero everywhere has no ratio, and `inf` or `nan` would reach the
    client as a figure to print. None is a thing a client can decline to draw."""
    assert Q._distribution(np.zeros(100, np.float32))["tail_ratio"] is None


def test_a_metric_that_is_one_number_everywhere_still_reports_that_number():
    """The chip's aspect ratio is 1.0 on every face. A degenerate range must still produce bins
    rather than an error, because a perfect region is a real answer."""
    d = Q._distribution(np.ones(500, np.float32))
    assert d["min"] == 1.0 and d["p50"] == 1.0
    assert sum(d["histogram"]["counts"]) == 500
    assert d["tail_ratio"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- the hotspots

def _places(n=400, worst_at=0):
    """n faces strung out along x, with one of them the worst, and a patch table for them."""
    v = np.linspace(1.0, 2.0, n, dtype=np.float32)
    v[worst_at] = 99.0
    centroids = np.zeros((n, 3), np.float32)
    centroids[:, 0] = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return v, centroids, np.zeros(n, np.uint16)


def test_the_worst_faces_are_reported_even_when_none_of_them_cleared_the_bar():
    """This is the whole point. Every delivered mesh passes every bar, so "nothing over the bar"
    used to mean "no hot spots" and the viewer's Worst button was disabled on exactly the meshes
    a reader was sent."""
    v, c, p = _places()
    out = Q._sidecar_hotspots("non_ortho", v, 1000.0, c, p, ["wall"], separation=0.0)
    assert out, "a passing mesh still has a worst face"
    assert all(h["over_bar"] is False for h in out)
    assert out[0]["value"] == pytest.approx(99.0)


def test_a_face_over_the_bar_is_never_passed_over_for_one_under_it():
    """Where some faces DO clear the bar, the list is those faces and says so, exactly as before.
    The fallback is a fallback."""
    v, c, p = _places()
    out = Q._sidecar_hotspots("non_ortho", v, 50.0, c, p, ["wall"], separation=0.0)
    assert [h["over_bar"] for h in out] == [True]
    assert out[0]["value"] == pytest.approx(99.0)


def test_the_single_worst_face_is_first_however_the_rest_are_spread():
    """The spacing rule is greedy in rank order, so it can drop a face for being near a better
    one and can never promote a worse face over a better."""
    v, c, p = _places(worst_at=137)
    out = Q._sidecar_hotspots("skewness", v, 1000.0, c, p, ["wall"], separation=0.1)
    assert out[0]["value"] == pytest.approx(99.0)
    assert [h["value"] for h in out] == sorted((h["value"] for h in out), reverse=True)


def test_two_hotspots_are_never_the_same_place():
    """The worst faces of a mesh are each other's neighbours - a bad corner is a few thousand
    faces, not one - so a list ranked by value alone is sixty faces of one corner and a reader
    who visits all of them has seen one place."""
    v, c, p = _places(n=2000)
    sep = 0.1
    out = Q._sidecar_hotspots("non_ortho", v, 1000.0, c, p, ["wall"], separation=sep)
    # AN EMPTY LIST SATISFIES A SPACING RULE VACUOUSLY. Found by putting the old "return [] when
    # nothing cleared the bar" back: every other test here went red and this one stayed green,
    # because there were no two places left to be in the same place.
    assert len(out) > 1, "there is more than one place to go on a mesh of two thousand faces"
    xs = [h["x"] for h in out]
    for i, a in enumerate(xs):
        for b in xs[i + 1:]:
            assert abs(a - b) >= sep - 1e-6, f"{a} and {b} are the same place"


def test_a_field_checkmesh_skipped_cannot_come_back_as_the_worst_of_the_mesh():
    """`v > limit` is False for a NaN so the over-the-bar branch could never choose one; ranking
    by value has no such protection, and a region whose field checkMesh skipped would be reported
    as sixty places with no number, presented as the worst faces there are."""
    v, c, p = _places(n=300)
    v[10:] = np.nan
    out = Q._sidecar_hotspots("non_ortho", v, 1000.0, c, p, ["wall"], separation=0.0)
    assert out, "the ten faces that WERE measured are still places"
    assert all(np.isfinite(h["value"]) for h in out)
    assert len(out) <= 10


def test_a_metric_with_nothing_measured_at_all_is_an_empty_list_not_an_error():
    """And the viewer's refusal then says the true thing - that this mesh shipped no per-face
    quality - rather than that nothing cleared a bar."""
    v, c, p = _places(n=50)
    v[:] = np.nan
    assert Q._sidecar_hotspots("non_ortho", v, 1000.0, c, p, ["wall"], separation=0.0) == []


def test_the_whole_region_reports_places_for_every_metric_it_measured(region):
    """End to end on the fixture mesh: three metrics, none of them near its bar, and a place to
    go for each."""
    hs = _fields(region)["hotspots"]
    assert {h["metric"] for h in hs} == {"non_ortho", "skewness", "aspect_ratio"}
    assert all(h["over_bar"] is False for h in hs)
    assert all(h["patch"] in {"inlet", "outlet", "wall"} for h in hs)
