# Responsibility: A11 step 2 priority 1 - a CONTACT (two faces touching with no shared edge) is the mesher's
#                 commonest cause, so it must reach the sweep and be aimed at with ops a ZERO gap allows.
# Boundaries: hermetic; the kernel half is proved natively in test_native_repair_v4_contact.py.
from __future__ import annotations

from meshpipeline.cad.repair_v4 import search
from meshpipeline.cad.repair_v4.types import Site


def _site(**kw) -> Site:
    g = {"clashes": kw.pop("clashes", []), "mesher_faces": kw.pop("mesher_faces", [])}
    g.update(kw.pop("graph", {}))
    return Site(id="s1", kind=kw.pop("kind", "proximity"), entities=kw.pop("entities", ["F1", "F2", "F3"]),
                xyz=[], meshers_failing=kw.pop("meshers_failing", ["gmsh_default"]), dist_over_h=None,
                graph=g, view_png=None, **kw)


def _sweep(site, diag=100.0):
    return search.DetGenerator()._sweep(site, diag)


def _rows(site, op, diag=100.0):
    return [r for r in _sweep(site, diag) if r[0] == op]


def test_proximity_splits_are_aimed_at_the_measured_pairs_not_every_combination() -> None:
    """A11: aim at the named faces. The sweep used to pair every face with every other, which proposes work the
    mesher never complained about and crowds out the pair it did."""
    clashes = [{"a": "F1", "b": "F7", "dist_mm": 0.21, "touching": False}]
    rows = _rows(_site(entities=["F1", "F2", "F3", "F7"], clashes=clashes), "split_by_proximity")
    assert rows, "a measured near pair must still be proposed"
    for _op, _t, params, _why in rows:
        assert params["face_pairs"] == [["F1", "F7"]]


def test_a_contact_is_never_handed_to_the_proximity_split() -> None:
    """Its plan is sized from the gap (`max(gap, 1e-9)`), so a zero gap asks for an infinitely fine split."""
    clashes = [{"a": "F1", "b": "F2", "dist_mm": 0.0, "touching": True},
               {"a": "F1", "b": "F7", "dist_mm": 0.4, "touching": False}]
    rows = _rows(_site(entities=["F1", "F2", "F7"], clashes=clashes), "split_by_proximity")
    assert rows
    for _op, _t, params, _why in rows:
        assert ["F1", "F2"] not in params["face_pairs"]
        assert params["face_pairs"] == [["F1", "F7"]]


def test_a_contact_gets_the_ops_a_zero_gap_allows() -> None:
    site = _site(entities=["F1", "F2"], clashes=[{"a": "F1", "b": "F2", "dist_mm": 0.0, "touching": True}])
    ops = {r[0] for r in _sweep(site)}
    assert "split_angle" in ops                     # separate the faces meeting at the contact
    assert "remove_duplicate_faces" in ops          # a coincident pair is two copies of one face
    assert "split_by_proximity" not in ops          # nothing to sag towards


def test_a_contact_proposes_defeaturing_the_SMALLER_face_through_the_located_source() -> None:
    """LEAD-2's instruction: defeature the smaller touching face, through D16's `located` source, not part-wide."""
    site = _site(entities=["F1", "F2"], mesher_faces=["F2"],
                 clashes=[{"a": "F1", "b": "F2", "dist_mm": 0.0, "touching": True,
                           "area_a_mm2": 900.0, "area_b_mm2": 3.5}])
    rows = _rows(site, "defeature")
    assert rows, "a contact must offer the removal of the small side"
    targets = [t for _op, t, _p, _w in rows]
    assert ["F2"] in targets                                  # the smaller face, never the big one alone
    assert all("F1" not in t for t in targets)
    assert all(p.get("source") == "located" for _op, _t, p, _w in rows)


def test_without_measured_areas_the_contact_does_not_guess_which_face_is_small() -> None:
    site = _site(entities=["F1", "F2"], mesher_faces=["F1", "F2"],
                 clashes=[{"a": "F1", "b": "F2", "dist_mm": 0.0, "touching": True}])
    rows = _rows(site, "defeature")
    # both sides are offered, as separate candidates; nothing is invented about which is smaller
    assert sorted(t for _o, t, _p, _w in rows) == [["F1"], ["F2"]]


def test_a_low_quality_point_does_not_make_its_face_the_defect() -> None:
    """My own caveat, carried into the build: on 263d5e2d5bab both members name face 5 while the 2995:1 sliver is
    face 9. A site whose only evidence is a low_quality POINT proposes no removal of that face."""
    site = _site(kind="proximity", entities=["F5"], mesher_faces=["F5"],
                 graph={"points": [{"xyz": [1.0, 2.0, 3.0], "entity": "F5", "mesher": "gmsh_default",
                                    "kind": "low_quality"}]}, clashes=[])
    assert not _rows(site, "defeature"), "a worst-element point is not evidence that the face is wrong"


def test_the_sliver_family_still_gets_its_own_ops() -> None:
    rows = {r[0] for r in _sweep(_site(kind="sliver", entities=["F4"]))}
    assert "remove_small" in rows or "defeature" in rows


def test_every_contact_proposal_is_inside_its_op_schema() -> None:
    """A proposal the op's own schema refuses costs a candidate and buys nothing. This caught
    `remove_duplicate_faces` being proposed with no params at all: the op acts on PAIRS and requires them."""
    from meshpipeline.cad.repair_v4.ops import validate_call
    from meshpipeline.cad.repair_v4.workspace import Workspace

    faces = ["F1", "F2", "F7"]
    ws = Workspace(brep_path="/fake/a.brep", ids={f: f"fp{f}" for f in faces}, labels={}, history=[],
                   original="/fake/o.brep", indices={f: i for i, f in enumerate(faces, start=1)})
    site = _site(entities=faces, clashes=[{"a": "F1", "b": "F2", "dist_mm": 0.0, "touching": True,
                                           "area_a_mm2": 2.0, "area_b_mm2": 9.0},
                                          {"a": "F1", "b": "F7", "dist_mm": 0.3, "touching": False}])
    checked = 0
    for op, targets, params, _why in _sweep(site):
        if op in ("remove_duplicate_faces", "split_by_proximity", "unify_same_domain"):
            assert validate_call(op, list(targets), dict(params), ws) == [], (op, targets, params)
            checked += 1
    assert checked, "the sweep proposed none of the pair ops, so nothing was checked"


def test_a_contact_measured_on_any_site_kind_gets_its_ops() -> None:
    """Lane C12's second finding: a post-cut `self_overlap` site carried 4 touching clashes with both areas and the
    sweep built 0 contact rows, because the builder was reached only for `proximity`. A gap-0 pair is the same
    geometry whatever the site was named for."""
    clashes = [{"a": "F127", "b": "F123", "dist_mm": 0.0, "touching": True,
                "area_a_mm2": 768.0, "area_b_mm2": 620.0}]
    for kind in ("self_overlap", "invalid", "sliver", "periodic", "proximity"):
        ops = {r[0] for r in _sweep(_site(kind=kind, entities=["F127", "F123"], clashes=clashes))}
        assert "remove_duplicate_faces" in ops, (kind, ops)
        smaller = [r for r in _sweep(_site(kind=kind, entities=["F127", "F123"], clashes=clashes))
                   if r[0] == "defeature" and r[1] == ["F123"]]
        assert smaller, f"{kind}: the smaller face (620 mm2) must be offered for removal"


def test_the_near_miss_split_stays_proximitys_own() -> None:
    """B19 (owner priority 2026-11-24): a site's kind sets the ORDER of the families, never which are allowed, so a
    near pair on another kind is offered the near-miss split too - but only a proximity site prefers it."""
    near = [{"a": "F1", "b": "F2", "dist_mm": 0.3, "touching": False}]
    prox = [r[0] for r in _sweep(_site(kind="proximity", entities=["F1", "F2"], clashes=near))]
    other = [r[0] for r in _sweep(_site(kind="sliver", entities=["F1", "F2"], clashes=near))]
    assert "split_by_proximity" in prox and "split_by_proximity" in other
