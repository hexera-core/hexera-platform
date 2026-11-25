# Responsibility: Lock the intake to its standard questionnaire - drive every route through a matrix of user behaviours (and of model misbehaviours) and assert the exact sequence of steps the chat shows.
# Boundaries: the real chat route, message authority, intake turn, executor and gates, in process; the database, the object store, the geometry worker and the dispatch are stood in for, and the model is a scripted stand-in (good, or misbehaving on purpose). No network, no model.
"""THE STANDARD QUESTIONNAIRE, LOCKED (agents/intake/flow).

    FEA           purpose -> stage -> confirmed -> analysis -> plan -> started
    CFD           purpose -> stage -> confirmed -> flow_question -> plan -> started

Each case below is one way a user (or a model) could take a chat off that sequence. The assertion is
on step ids, never prose, so a future change that adds a question, repeats a step, skips one, falls
back to a chat line where a box was meant, or shows the application's own words as the user's fails
here.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import meshpipeline.agents.intake.approval as ap
from meshpipeline.agents.intake import flow
from meshpipeline.agents.intake import message as msg
from meshpipeline.agents.intake.admission_token import CONFIRM_REQUIREMENTS_ASK
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import purpose_map as pm
from meshpipeline.application.geometry_confirmation import (
    CONFIRMED_MARK,
    fea_confirmation_message,
    with_declaration,
)
from meshpipeline.contracts import user_notes as un

SID = uuid.UUID("5eed0000-1111-4222-8333-444455556666")
P, S, C, A, F, PL, ST, ANS, PC, FK = (flow.PURPOSE, flow.STAGE, flow.CONFIRMED, flow.ANALYSIS, flow.FLOW_QUESTION,
                                      flow.PLAN, flow.STARTED, flow.ANSWER, flow.PURPOSE_CLARIFY, flow.FLOW_KIND)

FEA_DECL = fea_confirmation_message(SimpleNamespace(
    part="Bracket", bodies=1, fea_connection="", clean_summary="The geometry is clean.",
    size_mm=[60.0, 50.0, 10.0], unit="mm", unit_fixed=False))
INTERNAL_DECL = (
    f"{CONFIRMED_MARK} the file is the fluid volume itself (Elbow), input_kind fluid-domain; the fluid flows "
    "through it. Openings: inlet (inlet), 40 mm across at (0, 0, 0) mm; outlet (outlet), 40 mm across at "
    "(100, 60, 0) mm. A point inside the flow: (50, 10, 0) mm. Part size: 100 x 60 x 40 mm. "
    "The file is in millimetres.")
EXTERNAL_DECL = (
    f"{CONFIRMED_MARK} the file is a solid body (Car), input_kind solid-body; the fluid flows around it. "
    "No openings: the fluid flows around the whole body. The fluid travels along +x. Reference length: "
    "4000 mm along the flow. Far-field margins in reference lengths: 5 upstream, 10 downstream, 5 to each "
    "side, 5 above. The part is free in the flow; every far-field face is open. Part size: 4000 x 1800 x "
    "1400 mm. The file is in millimetres.")
#: the openings the stage stores with an internal declaration (geometry_confirmation.patches_from)
INTERNAL_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40.0, "near_mm": [0.0, 0.0, 0.0]},
                    {"name": "outlet", "type": "outlet", "diameter_mm": 40.0, "near_mm": [100.0, 60.0, 0.0]},
                    {"name": "wall", "type": "wall"}]
FLOW_Q = ("Flow conditions - I'd go with:\n1. Air at 20 °C and sea-level pressure\n2. 10 m/s\n"
          "3. k-ω SST with wall functions\nOK, or tell me what to change?")
#: words a chat on the FEA route never shows in any line the application wrote
CFD_WORDS = re.compile(r"\b(cells?|layers?|layer count|inlet|outlet|far[- ]?field|flow|fluid|y\+|prism)\b", re.I)


# ------------------------------------------------------------------------------- the stand-ins ----

class _Repo:
    """The session repository over one in-memory session."""

    def __init__(self, world):
        self.w = world

    async def get_for_update(self, db, session_id):
        return self.w.session

    async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
        return self.w.session

    async def append_message(self, db, session_id, role, content):
        self.w.session.messages = [*self.w.session.messages, {"role": role, "content": content}]

    async def set_messages(self, db, session_id, messages):
        self.w.session.messages = list(messages)

    async def set_intake_gate(self, db, session_id, gate):
        self.w.session.intake_gate = dict(gate or {})

    async def set_user_notes(self, db, session_id, notes):
        self.w.session.user_notes = un.merge(self.w.session.user_notes, notes)

    async def append_intake_event(self, db, session_id, event):
        pass

    async def release_job(self, db, session_id):
        self.w.session.job_id = None

    async def bind_geometry_interpretation(self, db, session_id, iid):
        self.w.session.geometry_interpretation_id = iid

    def __getattr__(self, name):
        column = {"set_request_txt": "request_txt", "set_review_brief_txt": "review_brief_txt",
                  "set_intake_patches": "intake_patches", "set_dimensionality": "dimensionality",
                  "set_requested_mesh_fidelity": "requested_mesh_fidelity", "set_purpose": "purpose",
                  "set_input_kind": "input_kind", "set_mesh_engine": "mesh_engine", "set_task_label": "domain",
                  "set_engine_params": "engine_params"}.get(name)
        if column is None:
            raise AttributeError(name)

        async def _set(db, session_id, value):
            setattr(self.w.session, column, value)
        return _set


def _resp(tool=None, args=None, text=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    calls = (ToolCallRequest(id=f"m-{tool}", name=tool, arguments=json.dumps(args or {})),) if tool else ()
    return ModelRoundResult(tool_calls=calls, assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop")


class _Model:
    """A stand-in intake model: it reads the conversation and the turn's tool results and does what a
    good model does - unless `bad` names a misbehaviour to commit once."""

    def __init__(self, bad: str = ""):
        self.bad, self.done_bad, self.calls = bad, False, 0

    def _misbehave(self, kind):
        if self.bad == kind and not self.done_bad:
            self.done_bad = True
            return True
        return False

    async def __call__(self, *, messages, job_id, user_id, name, tools):
        self.calls += 1
        if self.bad == "fails":
            raise RuntimeError("the model is down")
        convo = [m for m in messages if m.get("role") in ("user", "assistant")]
        said = [m for m in convo if m.get("role") == "user"]
        latest = str(said[-1]["content"]) if said else ""
        nudged = latest.startswith("[SYSTEM]")
        tools_now = [m for m in messages if m.get("role") == "tool"]
        decl = flow.latest_declaration(convo)
        fea = flow.route_of(convo) == flow.ROUTE_FEA
        if tools_now:
            return self._tool_step(str(tools_now[-1]["content"]), convo, decl, fea)
        user_said = latest if not nudged else str(next(
            (m["content"] for m in reversed(said) if not str(m["content"]).startswith("[SYSTEM]")), ""))
        if self._misbehave("empty"):
            return _resp(text="")
        if fea:
            if self._misbehave("fea_asks_loads"):
                return _resp(text="What loads and supports should I apply, and which material is it?")
            if self._misbehave("own_notes_question"):
                return _resp(text=un.QUESTION)
            if flow.asks_something(user_said) and not nudged:
                return _resp(text="Every face is kept as a named group, so you set it up in your own solver.")
            return _resp("propose_engine_selection", {"engine": "Gmsh"})
        # CFD (and before any route): the model only answers what was asked
        if flow.asks_something(user_said) and not nudged:
            planned = any(flow.PLAN in flow.step_of(m.get("content")) for m in convo if m.get("role") == "assistant")
            if planned:
                return _resp(text="The run sizes the mesh from the part; the plan shows what it will be.")
            if not decl:
                return _resp(text="I mesh parts for CFD and FEA.")
            return _resp(text="y⁺ is how far the first cell sits from the wall, in wall units.")
        if self._misbehave("cfd_second_question"):
            return _resp(text="How many prism layers do you want on the walls?")
        if self._misbehave("own_notes_question"):
            return _resp(text=un.QUESTION)
        return _resp("propose_engine_selection", {"engine": "Gmsh"})

    def _case(self, decl, fea):
        if fea:
            return {"purpose": "Structural / FEA", "input_kind": "Solid body", "patches": []}
        if "flows through it" in decl:
            return {"purpose": "internal_cfd", "input_kind": "fluid-domain",
                    "patches": [{"name": "inlet", "type": "inlet", "diameter_mm": 40, "near_mm": [0, 0, 0]},
                                {"name": "outlet", "type": "outlet", "diameter_mm": 40, "near_mm": [100, 60, 0]},
                                {"name": "wall", "type": "wall"}]}
        return {"purpose": "external_cfd", "input_kind": "solid-body",
                "patches": [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}]}

    def _tool_step(self, last, convo, decl, fea):
        case = self._case(decl, fea)
        if "Engines that can:" in last:
            engine = last.split("Engines that can:", 1)[1].split(".")[0].split(",")[0].strip()
            return _resp("propose_engine_selection", {"engine": engine})
        got = re.search(r"^(?:.*\n\n)?(\S[^\n]*?) is (?:SELECTED|already selected)", last, re.S)
        if got or "is SELECTED" in last:
            engine = got.group(1) if got else "Gmsh"
            self.engine = engine
            return _resp("preview_selected_admission", {"selected_engine": engine, "dimensionality": "3D",
                                                        "engine_params": flow._engine_defaults(_key(engine)),
                                                        **case})
        if '"preview_token"' in last:
            token = json.loads(last.split("\n\n")[-1])["preview_token"]
            quotes = [str(m["content"]) for m in convo if m.get("role") == "user" and re.search(r"\d", str(m["content"]))
                      and not str(m["content"]).startswith("[SYSTEM]")]
            args = {"domain": "test case", "dimensionality": "3D", "mesh_engine": self.engine,
                    "engine_source": "suggested_confirmed", "engine_params": flow._engine_defaults(_key(self.engine)),
                    "request_txt": ("A complete summary of the requirements the user confirmed for this case, with "
                                    "every stated value and the mesh requirements, in plain words."),
                    "review_brief_txt": "A valid mesh, every boundary present, well-shaped cells, features kept.",
                    "preview_token": token, **case}
            if not fea and case["purpose"] == "external_cfd":
                args.update(flow_axis="+x", reference_length_m=4.0,
                            requested_extents={"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5})
            if quotes:
                args["user_note_quotes"] = quotes
            return _resp("submit_requirements", args)
        raise AssertionError(f"the stand-in model did not expect this tool result: {last[:300]}")


def _key(engine_display: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.normalize_tool_args({"mesh_engine": engine_display})["mesh_engine"]


class World:
    """One conversation, driven through the real chat route."""

    def __init__(self, monkeypatch, model):
        import meshpipeline.contracts.model_inference as llm_router
        from meshpipeline.agents.intake import measured_choice
        from meshpipeline.api.v1 import chat as chatmod
        from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT
        from meshpipeline.application import geometry_check as gc
        from meshpipeline.application import part_matches
        from meshpipeline.contracts import object_storage
        from meshpipeline.persistence.repositories import session_repository as srmod

        self.model, self.confirmed, self.queued, self.dispatched, self.errors = model, False, False, False, []
        #: the stored geometry check, as application/geometry_check.read_check serves it ({} until a "named" step)
        self.check: dict = {}
        self.shown: list[tuple[str, str]] = []          # what the console drew: (role, text)
        self.session = SimpleNamespace(
            id=SID, owner_id="alice", job_id=None, geometry_source_id=uuid.uuid4(),
            geometry_interpretation_id=uuid.uuid4(), intake_gate={}, geometry_source=None,
            messages=[{"role": "assistant", "content": UPLOAD_ACKNOWLEDGEMENT}],
            request_txt="", review_brief_txt="", domain="", mesh_engine="", engine_params={},
            intake_patches=[], dimensionality="", purpose="", input_kind="", requested_mesh_fidelity=None,
            user_notes=[])
        self.shown.append(("assistant", UPLOAD_ACKNOWLEDGEMENT))
        repo = _Repo(self)
        monkeypatch.setattr(srmod, "SessionRepository", lambda: repo)

        @asynccontextmanager
        async def _db():
            async def _commit():
                return None
            yield SimpleNamespace(commit=_commit)
        monkeypatch.setattr(chatmod, "get_db", _db)
        self.repo, self.db = repo, _db
        # the hold lets the chat go once the part is confirmed, or once the naming has landed (a ready check)
        monkeypatch.setattr(gh, "hold_applies",
                            lambda sid: None if self.confirmed or self.check.get("status") == "ready"
                            else ("wait" if self.queued else "queue"))

        def _queue(*a, **k):
            self.queued = True
            return True
        monkeypatch.setattr(gh, "queue_naming", _queue)

        def _reopen(sid):
            self.confirmed, self.queued, self.restaged = False, False, self.restaged + 1
        self.restaged = 0
        monkeypatch.setattr(gh, "reopen_stage", _reopen)
        monkeypatch.setattr(gh, "stage_can_reopen", lambda sid: True)

        async def _none(*a, **k):
            return None

        async def _blank(*a, **k):
            return ""

        async def _false(*a, **k):
            return False
        monkeypatch.setattr(gh, "interpretation_payload", _none)
        monkeypatch.setattr(gh, "upload_check_mode", _blank)
        monkeypatch.setattr(gh, "stored_thermal", lambda sid: (False, ""))
        monkeypatch.setattr(gh, "stage_settles_unit", lambda sid: False)
        monkeypatch.setattr(gh, "measured_size_mm", lambda sid: None)
        monkeypatch.setattr(msg, "notes_unread", _false)
        monkeypatch.setattr(part_matches, "view", lambda *a, **k: None)
        monkeypatch.setattr(measured_choice, "staged_upload", lambda *a, **k: "")
        monkeypatch.setattr(gc, "read_check", lambda sid: self.check)

        class _NoStore:
            def get_bytes(self, *, object_key):
                raise object_storage.ObjectNotFound(object_key)
        monkeypatch.setattr(object_storage, "get_object_store", lambda: _NoStore())

        async def _dispatch(session, session_repo, owner_id, session_id, **kw):
            self.dispatched = True
            return SimpleNamespace(status=None, ok=True, message="Mesh generation started.", job_id=uuid.uuid4())
        monkeypatch.setattr(ap, "confirm_pending_approval", _dispatch)
        monkeypatch.setattr(llm_router, "call_intake_model", model)

    # the user's two inputs
    async def say(self, text: str):
        from fastapi import HTTPException

        from meshpipeline.api.schemas.chat import ChatMessageIn
        from meshpipeline.api.v1.chat import chat_message
        self.shown.append(("user", text))
        try:
            out = await chat_message(ChatMessageIn(session_id=SID, content=text), owner_id="alice",
                                     organization_id="")
        except HTTPException as exc:
            self.errors.append(exc.status_code)
            return None
        self.shown.append(("assistant", out.reply))
        return out

    async def named(self, facts: dict):
        """THE NAMING LANDS, then the stage reads the check (api/v1/geometry.get_check): the flow-kind record is
        written as the naming writes it - the real authority over the part's facts and the user's words so far
        (application/flow_kind.decide; no pictures here) - and the one question is asked when it is due."""
        import logging

        from meshpipeline.application import flow_kind as fk
        facts = dict(facts)
        facts["flow_kind"] = fk.decide(facts, words=gh.purpose_from(self.session.messages))
        self.check = {"status": "ready", "named": True, "facts": facts}
        asked = await msg.ask_flow_kind(SID, "alice", facts=facts, session_repo=self.repo, db_factory=self.db,
                                        logger=logging.getLogger("test"))
        if asked:
            self.shown.append(("assistant", asked))
        return asked

    async def proceed_on_stage(self, declaration: str, patches=None):
        """The panel's Proceed, as api/v1/geometry.confirm_check ends: the declaration replaces any earlier
        one, then one chat turn runs for the user - whose words the console never draws."""
        from fastapi import HTTPException

        from meshpipeline.api.schemas.chat import ChatMessageIn
        from meshpipeline.api.v1.chat import chat_message
        self.session.messages = with_declaration(self.session.messages, declaration)
        self.session.intake_patches = list(patches if patches is not None else
                                           (INTERNAL_PATCHES if declaration == INTERNAL_DECL else []))
        self.confirmed = True
        self.shown.append(("assistant", declaration))
        try:
            out = await chat_message(ChatMessageIn(session_id=SID, content=gh.CONTINUE_TEXT), owner_id="alice",
                                     organization_id="")
        except HTTPException as exc:      # the route keeps the confirmation; the next message resumes
            self.errors.append(exc.status_code)
            return None
        if out.reply:
            self.shown.append(("assistant", out.reply))
        return out

    def steps(self) -> list[str]:
        read = flow.steps(self.session.messages)
        return [*read, ST] if self.dispatched else read


async def _run(monkeypatch, script, *, bad=""):
    world = World(monkeypatch, _Model(bad))
    world.answers = []
    for kind, value in script:
        if kind == "say":
            world.answers.append(await world.say(value))
        elif kind == "named":
            await world.named(value)
        else:
            await world.proceed_on_stage(value)
    return world


# --------------------------------------------------------------------------------- the matrix ----

FEA_BUTTON = "The part's strength, stress or vibration (FEA)"
#: WHAT THE MEASURING STEP READ, as the naming finds it: a straight convex bar with two flat ends - the fluid
#: inside a straight pipe, or a bar in a cross-flow; the flow-kind authority cannot tell (FLOW-2: undecided)
BAR = {"body_kind": "bar", "flow": "internal", "input_kind": "fluid-domain", "size_mm": [40.0, 40.0, 200.0],
       "openings": [{"id": 1, "role": "inlet", "normal": [0, 0, -1], "centroid_mm": [20, 20, 0], "diameter_mm": 40.0},
                    {"id": 2, "role": "outlet", "normal": [0, 0, 1], "centroid_mm": [20, 20, 200], "diameter_mm": 40.0}]}
#: ...and a closed solid with no passage mouths: a body in a flow, which the shape decides (FLOW-2: external)
BODY = {"body_kind": "single_solid", "flow": "external", "input_kind": "solid-body",
        "size_mm": [4000.0, 1800.0, 1400.0], "openings": []}
THROUGH, AROUND = (send for _label, send in flow.FLOW_KIND_CHOICES)
CASES = {
    # FEA
    "fea_standard": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"), ("say", "Proceed")],
                     "", [P, S, C, A, PL, ST]),
    "fea_two_at_once": ([("say", "Static stress FEA of this bracket"), ("stage", FEA_DECL), ("say", "Proceed")],
                        "", [P, S, C, PL, ST]),
    "fea_typed_analysis": ([("say", "stress analysis"), ("stage", FEA_DECL), ("say", "Proceed")],
                           "", [P, S, C, PL, ST]),
    "fea_purpose_unclear": ([("say", "mesh this please"), ("say", FEA_BUTTON), ("stage", FEA_DECL),
                             ("say", "Vibration (modal)"), ("say", "yes")], "", [P, PC, S, C, A, PL, ST]),
    "fea_off_script_at_analysis": ([("say", "FEA"), ("stage", FEA_DECL),
                                    ("say", "what is the difference between modal and static?"),
                                    ("say", "Buckling"), ("say", "Proceed")], "", [P, S, C, A, ANS, A, PL, ST]),
    "fea_off_script_at_plan": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"),
                                ("say", "what element type will it use?"), ("say", "Proceed")],
                               "", [P, S, C, A, PL, ANS, PL, ST]),
    "fea_hedge_at_plan": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"), ("say", "maybe"),
                           ("say", "yes")], "", [P, S, C, A, PL, PL, ST]),
    "fea_change_mind_analysis": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"),
                                  ("say", "make it buckling"), ("say", "Proceed")], "", [P, S, C, A, PL, PL, ST]),
    "fea_coming_soon": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Fatigue (coming soon)"),
                         ("say", "Proceed")], "", [P, S, C, A, PL, ST]),
    "fea_spec_pasted_early": ([("say", "FEA static stress. Spec: 2 mm elements near the 4 bolt holes, 5 kN on the tab"),
                               ("stage", FEA_DECL), ("say", "Proceed")], "", [P, S, C, PL, ST]),
    "fea_message_while_drawing": ([("say", "FEA"), ("say", "hello?"), ("stage", FEA_DECL), ("say", "Static stress"),
                                   ("say", "Proceed")], "", [P, S, S, C, A, PL, ST]),
    "fea_analysis_with_words_model_asks_loads": (
        [("say", "FEA"), ("stage", FEA_DECL), ("say", "static stress, it is bolted down at the base"),
         ("say", "Proceed")], "fea_asks_loads", [P, S, C, A, PL, ST]),
    "fea_model_asks_its_own_notes_question": (
        [("say", "FEA"), ("stage", FEA_DECL), ("say", "static stress, it is bolted down at the base"),
         ("say", "Proceed")], "own_notes_question", [P, S, C, A, PL, ST]),
    "fea_model_says_nothing_at_plan": (
        [("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"), ("say", "what element type will it use?"),
         ("say", "Proceed")], "empty", [P, S, C, A, PL, PL, ST]),
    "fea_model_says_nothing_at_analysis": (
        [("say", "FEA"), ("stage", FEA_DECL), ("say", "what does modal mean?"), ("say", "Static stress"),
         ("say", "Proceed")], "empty", [P, S, C, A, A, PL, ST]),
    # a question before any route: answered, then the purpose question
    "first_message_is_a_question": ([("say", "what can you mesh?"), ("say", FEA_BUTTON), ("stage", FEA_DECL),
                                     ("say", "Static stress"), ("say", "Proceed")], "", [P, ANS, PC, S, C, A, PL, ST]),
    # CFD internal
    "cfd_internal_standard": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                               ("say", "OK"), ("say", "Proceed")], "", [P, S, C, F, PL, ST]),
    "cfd_internal_resolve_the_wall": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                       ("say", "Resolve the wall (y⁺ ≈ 1)"), ("say", "Proceed")], "", [P, S, C, F, PL, ST]),
    "cfd_internal_own_values": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                 ("say", "no, oil at 3 m/s"), ("say", "Proceed")], "", [P, S, C, F, PL, ST]),
    "cfd_internal_conditions_early": ([("say", "CFD of water through this elbow at 2 m/s"), ("stage", INTERNAL_DECL),
                                       ("say", "Proceed")], "", [P, S, C, PL, ST]),
    "cfd_internal_off_script_at_flow": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                         ("say", "what is y+?"), ("say", "OK"), ("say", "Proceed")],
                                        "", [P, S, C, F, ANS, F, PL, ST]),
    "cfd_internal_model_misbehaves": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                       ("say", "OK"), ("say", "Proceed")], "cfd_second_question", [P, S, C, F, PL, ST]),
    "cfd_off_script_at_plan": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                ("say", "OK"), ("say", "how many cells will it have?"), ("say", "Proceed")],
                               # NOTE-20: a question about the mesh's count is answered from the plan's numbers; the
                               # plan stands on the card, so it is not shown again before the Proceed
                               "", [P, S, C, F, PL, ANS, ST]),
    "cfd_question_with_the_plan_due": ([("say", "CFD of water through this elbow at 2 m/s"), ("stage", INTERNAL_DECL),
                                        ("say", "hmm"), ("say", "Proceed")], "", [P, S, C, PL, PL, ST]),
    # a change of analysis after the stage: the stage opens again on the new route, nothing asked in chat
    "fea_then_cfd_after_stage": ([("say", "FEA"), ("stage", FEA_DECL), ("say", "actually it is CFD, air flowing through it"),
                                  ("stage", INTERNAL_DECL), ("say", "OK"), ("say", "Proceed")],
                                 "", [P, S, A, S, C, F, PL, ST]),
    "cfd_then_fea_after_stage": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                  ("say", "now a static stress analysis of the same part"), ("stage", FEA_DECL),
                                  ("say", "Proceed")], "", [P, S, F, S, C, PL, ST]),
    "cfd_change_of_mind_at_the_plan": ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                        ("say", "OK"), ("say", "actually I want a structural FEA of it"),
                                        ("stage", FEA_DECL), ("say", "Static stress"), ("say", "Proceed")],
                                       "", [P, S, F, PL, S, C, A, PL, ST]),
    # CFD external
    "cfd_external_standard": ([("say", "external aero over this car"), ("stage", EXTERNAL_DECL), ("say", "OK"),
                               ("say", "Proceed")], "", [P, S, C, F, PL, ST]),
    "cfd_external_hedge_then_yes": ([("say", "external aero over this car"), ("stage", EXTERNAL_DECL),
                                     ("say", "OK"), ("say", "hmm, maybe"), ("say", "yes")],
                                    "", [P, S, C, F, PL, PL, ST]),
    # through or around: asked once the naming has landed, only when still undecided and no words decided it
    "cfd_flow_kind_undecided": ([("say", "CFD please"), ("named", BAR), ("say", THROUGH),
                                 ("stage", INTERNAL_DECL), ("say", "OK"), ("say", "Proceed")],
                                "", [P, S, FK, S, C, F, PL, ST]),
    "cfd_flow_kind_undecided_around": ([("say", "CFD please"), ("named", BAR), ("say", AROUND),
                                        ("stage", EXTERNAL_DECL), ("say", "OK"), ("say", "Proceed")],
                                       "", [P, S, FK, S, C, F, PL, ST]),
    "cfd_flow_kind_said_first": ([("say", "CFD of the air flowing around this part"), ("named", BAR),
                                  ("stage", EXTERNAL_DECL), ("say", "OK"), ("say", "Proceed")],
                                 "", [P, S, C, F, PL, ST]),
    "cfd_flow_kind_shape_decides": ([("say", "CFD please"), ("named", BODY), ("stage", EXTERNAL_DECL),
                                     ("say", "OK"), ("say", "Proceed")], "", [P, S, C, F, PL, ST]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_every_chat_follows_the_standard_questionnaire(monkeypatch, name):
    script, bad, expected = CASES[name]
    world = await _run(monkeypatch, script, bad=bad)
    assert world.errors == []
    # THE ORDER IS NEVER THE MODEL'S: a chat with no question in it never calls the model at all
    if not any(flow.asks_something(v) for k, v in script if k == "say"):
        assert world.model.calls == 0, (name, world.model.calls)
    assert world.steps() == expected, (name, world.steps(), [m["content"][:90] for m in world.session.messages])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_no_chat_shows_a_retired_line_the_stages_words_as_the_users_or_a_cfd_word_on_fea(monkeypatch, name):
    script, bad, _ = CASES[name]
    world = await _run(monkeypatch, script, bad=bad)
    assistant = [t for r, t in world.shown if r == "assistant"]
    # the old notes chat question and the old three-line confirmation are never said
    assert not any(un.QUESTION in t or CONFIRM_REQUIREMENTS_ASK in t or "Shall I proceed" in t for t in assistant)
    # a button press is never drawn as the user's words
    assert gh.CONTINUE_TEXT not in [t for r, t in world.shown if r == "user"]
    if flow.route_of(world.session.messages) == flow.ROUTE_FEA:
        # every line the APPLICATION wrote on an FEA chat speaks FEA (the model's own answers are checked by
        # fea_wording; a declaration is the stage's card)
        # (from the FEA stage on: a chat that came from CFD said CFD words while it was CFD)
        read = flow.read_messages(world.session.messages)
        start = max((i for i, (_m, st_) in enumerate(read) if C in st_), default=0)
        for got, steps in read[start:]:
            # (the purpose questions come before any route: they name both)
            if got.get("role") == "assistant" and not {ANS, C, P, PC} & set(steps):
                assert not CFD_WORDS.search(str(got["content"])), got["content"]


@pytest.mark.asyncio
async def test_the_fea_plan_is_the_applications_when_the_answer_leaves_nothing_to_read(monkeypatch):
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress")])
    # the stage's Proceed asked the analysis without a model; the button answer got the plan without one
    assert world.model.calls == 0
    assert world.session.messages[-1]["content"] == ap.PROCEED_ASK
    assert world.session.purpose == "structural" and world.session.mesh_engine
    assert world.session.domain == "Bracket Static stress FEA"
    assert "Static stress".lower() in world.session.request_txt.lower()
    assert ap.is_live(world.session.intake_gate.get("approval"))


@pytest.mark.asyncio
async def test_a_changed_analysis_is_the_one_the_new_plan_carries(monkeypatch):
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress"),
                                     ("say", "make it buckling")])
    assert world.session.domain == "Bracket Buckling FEA"
    assert "buckling analysis" in world.session.request_txt
    assert world.session.messages[-1]["content"].startswith("Changed from the last summary")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,fluid,speed,band", [
    ("OK", "water at 20 °C", "1 m/s", "y⁺ 30–300"),
    ("Resolve the wall (y⁺ ≈ 1)", "water at 20 °C", "1 m/s", "y⁺ ≈ 1"),
    ("no, oil at 3 m/s", "oil", "3 m/s", "y⁺ 30–300"),
])
async def test_the_cfd_plan_is_the_applications_and_carries_the_answer(monkeypatch, answer, fluid, speed, band):
    world = await _run(monkeypatch, [("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                     ("say", answer)])
    assert world.model.calls == 0
    s_ = world.session
    assert s_.purpose == "internal_cfd" and s_.input_kind == "fluid-domain"
    assert f"Fluid: {fluid} at {speed}" in s_.request_txt and band in s_.request_txt
    assert [p["name"] for p in s_.intake_patches] == ["inlet", "outlet", "wall"]
    assert s_.domain == "Elbow internal flow"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,y_plus,speed", [
    ("OK", 50.0, 1.0),
    ("Resolve the wall (y⁺ ≈ 1)", 1.0, 1.0),
    ("water at 2.5 m/s, resolve the wall", 1.0, 2.5),
])
async def test_the_conditions_answer_typed_or_clicked_reaches_the_run(monkeypatch, answer, y_plus, speed):
    # CFDREQ-8: the plan's conditions sentence is what the run reads back - a click on "Resolve the wall" is never
    # a note (NOTE-1), so before this the click reached no engine and the first layer kept the planner's own size
    from meshpipeline.contracts import flow_conditions as FC

    world = await _run(monkeypatch, [("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                     ("say", answer)])
    got = FC.read(world.session.request_txt, world.session.intake_patches)
    assert got is not None and got["y_plus"] == y_plus and got["velocity_ms"] == speed and got["why"] == ""


@pytest.mark.asyncio
async def test_the_external_plan_carries_the_far_field_the_stage_confirmed(monkeypatch):
    world = await _run(monkeypatch, [("say", "external aero over this car"), ("stage", EXTERNAL_DECL), ("say", "OK")])
    assert world.model.calls == 0 and world.session.purpose == "external_cfd"
    payload = world.session.intake_gate["approval"]["payload"]
    assert payload["flow_axis"] == "+x" and payload["reference_length_m"] == 4.0
    assert payload["requested_extents"] == {"upstream": 5.0, "downstream": 10.0, "lateral": 5.0, "vertical": 5.0}
    assert "Fluid: air at 20 °C at 10 m/s" in world.session.request_txt


@pytest.mark.asyncio
async def test_the_conditions_question_has_its_buttons_and_reads_a_named_fluid(monkeypatch):
    world = await _run(monkeypatch, [("say", "air flowing through this duct"), ("stage", INTERNAL_DECL)])
    asked = world.session.messages[-1]["content"]
    assert asked.startswith(flow.INTERNAL_LEAD) and "air at 20 °C at 1 m/s at the inlet" in asked
    assert [c["label"] for c in pm.choices_for(asked)] == ["OK", "Resolve the wall (y⁺ ≈ 1)"]


@pytest.mark.asyncio
async def test_a_change_of_analysis_after_the_stage_opens_the_stage_again(monkeypatch):
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL)])
    out = await world.say("actually it is CFD, air flowing through it")
    assert out.restage is True and world.restaged == 1 and world.model.calls == 0
    assert out.reply == gh.HOLD_REPLY                     # the stage step's own line, nothing asked in chat
    assert not flow.latest_declaration(world.session.messages)     # the old confirmation is withdrawn
    assert pm.reading_for(world.session.messages).route == pm.ROUTE_FLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("script,open_step,buttons", [
    ([("say", "FEA"), ("stage", FEA_DECL)], A, True),
    ([("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL)], F, True),
    ([("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress")], PL, False),
])
async def test_a_reload_mid_intake_serves_the_conversation_and_its_open_step(monkeypatch, script, open_step, buttons):
    # the page restores an unfinished conversation from the history: its messages, the open step's
    # buttons, and the plan card when a plan stands (ui/js/main.js openSession, one drawing path)
    from meshpipeline.api.v1 import chat as chatmod
    world = await _run(monkeypatch, script)
    history = await chatmod.get_chat_history(SID, owner_id="alice", organization_id="")
    assert flow.steps(history["messages"])[-1] == open_step
    assert bool(history["choices"]) is buttons
    assert (history["brief"] or {}).get("proceed_ask") == (ap.PROCEED_ASK if open_step == PL else None)
    assert history["job_id"] is None


@pytest.mark.asyncio
async def test_a_spec_pasted_early_is_kept_word_for_word_on_the_plan(monkeypatch):
    spec = "FEA static stress. Spec: 2 mm elements near the 4 bolt holes, 5 kN on the tab"
    world = await _run(monkeypatch, [("say", spec), ("stage", FEA_DECL)])
    assert [n["text"] for n in un.clean(world.session.user_notes)] == [spec]


@pytest.mark.asyncio
async def test_the_plan_card_carries_its_proceed_and_the_history_never_shows_the_stages_turn(monkeypatch):
    from meshpipeline.api.v1 import chat as chatmod
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "Static stress")])
    brief = await chatmod._brief(world.session, "alice")
    assert brief["proceed_ask"] == ap.PROCEED_ASK            # the card's Proceed button is drawn from it
    assert gh.CONTINUE_TEXT in [m["content"] for m in world.session.messages]   # the model still reads it
    history = await chatmod.get_chat_history(SID, owner_id="alice", organization_id="")
    assert gh.CONTINUE_TEXT not in [m["content"] for m in history["messages"]]


@pytest.mark.asyncio
async def test_the_stage_turn_needs_no_model_so_a_model_failure_cannot_silence_it(monkeypatch):
    for decl, nxt in ((INTERNAL_DECL, F), (FEA_DECL, A)):
        world = await _run(monkeypatch, [("say", "water flowing through this elbow" if decl != FEA_DECL else "FEA"),
                                         ("stage", decl)], bad="fails")
        assert world.errors == [] and flow.steps(world.session.messages) == [P, S, C, nxt]


def test_the_stage_turn_is_the_applications_in_every_reader():
    assert un.system_written(gh.CONTINUE_TEXT) and gh.CONTINUE_TEXT in un.SYSTEM_TURNS
    assert flow.plain_step_answer(gh.CONTINUE_TEXT)


def test_every_application_step_has_one_fixed_line():
    assert gh.WAIT_REPLY == gh.HOLD_REPLY == gh.FEA_WAIT_REPLY
    assert gh.HOLD_REPLY.endswith(pm.FEA_HOLD)
    assert ap.CLARIFICATION == ap.PROCEED_ASK
    assert flow.step_of(ap.PROCEED_ASK) == (PL,)
    assert flow.step_of("Changed from the last summary: analysis.\n\n" + ap.PROCEED_ASK) == (PL,)
    assert flow.step_of("A short answer.\n\n" + pm.ANALYSIS_QUESTION) == (ANS, A)
    assert flow.step_of("A short answer.\n\n" + pm.DOMAIN_QUESTION) == (ANS, PC)
    assert flow.step_of(flow.FLOW_KIND_QUESTION) == (FK,)
    assert [c["label"] for c in pm.choices_for(flow.FLOW_KIND_QUESTION)] == ["Through the inside", "Around the outside"]
    assert pm.choices_for("A short answer.\n\n" + pm.ANALYSIS_QUESTION) == pm.choices_for(pm.ANALYSIS_QUESTION)


# ------------------------------------------------------------------------- through or around ----

@pytest.mark.asyncio
@pytest.mark.parametrize("button,preset", [(THROUGH, {"flow": "internal", "input_kind": "fluid-domain"}),
                                           (AROUND, {"flow": "external", "input_kind": "solid-body"})])
async def test_the_answer_to_the_flow_kind_question_sets_the_stage_before_proceed(monkeypatch, button, preset):
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BAR), ("say", button)])
    record = world.check["facts"]["flow_kind"]
    assert record["kind"] == "undecided" and record["by"] == "guess" and record["question"] == flow.FLOW_KIND_QUESTION
    out = world.answers[-1]
    # the button is read as the user's stated words (FLOW-1) and the stage's form takes the answer
    assert out.stage_flow == preset, out
    assert out.reply == gh.HOLD_REPLY and out.choices == []
    assert world.model.calls == 0
    # asked once: the stage reading the check again asks nothing more
    assert await world.named(BAR) == ""
    assert flow.steps(world.session.messages).count(FK) == 1


@pytest.mark.asyncio
async def test_words_that_say_the_flow_are_never_asked_about(monkeypatch):
    world = await _run(monkeypatch, [("say", "CFD of the air flowing around this part"), ("named", BAR)])
    record = world.check["facts"]["flow_kind"]
    assert record["kind"] == "external" and record["by"] == "words" and record["rule"] == "FLOW-1"
    assert FK not in flow.steps(world.session.messages)


@pytest.mark.asyncio
async def test_a_flow_said_later_beats_the_stored_verdict(monkeypatch):
    # the shape decided "around" (FLOW-2); the user says otherwise before Proceed: their words win (FLOW-1)
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BODY), ("say", "the water flows through it")])
    facts = world.check["facts"]
    assert facts["flow_kind"]["kind"] == "external" and facts["flow_kind"]["by"] == "geometry"
    assert FK not in flow.steps(world.session.messages)
    assert world.answers[-1].stage_flow["flow"] == "internal"
    now = flow.geometry_flow_kind(facts, world.session.messages)
    assert now["flow"] == "internal" and now["by"] == "words"


def test_the_seam_reads_the_record_only_once_the_naming_has_landed(monkeypatch):
    from meshpipeline.application import flow_kind as fk
    from meshpipeline.application import geometry_check as gc
    facts = {**BAR, "flow_kind": fk.decide(BAR)}
    for stored, seen in (({"status": "scouted", "named": False, "facts": facts}, False),
                         ({"status": "ready", "named": True, "facts": facts}, True),
                         ({"status": "ready", "named": True, "facts": {**facts, "check_mode": "thermal"}}, False),
                         ({}, False)):
        monkeypatch.setattr(gc, "read_check", lambda sid, s=stored: s)
        assert bool(flow.geometry_flow_facts(str(SID))) is seen, stored
    # a question about the flow states nothing
    assert flow.stated_flow_kind(facts, [{"role": "user", "content": "is the flow through it?"}]) is None
    assert flow.stated_flow_kind(facts, [{"role": "user", "content": THROUGH}])["kind"] == "internal"
    assert flow.stated_flow_kind({}, [{"role": "user", "content": THROUGH}]) is None


def test_the_flow_kind_question_is_one_string_and_its_buttons_are_the_users_stated_words():
    from meshpipeline.application import flow_kind as fk
    assert flow.FLOW_KIND_QUESTION is fk.QUESTION
    assert [fk.words_reading(send).kind for _l, send in flow.FLOW_KIND_CHOICES] == ["internal", "external"]
    assert all(fk.words_reading(send).tier == "said" for _l, send in flow.FLOW_KIND_CHOICES)
