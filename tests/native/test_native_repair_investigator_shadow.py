# Responsibility: Prove on the REAL kernel that a shadow investigation changes nothing - not the
#                 shape's bytes, not the report the region loop delivers.
# Boundaries: the native tier. The shadow posture's rules (the roster, the discarded conclusion,
#             the fit) are unit-tested in tests/unit/cad/test_repair_investigator_shadow.py; what
#             is proved here is that the composite, the real `run_tool`, the real
#             `RepairSession` and the real region walk compose WITHOUT the shadow leaving a mark.
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
from meshpipeline.cad.repair import (
    caps,  # noqa: E402
    region_loop,  # noqa: E402
)
from meshpipeline.cad.repair import investigator as inv  # noqa: E402
from meshpipeline.cad.repair import model_author as ma  # noqa: E402
from meshpipeline.cad.repair import session as session_module  # noqa: E402
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity  # noqa: E402
from meshpipeline.cad.repair.localize import EntityDefect  # noqa: E402
from meshpipeline.cad.repair.regions import Region  # noqa: E402


def _box(dx, dy, dz, at=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), dx, dy, dz).Shape()


def _overlapping():
    """TWO INTERPENETRATING BOXES in one compound - the volume-stage overlap the investigator
    exists for, and the fixture its live tests use."""
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in (_box(10.0, 10.0, 10.0), _box(10.0, 10.0, 10.0, at=(5.0, 0.0, 0.0))):
        builder.Add(compound, shape)
    return compound


def _defect(index: int = 1) -> EntityDefect:
    return EntityDefect(
        # `mesh_edge_through_face` is what `fragment_bodies` ANSWERS, and 3d-stage evidence is
        # what it requires - so the script's plan is refused for the posture under test, if it is
        # refused at all, and not because the operation does not fit the defect.
        code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error,
        entity_type="face", entity_index=index, message="an edge runs through a face",
        measurements={"stage": "3d"},
        location={"bbox_min": (5.0, 0.0, 0.0), "bbox_max": (10.0, 10.0, 10.0)},
        fingerprint="fp-overlap")


def _brep_defect() -> EntityDefect:
    """A defect the RULES author proposes for: `small_edge` is `fix_small_edges`'s, and that is a
    kernel operation that really runs on this shape. The loop identity test needs a driver that
    actually proposes something, or it is evidence about nothing."""
    return EntityDefect(
        code=DefectCode.small_edge, severity=DefectSeverity.error,
        entity_type="edge", entity_index=1, message="a very short edge",
        measurements={"length_mm": 1e-6},
        location={"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (0.1, 0.1, 0.1)},
        fingerprint="fp-small-edge")


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


class _Reply:
    def __init__(self, text: str) -> None:
        self.assistant_text = text
        self.tool_calls = ()
        self.ok = True


def _tool(name: str, **args) -> _Reply:
    return _Reply(json.dumps({"tool": name, "args": args}))


class Script:
    """A scripted `converse_tools_sync`. The model is the ONE thing a test may not call; every
    other seam here is the real one. It records the tool schemas it was handed."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.handed: list[tuple[str, ...]] = []

    def __call__(self, messages, *, job_id, timeout_s, tools=(), name="", user_id=""):
        self.handed.append(tuple(schema.name for schema in tools))
        if not self.replies:
            return _Reply(json.dumps({"tool": "conclude",
                                      "args": {"escalate": {"reason": "out of script"}}})), False
        return self.replies.pop(0), False


#: Every reply a shadow could possibly make, in one script: it looks, it measures with the real
#: kernel, it probes a rung of the real ladder, it asks for the `edit` tool it does not have, and
#: it concludes a package that validates. If ANY of those could mark the part, this is the script
#: that would do it.
def _everything() -> Script:
    return Script(
        _tool("look"),
        _tool("measure", what="pair_contact", targets=["solid:1", "solid:2"]),
        _tool("probe", tier="0"),
        _tool("edit", op="fragment_bodies", fingerprint="fp-overlap"),
        _Reply(json.dumps({"tool": "conclude", "args": {"package": {
            "intent": "fragment the interpenetrating bodies",
            "plan": [{"op": "fragment_bodies", "fingerprint": "fp-overlap"}],
            "cleared": ["fp-overlap"], "unchanged": [],
            # INSIDE the run's own residual caps, which are a fraction of the
            # part's area and volume: a claim past them is refused for that reason, and this
            # test is not about that refusal.
            "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}}})))


@pytest.fixture
def scripted(monkeypatch):
    def install(script: Script) -> Script:
        monkeypatch.setattr(ma, "converse_tools_sync", script)
        monkeypatch.setattr(ma, "_configured", lambda: True)
        return script
    return install


def test_shadow_leaves_shape_bytes_identical(scripted, monkeypatch):
    """THE DEFINING PROPERTY, on the kernel: the session's BREP bytes are what they were.

    The real `run_tool`, the real ladder, the real `caps.measure`, the real shape - and a script
    that looks, measures, probes, tries to edit and concludes a valid package. Afterwards the
    shape is byte-identical, no package reached the session, and the `edit` was refused as a tool
    this posture does not have.
    """
    # THE OPERATION THE SCRIPT NAMES HAS TO BE IN THE ACTIVE CATALOGUE, or the conclusion is
    # refused for a reason that has nothing to do with the posture under test.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True, raising=False)
    shape = _overlapping()
    session = session_module.RepairSession(shape, remaining_s=lambda: 120.0)
    before = _brep(session.shape)

    script = scripted(_everything())
    watcher = inv.investigator(shape, original=caps.measure(shape), job_id="native-shadow-1",
                               tolerance_mm=0.05, shadow=True,
                               budget=inv._Budget(tool_calls=8, model_calls=10, wall_s=240.0))
    watcher.observe_part((_defect(),))
    watcher.observe_shape(session.shape)

    # A VALIDATED PACKAGE IS STILL DISCARDED: the author returns nothing to commit.
    assert watcher(_region(), (_defect(),), ()) is None

    record = watcher.investigations[-1]
    assert record["end"] == "concluded", record
    assert record["conclusion"]["form"] == "package", record["turns"]
    assert [item["op"] for item in record["conclusion"]["proposal"]["plan"]] == \
        ["fragment_bodies"]
    turns = {turn["tool"]: turn for turn in record["turns"]}
    assert set(turns) == {"look", "measure", "probe", "edit", "conclude"}
    assert turns["edit"]["ok"] is False
    assert "unknown tool 'edit'" in turns["edit"]["error"]
    # NO CLONE WAS EVER MADE, so there is no edit log and nothing to replay.
    assert record["edits"] == []
    # AND THE ROSTER THE MODEL SAW WAS READ-ONLY, plus `conclude`. `conclude` is the driver's own
    # tool, not one of the registry's, and is declared on the native transport since bc88c9f9
    # ("declare conclude as a tool", squashed into #494): an undeclared `conclude` left a native
    # model no way to finish, and a live run made 16 tool calls over 3 regions without one
    # conclusion. It edits nothing, so the roster is still read-only. The unit tier pins the same
    # list (test_repair_investigator_shadow.py::test_the_shadow_hands_the_model_only_the_read_only_schemas).
    assert script.handed[0] == (*inv._read_only_names(), "conclude")

    # THE BYTES. Read-only tools on the real kernel, and the part is untouched.
    assert _brep(session.shape) == before
    assert list(session.packages) == []
    assert _brep(shape) == before


def test_the_shadow_delivers_the_same_run_as_the_rules_alone(scripted, monkeypatch):
    """The region loop's delivered report is the RULES run, on the real kernel and the real
    session - identical once every `*_s` key is stripped at every depth (each entry in
    `operations` carries its own `apply_s`, so stripping only `timings` is not enough), and the
    final shape's BREP bytes are identical too.

    THE SCANNER IS INJECTED and nothing else is. A mesh-stage defect does not come from a BRep
    scan at all - `mesh_probe` reports it, which this tier does not run - so a session whose
    scanner reports it is reporting it the way the run would. Everything the claim is actually
    about stays real: the shape, `caps.measure`, the catalogue's operations, `try_package`, the
    walk, and the whole investigation beside it.
    """
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True, raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_ATTEMPTS", 1, raising=False)
    script = scripted(_everything())

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if not (str(k).endswith("_s") or str(k) == "seconds")}
        if isinstance(value, (list, tuple)):
            return [strip(v) for v in value]
        return value

    def run(wrap: bool):
        shape = _overlapping()
        original = caps.measure(shape)
        rules = ma.rules_author(shape, original=original)
        author = rules
        if wrap:
            author = inv.shadow_investigator(
                shape, original=original, job_id="native-shadow-2", tolerance_mm=0.05,
                rules=rules,
                budget=inv._Budget(tool_calls=8, model_calls=10, wall_s=240.0))
        session = session_module.RepairSession(
            shape, remaining_s=lambda: 240.0, scanner=lambda current: ([_brep_defect()], True))
        result = region_loop.run_regions(shape, author=author, tolerance_mm=0.05,
                                         session=session)
        return {"report": strip(result.to_dict()), "brep": _brep(result.shape)}, author

    plain, _ = run(False)
    watched, composite = run(True)
    assert watched == plain
    # AND IT WAS NOT VACUOUS. An identity that holds because no region was authored for, or
    # because the shadow was never asked, is evidence about nothing.
    assert plain["report"]["regions"], "the walk authored for no region at all"
    assert plain["report"]["packages"], "the rules proposed nothing to be identical about"
    assert composite.shadow_investigations, "the shadow was never asked"
    assert script.handed, "the shadow made no model call"
