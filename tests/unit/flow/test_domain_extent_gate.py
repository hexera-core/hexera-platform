# Responsibility: Verify the domain-extent gate compares the requested box against the manifest, failing safe.
from meshpipeline.engines import domain_extent_gate as g  # noqa: E402


def _manifest(up, down, lat, chord=1.0):
    # body bbox [0,1] in x and [0,?] in y; box placed to yield the given multiples
    body = {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 0.2}
    box = {
        "xmin": body["xmin"] - up * chord,
        "xmax": body["xmax"] + down * chord,
        "ymin": body["ymin"] - lat * chord,
        "ymax": body["ymax"] + lat * chord,
    }
    return {"geometry": {"chord": chord, "body_bbox": body, "domain_box": box}}


def test_parse_explicit_extents():
    txt = "Please use D_UPSTREAM=20 D_DOWNSTREAM=30 D_LATERAL=20 chord lengths"
    out = g.parse_requested_extents(txt)
    assert out == {"upstream": 20.0, "downstream": 30.0, "lateral": 20.0}


def test_parse_phrase_extents():
    txt = "20 chords upstream, 30 downstream, 20 above/below"
    out = g.parse_requested_extents(txt)
    assert out["upstream"] == 20.0 and out["downstream"] == 30.0 and out["lateral"] == 20.0


def test_gate_passes_on_match():
    ok, diag = g.check_domain_extents({"upstream": 20, "downstream": 30, "lateral": 20},
                                      _manifest(20, 30, 20))
    assert ok and diag == ""


def test_gate_rejects_overridden_prior():
    # user asked 20/30/20 but the builder used its 15/25/12 prior
    ok, diag = g.check_domain_extents({"upstream": 20, "downstream": 30, "lateral": 20},
                                      _manifest(15, 25, 12))
    assert not ok
    assert "[DOMAIN_EXTENT_MISMATCH]" in diag


def test_gate_reads_cfmesh_manifest_shape():
    # regression: the live cfMesh manifest uses body_box (not body_bbox) and
    # a domain_box dict + chord. The gate must compute applied multipliers from it.
    body = {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 0.2}
    chord = 1.0
    box = {"xmin": -15.0, "xmax": 1.0 + 25.0, "ymin": -12.0, "ymax": 0.2 + 12.0}
    manifest = {"geometry": {"chord": chord, "body_box": body, "domain_box": box}}
    ok, diag = g.check_domain_extents({"upstream": 20, "downstream": 30, "lateral": 20}, manifest)
    assert not ok and "[DOMAIN_EXTENT_MISMATCH]" in diag


def test_gate_fails_safe_when_no_requested():
    ok, _ = g.check_domain_extents({}, _manifest(15, 25, 12))
    assert ok


def test_gate_fails_safe_when_manifest_lacks_geometry():
    ok, _ = g.check_domain_extents({"upstream": 20}, {"geometry": {}})
    assert ok
