# Responsibility: lane C17 - the published shape of search_seq's local verification (the result's trace words).
# Boundaries: hermetic; no kernel.
from __future__ import annotations

from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult, PairResult


def test_the_outcome_and_trace_line_say_what_was_measured() -> None:
    assert LocalResult(True, [], {}, 1.24).line() == "gone (1.2 s)"
    assert LocalResult(False, [], {}, 0.5).outcome == "still_there"
    broke = LocalResult(True, [{"code": "open_shell", "entities": ["B3"]}], {}, 0.8)
    assert broke.outcome == "new_defect" and broke.line() == "new defect: open_shell on B3 (0.8 s)"


def test_a_cluster_defect_carries_its_pairs_and_the_result_reports_each() -> None:
    d = Defect("s1", "validity", "body_overlap", ("B1", "B2", "B3"), pairs=(("B1", "B2"), ("B2", "B3")))
    r = LocalResult(False, [], {}, 2.0, pairs=[PairResult(("B1", "B2"), True), PairResult(("B2", "B3"), False)])
    assert d.pairs[0] == ("B1", "B2") and [p.gone for p in r.pairs] == [True, False]


def test_every_entrant_meshes_a_before_region_that_EXISTS(tmp_path, monkeypatch) -> None:
    """v5 step 5 (jig, seam_variant on 00607178): the remembered "before" check hid that only the FIRST call exported
    the before-region; the second entrant meshed a path nobody wrote (FileNotFoundError, the whole file lost)."""
    import os
    from types import SimpleNamespace

    from meshpipeline.cad.repair_v4 import local_verify as lv

    brep = tmp_path / "before.brep"
    brep.write_text("x")
    ws_before = SimpleNamespace(brep_path=str(brep), ids={"B1": "fp", "F1": "fp"}, indices={"B1": 1, "F1": 1},
                                history=[], part_diag_mm=10.0)

    def kernel(pool, fn, payload, timeout_s):
        if payload.get("export"):
            with open(payload["export"], "w") as fh:
                fh.write("region")
        return {"solids": [1], "neighbours": [], "codes": {}, "overlaps": [], "tested_pairs": []}

    meshed: list[bool] = []

    def mesh(pool, path, member):
        meshed.append(os.path.exists(path))
        return SimpleNamespace(status="pass", error_kind=None, seconds=0.0)

    monkeypatch.setattr(lv, "_kernel", kernel)
    monkeypatch.setattr(lv, "_mesh_region", mesh)
    monkeypatch.setattr(lv, "_BEFORE", {})
    d = Defect("s1", "mesher", "periodic_surface", ("F1",), member="gmsh_alt")
    for n in range(2):                    # two entrants of one tournament: same ws_before
        after = tmp_path / f"after{n}.brep"
        after.write_text("y")
        ws_after = SimpleNamespace(brep_path=str(after), ids=ws_before.ids, indices=ws_before.indices, history=[],
                                   part_diag_mm=10.0)
        lv.verify_local(None, ws_before, ws_after, d, timeout_s=1.0)
        # entrants run in PARALLEL: the next one can miss the remembered mesh while hitting the remembered check
        for key in [k for k in lv._BEFORE if k[0] == "mesh"]:
            del lv._BEFORE[key]
    assert len(meshed) == 4 and all(meshed)
