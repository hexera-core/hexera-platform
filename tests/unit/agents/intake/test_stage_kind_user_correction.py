# Responsibility: Verify KIND-1 (review on #465): the input kind the user confirmed on the stage wins over the model's
# word, and the user's own correction in the chat after the stage wins over the stage - in any words, proved by the
# model's exact quote of a message the user wrote after the stage - so no second far-field box is built round an
# upload that already is the air.
# Boundaries: the executor's tool boundary with the real admission gate (the staged file's form and facts stood in
# for), geometry_confirmation.declared_case, and one intake turn end to end with the model's rounds scripted.
from __future__ import annotations

import asyncio
import json

import pytest

import meshpipeline.agents.intake.executor as ex_mod
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor, kind_said_by_user
from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK, declared_case

EXTERNAL = [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
STAGE = {"role": "assistant", "content": f"{CONFIRMED_MARK} the file is the car's surface, input_kind body-surface; "
                                         "the fluid flows around it."}
#: a correction in words no list of ours holds
CORRECTION = ("It came out of our wind tunnel rig already - what you got is what the car sits in, so just mesh the "
              "inside of it.")
QUOTE = "what you got is what the car sits in, so just mesh the inside of it"
DECLARED = ("external_cfd", "body-surface")


def _executor(monkeypatch, conversation, *, engine="gmsh"):
    es = ex_mod.es
    sel = es.propose(engine, session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    sel = {**sel, "state": es.CONFIRMED, "confirmed_revision": "r1", "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r1", user_msg_count=2,
                              declared_case=declared_case(conversation), selection=sel,
                              conversation=tuple(conversation),
                              user_texts=tuple(m["content"] for m in conversation if m["role"] == "user"))
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_form", lambda self: "cad")
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_facts", lambda self: {})
    monkeypatch.setattr(IntakeToolExecutor, "_source_patches", lambda self, eng: ())
    return st, IntakeToolExecutor(state=st, job_id="j", implemented_engines=["gmsh", "snappy", "cfmesh"],
                                  search_tool=lambda *a, **k: "")


def _preview(ex, **args):
    res = asyncio.run(ex.run("preview_selected_admission", {
        "selected_engine": "gmsh", "purpose": "external_cfd", "dimensionality": "3D", "patches": EXTERNAL,
        "engine_params": {"element_order": "1"}, **args}))
    return res, json.loads(res.content.split("\n\n")[-1])


def test_the_reviewers_words_lift_the_stages_kind():
    said = "The upload already includes the air box; mesh its contents directly, without adding another box."
    assert declared_case([STAGE]) == DECLARED
    assert declared_case([STAGE, {"role": "user", "content": said}]) is None
    for other in ("no need for another far-field, the file is the air", "the air volume is the file",
                  "don't put an extra box round it"):
        assert declared_case([STAGE, {"role": "user", "content": other}]) is None, other
    # what is not about the file still leaves the stage standing
    for other in ("5 prism layers on the car", "air at 40 m/s", "your call", "refine the wake behind the car"):
        assert declared_case([STAGE, {"role": "user", "content": other}]) == DECLARED, other


def test_the_users_correction_in_their_own_words_is_the_kind_meshed(monkeypatch):
    convo = [STAGE, {"role": "user", "content": CORRECTION}]
    assert declared_case(convo) == DECLARED           # no word list knows these words: the stage still stands
    st, ex = _executor(monkeypatch, convo)
    # the model's word with no quote of the user's is replaced by the stage's - and told how to bring the quote
    res, _out = _preview(ex, input_kind="fluid-domain")
    assert "as the user confirmed on the geometry stage" in res.content and "input_kind_user_words" in res.content
    assert st.pending is None or st.pending["canonical"]["input_kind"] == "body-surface"
    # quoted from the user's message after the stage: kept, and the turn's case is theirs from here on
    res, out = _preview(ex, input_kind="fluid-domain", input_kind_user_words=QUOTE)
    assert "is kept: the user corrected the geometry stage" in res.content, res.content
    assert out["verdict"] == "supported", out
    assert st.pending["canonical"]["input_kind"] == "fluid-domain"
    assert "input_kind_user_words" not in json.dumps(st.pending)          # the quote is no part of the payload
    assert st.declared_case == ("external_cfd", "fluid-domain")
    # the submission in the same turn reads the same kind with no quote, so the token matches
    sub = asyncio.run(ex.run("submit_requirements", {
        "mesh_engine": "gmsh", "purpose": "external_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
        "patches": EXTERNAL, "engine_params": {"element_order": "1"}, "engine_source": "suggested_confirmed",
        "domain": "car external aerodynamics",
        "request_txt": "External aerodynamics of the uploaded air volume around the car, meshed as it is, one wall "
                       "patch for the car and one far field, a full 3D mesh at the standard detail.",
        "review_brief_txt": "The car surface is captured cleanly and the cells near the body are of good quality.",
        "preview_token": out["preview_token"]}))
    assert sub.accepted and "NOT authorized" not in sub.content, sub.content


@pytest.mark.parametrize("quote,where", [
    (QUOTE, "before"),                        # words from before the stage were said of another case
    ("the file already is the air around the car", "invented"),     # words the user never wrote
    ("mesh", "too short"),
])
def test_the_models_drift_still_gives_way_to_the_stage(monkeypatch, quote, where):
    convo = ([{"role": "user", "content": CORRECTION}, STAGE, {"role": "user", "content": "5 layers please"}]
             if where == "before" else [STAGE, {"role": "user", "content": CORRECTION}])
    st, ex = _executor(monkeypatch, convo)
    res, _out = _preview(ex, input_kind="fluid-domain", input_kind_user_words=quote)
    assert "as the user confirmed on the geometry stage" in res.content, (where, res.content)
    assert st.declared_case == DECLARED
    assert st.pending is None or st.pending["canonical"]["input_kind"] == "body-surface"


def test_the_quote_proves_only_a_change_of_kind_for_the_stages_purpose():
    said = [CORRECTION]
    base = {"purpose": "external_cfd", "input_kind": "fluid-domain"}
    assert kind_said_by_user(base, DECLARED, QUOTE, said) is True
    assert kind_said_by_user({**base, "input_kind": "body-surface"}, DECLARED, QUOTE, said) is False   # the same kind
    assert kind_said_by_user({**base, "purpose": "internal_cfd"}, DECLARED, QUOTE, said) is False      # another purpose
    assert kind_said_by_user({**base, "input_kind": "wing"}, DECLARED, QUOTE, said) is False           # no such kind
    assert kind_said_by_user(base, DECLARED, QUOTE, None) is False                                      # no stage
    assert kind_said_by_user(base, DECLARED, f'"{QUOTE.upper()}."', said) is True     # case and marks aside
    # (one whole turn of it, the engine chosen again for the corrected kind: test_engine_follows_the_analysis)
