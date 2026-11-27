"""v5 step 9: surface_crossing - the crossing-kind detectors, the cause they put on a defect, the rule seed per kind,
the two repairs' parameters, and (where OCP is installed) the injected cases through the detector and the rule seed."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v5 import crossing as X
from meshpipeline.cad.repair_v5 import loop

TOL = 0.05


def _row(**kw: Any) -> dict:
    base = {"a": "F1", "b": "F2", "body_a": "B1", "body_b": "B2", "surface_a": "plane", "surface_b": "plane",
            "gap_mm": 0.0, "normal_angle_deg": 0.0, "area_a_mm2": 100.0, "area_b_mm2": 100.0, "box_dev_mm": 0.0,
            "section_mm": 0.0}
    return {**base, **kw}


def _defect(**kw: Any) -> Defect:
    base: dict[str, Any] = {"entities": ("F1",), "member": "gmsh_default", "cls": "surface_crossing",
                            "rule": "edge_through_face: edge_through_face"}
    return Defect("S@F1", "mesher", "edge_through_face", **{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(("row", "kind"), [
    (_row(), "duplicate_faces"),
    (_row(area_b_mm2=60.0), "near_coincident"),                      # coplanar, partly overlapping
    (_row(area_b_mm2=60.0, normal_angle_deg=12.0, section_mm=4.0), "true_crossing"),
    (_row(surface_b="cylinder", normal_angle_deg=90.0), None),        # a touch: no section, not parallel
    (_row(gap_mm=0.2), None),                                         # over the fidelity bound: not touching
    (_row(box_dev_mm=0.2, area_b_mm2=100.0, normal_angle_deg=None, section_mm=1.0), "true_crossing"),
])
def test_the_crossing_kind_of_one_measured_pair(row: dict, kind: str | None) -> None:
    assert X.crossing_kind(row, TOL) == kind


def test_a_duplicate_pair_picks_remove_duplicate_faces_and_carries_the_pairs_across_bodies_only() -> None:
    rows = [_row(a="F3", b="F4", area_b_mm2=60.0, normal_angle_deg=30.0, section_mm=2.0),
            _row(), _row(a="F5", b="F6", body_b="B1")]          # the last is a same-body duplicate
    d = X.attach_crossing(_defect(), rows, TOL)
    assert d.cause["kind"] == "duplicate_faces" and loop.table_key(d) == "surface_crossing:duplicate_faces"
    assert loop.CLASS_REPAIRS[loop.table_key(d)][0] == ("remove_duplicate_faces",)
    assert d.cause["duplicate_pairs"] == [["F1", "F2"]]
    assert d.cause["aim"] == ["F1", "F2", "F5", "F6"] and d.cause["body_a"] == "B1" and d.cause["body_b"] == "B2"
    assert "measured duplicate_faces" in d.rule
    targets, params = seq.params_for("remove_duplicate_faces", d, live={"F1", "F2", "F5", "F6"})
    assert targets == [] and params == {"pairs": [["F1", "F2"]]}


def test_a_true_crossing_seeds_the_imprint_aimed_at_the_crossing_faces() -> None:
    d = X.attach_crossing(_defect(), [_row(a="F7", b="F9", area_b_mm2=10.0, normal_angle_deg=40.0, section_mm=3.0)],
                          TOL)
    assert loop.table_key(d) == "surface_crossing:true_crossing"
    assert loop.CLASS_REPAIRS[loop.table_key(d)][0] == ("imprint_crossing",)
    targets, params = seq.params_for("imprint_crossing", d, live={"F1", "F7", "F9"})
    assert targets == ["F7", "F9"] and params == {"max_partners": 1}
    # no duplicate measured: remove_duplicate_faces says why instead of guessing
    with pytest.raises(seq.NoParameters, match="duplicate"):
        seq.params_for("remove_duplicate_faces", d)
    # conformalise is aimed at the crossing pair's bodies
    assert seq.params_for("conformalise", d)[0] == ["B1", "B2"]


def test_a_defect_with_no_crossing_keeps_its_cause_and_the_class_row_imprints_its_own_faces() -> None:
    d = _defect(cause={"contacts": 0})
    assert X.attach_crossing(d, [_row(surface_b="cylinder", normal_angle_deg=90.0)], TOL) is d
    assert loop.table_key(d) == "surface_crossing"
    assert loop.CLASS_REPAIRS["surface_crossing"][0] == ("imprint_crossing",)
    assert seq.params_for("imprint_crossing", d) == (["F1"], {"max_partners": seq.IMPRINT_PARTNERS})


class _Pool:
    def __init__(self, answer: dict | None) -> None:
        self.answer, self.calls = answer, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> Any:
        self.calls.append((fn, payload))
        return SimpleNamespace(ok=self.answer is not None, value=self.answer, status="ok", error=None)


def _ws() -> Any:
    ids = {f"F{i}": "fp" for i in range(1, 5)} | {"B1": "fp", "B2": "fp"}
    indices = {**{f"F{i}": i for i in range(1, 5)}, "B1": 1, "B2": 2}
    return SimpleNamespace(ids=ids, indices=indices, brep_path="/x.brep", part_diag_mm=20.0)


def test_only_surface_crossing_defects_are_measured_once_per_face_set_and_a_failed_call_changes_nothing() -> None:
    raw = {"pairs": [{**_row(), "a": 1, "b": 3, "solid_a": 1, "solid_b": 2}]}
    pool = _Pool(raw)
    other = _defect(cls="low_quality")
    out = X.measure_crossings(pool, _ws(), [_defect(), _defect(), other])
    assert [fn for fn, _ in pool.calls] == ["v5_crossing_facts"]
    assert pool.calls[0][1]["tol_mm"] == X.tolerance_mm(20.0) == pytest.approx(0.02)
    assert out[0].cause["duplicate_pairs"] == [["F1", "F3"]] and out[2] is other
    failed = X.measure_crossings(_Pool(None), _ws(), [_defect()])
    assert failed[0].cause == {}


# --- real geometry (OCP): the injected cases through the detector and the rule seed --------------------------------

@pytest.fixture(scope="module")
def injected(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools" / "validation" / "v4_eval"))
    import inject

    tmp = tmp_path_factory.mktemp("crossing")
    part = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 10).Shape(),
                           BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(30, 10, -1), gp_Dir(0, 0, 1)), 3, 12).Shape()).Shape()
    src = tmp / "src.step"
    inject.write_step(part, src)
    out = {}
    for kind in ("duplicate_faces", "true_crossing", "edge_through_face"):
        from OCP.BRepTools import BRepTools

        shape = inject.read_step(inject.inject(src, kind, 1, tmp / kind).path)
        out[kind] = tmp / f"{kind}.brep"
        BRepTools.Write_s(shape, str(out[kind]))
    return out


def _measure(brep: Path) -> dict[str, list[dict]]:
    from meshpipeline.cad.repair_v4 import (
        kernel_calls,  # noqa: F401 - registers the kernel fns
        ws_kernel,
    )
    from meshpipeline.cad.repair_v5.crossing_kernel import v5_crossing_facts

    n = ws_kernel._maps(ws_kernel.read_brep(str(brep)))["face"].Extent()
    rows = v5_crossing_facts({"brep_path": str(brep), "tol_mm": X.tolerance_mm(45.0), "part_diag_mm": 45.0,
                              "faces": list(range(1, n + 1))})["pairs"]
    kinds: dict[str, list[dict]] = {}
    for r in rows:
        k = X.crossing_kind(r, X.tolerance_mm(45.0))
        if k:
            kinds.setdefault(k, []).append(r)
    return kinds


def test_duplicate_faces_injected_is_detected_and_its_rule_seed_merges_the_pair(injected: dict[str, Path]) -> None:
    from meshpipeline.cad.repair_v4.body_kernel import _remove_duplicate_faces

    kinds = _measure(injected["duplicate_faces"])
    assert set(kinds) == {"duplicate_faces"}
    pairs = [[r["a"], r["b"]] for r in kinds["duplicate_faces"] if r["solid_a"] != r["solid_b"]]
    res = _remove_duplicate_faces({"brep_path": str(injected["duplicate_faces"]), "pairs": pairs, "quantum": 1e-6,
                                   "out_path": str(injected["duplicate_faces"].with_suffix(".out.brep")),
                                   "part_diag_mm": 45.0, "ids": {"face": {}, "edge": {}}})
    assert "declined" not in res and len(res["merged_faces"]) == len(pairs)


def test_true_crossing_injected_is_detected_and_imprinted_along_the_crossing(injected: dict[str, Path]) -> None:
    from meshpipeline.cad.repair_v4.ops.crossing import _imprint_crossing

    kinds = _measure(injected["true_crossing"])
    assert "true_crossing" in kinds and "duplicate_faces" not in kinds
    faces = sorted({f for r in kinds["true_crossing"] for f in (r["a"], r["b"])})
    res = _imprint_crossing({"brep_path": str(injected["true_crossing"]), "faces": faces, "max_partners": 4,
                             "out_path": str(injected["true_crossing"].with_suffix(".out.brep")), "quantum": 1e-6,
                             "part_diag_mm": 45.0})
    assert "declined" not in res and res["params"]["measured"]["section_length_mm"] > 0


def test_edge_through_face_injected_is_a_crossing_the_imprint_guard_refuses_to_split_a_body(
        injected: dict[str, Path]) -> None:
    """The fin overlaps the part (layer 1's body_overlap, the overlap class's menu); imprinting its loop on both
    bodies would split a solid, and the split guard says so instead of delivering it."""
    from meshpipeline.cad.repair_v4.ops.crossing import _imprint_crossing

    kinds = _measure(injected["edge_through_face"])
    assert "true_crossing" in kinds
    faces = sorted({f for r in kinds["true_crossing"] for f in (r["a"], r["b"])})
    res = _imprint_crossing({"brep_path": str(injected["edge_through_face"]), "faces": faces, "max_partners": 4,
                             "out_path": str(injected["edge_through_face"].with_suffix(".out.brep")),
                             "quantum": 1e-6, "part_diag_mm": 45.0})
    assert "solid count" in res.get("declined", "")
