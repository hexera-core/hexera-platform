# Responsibility: Verify "Request a change" on a delivered mesh: marks tie to the right CAD faces in the right unit, "only there" holds, a change starts a new run without the reviewer, a doubt still goes to the reviewer with marks it can show, and the reply is truthful under the guard.
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.simulation as sim_module
from meshpipeline.agents.intake import change_reply
from meshpipeline.application import change_request as CR
from meshpipeline.application import dispute_operation, face_pick, spend_gate
from meshpipeline.cad.face_map import FaceMap
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.persistence.models import JobStatus

_PARENT = uuid.UUID("aaaabbbb-1111-4111-b111-aaaaaaaaaaaa")
_CHILD = uuid.UUID("ccccdddd-2222-4222-b222-cccccccccccc")
_SESSION = "5e55e55e-0000-4000-8000-000000000001"
_OWNER_WORDS = "make the edges around this hole specifically finer mesh. NO where else, just that whole specifically"


def _plate() -> FaceMap:
    """A 100 x 100 mm plate face (Face_1, z = 0) and the 10 x 10 mm wall of a hole in it (Face_2, at x = 50 mm),
    in the map's metres (the skin was in mm: scale_to_m 0.001)."""
    mm = 0.001
    xyz = np.array([[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0],          # Face_1
                    [50, 40, 0], [50, 50, 0], [50, 50, -10], [50, 40, -10]],     # Face_2
                   dtype=np.float32) * mm
    tris = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]], dtype=np.int32)
    return FaceMap(xyz=xyz, tris=tris, label=np.array([0, 0, 1, 1], dtype=np.int32), names=["Face_1", "Face_2"],
                   kind=["plane", "plane"], radius_mm=np.array([np.nan, np.nan]),
                   area_m2=np.array([0.01, 0.0001]), centroid_m=np.array([[0.05, 0.05, 0], [0.05, 0.045, -0.005]]),
                   normal=np.array([[0, 0, 1], [1, 0, 0]], dtype=float), extent_mm=np.array([[100, 100], [10, 10]]),
                   flat=np.array([True, True]), scale_to_m=mm, basis="cad")


def _mark(x, y, z, radius_m, **kw):
    return {"x": x, "y": y, "z": z, "span": radius_m * 2.5, "patch": "Face_1", "note": "", **kw}


# ------------------------------------------------------------------------------- VER-10: marks -> faces ----
def test_a_mark_beside_the_hole_ties_the_face_under_it_and_the_holes_wall_in_its_circle():
    marks = CR.resolve_marks(_plate(), [_mark(0.048, 0.045, 0.0, 0.005)], span_factor=2.5)
    assert [f["id"] for f in marks[0].faces] == ["Face_1", "Face_2"]
    assert marks[0].faces[1]["label"] == "Face 2 · flat 10 × 10 mm"
    assert abs(marks[0].radius_m - 0.005) < 1e-12


def test_a_mark_away_from_the_hole_ties_only_the_face_under_it():
    marks = CR.resolve_marks(_plate(), [_mark(0.010, 0.010, 0.0, 0.005)], span_factor=2.5)
    assert [f["id"] for f in marks[0].faces] == ["Face_1"]


def test_a_big_face_that_only_passes_under_the_circle_is_not_meant():
    # a mark on the hole's wall with a circle reaching the plate: the plate (100 mm, far bigger than the circle) is
    # not meant, the wall is
    marks = CR.resolve_marks(_plate(), [_mark(0.050, 0.045, -0.005, 0.006)], span_factor=2.5)
    assert [f["id"] for f in marks[0].faces] == ["Face_2"]


def test_the_mark_is_read_in_the_unit_the_part_stands_in_now():
    # the map was made while the part read as mm; the unit was since confirmed as cm, so the delivered mesh (in
    # metres) is ten times larger - the ratio face_pick.current gives is applied, as a stage click is
    flag = _mark(0.48, 0.45, 0.0, 0.05)
    assert CR.resolve_marks(_plate(), [flag], scale=10.0, span_factor=2.5)[0].faces[0]["id"] == "Face_1"
    assert not CR.resolve_marks(_plate(), [flag], scale=1.0, span_factor=2.5)[0].tied


def test_a_mark_off_the_part_ties_nothing_and_two_marks_round_one_hole_are_one_spot():
    marks = CR.resolve_marks(_plate(), [_mark(0.048, 0.045, 0.0, 0.005), _mark(0.048, 0.042, 0.0, 0.005),
                                        _mark(0.5, 0.5, 0.5, 0.005)], span_factor=2.5)
    assert [m.tied for m in marks] == [True, True, False]
    assert CR.separate_spots(marks) == 1
    assert [f["id"] for f in CR.faces_of(marks)] == ["Face_1", "Face_2"]


# ------------------------------------------------------------------------------- VER-10: only there ----
def test_only_there_holds_even_with_the_owners_whole_for_hole():
    note = CR.change_note(_OWNER_WORDS, [{"id": "Face_2", "label": "Face 2 · flat 10 × 10 mm", "sig": {}}])
    earlier = {"text": "3 mm elements on the flange", "source": "chat", "at": ""}
    req = {"local": [{"id": "a", "what": "finer", "quote": "make the edges around this hole specifically finer mesh"},
                     {"id": "b", "what": "size", "mm": 3.0, "quote": "3 mm elements on the flange",
                      "where": "the flange"}],
           "sizes": {"global_mm": 1.0},
           "rows": [{"id": "g", "field": "global", "quote": "NO where else, just that whole specifically"}]}
    out = MR.bind_picks(req, [earlier, note])
    assert out["sizes"]["global_mm"] is None, "the whole-mesh size moved off the faces the user marked"
    bound = {e["id"]: e for e in out["local"]}
    assert bound["a"]["faces"] == ["Face_2"] and bound["g"]["faces"] == ["Face_2"]
    assert "faces" not in bound["b"] and bound["b"]["where"] == "the flange", "an earlier requirement changed"
    assert MR.says_whole_mesh("2 mm overall") and not MR.says_whole_mesh("just that whole specifically")


# ------------------------------------------------------------------------------- VER-9 / VER-11: which path ----
def test_the_words_decide_change_or_review():
    assert CR.is_change(_OWNER_WORDS)
    assert CR.is_change("2 mm here please") and CR.is_change("I think this should be finer")
    for doubt in ("I think this region is bad, look again", "is this ok?", "", "check this again"):
        assert not CR.is_change(doubt), doubt


def _parent_payload():
    from meshpipeline.application.dispatch_contract import build
    return build(job_id=str(_PARENT), owner_id="owner", session_id=_SESSION, purpose="structural",
                 mesh_engine="gmsh", review_brief_txt="BRIEF", meshing_standard={"text": "team standard"},
                 user_notes=[{"text": "3 mm elements on the flange", "source": "chat", "at": ""}])


def test_the_new_run_keeps_everything_the_finished_run_had_and_adds_the_change():
    note = CR.change_note(_OWNER_WORDS, [{"id": "Face_2", "label": "Face 2", "sig": {}}])
    out = CR.change_payload({**_parent_payload(), "user_dispute": {"of_job_id": "x"}}, job_id=str(_CHILD), note=note,
                            brief=CR.brief_line(_OWNER_WORDS, note["faces"], of_job_id=str(_PARENT)))
    assert out["job_id"] == str(_CHILD) and "user_dispute" not in out
    assert out["meshing_standard"] == {"text": "team standard"} and out["mesh_engine"] == "gmsh"
    assert [n["text"] for n in out["user_notes"]] == ["3 mm elements on the flange", _OWNER_WORDS]
    assert out["user_notes"][-1]["faces"][0]["id"] == "Face_2"
    assert out["review_brief_txt"].startswith("BRIEF") and "CHANGE ASKED ON THE PREVIOUS RESULT" in out[
        "review_brief_txt"]
    from meshpipeline.pipeline.graph import route_after_engine_select
    assert route_after_engine_select({"user_dispute": out.get("user_dispute") or {}}) == "node_repair_inspect", (
        "the new run went to the reviewer first")


@asynccontextmanager
async def _fake_db():
    yield AsyncMock()


def _app():
    app = FastAPI()
    app.include_router(sim_module.router, prefix="/api/v1/simulation")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _parent():
    t = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    return SimpleNamespace(id=_PARENT, status=JobStatus.succeeded, workspace_purged=False,
                           dispatch_payload=_parent_payload(), started_at=t, ended_at=t + timedelta(minutes=29),
                           geometry_source_id=None, geometry_interpretation_id=None, geometry_source=None)


async def _post(body, *, no_model=True):
    sent = {}

    async def _dispatch(job_id, payload, **kw):
        sent["payload"] = payload

    child = SimpleNamespace(id=_CHILD, dispute_operation_key=None, geometry_source_id=None,
                            geometry_interpretation_id=None)
    sessions = SimpleNamespace(get_by_job_id=AsyncMock(return_value=None))
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=_parent())), \
            patch.object(sim_module.svc, "check_quotas", new=AsyncMock()), \
            patch.object(spend_gate, "admit", new=AsyncMock(return_value="pro")), \
            patch.object(dispute_operation, "claim", new=AsyncMock(return_value=None)), \
            patch.object(dispute_operation, "dispatch_or_record_failure", new=_dispatch), \
            patch.object(face_pick, "current", return_value=(_plate(), 1.0)), \
            patch("meshpipeline.persistence.repositories.job_repository.JobRepository.create",
                  new=AsyncMock(return_value=child)), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository.get_by_job_id",
                  new=sessions.get_by_job_id), \
            patch("meshpipeline.agents.intake.plan_reply._ask", new=AsyncMock(side_effect=RuntimeError("no model"))):
        async with _app() as c:
            resp = await c.post(f"/api/v1/simulation/{_PARENT}/dispute", json=body)
    return resp, sent.get("payload")


async def test_a_change_request_starts_a_new_run_without_asking_the_reviewer():
    resp, payload = await _post({"comment": _OWNER_WORDS, "mode": "rebuild",
                                 "flags": [_mark(0.048, 0.045, 0.0, 0.005), _mark(0.048, 0.042, 0.0, 0.005)]})
    assert resp.status_code == 202, resp.text
    got = resp.json()
    assert got["kind"] == "change" and got["job_id"] == str(_CHILD) and got["session_id"] == _SESSION
    assert [f["id"] for f in got["faces"]] == ["Face_1", "Face_2"] and got["last_run_minutes"] == 29
    assert "user_dispute" not in payload, "the change went to the reviewer as a dispute"
    assert payload["user_notes"][-1]["text"] == _OWNER_WORDS
    assert [f["id"] for f in payload["user_notes"][-1]["faces"]] == ["Face_1", "Face_2"]
    # the model is down: the code sentence, which says where, that nothing else changes, the new run and its time
    assert got["reply"] == ("Got it: I'll make that change on Face 1 · flat 100 × 100 mm, Face 2 · flat 10 × 10 mm "
                            "only, and nothing else changes. Starting a new run; the last one took 29 min.")
    assert "meets your brief" not in got["reply"]


async def test_marks_off_the_part_start_nothing_and_ask_for_the_spot():
    resp, payload = await _post({"comment": "finer here", "flags": [_mark(0.5, 0.5, 0.5, 0.005)]})
    got = resp.json()
    assert got["kind"] == "ask" and got["job_id"] is None and payload is None
    assert "nothing has started" in got["reply"] and "Mark the spot" in got["reply"]


async def test_a_doubt_still_goes_to_the_reviewer_as_before():
    resp, payload = await _post({"comment": "I think this region is bad, look again",
                                 "flags": [_mark(0.048, 0.045, 0.0, 0.005)]})
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "review" and resp.json()["reply"] == ""
    assert payload["user_dispute"]["comment"] == "I think this region is bad, look again"
    from meshpipeline.pipeline.graph import route_after_engine_select
    assert route_after_engine_select({"user_dispute": payload["user_dispute"]}) == "node_reviewer"


def test_the_reviewer_is_handed_marks_it_can_show(tmp_path):
    from meshpipeline.agents.reviewer.context import build_review_prompt
    _system, review = build_review_prompt(
        manifest={"patch_views": {}}, nav_context={}, workspace=tmp_path, step_basename="part.step",
        patch_names=["solid", "free"], patch_colour_legend="", renderable_patches=["solid", "free"],
        mesh_units="m", request="", review_brief="", job_id="j", engine="gmsh", purpose="structural",
        user_dispute={"of_job_id": "p", "mode": "rebuild", "comment": "",
                      "flags": [_mark(0.048, 0.045, 0.0, 0.018, patch="Face_12")]})
    assert "go_to_coordinates(x=0.048, y=0.045, z=0, span=0.045" in review
    assert "[patch: Face_12]" not in review, "a name the review cannot isolate was handed over to isolate"
    assert "without patch_name" in review and "radius: 0.018 m" in review


# ------------------------------------------------------------------------------- VER-12: the reply ----
def _facts(**kw):
    faces = [{"id": "Face_12", "label": "Face 12 · round Ø15 mm"}]
    return CR.ChangeFacts(kind=CR.KIND_CHANGE, words=_OWNER_WORDS, faces=faces, marks=2, spots=1, one_place=True,
                          last_run_minutes=29, **kw).as_dict()


async def test_a_truthful_model_reply_is_said():
    ok = ("Got it: finer mesh on the edges of that hole only (Face 12), nothing else changes. Starting a new run; "
          "the last one took 29 min.")
    got = await change_reply.written(_facts(), ask_fn=AsyncMock(return_value=ok))
    assert got == {"text": ok, "source": change_reply.SOURCE_MODEL, "why": ""}


async def test_the_guard_refuses_an_untruthful_reply():
    f = _facts()
    assert change_reply.check("The mesh meets your brief. Starting a new run on Face 12.", f) == \
        "a verdict on the old mesh"
    assert change_reply.check("Got it, finer on Face 12 and nothing else changes.", f) == "no new run said"
    assert change_reply.check("Finer on Face 12; starting a new run, about 45 min.", f).startswith("a number")
    off = _facts(untied=[2])
    assert change_reply.check("Finer on Face 12, starting a new run now.", off) == "a mark not on the part, not said"
    got = await change_reply.written(f, ask_fn=AsyncMock(return_value="Great! The mesh meets your brief!"))
    assert got["source"] == change_reply.SOURCE_FALLBACK and got["text"] == f["plain_version_that_is_exactly_right"]
