# Responsibility: Prove the LIVE investigator, built the way production builds it
#                 (`model_author.select_author`), gets a real scratch clone its `edit` tool can
#                 change - and that the edit lands on the clone, never on the part.
"""The live wiring, end to end, with no kernel and no router.

Every other investigator test injects a fake `scratch_factory` or a fake `run_tool`, which is how
a production investigator with NO scratch clone shipped: `select_author` passes no factory, the
default lookup found nothing, `Workspace.editor` stayed None, and every `edit` was refused with
"this workspace has no scratch clone". Here only the leaves are doubles - the model's replies, the
copy (`_clone`), the counts, the operation catalogue, the scratch session's scanner and measurer -
and the driver, `Scratch.of`, `Editor`, `run_tool` and `_conclude` are the real ones.
"""
from __future__ import annotations

import json

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import investigator as inv
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair import model_author as ma
from meshpipeline.cad.repair import session as sess
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import TargetPackage
from meshpipeline.cad.repair.regions import Region

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=20, edges=60, vertices=40,
                   diagonal_mm=200.0, max_tolerance_mm=0.01,
                   surface_area_mm2=1000.0, volume_mm3=8000.0)
FP = "edge:aaaa"
DEFECT = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                      entity_type="edge", entity_index=1, message="x", fingerprint=FP,
                      measurements={},
                      location={"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.0, 1.0, 1.0)})
REGION = Region(index=0, fingerprints=frozenset({FP}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(2.0, 2.0, 2.0), face_indices=frozenset({1, 2}))


class _Shape:
    def __init__(self, name: str) -> None:
        self.name = name


class _Op:
    """A catalogue double: records the shape every `apply` was handed."""

    name = "fix_small_edges"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self) -> None:
        self.calls: list = []

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls.append(shape)
        return OpOutcome(shape=_Shape("edited"),
                         mapping=EntityMapping(moved={}, gone=frozenset({FP}), basis="kernel"))


class _Call:
    def __init__(self, name: str, args: dict) -> None:
        self.id, self.name, self.arguments = "c1", name, json.dumps(args)


class _Reply:
    def __init__(self, *, tool_calls=(), text: str = "") -> None:
        self.tool_calls, self.assistant_text, self.ok = tuple(tool_calls), text, True


class _Router:
    """A scripted `converse_tools_sync`. Past its script it escalates, so a refused turn ends
    the investigation rather than the test."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.results: list[str] = []

    def __call__(self, messages, *, job_id, timeout_s, tools=(), name="", user_id=""):
        self.results.append(json.dumps(messages[-1], default=str))
        if self.replies:
            return self.replies.pop(0), False
        return _Reply(text=json.dumps({"tool": "conclude", "args": {
            "escalate": {"reason": "script spent"}}})), False


@pytest.fixture
def live(monkeypatch):
    op = _Op()
    monkeypatch.setattr(it, "_clone", lambda shape: _Shape(f"clone-of-{shape.name}"))
    monkeypatch.setattr(it, "_counts_of", lambda shape: {"faces": 20, "solids": 1})
    monkeypatch.setattr(it, "_solids_of", lambda shape: 1)
    monkeypatch.setattr(ma, "op_catalogue", lambda: (op,))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    # THE REAL `Scratch.of`, with only the scratch session's kernel reads scripted: the scanner
    # reports the edited clone clean and the measurer reports the upload's numbers.
    real_of = it.Scratch.of
    opened: list = []

    def scratch_of(source, **kwargs):
        scratch = real_of(source, measurer=lambda _shape: ORIGINAL,
                          scanner=lambda _shape, valid=None: ((), True), **kwargs)
        opened.append(scratch)
        return scratch

    monkeypatch.setattr(it.Scratch, "of", scratch_of)

    # THE PRODUCTION POSTURE: investigator live, a router configured, no tournament.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT", "off", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    monkeypatch.setattr(ma, "_configured", lambda: True)
    return op, opened


def test_live_investigator_edits_a_scratch_clone_and_concludes_a_package(live, monkeypatch):
    op, opened = live
    part = _Shape("part")
    router = _Router(
        _Reply(tool_calls=[_Call("edit", {"op": "fix_small_edges", "fingerprint": FP})]),
        _Reply(text=json.dumps({"tool": "conclude", "args": {"package": {
            "intent": "remove the small edge", "cleared": [FP], "unchanged": [],
            "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}}})))
    monkeypatch.setattr(ma, "converse_tools_sync", router)

    author, label = ma.select_author(part, original=ORIGINAL, job_id="job-live",
                                     tolerance_mm=0.01)
    assert label == inv.LABEL
    author.observe_part((DEFECT,))
    package = author(REGION, (DEFECT,), ())

    record = author.investigations[-1]
    # What the model was told after its `edit` - the tool result it reads next.
    assert "no scratch clone" not in router.results[1], router.results[1]
    assert len(opened) == 1
    # THE EDIT RAN ON THE CLONE, and the part was never handed to an operation.
    assert [s.name for s in op.calls] == ["clone-of-part"]
    assert part.name == "part" and opened[0].shape is not part
    assert [e.status for e in opened[0].edits] == [sess.KEPT]
    # AND A PACKAGE WAS CONCLUDED, whose plan is the kept-edit log.
    assert record["end"] == "concluded", record
    assert isinstance(package, TargetPackage)
    assert [item["op"].name for item in package.plan] == ["fix_small_edges"]
