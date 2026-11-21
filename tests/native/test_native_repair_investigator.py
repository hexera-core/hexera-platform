# Responsibility: Drive the investigator's bounded tool loop against the REAL kernel, the REAL
#                 tools and a REAL `RepairSession`, so the loop's seams are exercised rather than
#                 faked. Only the model is scripted - it is the one thing a test may not call.
# Boundaries: the native tier. The rules (the protocol, the budgets, the runaway guards, the
#             refusals) are unit-tested in tests/unit/cad/test_repair_investigator.py; what is
#             proved here is that the loop composes with the real `run_tool`, the real ladder and
#             the real `try_package`, and that an investigation leaves the session alone.
from __future__ import annotations

import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair import caps  # noqa: E402
from meshpipeline.cad.repair import investigator as inv  # noqa: E402
from meshpipeline.cad.repair import model_author as ma  # noqa: E402
from meshpipeline.cad.repair import session as session_module  # noqa: E402
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity  # noqa: E402
from meshpipeline.cad.repair.localize import EntityDefect  # noqa: E402
from meshpipeline.cad.repair.package import TargetPackage  # noqa: E402
from meshpipeline.cad.repair.regions import Region  # noqa: E402


def _box(dx, dy, dz, at=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), dx, dy, dz).Shape()


def _overlapping():
    """TWO INTERPENETRATING BOXES in one compound: the volume-stage overlap this loop exists for,
    and the fixture `pair_contact` and `fragment_bodies` are both about."""
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in (_box(10.0, 10.0, 10.0), _box(10.0, 10.0, 10.0, at=(5.0, 0.0, 0.0))):
        builder.Add(compound, shape)
    return compound


def _defect(index: int = 1) -> EntityDefect:
    return EntityDefect(
        code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
        entity_type="face", entity_index=index, message="overlapping facets",
        measurements={"stage": "3d"},
        location={"bbox_min": (5.0, 0.0, 0.0), "bbox_max": (10.0, 10.0, 10.0)},
        fingerprint="fp-overlap")


def _region() -> Region:
    return Region(index=0, fingerprints=frozenset({"fp-overlap"}), bbox_min=(0.0, 0.0, 0.0),
                  bbox_max=(15.0, 10.0, 10.0), face_indices=frozenset({1}))


def _brep(shape) -> bytes:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "shape.brep"
        BRepTools.Write_s(shape, str(path))
        return path.read_bytes()


class Script:
    """A scripted `converse_tools_sync`. The model is the ONE thing a test may not call; every
    other seam below is the real one."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.rounds = 0

    def __call__(self, messages, *, job_id, timeout_s, tools=(), name="", user_id=""):
        self.rounds += 1
        if not self.replies:
            return _Reply(json.dumps({"tool": "conclude",
                                      "args": {"escalate": {"reason": "out of script"}}})), False
        return self.replies.pop(0), False


class _Reply:
    def __init__(self, text: str) -> None:
        self.assistant_text = text
        self.tool_calls = ()
        self.ok = True


def _tool(name: str, **args) -> _Reply:
    return _Reply(json.dumps({"tool": name, "args": args}))


@pytest.fixture
def scripted(monkeypatch):
    def install(*replies):
        script = Script(*replies)
        monkeypatch.setattr(ma, "converse_tools_sync", script)
        monkeypatch.setattr(ma, "_configured", lambda: True)
        return script
    return install


def test_read_only_investigation_measures_probes_and_leaves_the_session_untouched(scripted):
    """THE REAL TOOLS on the real kernel: `measure pair_contact` finds the common volume, tier 0
    answers, and the session's shape is byte-identical afterwards."""
    shape = _overlapping()
    session = session_module.RepairSession(shape, remaining_s=lambda: 60.0)
    before = _brep(session.shape)

    script = scripted(
        _tool("measure", what="pair_contact", targets=["solid:1", "solid:2"]),
        _tool("probe", tier="0"),
        _Reply(json.dumps({"tool": "conclude",
                           "args": {"mesher_side": {"mesher_problem": ["fp-overlap"],
                                                    "reason": "the two bodies are each valid"}}})))
    author = inv.investigator(shape, original=caps.measure(shape), job_id="native-1",
                              tolerance_mm=0.05,
                              budget=inv._Budget(tool_calls=6, model_calls=8, wall_s=120.0))
    author.observe_shape(session.shape)
    assert author(_region(), (_defect(),), ()) is None     # a verdict is not a package

    record = author.investigations[-1]
    assert record["end"] == "concluded"
    assert record["conclusion"]["form"] == "mesher_side"
    assert [t["tool"] for t in record["turns"]] == ["measure", "probe", "conclude"]
    assert all(t["ok"] for t in record["turns"])
    assert author.verdicts[0]["mesher_problem"] == ["fp-overlap"]
    assert script.rounds == 3
    # AND THE SESSION IS WHERE IT WAS. An investigation reads; it does not write.
    assert _brep(session.shape) == before
    assert session.packages == () or not session.packages


@pytest.mark.skipif(not hasattr(session_module, "Scratch"),
                    reason="SEAM (Task 10): `session.Scratch` and the `edit` tool are another "
                           "lane's; without a scratch clone there is no kept edit to conclude "
                           "on, which this test is entirely about")
def test_overlap_fixture_investigation_commits_through_try_package(scripted, monkeypatch):
    """measure -> edit (fragment_bodies on the CLONE) -> probe -> conclude, and the verdict comes
    from the NORMAL session's `try_package`, with the reviewer posture as configured."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True, raising=False)
    shape = _overlapping()
    session = session_module.RepairSession(shape, remaining_s=lambda: 120.0)
    region, defects = _region(), (_defect(),)

    scripted(
        _tool("measure", what="pair_contact", targets=["solid:1", "solid:2"]),
        _tool("edit", op="fragment_bodies", fingerprint="fp-overlap"),
        _tool("probe", tier="1"),
        _Reply(json.dumps({"tool": "conclude", "args": {"package": {
            "intent": "fragment the interpenetrating bodies",
            "cleared": ["fp-overlap"], "unchanged": [],
            "max_area_delta_mm2": 1e9, "max_volume_delta_mm3": 1e9}}})))
    author = inv.investigator(shape, original=caps.measure(shape), job_id="native-2",
                              tolerance_mm=0.05,
                              budget=inv._Budget(tool_calls=8, model_calls=10, wall_s=240.0))
    author.observe_part(defects)
    author.observe_shape(session.shape)
    package = author(region, defects, ())

    record = author.investigations[-1]
    assert record["end"] == "concluded", record
    assert isinstance(package, TargetPackage)
    # THE PLAN IS THE KEPT-EDIT LOG.
    assert [item["op"].name for item in package.plan] == ["fragment_bodies"]
    assert record["edits"]

    verdict = session.try_package(package)
    assert verdict.region_index == 0
    assert verdict.status in (session_module.COMMITTED, session_module.REVERTED,
                              session_module.ESCALATED)
    # THE VERDICT IS THE SESSION'S, never the author's: the loop judges, the author proposes.
    assert verdict.reason or verdict.committed
