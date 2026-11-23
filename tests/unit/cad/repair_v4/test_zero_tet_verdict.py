# Responsibility: Prove a gmsh mesh with ZERO tetrahedra is a vote against the file (a scored `fail`), not an
#                 infrastructure failure (an unscored `inconclusive`), while a real infrastructure failure
#                 still is excluded. Task 30: the message table built `min(qualities)` eagerly and raised.
# Boundaries: hermetic - gmsh is a fake module. The real-gmsh zero-tet case is in
#             tests/native/test_native_repair_v4_zero_tet.py.
from __future__ import annotations

import sys
import types

import pytest

from meshpipeline.cad.repair_v4.verdict import assemble, mesh_child
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict, verdict
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.gmsh_members import result_from_call
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult


def _gmsh_with(qualities: list[float]) -> types.ModuleType:
    """A gmsh whose meshing succeeds and yields `len(qualities)` tetrahedra (none for `[]`)."""
    mod = types.ModuleType("gmsh")
    values: dict[str, float] = {}
    tets = list(range(1, len(qualities) + 1))
    mod.initialize = lambda **_k: None  # type: ignore[attr-defined]
    mod.finalize = lambda: None  # type: ignore[attr-defined]
    mod.option = types.SimpleNamespace(  # type: ignore[attr-defined]
        setNumber=lambda k, v: values.__setitem__(k, v), getNumber=lambda k: values.get(k, 0.0)
    )
    mod.logger = types.SimpleNamespace(start=lambda: None, get=lambda: [])  # type: ignore[attr-defined]
    mesh = types.SimpleNamespace(
        generate=lambda _dim: None,
        getElements=lambda _dim: ([4], [tets], [[]]) if tets else ([], [], []),
        getElementQualities=lambda _ids, _name: list(qualities),
    )
    mod.model = types.SimpleNamespace(  # type: ignore[attr-defined]
        occ=types.SimpleNamespace(importShapes=lambda *_a: None, synchronize=lambda: None),
        getEntities=lambda _dim: [(2, 1)],
        getBoundingBox=lambda *_a: (0.0, 0.0, 0.0, 1.0, 1.0, 1.0),
        mesh=mesh,
    )
    return mod


def _answer(monkeypatch: pytest.MonkeyPatch, qualities: list[float]) -> dict:
    monkeypatch.setitem(sys.modules, "gmsh", _gmsh_with(qualities))
    return mesh_child.run_in_process({"step": "/x.step", "profile": "default"})


def test_a_mesh_with_no_tetrahedra_is_the_files_answer_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(monkeypatch, [])
    assert answer["status"] == "fail"
    assert answer["error_kind"] == "no_volume"
    assert answer["message"] == "gmsh produced no tetrahedra"
    assert answer["elements"] == 0 and answer["min_sicn"] is None


@pytest.mark.parametrize(
    ("qualities", "kind"),
    [([0.5, 0.3], None), ([0.5, 0.001], "low_quality"), ([0.5, -0.1], "inverted_elements"),
     ([float("nan")], "inverted_elements")],
)
def test_the_other_kinds_keep_their_words(monkeypatch: pytest.MonkeyPatch, qualities: list[float], kind: str | None) -> None:
    answer = _answer(monkeypatch, qualities)
    assert answer["error_kind"] == kind
    assert (answer["message"] == "") is (kind is None)


def _verdict_for(monkeypatch: pytest.MonkeyPatch, call: CallResult) -> Verdict:
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: ValidReport(True, [], [], "mm", 1, True))
    monkeypatch.setattr(
        assemble, "check_faithful_steps",
        lambda *a, **k: FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1, [], []),
    )

    class Member:
        name = "gmsh_default"
        config: dict = {}

        def run(self, pool, step, *, timeout_s):
            return result_from_call(self.name, call)

    return verdict(
        None, "s.step", "o.step", members=[Member()],  # type: ignore[arg-type]
        run_panel=lambda pool, step, members, *, timeout_s: [m.run(pool, step, timeout_s=timeout_s) for m in members],
    )


def test_a_zero_tet_mesh_reads_as_a_file_failure_and_is_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(monkeypatch, [])
    v = _verdict_for(monkeypatch, CallResult(True, answer, None, "ok", 1.0))
    assert v.panel[0].status == "fail" and v.panel[0].error_kind == "no_volume"
    assert not v.inconclusive
    assert v.counts_for_scoreboard  # the invisible half of the bug: the file must be COUNTED, as a failure
    assert not v.clean


@pytest.mark.parametrize("call", [
    CallResult(False, None, "worker died", "crashed", 1.0),
    CallResult(True, {"status": "crashed", "error_kind": INFRASTRUCTURE_KIND, "cause": "sigkill",
                      "message": "killed", "located": [], "min_sicn": None, "elements": 0}, None, "ok", 1.0),
])
def test_a_real_infrastructure_failure_is_still_inconclusive_and_unscored(monkeypatch: pytest.MonkeyPatch, call: CallResult) -> None:
    v = _verdict_for(monkeypatch, call)
    assert v.inconclusive
    assert not v.counts_for_scoreboard
