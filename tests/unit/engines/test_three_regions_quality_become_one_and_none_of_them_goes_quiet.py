"""Three regions' quality become one response, and none of them goes quiet doing it.

`_merge_quality` seeds the response from the FIRST region with

    into.update({k: v for k, v in one.items() if k not in ("patches", "hotspots", "metrics")})

and never revisits that set, so any key it does not know by name is taken from region one and the
other two are dropped without a word. The per-region distribution block is exactly such a key, and
dropping it would leave a panel showing the coolant's numbers under a heading naming all three.

The two figures that DO merge up to the whole mesh need rules of their own, and neither is the max
rule that was already there:

    min    the smallest of the regions, not the largest - the max rule reports the better one
    mean   weighted by face count; on the delivered plate a flat average of the three means gives
           0.18295682 where the face-weighted mean is 0.26152453, a 43% error

The percentiles and the histogram get no merge rule at all, deliberately: two regions' histograms
have different bin edges and two medians do not average. They live only in `regions`.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate.viewer import _merge_quality


def _one(region: str, *, n_faces: int, mean: float, lo: float, hi: float, over: int = 0) -> dict:
    """One region's fields in the shape `quality_fields_from_sidecar` returns them."""
    return {"basis": "checkMesh_per_metric",
            "metrics": {"non_ortho": {"label": "non-orthogonality", "unit": "°",
                                      "limit": 65.0, "max": hi, "min": lo, "mean": mean,
                                      "n_over": over, "n_faces": n_faces, "scope": "boundary"}},
            "patches": {f"{region}/wall": {"count": n_faces}},
            "hotspots": [{"metric": "non_ortho", "value": hi, "over_bar": over > 0,
                          "x": 0.0, "y": 0.0, "z": 0.0, "patch": f"{region}/wall"}],
            "regions": {region: {"n_faces": n_faces,
                                 "metrics": {"non_ortho": {"max": hi, "min": lo, "p50": mean,
                                                           "histogram": {"counts": [n_faces],
                                                                         "edges": [lo, hi],
                                                                         "max_count": n_faces}}}}}}


FLUID = _one("fluid", n_faces=16_622_416, mean=0.26571, lo=0.0, hi=46.9146)
SOLID = _one("solid", n_faces=18_104_116, mean=0.28316, lo=0.5, hi=48.3939)
CHIP = _one("chip", n_faces=1_763_676, mean=1.4156e-06, lo=0.25, hi=0.0024142)


def _merged():
    out: dict = {}
    for one in (FLUID, SOLID, CHIP):
        _merge_quality(out, one)
    return out


def test_every_regions_distribution_survives_the_merge():
    """Not just the first one's. This is the key the seed above would have taken from region one
    and then never looked at again."""
    assert sorted(_merged()["regions"]) == ["chip", "fluid", "solid"]


def test_a_regions_own_histogram_is_still_its_own_after_the_merge():
    """Nothing is folded together, because nothing can be: two regions' bin edges differ and a
    merged histogram would be a shape no region has."""
    r = _merged()["regions"]
    assert r["fluid"]["metrics"]["non_ortho"]["histogram"]["edges"] == [0.0, 46.9146]
    assert r["chip"]["metrics"]["non_ortho"]["histogram"]["edges"] == [0.25, 0.0024142]
    assert r["solid"]["n_faces"] == 18_104_116


def test_the_merged_mean_is_weighted_by_the_faces_behind_it():
    """The regions differ by millions of faces, so the straight average of three means is a
    number no face in any of them has."""
    total = sum(x["metrics"]["non_ortho"]["n_faces"] for x in (FLUID, SOLID, CHIP))
    want = sum(x["metrics"]["non_ortho"]["mean"] * x["metrics"]["non_ortho"]["n_faces"]
               for x in (FLUID, SOLID, CHIP)) / total
    flat = sum(x["metrics"]["non_ortho"]["mean"] for x in (FLUID, SOLID, CHIP)) / 3
    got = _merged()["metrics"]["non_ortho"]["mean"]
    assert got == pytest.approx(want, rel=1e-9)
    assert got != pytest.approx(flat, rel=1e-3), "a flat average would be a different number"


def test_the_merged_min_is_the_smallest_of_them_and_not_the_first():
    """Under the max rule that was already there, `min` would report whichever region was least
    good at being small, which is the opposite of what the word says."""
    assert _merged()["metrics"]["non_ortho"]["min"] == pytest.approx(0.0)


def test_the_merged_max_is_still_the_worst_and_the_counts_still_add_up():
    """The rules that were already right are untouched."""
    md = _merged()["metrics"]["non_ortho"]
    assert md["max"] == pytest.approx(48.3939)
    assert md["n_faces"] == 16_622_416 + 18_104_116 + 1_763_676
    assert md["n_over"] == 0


def test_every_regions_places_are_in_the_one_list():
    """Hotspots concatenate - the viewer's Worst button reads one list and filters it by the
    metric the reader picked, so a region missing from it is a region nothing can fly to."""
    hs = _merged()["hotspots"]
    assert {h["patch"] for h in hs} == {"fluid/wall", "solid/wall", "chip/wall"}


def test_the_first_region_alone_is_merged_without_losing_its_own_block():
    """A single-region mesh takes the seed path and never reaches the fold, so the seed has to
    carry `regions` itself rather than relying on the update below it."""
    out: dict = {}
    _merge_quality(out, FLUID)
    assert list(out["regions"]) == ["fluid"]
    assert out["metrics"]["non_ortho"]["min"] == pytest.approx(0.0)


def test_a_region_that_published_nothing_cannot_empty_what_the_others_published():
    """A region whose sidecar was unreadable returns None, and the merge is asked to fold it."""
    out = _merged()
    before = dict(out["regions"])
    _merge_quality(out, None)
    _merge_quality(out, {})
    assert out["regions"] == before
