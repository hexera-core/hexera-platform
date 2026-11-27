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


_OWNER = "owner-of-the-run"


def _app():
    from meshpipeline.api.security import owner_dep
    app = FastAPI()
    app.include_router(sim_module.router, prefix="/api/v1/simulation")
    app.dependency_overrides[owner_dep] = lambda: _OWNER
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _chat(messages=()):
    """The session the finished run belongs to, as the store holds it."""
    return SimpleNamespace(id=uuid.UUID(_SESSION), owner_id=_OWNER, messages=list(messages))


def _parent():
    t = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    return SimpleNamespace(id=_PARENT, status=JobStatus.succeeded, workspace_purged=False,
                           dispatch_payload=_parent_payload(), started_at=t, ended_at=t + timedelta(minutes=29),
                           geometry_source_id=None, geometry_interpretation_id=None, geometry_source=None)


async def _post(body, *, no_model=True, chat=None, refuse=False, parent=None, face_map=None):
    sent = {}
    parent = parent if parent is not None else _parent()
    face_map = face_map if face_map is not None else (_plate(), 1.0)

    async def _dispatch(job_id, payload, **kw):
        sent["payload"] = payload

    child = SimpleNamespace(id=_CHILD, dispute_operation_key=None, geometry_source_id=None,
                            geometry_interpretation_id=None)
    sessions = SimpleNamespace(get_by_job_id=AsyncMock(return_value=None))
    # the session's conversation: the real append_message, on the row the lock read hands back
    chat = chat if chat is not None else _chat()
    _row = AsyncMock(side_effect=lambda db, sid: chat if sid == uuid.UUID(_SESSION) else None)
    admit = (AsyncMock(side_effect=spend_gate.OutOfCredits("No credits left")) if refuse
             else AsyncMock(return_value="pro"))
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=parent)), \
            patch.object(sim_module.svc, "check_quotas", new=AsyncMock()), \
            patch.object(spend_gate, "admit", new=admit), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository.get_for_update",
                  new=_row), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository.get_internal",
                  new=_row), \
            patch.object(dispute_operation, "claim", new=AsyncMock(return_value=None)), \
            patch.object(dispute_operation, "dispatch_or_record_failure", new=_dispatch), \
            patch.object(face_pick, "current", return_value=face_map), \
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
    # the model is down: the code sentence, which says where, the size step ("finer": half what the last mesh had
    # there, PLAN-fea-36 on a change request), that nothing else changes, the new run and its time
    assert got["reply"] == ("Got it: finer on Face 1 · flat 100 × 100 mm, Face 2 · flat 10 × 10 mm only, about half "
                            "the size the last mesh had there, and nothing else changes. Starting a new run; the last "
                            "one took 29 min.")
    # VER-9: the note carries the run it was asked on, so "finer" is against that mesh
    assert payload["user_notes"][-1]["change_of"] == str(_PARENT)
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
    got = resp.json()
    # VER-18: never an empty reply - where the second look goes, what the rebuild does, the rest kept
    assert got["kind"] == "review" and got["reply"].startswith("I'll take a second look at the spot you marked on")
    assert "finer mesh at that spot" in got["reply"] and "keep everything else you asked for" in got["reply"]
    assert [f["id"] for f in got["faces"]] == ["Face_1", "Face_2"] and got["last_run_minutes"] == 29
    assert payload["user_dispute"]["comment"] == "I think this region is bad, look again"
    from meshpipeline.pipeline.graph import route_after_engine_select
    assert route_after_engine_select({"user_dispute": payload["user_dispute"]}) == "node_reviewer"


_TWO_MARKS = [_mark(0.048, 0.045, 0.0, 0.005), _mark(0.048, 0.042, 0.0, 0.005)]


async def test_marks_with_no_words_rebuild_with_everything_the_first_run_was_asked():
    """VER-18, the live case (dev, 26 Nov: 8b79fdb2 -> 31e51296): one mark, no words. The second look's run is the
    finished run's own payload - its typed notes, the saved settings as applied, the engine - with the dispute and the
    mark's faces added, so the rebuild keeps them all and can make the mesh finer at the mark."""
    resp, payload = await _post({"comment": "", "mode": "rebuild", "flags": _TWO_MARKS[:1]})
    assert resp.status_code == 202, resp.text
    got = resp.json()
    assert got["kind"] == "review" and got["reply"] and got["last_run_minutes"] == 29
    assert [f["id"] for f in got["faces"]] == ["Face_1", "Face_2"]
    first = _parent_payload()
    assert [n["text"] for n in payload["user_notes"]] == [n["text"] for n in first["user_notes"]]
    assert payload["meshing_standard"] == first["meshing_standard"] and payload["mesh_engine"] == "gmsh"
    assert payload["purpose"] == first["purpose"] and payload["job_id"] == str(_CHILD)
    marks = payload["user_dispute"]["marks"]
    assert [m["ordinal"] for m in marks] == [1] and [f["id"] for f in marks[0]["faces"]] == ["Face_1", "Face_2"]
    assert all("sig" in f for f in marks[0]["faces"]), "the rebuild re-checks each face on its own surface"
    # what the rebuild asks: the first run's own requirements stay, and one "finer" at the mark
    from meshpipeline.engines import marked_spots as MS
    req = MS.with_marks({"local": [{"id": "r1", "what": "size", "mm": 3.0}]}, {"user_dispute": payload["user_dispute"]})
    assert [(e["id"], e["what"]) for e in req["local"]] == [("r1", "size"), ("mark_1", "finer")]


# VER-17: whichever worker takes it, and whatever of the old run is no longer kept
def _purged_parent(**kw):
    p = _parent()
    p.workspace_purged = True
    for k, v in kw.items():
        setattr(p, k, v)
    return p


async def test_a_change_still_starts_when_the_old_runs_mesh_is_no_longer_kept():
    # a worker cleared its own copy (worker_disk marks the job purged): a change is a new run from the upload
    resp, payload = await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS}, parent=_purged_parent())
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "change" and "user_dispute" not in payload


async def test_a_change_on_an_upload_no_longer_kept_is_told_before_anything_starts():
    gone = SimpleNamespace(purged_at=datetime(2026, 11, 20, tzinfo=UTC))
    resp, payload = await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS},
                                parent=_purged_parent(geometry_source=gone))
    assert resp.status_code == 409 and payload is None
    assert resp.json()["detail"] == CR.UPLOAD_GONE and "Upload the file again" in CR.UPLOAD_GONE


async def test_marks_that_cannot_be_tied_because_the_face_list_is_gone_still_start_the_change_and_say_so():
    resp, payload = await _post({"comment": "make it finer here", "flags": _TWO_MARKS}, face_map=(None, 1.0))
    assert resp.status_code == 202, resp.text
    got = resp.json()
    assert got["kind"] == "change" and got["job_id"] == str(_CHILD) and got["faces"] == []
    assert payload["user_notes"][-1]["text"] == "make it finer here"
    assert "Your marks were not used" in got["reply"] and "Starting a new run" in got["reply"]
    facts = CR.ChangeFacts(kind=CR.KIND_CHANGE, words="finer here", marks=2, marks_lost=True)
    assert change_reply.check(CR.plain_reply(facts), facts.as_dict()) == ""
    assert change_reply.check("Got it, starting a new run with finer mesh.", facts.as_dict()) != ""


async def test_a_second_look_at_a_mesh_kept_nowhere_is_told_in_plain_words():
    from meshpipeline.application.job_service import DISPUTE_MESH_GONE
    resp, payload = await _post({"comment": "I think this region is bad, look again", "flags": _TWO_MARKS},
                                parent=_purged_parent(artifacts=[]))
    assert resp.status_code == 409 and payload is None
    assert resp.json()["detail"] == DISPUTE_MESH_GONE and "on our side" not in DISPUTE_MESH_GONE


async def test_a_second_look_goes_ahead_when_the_seed_is_kept_though_a_worker_cleared_its_copy():
    from meshpipeline.application.review_seed import SEED_LOGICAL_KEY
    seed_row = SimpleNamespace(logical_key=SEED_LOGICAL_KEY)
    resp, payload = await _post({"comment": "I think this region is bad, look again", "flags": _TWO_MARKS},
                                parent=_purged_parent(artifacts=[seed_row]))
    assert resp.status_code == 202, resp.text
    assert payload["user_dispute"]["of_job_id"] == str(_PARENT)


async def test_a_change_request_and_its_reply_are_kept_in_the_chat_with_the_new_run_on_the_users_turn():
    chat = _chat([{"role": "assistant", "content": "Your mesh is ready."}])
    resp, _ = await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS}, chat=chat)
    assert resp.status_code == 202, resp.text
    got = resp.json()
    *_, user, reply = chat.messages
    assert user == {"role": "user", "job_id": str(_CHILD),
                    "content": f"{_OWNER_WORDS}\n\n2 spots marked: Face 1 · flat 100 × 100 mm, Face 2 · flat 10 × 10 mm"}
    assert reply == {"role": "assistant", "content": got["reply"]}
    # a reload: the history serves both, in order, and the run's box attaches after the person's turn
    from meshpipeline.contracts.user_notes import for_browser
    assert [for_browser(m) for m in chat.messages][-2:] == [user, reply]


async def test_an_ask_is_kept_in_the_chat_with_no_run_on_it():
    chat = _chat()
    resp, _ = await _post({"comment": "finer here", "flags": [_mark(0.5, 0.5, 0.5, 0.005)]}, chat=chat)
    assert resp.json()["kind"] == "ask"
    assert chat.messages == [{"role": "user", "content": "finer here\n\n1 spot marked"},
                             {"role": "assistant", "content": resp.json()["reply"]}]


async def test_a_refused_request_keeps_nothing_in_the_chat():
    chat = _chat()
    resp, payload = await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS}, chat=chat, refuse=True)
    assert resp.status_code == 402 and payload is None
    assert chat.messages == []


async def test_a_doubt_keeps_its_turn_and_a_repeat_of_a_kept_change_keeps_nothing_more():
    chat = _chat()
    await _post({"comment": "I think this region is bad, look again", "flags": _TWO_MARKS[:1]}, chat=chat)
    # VER-18: a second look says what it will do, so its turn is kept like a change's
    assert [m["role"] for m in chat.messages] == ["user", "assistant"]
    assert chat.messages[-1]["content"].startswith("I'll take a second look")
    other = _chat()
    await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS}, chat=other)
    await _post({"comment": _OWNER_WORDS, "flags": _TWO_MARKS}, chat=other)
    assert [m["role"] for m in other.messages] == ["user", "assistant"]


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
