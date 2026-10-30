"""Per-face quality taken from checkMesh, not re-derived, so a colour is a number already shown.

`render.face_quality` computes these metrics in numpy and refuses over MAX_FACES_FOR_FIELDS,
40 M faces, because the pass needs the whole mesh. Priced on the customer plate that is ~25.6 GB
resident for the coolant and ~36.3 GB for the metal against a 16 GB worker, so the cap is the
arithmetic written down rather than a limitation to route around.

checkMesh has already measured it, per region, on the machine that holds the mesh. Measured on the
delivered coupon 2026-10-30, its fields' maxima reproduce its own summary exactly - nonOrthoAngle
16.88060 against "Mesh non-orthogonality Max: 16.8806", skewness 0.69653 against "Max skewness =
0.696529", aspectRatio 1.20951 against "Max aspect ratio = 1.20951". So these tests are mostly
about one thing: that each value ends up on the face checkMesh measured it for, and that the three
different bases do not get muddled into one.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.render import boundary_index as B
from meshpipeline.render import boundary_quality as Q
from tests.foam_fixtures import write_quality_fields, write_row_of_hexes

#: The fixture mesh: three unit hexes, 14 boundary faces as inlet(1) + outlet(1) + wall(12).
N_CELLS = 3
N_BOUNDARY = 14

#: A distinct value per cell, so a wrong owner lookup cannot coincide with a right one.
CELL_NON_ORTHO = [11.0, 22.0, 33.0]
CELL_ASPECT = [1.5, 2.5, 3.5]


def _owner_of_boundary(polymesh):
    """The owner cell of each boundary face, read off the mesh the fixture wrote."""
    idx = B.open_index(polymesh)
    first = int(idx.meta["first_face"])
    txt = (polymesh / "owner").read_text()
    i = txt.index("(")
    j = txt.rindex(")")
    allo = np.fromstring(txt[i + 1:j], dtype=np.int64, sep=" ")  # noqa: NPY201
    return allo[first:first + idx.n_faces]


@pytest.fixture
def region(tmp_path):
    """A region directory shaped the way a delivered conjugate one is: the fields beside the
    polyMesh, which is where checkMesh writes them."""
    pm = tmp_path / "constant" / "fluid" / "polyMesh"
    write_row_of_hexes(pm)
    B.write_index(pm, region="fluid")
    owner = _owner_of_boundary(pm)
    write_quality_fields(
        pm.parent,
        cells=CELL_NON_ORTHO,                       # nonOrthoAngle's internal field
        per_face={"skewness": {"inlet": [0.11], "outlet": [0.22],
                               "wall": [round(0.3 + i / 100, 4) for i in range(12)]}},
        uniform={"nonOrthoAngle": {"inlet": 0.0, "outlet": 0.0, "wall": 0.0}},
        zero_gradient=("aspectRatio",),
    )
    # aspectRatio needs its own internal field, different from nonOrthoAngle's
    write_quality_fields(pm.parent, cells=CELL_ASPECT, names=("aspectRatio",),
                         zero_gradient=("aspectRatio",))
    return pm, owner


def test_a_boundary_faces_skewness_is_its_own_not_a_cell_maximum(region):
    """checkMesh writes skewness as a nonuniform boundaryField with one value per face of the
    patch, which is a real per-face measurement and the only one of the three that is."""
    pm, _owner = region
    Q.write_quality(pm, region="fluid")
    q = Q.open_quality(pm)
    assert q.basis("skewness") == "patch"

    idx = B.open_index(pm)
    byname = {str(p["name"]): p for p in idx.patches}
    got = {}
    for name, p in byname.items():
        f0, n = int(p["first_face"]), int(p["n_faces"])
        got[name] = q.values("skewness", np.arange(f0, f0 + n))
    assert got["inlet"].tolist() == pytest.approx([0.11])
    assert got["outlet"].tolist() == pytest.approx([0.22])
    assert got["wall"].tolist() == pytest.approx([round(0.3 + i / 100, 4) for i in range(12)])


def test_non_ortho_is_the_owner_cells_value_because_the_face_has_none(region):
    """checkMesh writes `value uniform 0` on the patch, which is true of the face and useless to
    colour: a boundary face has no non-orthogonality, the metric lives between two cells. The
    cell behind it is what a reader pointing at that face is asking about."""
    pm, owner = region
    Q.write_quality(pm, region="fluid")
    q = Q.open_quality(pm)
    assert q.basis("non_ortho") == "owner_cell"

    want = np.asarray(CELL_NON_ORTHO, dtype=np.float32)[owner]
    got = q.values("non_ortho", np.arange(N_BOUNDARY))
    assert got.tolist() == pytest.approx(want.tolist())
    assert set(np.unique(got).tolist()) <= set(CELL_NON_ORTHO), "a value no cell has"
    assert 0.0 not in np.unique(got).tolist(), "the patch's `uniform 0` was taken as a measurement"


def test_aspect_ratio_is_the_owner_cells_value_because_zero_gradient_means_that(region):
    pm, owner = region
    Q.write_quality(pm, region="fluid")
    q = Q.open_quality(pm)
    assert q.basis("aspect_ratio") == "owner_cell"
    want = np.asarray(CELL_ASPECT, dtype=np.float32)[owner]
    assert q.values("aspect_ratio", np.arange(N_BOUNDARY)).tolist() == pytest.approx(want.tolist())


def test_the_three_bases_are_recorded_in_the_file_not_implied(region):
    """They are not the same for all three, and a reader that assumes one of them is wrong about
    the other two."""
    pm, _ = region
    meta = Q.write_quality(pm, region="fluid")
    on_disk = json.loads((Q.quality_dir(pm) / "meta.json").read_text())
    assert on_disk["basis"] == {"skewness": "patch", "non_ortho": "owner_cell",
                                "aspect_ratio": "owner_cell"}
    assert on_disk["source"] == "checkMesh -writeFields"
    assert meta["figures"]["skewness"]["max"] == pytest.approx(0.41)
    assert meta["figures"]["non_ortho"]["max"] == pytest.approx(max(CELL_NON_ORTHO))


def test_a_field_checkmesh_did_not_write_is_named_rather_than_absent(region):
    pm, _ = region
    (pm.parent / "skewness").unlink()
    meta = Q.write_quality(pm, region="fluid")
    assert "skewness" not in meta["basis"]
    assert "skewness" in meta["notes"]
    q = Q.open_quality(pm)
    assert q.values("skewness", [0]) is None
    assert "non_ortho" in q.metrics, "the others still land"


def test_no_fields_at_all_is_refused_in_words(region):
    pm, _ = region
    for name in ("nonOrthoAngle", "skewness", "aspectRatio"):
        (pm.parent / name).unlink()
    with pytest.raises(Q.QualityUnavailable, match="checkMesh wrote none of"):
        Q.write_quality(pm, region="fluid")


def test_a_patch_whose_value_count_disagrees_is_dropped_not_stretched(region):
    """checkMesh writing a different number of values than the patch has faces means these are
    not that patch's faces. Using them anyway would colour every face after the first with its
    neighbour's number."""
    pm, _ = region
    write_quality_fields(pm.parent, cells=CELL_NON_ORTHO, names=("skewness",),
                         per_face={"skewness": {"wall": [0.5] * 11}},   # 11 for 12 faces
                         zero_gradient=())
    meta = Q.write_quality(pm, region="fluid")
    assert "skewness" in meta["notes"] and "11" in meta["notes"]["skewness"]


def test_quality_numbered_against_another_index_is_refused(region):
    """Two files written together and replaceable apart. An array whose face i is not the index's
    face i colours every face with some other face's number, and nothing downstream could tell."""
    pm, _ = region
    Q.write_quality(pm, region="fluid")
    assert Q.has_quality(pm)
    # the index is rebuilt over a different patch set; the quality is now numbered against nothing
    B.write_index(pm, region="fluid", skip_names=("outlet",))
    assert not Q.has_quality(pm)
    with pytest.raises(Q.QualityUnavailable, match="different boundary index"):
        Q.open_quality(pm)


def test_a_version_this_does_not_know_is_refused(region):
    pm, _ = region
    Q.write_quality(pm, region="fluid")
    p = Q.quality_dir(pm) / "meta.json"
    meta = json.loads(p.read_text())
    meta["version"] = Q.QUALITY_VERSION + 1
    p.write_text(json.dumps(meta))
    with pytest.raises(Q.QualityUnavailable, match="quality version"):
        Q.open_quality(pm)


def test_it_carries_no_bar_and_judges_nothing(region):
    """Whoever shows these decides what is bad. A limit stored here would be a second opinion
    drifting from the engine's, and checkMesh's boundary skewness bar is 20 where the internal one
    is 4 - a file that carried one number for both would paint faces red that checkMesh passed."""
    pm, _ = region
    meta = Q.write_quality(pm, region="fluid")
    blob = json.dumps(meta).lower()
    for word in ("limit", "bar", "threshold", "over", "bad", "fail"):
        assert word not in blob, word


def _cut(region_tuple, radius=99.0):
    from meshpipeline.render import detail_window as D

    pm, _ = region_tuple
    workspace = pm.parents[2]            # <ws>/constant/fluid/polyMesh -> <ws>
    return D.detail_at(workspace, centre=(1.5, 0.5, 0.5), radius=radius)


def test_the_detail_cut_carries_the_values_for_the_faces_it_sends(region):
    """A cut is the one place a per-face number is also a per-drawn-polygon number: nothing is
    merged, one delivered face is one polygon, packed in the ids' own order."""
    pm, _ = region
    Q.write_quality(pm, region="fluid")
    cut = _cut(region)

    byname = {q["name"].split("/")[-1]: q for q in cut["patches"]}
    assert set(byname) == {"inlet", "outlet", "wall"}
    for name, q in byname.items():
        qual = q["quality"]
        assert qual is not None, name
        assert sorted(qual["values"]) == ["aspect_ratio", "non_ortho", "skewness"]
        for metric, arr in qual["values"].items():
            assert arr.size == q["face_count"], (name, metric)

    # each patch gets ITS OWN measurements and not a neighbour's. Sorted rather than in order:
    # `faces_near` returns ids in bucket order, and `pack` keeps that order, so the sequence is
    # the drawing's and not the boundary file's. What must hold is that the SET is this patch's.
    def vals(name, metric="skewness"):
        return sorted(float(v) for v in byname[name]["quality"]["values"][metric])

    assert vals("inlet") == pytest.approx([0.11])
    assert vals("outlet") == pytest.approx([0.22])
    assert vals("wall") == pytest.approx(sorted(round(0.3 + i / 100, 4) for i in range(12)))
    # and the owner-cell metric only ever carries values some cell actually has
    assert set(np.unique(np.round(byname["wall"]["quality"]["values"]["non_ortho"], 3)).tolist()) \
        <= set(CELL_NON_ORTHO)


def test_the_basis_travels_with_the_values(region):
    """Three different bases, and a client that draws them against one bar paints faces red that
    checkMesh called fine: its boundary skewness limit is 20 where the internal one is 4."""
    pm, _ = region
    Q.write_quality(pm, region="fluid")
    for q in _cut(region)["patches"]:
        assert q["quality"]["basis"] == {"skewness": "patch", "non_ortho": "owner_cell",
                                          "aspect_ratio": "owner_cell"}


def test_a_truncated_window_describes_the_faces_it_actually_sent(region, monkeypatch):
    """The arrays are gathered AFTER the budget cuts the face list down, never before."""
    from meshpipeline.render import detail_window as D

    pm, _ = region
    Q.write_quality(pm, region="fluid")
    monkeypatch.setattr(D, "MAX_RETURN_FACES", 4)
    cut = _cut(region)
    assert cut["truncated"] is True
    assert cut["faces_returned"] < cut["faces_in_range"]
    for q in cut["patches"]:
        for metric, arr in q["quality"]["values"].items():
            assert arr.size == q["face_count"], (q["name"], metric)


def test_the_route_sends_them_as_the_arrays_the_viewer_already_decodes(region):
    """float32 per face, base64, in the polys' own order - the same shape the main surface's
    colours arrive in, so the client needs no second decode path."""
    from meshpipeline.render import detail_window as D

    pm, _ = region
    Q.write_quality(pm, region="fluid")
    body = D.detail_response("a-job", _cut(region))
    import base64
    for q in body["patches"]:
        assert sorted(q["quality_b64"]) == ["aspect_ratio", "non_ortho", "skewness"]
        assert q["quality_basis"]["skewness"] == "patch"
        for metric, b in q["quality_b64"].items():
            arr = np.frombuffer(base64.b64decode(b), dtype="<f4")
            assert arr.size == q["face_count"], (q["name"], metric)


def test_the_bar_travels_with_the_values_and_skewness_gets_the_boundary_one(region):
    """Without a bar a client can only invent a ramp, and then 18 degrees and 68 degrees are the
    same hue. And the bar for a cut's skewness is checkMesh's BOUNDARY bar, 20, not the internal
    4 the main surface uses - every value in a cut is a boundary face's own, and judging them
    against 4 paints faces red that checkMesh passed."""
    from meshpipeline.render import detail_window as D

    pm, _ = region
    Q.write_quality(pm, region="fluid")
    body = D.detail_response("a-job", _cut(region))
    for q in body["patches"]:
        md = q["quality_metrics"]
        assert {m: md[m]["limit"] for m in md} == {"non_ortho": 65.0, "skewness": 20.0,
                                                    "aspect_ratio": 1000.0}
    # and it is the boundary bar precisely because the basis says so
    assert D.QUALITY_BARS["skewness"] == 20.0
    from meshpipeline.render.face_quality import INTERNAL_SKEW_LIMIT
    assert INTERNAL_SKEW_LIMIT == 4.0, "the internal bar, which a cut must NOT use"
    assert D.QUALITY_BARS["skewness"] != INTERNAL_SKEW_LIMIT


def test_the_descriptors_are_the_shape_the_main_surface_already_sends(region):
    """So the viewer's ramp and legend read a cut's metrics unchanged, and no metric needs a
    special case in the client."""
    from meshpipeline.render import detail_window as D

    pm, _ = region
    Q.write_quality(pm, region="fluid")
    md = D.detail_response("a-job", _cut(region))["patches"][0]["quality_metrics"]

    assert md["non_ortho"]["unit"] == "°" and md["skewness"]["unit"] == ""
    for m in md:
        assert md[m]["max"] >= 0.0 and md[m]["n_faces"] > 0
        assert md[m]["n_over"] == 0, "nothing in this fixture is over its bar"
    # checkMesh's aspect bar is 1000 against cells at 1 to 3, so a ramp drawn to the bar is one
    # flat colour; the span becomes the data's own, exactly as the main surface does it
    assert md["aspect_ratio"]["floor"] == 1.0
    assert md["aspect_ratio"]["scale_to"] == pytest.approx(max(md["aspect_ratio"]["max"], 2.0))
    assert "scale_to" not in md["non_ortho"], "65 deg is a real bar and the ramp should show it"
    assert md["skewness"]["boundary_limit"] == 20.0


def test_a_cut_with_no_quality_beside_it_simply_has_none(region):
    """A mesh delivered before any of this behaves exactly as it did, and the key is ABSENT
    rather than zero-filled: a number that was not measured is not a good one, and a client
    handed zeros paints a clean mesh."""
    from meshpipeline.render import detail_window as D

    cut = _cut(region)                                   # no write_quality call
    assert all(q["quality"] is None for q in cut["patches"])
    body = D.detail_response("a-job", cut)
    assert all("quality_b64" not in q for q in body["patches"])
    assert all(q["face_count"] > 0 for q in body["patches"]), "the cut itself still works"


def test_it_lives_beside_the_index_not_inside_it(region):
    """boundary_index says of itself that it knows where faces are and nothing about whether they
    are any good. They fail apart: a region whose fields checkMesh did not write still gets an
    index, and the detail window still works there without colours."""
    pm, _ = region
    Q.write_quality(pm, region="fluid")
    assert Q.quality_dir(pm) != B.index_dir(pm)
    assert Q.quality_dir(pm).is_dir() and B.index_dir(pm).is_dir()
    import shutil
    shutil.rmtree(Q.quality_dir(pm))
    assert B.has_index(pm), "losing the colours must not lose the detail window"
