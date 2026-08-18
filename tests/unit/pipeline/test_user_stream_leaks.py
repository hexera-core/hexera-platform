# Responsibility: Verify only the realtime package writes to the user channel, through a closed event set.
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent.parent
APP = ROOT / "src" / "meshpipeline"

import meshpipeline.events as E  # noqa: E402


# 1. one writer
def test_only_the_realtime_package_may_write_to_the_user_channel():
    hits = subprocess.run(
        ["grep", "-rln", "-e", "jobs:.*:events", "-e", "jobs:.*:logs",
         "--include=*.py", str(APP)],
        capture_output=True, text=True).stdout.split()
    # the transport (the one writer) and the channel-NAMING convention are the only two
    # places the wire address appears; everything else goes through JobPublisher.
    _allowed = ("adapters/event_stream/redis.py", "events/channels.py")
    offenders = [h for h in hits if not any(a in h for a in _allowed)]
    assert not offenders, (
        f"these modules write to the user channel directly, bypassing JobPublisher "
        f"and the closed event set: {offenders}")


def test_the_publisher_exposes_no_free_prose_door():
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    assert not hasattr(JobPublisher, "__call__") or not callable(
        getattr(JobPublisher(""), "__call__", None)), (
        "JobPublisher is callable again - the (level, msg) prose door is back")


# 2. the closed set
def test_an_event_outside_the_closed_set_cannot_be_constructed():
    with pytest.raises(ValueError):
        E.UiEvent("executor_output", "executor", {"text": "..."})
    with pytest.raises(ValueError):
        E.UiEvent("note", "not_a_stage", {"text": "hi", "tone": "info"})
    with pytest.raises(ValueError):
        E.note("executor", "hi", tone="CRITICAL")


def test_the_wire_carries_type_and_stage_so_the_ui_never_guesses():
    w = E.check("executor", "The mesh is structurally sound", ok=True).wire()
    assert w["type"] == "check" and w["stage"] == "executor"
    assert w["ok"] is True and w["statement"] == "The mesh is structurally sound"
    assert "level" not in w and "msg" not in w, "the old prose wire is back"


def test_every_tool_has_a_word_for_the_person_watching():
    from meshpipeline.agents.builder.tools import ACTIONS as B_ACTIONS
    from meshpipeline.agents.builder.tools import _active_tools
    from meshpipeline.agents.reviewer.tools import ACTIONS as R_ACTIONS
    from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS
    # every tool the builder can be given, across every engine
    builder_tools = [t for e in ("cfmesh", "snappy", "gmsh", "vmtk", "snappy_multiregion")
                     for t in _active_tools(e)]
    for roster, actions, who in ((builder_tools, B_ACTIONS, "builder"),
                                 (REVIEWER_TOOLS, R_ACTIONS, "reviewer")):
        for t in roster:
            name = t["function"]["name"] if "function" in t else t["name"]
            assert name in actions, f"{who} tool {name!r} has no word for the user"
            assert "_" not in actions[name], f"{who} action for {name!r} is not English"


# 4. the frontend is a renderer.
# There used to be a scan here forbidding eight engine words from appearing anywhere under
# ui/js/. It forbade nothing a new engine would actually do - an engine reaches the browser as
# event DATA, and a scan cannot tell data from a comment - while failing for any unrelated file
# that happened to use one of the words. The durable property is behavioural and lives with the
# code that would break it: tests/unit/pipeline/test_ui_data_parity.py drives the real
# surface-response builder with an engine that exists nowhere in the catalog and requires it to
# come back whole. If anything on the path to the browser dispatched on engine name, that fails.


def test_chain_of_thought_is_published_only_when_the_deployment_says_so():
    from meshpipeline.trace.policy import RAW, SAFE, project

    assert E.REASONING in E.EVENT_TYPES, "the dual-mode contract is gone"

    raw_wire = {"type": "reasoning", "stage": "builder", "id": "r:1",
                "agent": "builder", "phase": "completed", "status": "success",
                "duration_ms": 8400, "token_count": 1276,
                "content": "Let me try listing the directory instead."}
    safe = project(raw_wire, SAFE)
    assert safe["content"] is None, "safe mode published the model's deliberation"
    assert safe["duration_ms"] == 8400 and safe["token_count"] == 1276

    shown = project(raw_wire, RAW)
    assert "listing the directory" in shown["content"], "raw mode showed nothing"

    # tool arguments and inspection images obey the same gate
    call = project({"type": "tool_call", "stage": "reviewer", "id": "c1",
                    "agent": "reviewer", "tool_name": "go_to_coordinates",
                    "arguments": {"x": 0.932}, "status": "started"}, SAFE)
    assert call["tool_name"] is None and call["arguments"] is None
    assert call["public_label"] == "Adjusted inspection view"
    assert project({"type": "screenshot", "stage": "reviewer",
                    "image": "AAAA"}, SAFE) is None
