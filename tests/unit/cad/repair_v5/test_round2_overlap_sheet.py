"""CRS v5 round 2: robust overlap Booleans (the retry ladder, pairs by overlap volume, an untouched invalid body no longer
vetoes every Boolean) and the owner-approved `drop_sheet` for a stray sheet body."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.cad.repair_v5.test_tournament import _defect

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import open_shell as S
from meshpipeline.cad.repair_v5 import overlap as O
from meshpipeline.cad.repair_v5 import overlap_kernel as K
from meshpipeline.cad.repair_v5 import tournament as T


def test_a_failed_boolean_is_retried_healed_then_at_fuzzy_steps_from_the_overlap_thickness() -> None:
    ladder = K.attempts(2.0, 100.0)
    assert ladder[0] == {"fuzzy_mm": 0.0, "heal_targets": False}
    assert ladder[1] == {"fuzzy_mm": 0.0, "heal_targets": True}
    # 1e-3 and 1e-2 of the 2 mm thickness, both under the 1e-4 x diagonal cap (0.01 mm)
    assert [a["fuzzy_mm"] for a in ladder[2:]] == [pytest.approx(0.002), pytest.approx(0.01)]
    assert K.fuzzy_ladder(None, 100.0) == [pytest.approx(0.001), pytest.approx(0.01)]


def test_retries_stop_at_the_first_answer_that_is_not_an_occt_failure() -> None:
    seen: list[dict] = []

    def run(payload: dict) -> dict:
        seen.append(payload)
        return {"declined": "invalid_result"} if len(seen) < 3 else {"record": {}}

    got, tries = K._with_retries(run, {"bodies": [1, 2]}, K.attempts(1.0, 100.0))
    assert "record" in got and [t["result"] for t in tries] == ["invalid_result", "invalid_result", "applied"]
    seen.clear()
    got, tries = K._with_retries(lambda p: {"declined": "no_overlap"}, {}, K.attempts(1.0, 100.0))
    assert got["declined"] == "no_overlap" and len(tries) == 1


def test_a_cluster_is_played_largest_overlap_first() -> None:
    d = _defect("body_overlap", "overlap_self_intersection", "validity", entities=("B1", "B2", "B3"),
                pairs=(("B1", "B2"), ("B1", "B3"), ("B2", "B3")),
                cause={"pair_common_mm3": {"B1|B3": 50.0, "B2|B3": 900.0}})
    assert T.pairs_by_volume(d) == [("B2", "B3"), ("B1", "B3"), ("B1", "B2")]


def test_the_overlap_detector_carries_each_pairs_volume_onto_the_cause() -> None:
    d = _defect("body_overlap", "overlap_self_intersection", "validity", entities=("B1", "B2"),
                pairs=(("B1", "B2"),))
    (out,) = O.attach_overlap_facts([d], {"duplicates": [], "self_int": {}, "common": {"1|2": 12.5}},
                                    {("B", 1): "B1", ("B", 2): "B2"})
    assert out.cause["pair_common_mm3"] == {"B1|B2": 12.5}


def _sheet_facts(solid_free_edges: int = 0, volume: float = 10.0) -> dict:
    return {"diag_mm": 100.0, "free_edges": 4, "gaps": [], "non_manifold": [], "wire_gaps": [],
            "units": [{"name": "S1", "kind": "solid", "index": 1, "volume": volume, "free_edges": solid_free_edges},
                      {"name": "H1", "kind": "shell", "closable": False, "diag_mm": 30.0, "free_edges": 4}],
            "loops": [{"edges": [1, 2, 3, 4], "span_mm": 30.0, "units": ["H1"]}]}


def test_a_stray_sheet_beside_closed_solids_is_dropped_and_otherwise_stays_unrepairable() -> None:
    c = S.shell_cause("open_shell", _sheet_facts())
    assert c["kind"] == "sheet" and "unrepairable" not in c and "drop it" in c["why"]
    d = _defect("open_shell", "open_shell_volume", "validity", entities=(), cause=c)
    assert loop.table_key(d) == "open_shell_volume:sheet"
    assert loop.CLASS_REPAIRS["open_shell_volume:sheet"][0] == ("drop_sheet",)
    assert seq.params_for("drop_sheet", d) == ([], {})
    # an OPEN solid beside it: the sheet may be its missing face; no solid at all: nothing would be left
    assert S.shell_cause("open_shell", _sheet_facts(solid_free_edges=3))["unrepairable"]
    assert S.shell_cause("open_shell", _sheet_facts(volume=-1.0))["unrepairable"]


def test_the_sheet_kind_is_remeasured_as_the_stray_sheets_left() -> None:
    from meshpipeline.cad.repair_v4.local_verify import LocalResult

    before = _sheet_facts()
    after = {**before, "free_edges": 0, "loops": [], "units": before["units"][:1]}
    d = _defect("open_shell", "open_shell_volume", "validity", entities=(), cause={"kind": "sheet"})
    r = S.remeasured(None, "b", "a", d, LocalResult(False, [], {}, 0.0, []),
                     measure=lambda pool, ws: {"b": before, "a": after}[ws])
    assert r.gone and r.detail["remeasured"] == "sheet: 1 -> 0"


# --- with OCP ----------------------------------------------------------------------------------------------------


def _write(tmp: Any, name: str, shape: Any) -> str:
    from OCP.BRepTools import BRepTools

    path = str(tmp / f"{name}.brep")
    BRepTools.Write_s(shape, path)
    return path


def _compound(parts: list) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c, b = TopoDS_Compound(), BRep_Builder()
    b.MakeCompound(c)
    for p in parts:
        b.Add(c, p)
    return c


def _open_box(at: Any) -> Any:
    """A solid on a 5 mm box's shell with one face missing (free edges; BRepCheck-invalid)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS_Shell

    from meshpipeline.cad.repair_v4.ops.sheets import _keys, _map

    sh, b = TopoDS_Shell(), BRep_Builder()
    b.MakeShell(sh)
    for f in _keys(_map(BRepPrimAPI_MakeBox(at, 5, 5, 5).Shape(), TopAbs_FACE))[:-1]:
        b.Add(sh, f)
    return BRepBuilderAPI_MakeSolid(sh).Solid()


def test_drop_sheet_removes_a_stray_face_and_declines_beside_an_open_solid(tmp_path: Any) -> None:
    pytest.importorskip("OCP")
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    from meshpipeline.cad.repair_v4.ops.sheets import _drop_sheet
    from meshpipeline.cad.repair_v4.verdict.valid_kernel import check_shape

    box = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    sheet = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 50), gp_Dir(0, 0, 1)), 0, 5, 0, 5).Face()
    path = _write(tmp_path, "stray", _compound([box, sheet]))
    out = str(tmp_path / "out.brep")
    rec = _drop_sheet({"brep_path": path, "out_path": out, "quantum": 1e-6, "params": {}, "ids": {}, "targets": []})
    assert "declined" not in rec, rec
    after = check_shape(out, booleans=False)
    assert after["hard"] == [] and after["solids"] == 1
    # beside an OPEN solid the sheet may be its missing face: declined
    path = _write(tmp_path, "open", _compound([_open_box(gp_Pnt(0, 0, 0)), sheet]))
    rec = _drop_sheet({"brep_path": path, "out_path": out, "quantum": 1e-6, "params": {}, "ids": {}, "targets": []})
    assert rec.get("declined", "").startswith("open_solid")


def test_an_untouched_invalid_body_no_longer_vetoes_a_valid_boolean(tmp_path: Any) -> None:
    """3e7c2a1197c3: the staged healer left B6 BRepCheck-invalid; every Boolean on other pairs read invalid_result."""
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair_v4 import body_kernel as BK

    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape()
    # an invalid third body far from the pair: a box whose shell lost a face
    path = _write(tmp_path, "part", _compound([a, b, _open_box(gp_Pnt(100, 0, 0))]))
    assert not BK._is_valid(BK.read_brep(path))
    got = BK._fuse({"brep_path": path, "out_path": str(tmp_path / "fused.brep"), "quantum": 1e-6, "ids": {},
                    "bodies": [1, 2], "fuzzy_mm": 0.0})
    assert "record" in got, got
