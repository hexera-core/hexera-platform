"""v5 step 10: the low_quality detectors (thresholds against the shipped mesh), their causes on the loop's defects,
layer 1's soft small_face/small_edge findings becoming defects, the ops' parameters, and re-measuring - on
synthetic facts. No kernel."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import quality as QL

H = 10.0                       # e = 1.0 mm, e^2 = 1.0 mm2
REV = {("F", 1): "F10", ("F", 2): "F20", ("F", 3): "F30", ("F", 4): "F40", ("E", 5): "E50",
       ("B", 1): "B1", ("B", 2): "B2", ("B", 3): "B3"}


def _nb(face: int, dihedral: float | None, shared: float) -> dict:
    return {"face": face, "shared_mm": shared, "edges": [9], "dihedral_deg": dihedral}


def _facts(**kw: Any) -> dict:
    return {"e_mm": QL.e_of(H), "small_edges": [], "small_faces": [], "narrow_faces": [], "solid_volumes": [],
            "part_volume": None, **kw}


SLIVER = {"face": 1, "area_mm2": 4.0, "width_mm": 0.2, "rs_width_mm": 3.0, "solid": 1,
          "neighbours": [_nb(2, 80.0, 20.0), _nb(3, 10.0, 20.0), _nb(4, 90.0, 0.2)]}
CHAMFER = {**SLIVER, "face": 4, "width_mm": 0.5, "neighbours": [_nb(2, 45.0, 20.0), _nb(3, 45.0, 20.0)]}
EDGE = {"edge": 5, "length_mm": 0.05, "faces": [2, 3], "dihedral_deg": 90.0}


def test_thresholds_are_relative_to_the_shipped_element_size() -> None:
    assert QL.e_of(H) == pytest.approx(QL.E_OF_H * H)
    faults = QL.faults_of(_facts(small_edges=[EDGE], narrow_faces=[SLIVER],
                                 small_faces=[{"face": 3, "area_mm2": 0.5, "width_mm": 0.6, "rs_width_mm": 1.0}]),
                          REV, h_mm=H)
    # worst first: measured / threshold (0.05 edge, 0.2 sliver, 0.5 face)
    assert [(f.kind, f.entity) for f in faults] == [("small_edge", "E50"), ("sliver", "F10"), ("small_face", "F30")]
    edge, sliver, small = faults
    assert edge.faces == ("F20", "F30") and edge.params["max_length_mm"] == pytest.approx(0.05 * QL.SIZE_MARGIN)
    # the sliver merges into the neighbour it continues smoothly into (10 deg), never across the 80/90 deg edges
    assert sliver.params["neighbour"] == "F30" and sliver.params["feature_deg"] == QL.FEATURE_ANGLE_DEG
    assert sliver.params["max_dev_mm"] == pytest.approx(QL.MERGE_DEVIATION_OF_E * QL.e_of(H))
    assert sliver.params["max_width_mm"] == pytest.approx(3.0 * QL.SIZE_MARGIN)    # remove_small's own width
    assert small.params["feature_deg"] == QL.NO_PROTECTION_DEG                     # small both ways: no protection


def test_a_sliver_whose_every_neighbour_is_a_feature_edge_is_protected() -> None:
    [chamfer] = QL.faults_of(_facts(narrow_faces=[CHAMFER]), REV, h_mm=H)
    assert "neighbour" not in chamfer.params and chamfer.params["protected"] == [45.0, 45.0]
    d = Defect("S@1", "mesher", "low_quality", ("F40",), cls="low_quality", cause=QL._cause(chamfer))
    with pytest.raises(seq.NoParameters, match="feature angle"):
        seq.params_for("refit_cluster", d)
    # merge_sliver still enters: the kernel declines it as `protected` and the match log says so
    assert seq.params_for("merge_sliver", d) == (["F40"], {"max_dev_mm": 1.0, "feature_deg": 30.0})


def test_small_and_floating_solids_by_volume_relative_to_the_part() -> None:
    vols = [{"solid": 2, "volume_mm3": 0.01, "touching": True, "nearest_solid": 1, "nearest_mm": 0.0},
            {"solid": 3, "volume_mm3": 0.02, "touching": False, "nearest_solid": 1, "nearest_mm": 12.0},
            {"solid": 1, "volume_mm3": 1000.0}]
    faults = QL.faults_of(_facts(solid_volumes=vols, part_volume=1000.03), REV, h_mm=H)
    assert [(f.kind, f.entity) for f in faults] == [("tiny_solid", "B2"), ("floating_solid", "B3")]
    tiny, floating = (QL._cause(f) for f in faults)
    assert tiny["aim"] == ["B2", "B1"] and floating["aim"] == ["B3"]
    td = Defect("S@1", "mesher", "low_quality", ("F1",), cls="low_quality", cause=tiny)
    assert seq.params_for("fuse", td)[0] == ["B1", "B2"]                 # fused INTO the solid it touches
    assert seq.params_for("drop_solid", td)[0] == ["B2"]                 # never the partner
    fd = dataclasses.replace(td, cause=floating)
    assert loop.table_key(fd) == "low_quality:floating_solid"
    assert seq.params_for("drop_solid", fd) == (["B3"], {"max_volume_fraction": pytest.approx(2e-5 * 1.05, rel=1e-3)})


def _site(sid: str, entities: list[str]) -> Site:
    return Site(sid, "proximity", entities, [], ["gmsh_alt"], None, {"failures": []}, None)


def test_a_low_quality_defect_gets_the_worst_fault_on_its_faces_as_cause_and_rule_seed() -> None:
    faults = QL.faults_of(_facts(small_edges=[EDGE], narrow_faces=[SLIVER]), REV, h_mm=H)
    d = Defect("S@F20", "mesher", "low_quality", ("F20",), member="gmsh_alt", cls="low_quality", rule="low_quality")
    sites = {"S@F20": _site("S@F20", ["F20"])}
    [got] = QL.attach_quality([d], faults, sites, soft={}, site_of=D._site_of)
    assert got.cause["kind"] == "small_edge" and got.cause["aim"] == ["E50"]
    assert loop.table_key(got) == "low_quality:small_edge"
    assert loop.CLASS_REPAIRS["low_quality:small_edge"][0] == ("collapse_edge",)
    assert seq.params_for("collapse_edge", got) == (["E50"], {"max_length_mm": pytest.approx(0.0525)})
    # a short edge that bounds a flagged sliver is its symptom: the sliver is the cause (VM, injected step)
    end = {**EDGE, "faces": [1, 2], "length_mm": 0.01}
    faults2 = QL.faults_of(_facts(small_edges=[end], narrow_faces=[{**SLIVER, "width_mm": 0.01}]), REV, h_mm=H)
    [face_first] = QL.attach_quality([d], faults2, {"S@F20": _site("S@F20", ["F20", "F10"])}, soft={},
                                     site_of=D._site_of)
    assert face_first.cause["kind"] == "sliver" and face_first.cause["aim"] == ["F10"]
    # the site's other faults of the same kind ride along, worst first, sized to the largest
    two = [{**SLIVER, "face": 1, "width_mm": 0.1}, {**SLIVER, "face": 2, "width_mm": 0.3, "rs_width_mm": 5.0}]
    [multi] = QL.attach_quality([d], QL.faults_of(_facts(narrow_faces=two), REV, h_mm=H),
                                {"S@F20": _site("S@F20", ["F20", "F10"])}, soft={}, site_of=D._site_of)
    assert multi.cause["aim"] == ["F10", "F20"] and multi.cause["max_width_mm"] == pytest.approx(5.0 * QL.SIZE_MARGIN)
    assert seq.params_for("merge_sliver", multi)[0] == ["F10", "F20"]
    # nothing measured on its faces: the class's general row
    far = dataclasses.replace(d, id="S@F40", entities=("F40",))
    [same] = QL.attach_quality([far], faults, {"S@F40": _site("S@F40", ["F40"])}, soft={}, site_of=D._site_of)
    assert same is far and loop.table_key(same) == "low_quality"


def test_soft_small_entity_findings_become_low_quality_defects_capped_and_never_twice() -> None:
    """Step 2's gap: the loop built defects from layer-1 HARD codes only, so the small_face/small_edge rows never
    fired. A soft finding the detector confirms under e is now a defect of its own."""
    many = [{**SLIVER, "face": i, "neighbours": []} for i in (1, 2, 3, 4)]
    faults = QL.faults_of(_facts(small_edges=[EDGE], narrow_faces=many), REV, h_mm=H)
    soft = {"F10": "small_face", "F20": "small_face", "F30": "small_face", "E50": "small_edge", "F99": "small_face"}
    lq = Defect("S@F40", "mesher", "low_quality", ("F40",), member="gmsh_alt", cls="low_quality")
    got = QL.attach_quality([lq], faults, {"S@F40": _site("S@F40", ["F40"])}, soft=soft, site_of=D._site_of,
                            diagonal_mm=50.0)
    made = [g for g in got if g.id.startswith(QL.SOFT_ID)]
    # F40 was taken by the mesher defect; F99 is soft but not under e (not measured): not a defect
    assert sorted(g.id for g in made) == ["Q@sliver:F10", "Q@sliver:F20", "Q@sliver:F30", "Q@small_edge:E50"]
    assert all(g.kind == "validity" and g.cls == "low_quality" and g.measurement == {"diagonal_mm": 50.0}
               for g in made)
    edge = next(g for g in made if g.code == "small_edge")
    assert edge.entities == ("E50",) and edge.cause["faces"] == ["F20", "F30"] and loop.table_key(edge) == "low_quality:small_edge"
    # capped per code
    capped = QL.attach_quality([], faults, {}, soft={f"F{i}0": "small_face" for i in range(1, 5)},
                               site_of=D._site_of)
    assert len(capped) == QL.MAX_SOFT_PER_KIND
    # a soft defect is not a validity failure: it goes in the mesh tier, where (CRS v5 r2) the cheap low_quality
    # class is tried before the slow close_faces one
    close = Defect("S@2", "mesher", "overlapping_facets", ("F1",), cls="close_faces")
    hard = Defect("S@3", "validity", "body_overlap", ("B1",), cls="overlap_self_intersection")
    assert [d.cls for d in D.order([*made[:1], close, hard])] == ["overlap_self_intersection", "low_quality",
                                                                  "close_faces"]


def test_every_low_quality_row_is_seeded_by_its_dedicated_merge() -> None:
    rows = loop.CLASS_REPAIRS
    assert rows["low_quality:sliver"][:2] == (("merge_sliver",), ("refit_cluster",))
    assert rows["low_quality:small_face"][0] == ("merge_sliver",)
    assert rows["low_quality:tiny_solid"][0] == ("fuse",)
    assert rows["low_quality:floating_solid"] == (("drop_solid",),)
    d = Defect("S@1", "mesher", "low_quality", ("F10",), cls="low_quality",
               cause=QL._cause(QL.faults_of(_facts(narrow_faces=[SLIVER]), REV, h_mm=H)[0]))
    assert seq.params_for("refit_cluster", d) == (["F10", "F30"], {"tol_mm": pytest.approx(1.0)})
    assert seq.params_for("remove_small", d) == (["F10"], {"max_width_mm": pytest.approx(3.15)})


def test_the_v5_only_ops_are_not_offered_to_v4s_proposer() -> None:
    from meshpipeline.cad.repair_v4 import proposer
    from meshpipeline.cad.repair_v4.ops import REGISTRY

    for op in ("merge_sliver", "collapse_edge", "drop_solid"):
        assert op in REGISTRY and op not in proposer.offered_ops()
    assert "remove_small" in proposer.offered_ops()


# --- re-measuring: layer 1's local check cannot see a soft code ------------------------------------------------

def _ws(ids: dict, history: list) -> SimpleNamespace:
    return SimpleNamespace(history=history, brep_path="/x", ids=dict.fromkeys(ids, "fp"), indices=ids)


def test_a_soft_defect_is_judged_by_re_measuring_not_by_the_blind_local_check(monkeypatch) -> None:
    blind = LocalResult(gone=True, new_hard=[], detail={}, seconds=0.0)
    cause = QL._cause(QL.faults_of(_facts(narrow_faces=[SLIVER]), REV, h_mm=H)[0])
    d = Defect("Q@sliver:F10", "validity", "small_face", ("F10",), cls="low_quality", cause=cause)
    before = _ws({"F10": 1, "F30": 3}, [])
    verify = loop.remeasuring(lambda *a, **k: blind)
    # merged away: the aimed face carried to nothing -> gone, no kernel call
    removed = Diff(op="merge_sliver", params={}, modified={}, generated={}, removed=["F10"], declared_added=[],
                   declared_removed=["F10"], seconds=0.0)
    monkeypatch.setattr(QL, "measure", lambda *a, **k: pytest.fail("no measurement needed"))
    assert verify(None, before, _ws({"F30": 3}, [removed]), d, [], timeout_s=1).gone is True
    # modified but still narrow -> still there
    kept = Diff(op="split_by_proximity", params={}, modified={"F10": ["F11"]}, generated={}, removed=[],
                declared_added=[], declared_removed=[], seconds=0.0)
    after = _ws({"F11": 1, "F30": 3}, [kept])
    monkeypatch.setattr(QL, "measure", lambda pool, ws, h_mm, faces=None: _facts(narrow_faces=[SLIVER]))
    monkeypatch.setattr("meshpipeline.cad.repair_v4.sites._reverse", lambda ws: {("F", 1): "F11", ("F", 3): "F30"})
    res = verify(None, before, after, d, [], timeout_s=1)
    assert res.gone is False and "sliver" in res.detail["remeasured"][0]
    monkeypatch.setattr(QL, "measure", lambda pool, ws, h_mm, faces=None: _facts())
    assert verify(None, before, after, d, [], timeout_s=1).gone is True
    # a mesher low_quality defect keeps the member's own answer
    m = dataclasses.replace(d, kind="mesher", code="low_quality", member="gmsh_alt")
    assert verify(None, before, after, m, [], timeout_s=1) is blind
