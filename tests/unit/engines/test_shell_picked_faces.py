# Responsibility: Verify NOTE-17 on the shell route: a size typed with a CAD face picked on the model sizes the shell mid-face(s) made from that face (cad/part_words.with_picks -> engines/shell_places, SHMESH-52), told on "the face you picked (...)" with the size measured; the rest of the shell keeps the global size; a picked face no longer on the model is told and nothing is guessed.
# Boundaries: a T joint built in the test and shelled for real through the FEA driver (gmsh + OCP, skipped without them); the mesh requirements and the parts reading are fakes (no model).
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402
from tests.unit.engines.test_shell_local_truth import _Run, _stage  # noqa: E402

from meshpipeline.cad import face_map as FM  # noqa: E402
from meshpipeline.cad import part_words as PW  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines import shell_run  # noqa: E402

GLOBAL, LOCAL = "8 mm elements", "2 mm here"


def _picked(ws: Path, point, normal) -> dict:
    """The face under a click as the geometry check's map names it (read off the upload, mm)."""
    from meshpipeline.cad.scout import read_cad

    fm = FM.from_cad(read_cad(ws / "upload.step"), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, point, normal)
    assert k is not None
    return {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}


def _req(face: dict, local: str = LOCAL, mm: float = 2) -> dict:
    args = json.dumps({"stated_unit": "none", "counts": [], "layers": [], "physics": [], "element_kind": [],
                       "limits": [], "other": [],
                       "sizes": [{"what": "global", "value": 8, "unit": "mm", "quote": GLOBAL}],
                       "local": [{"where": "here", "what": "size", "value": mm, "unit": "mm", "quote": local}]})
    req, dropped, _na = MR.check(args, f"{GLOBAL}. {local}", route="fea")
    assert not dropped, dropped
    return MR.bind_picks(req, [{"text": GLOBAL}, {"text": local, "faces": [face]}])


def _drive(ws: Path, monkeypatch, req: dict) -> dict:
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime

    async def ai(*_a, **_k):
        return {"decision": {}}

    async def decide(_state, **_kw):
        return MR.section("used", requirements=req, source="model")

    async def parts(*_a, **_kw):
        return PW.section("none")                       # the AI step off: the pick alone sizes the face

    monkeypatch.setattr(FD.FA, "decide", ai)
    monkeypatch.setattr(FD, "element_target", lambda _state, _ai: None)
    monkeypatch.setattr(MR, "decide", decide)
    monkeypatch.setattr(FD.PS, "decide", parts)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: (_ for _ in ()).throw(AssertionError("the solid mesher was called"))))
    state = {"purpose": "structural", "fea_mesh_as": "shell",
             "user_notes": [{"id": "n1", "text": GLOBAL}, {"id": "n2", "text": LOCAL}]}
    ok, _marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-pick", publish=None, run=_Run()))
    assert ok is True
    return shell_run.read(ws)


def test_a_size_typed_with_a_picked_face_sizes_the_shell_face_made_from_it(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    side = _picked(ws, [0.098, 0.050, 0.035], [-1.0, 0.0, 0.0])          # the stiffener's side face (x = 98 mm)
    rec = _drive(ws, monkeypatch, _req(side))
    assert rec["made"] is True
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is True and row["met"] is True, row
    assert row["delivered"].startswith(f"the face you picked ({side['label']}): elements are about"), row
    assert 0.8 * 2 <= row["measured"] <= 1.25 * 2
    (place,) = rec["places"]["places"]
    assert place["faces"] == 1 and place["id"] == "Pick_1" and place["set_mm"] == 2.0
    # the stiffener's mid-face alone was sized: the base plate keeps the global 8 mm across it; along the junction
    # line it shares with the stiffener it takes the stiffener's count (one conforming edge)
    req = MR.check(json.dumps({"stated_unit": "none", "limits": [], "other": [],
                               "sizes": [{"what": "global", "value": 8, "unit": "mm", "quote": GLOBAL}]}),
                   GLOBAL, route="fea")[0]
    alone = _drive(_stage(sf.t_joint(), tmp_path / "global_only"), monkeypatch, req)
    picked = {f["name"]: f["elements"] for f in rec["faces"]}
    base = {f["name"]: f["elements"] for f in alone["faces"]}
    (stiffener,) = place["mids"]
    assert picked[stiffener] >= 0.8 * 100 * 60 / 2 ** 2 and base[stiffener] <= 100 * 60 / 6 ** 2
    for mid in set(picked) - {stiffener}:
        assert picked[mid] <= 1.2 * (100 / 2) * (98 / 8), (picked, base)       # 2 mm along the junction, 8 mm across
    print("MEASURED shell", row["measured"], "picked run", picked, "global only", base)


def test_a_picked_face_no_longer_on_the_model_sizes_nothing_on_the_shell_and_is_told(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    side = _picked(ws, [0.098, 0.050, 0.035], [-1.0, 0.0, 0.0])
    gone = {**side, "sig": {**side["sig"], "area_m2": side["sig"]["area_m2"] * 1.5}}   # a different face now
    rec = _drive(ws, monkeypatch, _req(gone))
    assert rec["made"] is True
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is False, row
    assert "is no longer on the model as meshed" in row["delivered"] and "Pick_1" not in json.dumps(rec.get("places"))


def test_a_size_typed_with_a_hole_bore_picked_sizes_the_hole_edge_of_the_shell(tmp_path, monkeypatch):
    # SHMESH-52b: the bore is only an edge of the shell (its rim on the mid-plane): the size goes on that rim
    ws = _stage(sf.plate_with_hole(t=3.0, r=10.0), tmp_path / "ws")
    bore = _picked(ws, [0.110, 0.050, 0.0015], [-1.0, 0.0, 0.0])            # the bore at x = 110 mm
    rec = _drive(ws, monkeypatch, _req(bore, "1 mm elements here", 1))
    assert rec["made"] is True
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is True and row["met"] is True, row
    assert "on a shell it is an edge; the elements along it are about" in row["delivered"]
    assert 0.8 <= row["measured"] <= 1.25, row
    (place,) = rec["places"]["places"]
    assert place["edge"] is True and place["edges"] >= 1 and place["id"] == "Pick_1"
    print("MEASURED shell rim", row["measured"], row["delivered"])
