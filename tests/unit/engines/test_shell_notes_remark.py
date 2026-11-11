# Responsibility: Verify what the result's "From your notes" lines say when a SHELL is the delivered mesh and the notes' mesh requirements were read: the shell's own asked-against-delivered line, and not the solid plan's AI remark (it tells of a solid mesh that was never made); with no requirement read, the lines are the ones kept before.
# Boundaries: the real FEA driver and shell run on a small T-joint (a few seconds), the two readings of the notes stubbed (no model), no solid mesher.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer  # noqa: E402

from meshpipeline.contracts import user_notes  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines import shell_run  # noqa: E402

SOLID_REMARK = "Used your mesh-quality notes as guidance; this workflow retains the solid tetrahedron mesh."


class _Run:
    deadline_s = None

    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = sf.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def _drive(ws: Path, monkeypatch, reading: dict):
    """The real driver on a staged T-joint settled as a shell; the solid plan's AI leaves SOLID_REMARK."""
    from meshpipeline.engines import runtime

    async def ai(*_a, **_k):
        return {"decision": {"notes_remark": SOLID_REMARK}}

    async def decide(_state, **_kw):
        return reading

    monkeypatch.setattr(FD.FA, "decide", ai)
    monkeypatch.setattr(FD, "element_target", lambda _state, _ai: None)
    monkeypatch.setattr(MR, "decide", decide)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: (_ for _ in ()).throw(AssertionError("the solid mesher was called"))))
    state = {"purpose": "structural", "fea_mesh_as": "shell", "user_notes": [{"id": "n1", "text": CRITERIA}]}
    ok, _marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-remark", publish=None, run=_Run()))
    return ok


def test_a_delivered_shell_tells_its_own_line_not_the_solid_plans(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    req, dropped, _na = MR.check(json.dumps(criteria_answer()), CRITERIA)
    assert dropped == []
    assert _drive(ws, monkeypatch, MR.section("used", requirements=req, source="model")) is True
    assert shell_run.read(ws)["made"] is True
    rows = user_notes.read_remarks(ws, raw=True)
    assert [r["by"] for r in rows] == ["mesh_requirements"], rows
    assert "you asked for" in rows[0]["text"] and "solid" not in rows[0]["text"]


def test_with_no_requirement_read_the_remarks_are_as_before(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    assert _drive(ws, monkeypatch, MR.section("none")) is True
    assert shell_run.read(ws)["made"] is True
    assert user_notes.read_remarks(ws, raw=True) == [{"by": "fea_ai", "text": SOLID_REMARK}]


def test_dropping_a_remark_leaves_the_others_and_never_fails(tmp_path):
    user_notes.record_remark(tmp_path, "one", by="a")
    user_notes.record_remark(tmp_path, "two", by="b")
    user_notes.drop_remark(tmp_path, by="a")
    assert user_notes.read_remarks(tmp_path, raw=True) == [{"by": "b", "text": "two"}]
    user_notes.drop_remark(tmp_path, by="a")                            # nothing of that step: no change
    user_notes.drop_remark(tmp_path / "nowhere", by="b")
    user_notes.drop_remark(None, by="b")
    assert user_notes.read_remarks(tmp_path) == ["two"]
