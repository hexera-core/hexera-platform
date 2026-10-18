# Responsibility: Verify review-item minting: field set, camera shape, pairing, tried-by-defect and the cap.
from __future__ import annotations

import pytest

from meshpipeline.application import repair_review_minting as minting
from meshpipeline.cad.repair.plan import RunHistory
from meshpipeline.cad.repair.triage import DefectSplit, defect_from_entity, split


def _entity(code, kind, index, fingerprint, severity="error", **measurements) -> dict:
    return {"code": code, "severity": severity, "entity": f"{kind}:{index}", "entity_type": kind,
            "entity_index": index, "message": f"a {code}", "region": "unknown",
            "measurements": measurements, "location": {}, "fingerprint": fingerprint}


def _report(*entities) -> dict:
    return {"report": {"entities": list(entities)}}


def _mint(entities, measured=None, **kw):
    report = _report(*entities)
    return minting.mint_with_overflow(
        shape=kw.pop("shape", None), report=report, repair_run_measured=measured or {},
        split=split(report), **kw)


@pytest.fixture
def cameras(monkeypatch):
    seen = []

    def _camera(shape, defect):
        seen.append((shape, defect.entity_index))
        return {"centroid_mm": [1.0, 2.0, 3.0], "normal": [0.0, 0.0, 1.0], "extent_mm": 5.0}

    monkeypatch.setattr(minting, "camera_for", _camera)
    return seen


def test_an_item_carries_the_full_review_field_set_and_a_rules_proposal(cameras):
    items, dropped = _mint([_entity("open_shell", "edge", 8, "eb", boundary_edges=4)])
    item = items[0]
    assert dropped == 0
    assert item["group_key"] == "open_shell:sew_faces"
    assert item["proposal"]["op"] == "sew_faces" and item["proposal"]["source"] == "rules"
    assert (item["defect_code"], item["severity"], item["entity_type"], item["entity_index"],
            item["fingerprint"], item["origin"], item["pairing_key"]) == (
        "open_shell", "error", "edge", 8, "eb", "survivor", "")
    assert item["measurements"]["boundary_edges"] == 4
    assert item["message"] == "a open_shell"
    assert item["camera"] == {"centroid_mm": [1.0, 2.0, 3.0], "normal": [0.0, 0.0, 1.0],
                              "extent_mm": 5.0}


def test_a_kept_runs_triage_camera_is_computed_for_the_post_pass_index(cameras):
    repaired, original = object(), object()
    original_entity = _entity("small_face", "face", 3, "fa", "warning")
    renumbered = _entity("small_face", "face", 7, "fa", "warning")
    items, _ = _mint([original_entity], {"survivors": [renumbered], "operations_kept": 1},
                     shape=repaired, original_shape=original)
    assert items[0]["entity_index"] == 7
    assert cameras == [(repaired, 7)]


def test_a_triage_defect_missing_from_the_post_pass_scan_falls_back_to_the_original_shape(cameras):
    repaired, original = object(), object()
    items, _ = _mint([_entity("small_face", "face", 3, "fa", "warning")],
                     {"survivors": [_entity("small_face", "face", 7, "other", "warning")],
                      "operations_kept": 1}, shape=repaired, original_shape=original)
    assert items[0]["entity_index"] == 3
    assert cameras == [(original, 3)]


def _refusal(fingerprint) -> dict:
    history = RunHistory()
    history.refuse(fingerprint, "paired_rim", "this loop pairs with another on a parallel plane")
    return history.refusals[0]


def test_two_separate_bores_get_two_distinct_pairing_keys(cameras, monkeypatch):
    rims = [_entity("open_shell", "edge", i, fp, boundary_edges=4)
            for i, fp in enumerate(("a1", "a2", "b1", "b2"), start=1)]
    group = {"a1": "A", "a2": "A", "b1": "B", "b2": "B"}
    monkeypatch.setattr("meshpipeline.cad.repair.operations.free_wire_containing",
                        lambda shape, index: rims[index - 1]["fingerprint"])
    monkeypatch.setattr("meshpipeline.cad.repair.conservative._paired_rims",
                        lambda loops, shape=None: {0, 1} if group[loops[0]] == group[loops[1]] else set())
    items, _ = _mint(rims, {"survivors": rims, "operations_kept": 1,
                            "refusals": [_refusal(r["fingerprint"]) for r in rims]},
                     shape=object())
    keys = {i["fingerprint"]: i["pairing_key"] for i in items}
    assert keys["a1"] == keys["a2"] != keys["b1"] == keys["b2"]
    assert all(keys.values()) and all(i["origin"] == "refusal" for i in items)


def test_tried_credits_only_the_defect_the_operation_was_aimed_at(cameras):
    op = {"name": "sew_faces", "status": "rolled_back", "defect_code": "open_shell",
          "targets": ["e1"]}
    items, _ = _mint([_entity("open_shell", "edge", 1, "e1"), _entity("open_shell", "edge", 2, "e2")],
                     {"operations": [op], "stopped_because": "stalled", "operations_kept": 0})
    tried = {i["fingerprint"]: i["measurements"].get("tried") for i in items}
    assert tried == {"e1": ["sew_faces"], "e2": None}


def test_minting_is_capped_and_reports_what_it_dropped(cameras):
    entities = [_entity("wire_gap", "edge", i, f"w{i}", gap_mm=float(i)) for i in range(1, 502)]
    items, dropped = _mint(entities)
    assert len(items) == minting.MINT_CAP == 500
    assert dropped == 1
    # largest measurement first within the group: the smallest is the one dropped
    assert "w1" not in {i["fingerprint"] for i in items}


def test_defect_from_entity_round_trips_the_fields_used_here():
    assert defect_from_entity(_entity("wire_gap", "edge", 4, "w")).entity_index == 4
    assert isinstance(DefectSplit().repairable, tuple)


def test_rims_a_kept_operation_renamed_are_still_paired_at_mint(cameras, monkeypatch):
    # the refusal was recorded mid-run under a fingerprint the survivors no longer carry; the two
    # rims of the bore survive under new names and must still be minted as a pairing
    import meshpipeline.cad.repair.plan as plan
    asked = []

    def _rims(shape, defects):
        asked.append(sorted(d.fingerprint for d in defects))
        return frozenset({"rim:a", "rim:b"})
    monkeypatch.setattr(plan, "paired_rim_fingerprints", _rims)
    monkeypatch.setattr(minting, "_pairing_keys",
                        lambda shape, rims: {r.fingerprint: "pair:ab" for r in rims})
    survivors = [_entity("open_shell", "edge", 3, "rim:a", boundary_edges=4),
                 _entity("open_shell", "edge", 4, "rim:b", boundary_edges=4),
                 _entity("open_shell", "edge", 9, "gap:c", boundary_edges=6)]
    measured = {"survivors": survivors, "operations_kept": 1,
                "refusals": [{"defect": "rim:before-rename", "reason": "paired_rim"}]}
    items, _ = _mint(survivors, measured, shape=object(), original_shape=object())
    keys = {i["fingerprint"]: i["pairing_key"] for i in items}
    assert keys == {"rim:a": "pair:ab", "rim:b": "pair:ab", "gap:c": ""}, keys
    assert asked and "gap:c" in asked[0]             # computed over the whole minted pool


def test_one_entity_with_two_problems_mints_each_of_them(cameras):
    """One face can carry several defect codes under one fingerprint. Keyed by the fingerprint
    alone, the judgement item hid the repairable defect on the same face and two survivor codes
    on one edge collapsed to the first."""
    items, _ = _mint([_entity("small_face", "face", 3, "fa", "warning"),
                      _entity("curve_inconsistency", "face", 3, "fa"),
                      _entity("small_edge", "edge", 5, "eb"),
                      _entity("degenerate_edge", "edge", 5, "eb")])
    assert sorted((i["fingerprint"], i["defect_code"]) for i in items) == [
        ("eb", "degenerate_edge"), ("eb", "small_edge"),
        ("fa", "curve_inconsistency"), ("fa", "small_face")]


def test_a_relocated_judgement_defect_is_matched_on_its_code_too(cameras):
    repaired = object()
    items, _ = _mint([_entity("self_intersection", "face", 3, "fa"),
                      _entity("small_face", "face", 3, "fa", "warning")],
                     {"survivors": [_entity("self_intersection", "face", 9, "fa"),
                                    _entity("small_face", "face", 7, "fa", "warning")],
                      "operations_kept": 1}, shape=repaired)
    assert sorted((i["defect_code"], i["entity_index"]) for i in items) == [
        ("self_intersection", 9), ("small_face", 7)]


def test_a_kernel_failure_while_pairing_keeps_every_item_on_one_coarse_key(cameras, monkeypatch):
    # OCP's Standard_Failure is not a RuntimeError; uncaught, it took down the whole mint and every
    # review item for the run was lost. It must degrade to the coarse key like any kernel failure.
    standard = pytest.importorskip("OCP.Standard", reason="Standard_Failure is the kernel's own")
    rims = [_entity("open_shell", "edge", i, fp, boundary_edges=4)
            for i, fp in enumerate(("a1", "a2"), start=1)]
    monkeypatch.setattr("meshpipeline.cad.repair.operations.free_wire_containing",
                        lambda shape, index: rims[index - 1]["fingerprint"])

    def _kernel_fails(loops, shape=None):
        raise standard.Standard_Failure("BRepTools::OuterWire on a null face")

    monkeypatch.setattr("meshpipeline.cad.repair.conservative._paired_rims", _kernel_fails)
    items, _ = _mint(rims, {"survivors": rims, "operations_kept": 1,
                            "refusals": [_refusal(r["fingerprint"]) for r in rims]},
                     shape=object())
    keys = {i["fingerprint"]: i["pairing_key"] for i in items}
    assert set(keys) == {"a1", "a2"}
    assert keys["a1"] == keys["a2"] != ""
