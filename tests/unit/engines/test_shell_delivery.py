# Responsibility: Verify SHSTAGE-7, A SHELL RUN DELIVERS THE SHELL: when the user asked for a shell and it was made,
# the FEA driver makes no solid tet mesh beside it; the delivery's records are the shell's (the deck marker, the
# viewer's mesh.msh of plates grouped by mid-face, quality.json), the executor's finalize and gates pass it on its own
# validity, and the result block, the verdict rows and the count line are the shell's - never a solid's elements
# through a wall or SICN. A shell that cannot be made still falls through to the solid, said.
# Boundaries: the real FEA driver and shell run on a small T-joint (a few seconds), the solid mesher faked to record
# any call; no solver run.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import shell_delivery as SD  # noqa: E402
from meshpipeline.engines import shell_run  # noqa: E402


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


def _drive(ws: Path, monkeypatch, state: dict):
    from meshpipeline.engines import runtime

    calls: list = []
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": 1, "timed_out": False, "log_tail": ""}))
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    ok, marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-shell", publish=None, run=_Run()))
    return ok, marker, calls


@pytest.fixture(scope="module")
def delivered(tmp_path_factory):
    """A T-joint the user asked to mesh as a shell, through the real driver: (workspace, ok, solid mesher calls)."""
    mp = pytest.MonkeyPatch()
    try:
        ws = _stage(sf.t_joint(), tmp_path_factory.mktemp("tjoint") / "ws")
        ok, _marker, calls = _drive(ws, mp, {"purpose": "structural", "fea_mesh_as": "shell"})
    finally:
        mp.undo()
    return ws, ok, calls


def test_a_shell_run_makes_no_solid_and_delivers_the_shell(delivered):
    ws, ok, calls = delivered
    assert ok is True
    assert calls == [], "no solid tet mesh is made beside a shell"
    rec = shell_run.read(ws)
    q = json.loads((ws / "quality.json").read_text())
    assert q["fea"]["shell"] is True and q["cells"] == rec["elements"] > 0 and q["fea_inverted"] == 0
    assert q["element_order"] == 1 and q["fea"]["walls_mm"] == [4.0, 5.0]
    # the deck marker is the shell's own CalculiX deck; no solid solver file is left
    assert (ws / "mesh.inp").read_bytes() == (ws / "shell.inp").read_bytes()
    assert not (ws / "mesh.bdf").exists() and not (ws / "mesh.cdb").exists()
    assert json.loads((ws / "fea_passes.json").read_text())["delivered"] == "shell"


def test_the_executor_finalizes_and_gates_the_shell_on_its_own_validity(delivered):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh import gates as G
    from meshpipeline.engines.gmsh.gmsh_runner import check_mesh, finalize

    ws, _ok, _calls = delivered
    assert check_mesh(ws)["mesh_ok"] is True
    out = finalize(str(ws), [], "gmsh", domain="structural FEA")
    assert out["success"] is True, out
    man = json.loads((ws / "mesh_manifest.json").read_text())
    assert man["quality"]["fea"]["shell"] is True
    ctx = GateCtx(workspace=ws, engine="gmsh")
    assert G._gate_gmsh_manifest_valid(ctx) == (True, "")
    assert G._gate_sicn_floor(ctx) == (True, "")
    assert G._gate_resolution_floor(ctx) == (True, "")
    # a folded element is refused, said as the shell's
    folded = {**man["quality"], "fea_inverted": 3}
    ok, why = G._gate_sicn_floor(GateCtx(workspace=ws, engine="gmsh", manifest={"quality": folded}))
    assert not ok and "3 shell elements are folded or flat" in why


def test_the_result_is_the_shells_count_quality_and_one_plain_line(delivered):
    from meshpipeline.contracts.narration import quality_line
    from meshpipeline.engines.fea_results import results
    from meshpipeline.engines.verdict_limits import fea_limits

    ws, _ok, _calls = delivered
    rec = shell_run.read(ws)
    q = json.loads((ws / "quality.json").read_text())
    fea = results(ws)
    assert fea["elements"] == rec["elements"] and fea["title"] == "FEA shell mesh"
    assert fea["summary"] == SD.SUMMARY.format(n=rec["elements"], q=rec["quads"], t=rec["trias"])
    assert "each element carries its wall thickness in the file" in fea["summary"]
    assert fea["through_wall"] == {}
    keys = [r["key"] for r in fea["check"]["rows"]]
    assert keys[0] == "fea_inverted" and SD.WORST_KEY in keys and "shell_nastran_past" in keys
    assert not any(k in keys for k in ("through_wall_min", "elements_under_floor", "fea_min_scaled_jacobian"))
    assert fea["check"]["outcome"] in ("pass", "concerns")
    # the verdict reads the same rows: no solid wall or SICN row for a shell
    assert [r["key"] for r in fea_limits(q)] == keys[1:]
    assert [s["file"] for s in fea["solvers"]] == ["shell.inp", "shell_abaqus.inp", "shell.bdf", "shell.cdb"]
    assert all("tetra" not in json.dumps(fea).lower() for _ in [0])
    assert quality_line(q, past_limits=[]).startswith("Quality looks good:")
    assert "shell elements" in quality_line(q, past_limits=[])


def test_the_viewer_draws_the_plates_one_part_per_body_as_their_real_quads(delivered):
    import base64

    import numpy as np

    from meshpipeline.engines.fea_results import viewer_face_names
    from meshpipeline.engines.gmsh.viewer_surface import read_msh
    from meshpipeline.engines.registry import get_spec

    ws, _ok, _calls = delivered
    rec = shell_run.read(ws)
    q = json.loads((ws / "quality.json").read_text())
    # NO EMPTY "free" GROUP: every element is in a part
    assert q["groups"] == {} and q["default_group_used"] is False
    # THE PARTS ARE THE BODIES, each holding its mid-faces (no CAD names in this file: Part_k)
    body_of = {m["id"]: m["body"] for m in json.loads((ws / "shell.json").read_text())["mids"]}
    tags = q["fea"]["part_tags"]
    assert sorted(tags) == sorted({body_of[f["name"]] for f in rec["faces"]})
    assert sorted(t for ts in tags.values() for t in ts) == sorted(f["tag"] for f in rec["faces"])
    mesh = read_msh(ws, by_face=True, face_names=viewer_face_names(ws))
    assert mesh["cells"] == [], "no volume cells: the plates are the mesh"
    assert sorted(n for n, _p in mesh["patches"]) == sorted(tags)
    assert sum(len(p) for _n, p in mesh["patches"]) == rec["elements"]
    assert sorted({f["wall_mm"] for f in rec["faces"]}) == [4.0, 5.0]
    # the deck's metres (the T-joint is 200 mm long)
    span = np.ptp(np.asarray(mesh["points"]), axis=0)
    assert 0.05 < float(span.max()) < 1.0
    # QUADS ARE DRAWN AS QUADS: the payload carries each element as one polygon, never a quad's two triangles
    resp = get_spec("gmsh").viewer_surface(ws, roles={}, units="m")
    sizes: list[int] = []
    for p in resp["patches"]:
        assert "positions_b64" not in p
        recs = np.frombuffer(base64.b64decode(p["polys_b64"]), dtype="<u4")
        mine, k = [], 0
        while k < len(recs):
            mine.append(int(recs[k]))
            k += int(recs[k]) + 1
        assert p["face_count"] == len(mine)
        sizes += mine
    assert sizes.count(4) == rec["quads"] > 0 and sizes.count(3) == rec["trias"]


def test_a_shells_parts_carry_the_cads_names(tmp_path):
    """Each body by the STEP product name the assembly staging read (assembly.json), Part_k where it has none,
    holding the surfaces of its mid-faces; a mid-face that cannot be traced to its body leaves the mid-faces."""
    (tmp_path / "shell.json").write_text(json.dumps({"mids": [
        {"id": "Mid_1", "body": "Part_1"}, {"id": "Mid_2", "body": "Part_1"}, {"id": "Mid_4", "body": "Part_2"},
        {"id": "Mid_7", "body": "Part_3"}]}))
    (tmp_path / "assembly.json").write_text(json.dumps({"parts": [
        {"id": "Part_1", "name": "base plate"}, {"id": "Part_2", "name": ""}, {"id": "Part_3", "name": "pipe"}]}))
    rec = {"faces": [{"tag": 1, "name": "Mid_1"}, {"tag": 2, "name": "Mid_2"}, {"tag": 3, "name": "Mid_4"},
                     {"tag": 4, "name": "Mid_7"}]}
    assert SD.part_tags(tmp_path, rec) == {"base plate": [1, 2], "Part_2": [3], "pipe": [4]}
    q = SD.quality_of(rec, SD.part_tags(tmp_path, rec))
    assert q["fea"]["part_tags"] == {"base plate": [1, 2], "Part_2": [3], "pipe": [4]}
    assert "free" not in q["groups"]
    (tmp_path / "quality.json").write_text(json.dumps(q))
    from meshpipeline.engines.fea_results import viewer_face_names

    assert viewer_face_names(tmp_path) == {1: "base plate", 2: "base plate", 3: "Part_2", 4: "pipe"}
    # untraceable: the mid-faces themselves, as before
    rec["faces"].append({"tag": 5, "name": "Mid_9"})
    assert SD.part_tags(tmp_path, rec) == {}
    assert "part_tags" not in SD.quality_of(rec, {})["fea"]


def test_the_downloads_offer_the_shell_files_once(delivered):
    import uuid

    from meshpipeline.application.artifact_uploader import fea_export_plan

    ws, _ok, _calls = delivered
    files = [p.local_path.name for p in fea_export_plan(uuid.uuid4(), ws)]
    assert files == ["shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb"]


def test_the_shell_follows_the_users_count(tmp_path):
    """SHMESH-23 delivered: the count the user named is the shell's, said in its own line (never the solid's
    "built 133,845 - not reached")."""
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged",
                           json.dumps({"elements": 3000, "mode": "about"})]) == 0
    rec = shell_run.read(ws)
    assert rec["made"] and SD.deliver(ws, rec, target={"elements": 3000, "mode": "about"})
    from meshpipeline.engines.fea_results import results

    tgt = results(ws)["target"]
    assert tgt["asked"] == 3000 and tgt["built"] == rec["elements"]
    assert tgt["words"].startswith("Sized to the 3,000 elements you asked for (") or tgt["words"].startswith(
        "Your count (3,000 elements) was already met")
    assert 0.5 * 3000 <= rec["elements"] <= 2.0 * 3000, rec["elements"]


def test_a_shell_that_cannot_be_made_falls_through_to_the_solid(tmp_path, monkeypatch):
    """A said shell on a block: no shell, so the solid ladder runs (said by the shell run's words)."""
    ws = _stage(sf.chunky_block(), tmp_path / "block")
    _ok, _marker, calls = _drive(ws, monkeypatch, {"purpose": "structural", "fea_mesh_as": "shell"})
    assert calls, "the solid mesher ran"
    assert shell_run.read(ws)["made"] is False
    assert not SD.is_shell_workspace(ws)
    assert shell_run.read(ws)["words"].endswith("A solid mesh is delivered instead.")
