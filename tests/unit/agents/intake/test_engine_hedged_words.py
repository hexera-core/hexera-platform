# Responsibility: Verify hesitant, questioning or negated words about an engine are never taken as the user's choice
# of it (review on #465): "maybe cfMesh", "instead of cfMesh", "should I use cfMesh" choose nothing, and the plain
# choices ("cfMesh", "use cfMesh", "hmm, actually cfMesh") still do.
# Boundaries: engine_selection's readers over plain words, and one intake turn end to end with the model's rounds
# scripted (test_engine_never_asked_twice's harness).
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es


def _tool_call(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


def _run(messages, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    seen: list = []
    it = iter(responses)

    async def _call(**kw):
        seen[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict) and m.get("role") == "tool"]
        return next(it)
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": messages}
    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))
    return out, seen


AUTO = {"state": es.CONFIRMED, "via": es.VIA_AUTO, "engine": "snappy", "session": "s", "owner": "o",
        "expires_at": 9e12}

HESITANT = ["maybe cfMesh", "perhaps cfMesh", "cfMesh maybe", "possibly cfMesh", "probably cfMesh",
            "instead of cfMesh", "without cfMesh", "except cfMesh", "unless cfMesh", "considering cfMesh",
            "what about cfMesh", "how about cfMesh", "is cfMesh ok?", "not cfMesh", "rather than cfMesh",
            "maybe use cfMesh", "maybe go with cfMesh", "should I use cfMesh", "should we use cfMesh",
            "what if we use cfMesh", "i might use cfMesh", "could we use cfMesh", "I wonder if cfMesh would work",
            "I think cfMesh", "not sure, cfMesh"]
CHOSEN = ["cfMesh", "use cfMesh", "hmm, actually cfMesh", "cfMesh instead", "cfMesh please", "ok cfMesh",
          "try cfMesh", "switch to cfMesh", "let's go with cfMesh", "can you use cfMesh", "could you use cfMesh",
          "I'd prefer cfMesh", "we should use cfMesh", "use cfMesh if possible"]


@pytest.mark.parametrize("said", HESITANT)
def test_hesitant_words_choose_no_engine(said):
    assert es.user_leans_to("cfmesh", [said]) is False
    assert es.user_chose("cfmesh", [said]) is False
    assert es.switch_by_words(AUTO, session_id="s", owner_id="o", revision="r", latest_user_message=said) is None
    assert es.note_chooses_engine(said, "snappy") == ""


@pytest.mark.parametrize("said", CHOSEN)
def test_plain_words_still_choose_the_engine(said):
    assert es.user_leans_to("cfmesh", [said]) is True
    if said not in ("hmm, actually cfMesh", "cfMesh instead"):       # the two only the lean reads (no choice verb)
        assert es.user_chose("cfmesh", [said]) is True
        sw = es.switch_by_words(AUTO, session_id="s", owner_id="o", revision="r", latest_user_message=said)
        assert sw is not None and sw["engine"] == "cfmesh"


@pytest.mark.parametrize("said", ["maybe cfMesh", "instead of cfMesh", "should I use cfMesh", "is cfMesh ok?"])
def test_a_hesitant_word_does_not_replace_the_engine_the_application_chooses(said):
    # the model proposes snappyHexMesh; before, "maybe cfMesh" replaced it with cfMesh and confirmed that as the
    # user's own choice without asking
    out, seen = _run([{"role": "user", "content": "air around this car at 40 m/s"},
                      {"role": "assistant", "content": "Anything specific about the mesh?"},
                      {"role": "user", "content": said}], [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="What flow speed?"),
    ])
    sel = out["intake_gate"]["selection"]
    assert sel["engine"] == "snappy" and sel["via"] == es.VIA_AUTO, sel
    assert "named" not in seen[-1]["content"]
