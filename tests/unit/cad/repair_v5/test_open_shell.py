"""v5 step 11, open_shell_volume: the cause rules on measured facts (pure), the unrepairable verdict in the tournament,
the measured sewing ladder, and - with OCP - the detector and the two new ops on small built shapes."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.cad.repair_v5.test_tournament import _WS, _apply, _defect, _patched, _play

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import open_shell as S
from meshpipeline.cad.repair_v5 import tournament as T


def _facts(**kw: Any) -> dict:
    base: dict[str, Any] = {"diag_mm": 100.0, "units": [{"name": "S1", "kind": "solid", "index": 1, "volume": 10.0,
                                                          "free_edges": 0, "closable": True, "diag_mm": 100.0}],
                            "free_edges": 0, "gaps": [], "loops": [], "non_manifold": [], "wire_gaps": []}
    return {**base, **kw}


def test_paired_free_edges_are_a_gap_sewn_at_the_measured_gap_with_the_next_levels_as_variants() -> None:
    gaps = [{"edge": e, "gap_mm": g} for e, g in ((1, 0.001), (2, 0.0012), (3, 0.01), (4, 0.011))]
    c = S.shell_cause("open_shell", _facts(free_edges=4, gaps=gaps))
    assert c["kind"] == "gap" and c["sew_tol_mm"] == pytest.approx(1.1 * 0.011)
    assert c["gap_levels_mm"] == [0.0012, 0.011]
    d = _defect("open_shell", "open_shell_volume", "validity", entities=("B1",), cause=c)
    brief = T.Brief(d, (("sew_gap",),), _WS())
    assert T.variants(("sew_gap",), brief) == [{"tol_mm": pytest.approx(1.1 * 0.0012)}]


def test_an_unpartnered_small_loop_is_a_hole_and_a_large_one_is_unrepairable() -> None:
    loop_small = {"edges": [7, 8, 9], "span_mm": 2.0, "paired": 0, "units": ["S1"]}
    c = S.shell_cause("open_shell", _facts(free_edges=3, gaps=[{"edge": 7, "gap_mm": None}], loops=[loop_small]))
    assert c["kind"] == "hole" and c["aim_edges"] == [7, 8, 9] and "unrepairable" not in c
    big = {**loop_small, "span_mm": 40.0}
    c = S.shell_cause("open_shell", _facts(free_edges=3, loops=[big]))
    assert c["kind"] == "large_hole" and "fill cap" in c["unrepairable"]


def test_a_free_sheet_open_along_its_outline_is_a_genuine_sheet_body() -> None:
    units = [{"name": "H1", "kind": "shell", "closable": False, "diag_mm": 30.0, "free_edges": 4}]
    c = S.shell_cause("open_shell", _facts(units=units, free_edges=4,
                                           loops=[{"edges": [1, 2, 3, 4], "span_mm": 30.0, "units": ["H1"]}]))
    assert c["kind"] == "sheet" and "sheet body" in c["unrepairable"]
    c = S.shell_cause("open_shell", _facts(free_edges=2))
    assert c["kind"] == "open_chain" and c["unrepairable"]


def test_closed_topology_kinds() -> None:
    inverted = _facts(units=[{"name": "S1", "kind": "solid", "index": 3, "volume": -5.0, "free_edges": 0}])
    assert S.shell_cause("non_positive_volume", inverted)["aim_solids"] == [3]
    nm = _facts(non_manifold=[{"edge": 12, "faces": [1, 2, 3, 4]}])
    assert S.shell_cause("non_manifold", nm)["kind"] == "non_manifold"
    shell = _facts(units=[{"name": "H1", "kind": "shell", "closable": True, "diag_mm": 1.0}])
    assert S.shell_cause("no_solids", shell)["kind"] == "closed_shell"
    assert S.shell_cause("open_loop", _facts(wire_gaps=[{"face": 5, "gap_mm": 0.02}]))["kind"] == "wire_gap"
    assert S.shell_cause("no_volume", _facts())["kind"] == "mesher_closed"


def test_attach_translates_kernel_ids_and_keys_the_menu_row() -> None:
    d = _defect("open_shell", "open_shell_volume", "validity", entities=("B1",))
    facts = _facts(free_edges=3, loops=[{"edges": [7, 8, 9], "span_mm": 2.0, "units": ["S1"]}])
    reverse = {("E", 7): "E70", ("E", 8): "E80", ("E", 9): "E90", ("B", 1): "B1"}
    (out,) = S.attach_shell_causes([d], facts, reverse)
    assert out.cause["kind"] == "hole" and out.cause["aim"] == ["E70", "E80", "E90"]
    assert loop.table_key(out) == "open_shell_volume:hole"
    assert seq.params_for("fill_loop", out)[0] == ["E70", "E80", "E90"]


def test_an_unrepairable_defect_is_recorded_and_never_played() -> None:
    d = _defect("open_shell", "open_shell_volume", "validity", entities=("B1",),
                cause={"kind": "sheet", "unrepairable": "a genuine sheet body"})
    tried: list = []
    with _patched(_apply(tried)):
        out, t, counters, lines = _play(d, (("fill_loop",),))
    assert tried == [] and t.log.records == [] and counters.skipped_why["unrepairable"] == 1
    assert any("unrepairable: a genuine sheet body" in line for line in lines)


def test_one_part_wide_gap_under_two_codes_is_one_defect() -> None:
    facts = _facts(free_edges=2, gaps=[{"edge": 1, "gap_mm": 0.01}, {"edge": 2, "gap_mm": 0.01}])
    a = _defect("open_shell", "open_shell_volume", "validity", entities=())
    b = _defect("no_solids", "open_shell_volume", "validity", entities=())
    (out,) = S.attach_shell_causes([a, b], facts, {})
    assert out.code == "open_shell" and out.cause["also_codes"] == ["no_solids"]


def test_a_gap_reduced_is_progress_and_more_free_edges_is_a_new_defect() -> None:
    from meshpipeline.cad.repair_v4.local_verify import LocalResult

    paired = [{"edge": e, "gap_mm": 0.01} for e in range(10)]
    states = {"before": _facts(free_edges=10, gaps=paired), "after": _facts(free_edges=2, gaps=paired[:2]),
              "worse": _facts(free_edges=12, gaps=paired[:2])}
    d = _defect("open_shell", "open_shell_volume", "validity", entities=(), cause={"kind": "gap"})
    local = LocalResult(False, [], {}, 0.0, [])
    r = S.remeasured(None, "before", "after", d, local, measure=lambda pool, ws: states[ws])
    assert r.gone and not r.new_hard and r.detail["remeasured"] == "gap: 10 -> 2"
    r = S.remeasured(None, "before", "worse", d, local, measure=lambda pool, ws: states[ws])
    assert r.new_hard and "more free edges" in r.line()


# --- with OCP: the detector and the ops on built shapes ---------------------------------------------------------


def _ocp_cases(tmp: Any) -> dict[str, str]:
    pytest.importorskip("OCP")
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Shell

    def faces(shape: Any) -> list:
        out, e = [], TopExp_Explorer(shape, TopAbs_FACE)
        while e.More():
            out.append(e.Current())
            e.Next()
        return out

    def moved(shape: Any, v: tuple) -> Any:
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(*v))
        return BRepBuilderAPI_Transform(shape, t, True).Shape()

    def compound(parts: list) -> Any:
        c, b = TopoDS_Compound(), BRep_Builder()
        b.MakeCompound(c)
        for p in parts:
            b.Add(c, p)
        return c

    def shell(fs: list) -> Any:
        s, b = TopoDS_Shell(), BRep_Builder()
        b.MakeShell(s)
        for f in fs:
            b.Add(s, f)
        return s

    box = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    fs = faces(box)
    shapes = {
        "gap": compound([BRepBuilderAPI_MakeSolid(shell(fs[:-1] + [moved(fs[-1], (0, 0, 0.01))])).Solid(),
                         moved(box, (20, 0, 0))]),
        "sheet": compound([fs[0]]),
        "closed_shell": compound([shell(fs)]),
        "inside_out": compound([box.Reversed()]),
        "non_manifold": compound([BRepBuilderAPI_MakeSolid(shell(faces(
            BRepAlgoAPI_Fuse(box, moved(box, (10, 10, 0))).Shape()))).Solid()]),
    }
    out = {}
    for name, shape in shapes.items():
        out[name] = str(tmp / f"{name}.brep")
        BRepTools.Write_s(shape, out[name])
    return out


@pytest.mark.parametrize(("name", "code", "kind", "op", "params"), [
    ("gap", "open_shell", "gap", "_sew_gap", lambda c: {"tol_mm": c["sew_tol_mm"]}),
    ("closed_shell", "no_solids", "closed_shell", "_make_volume", lambda c: {}),
    ("inside_out", "non_positive_volume", "inside_out", "_orient_and_close", lambda c: {}),
    ("non_manifold", "non_manifold", "non_manifold", "_split_non_manifold", lambda c: {}),
    ("sheet", "no_solids", "sheet", None, None),
])
def test_detector_and_rule_seed_on_built_shapes_end_layer1_clean(tmp_path: Any, name: str, code: str, kind: str,
                                                                  op: str | None, params: Any) -> None:
    paths = _ocp_cases(tmp_path)
    from meshpipeline.cad.repair_v4.ops import manifold, shells
    from meshpipeline.cad.repair_v4.verdict.valid_kernel import check_shape
    from meshpipeline.cad.repair_v5.open_shell_kernel import v5_shell_facts

    facts = v5_shell_facts({"brep_path": paths[name]})
    cause = S.shell_cause(code, facts)
    assert cause["kind"] == kind
    assert check_shape(paths[name], booleans=False)["hard"]
    if op is None:
        assert cause["unrepairable"]
        return
    fn = getattr(manifold, op, None) or getattr(shells, op)
    out = str(tmp_path / f"{name}.out.brep")
    record = fn({"brep_path": paths[name], "out_path": out, "quantum": 1e-6, "params": params(cause),
                 "part_diag_mm": facts["diag_mm"], "targets": [], "ids": {}})
    assert "declined" not in record, record
    after = check_shape(out, booleans=False)
    assert after["hard"] == [] and after["solids"] >= 1
