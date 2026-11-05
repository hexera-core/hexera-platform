# Responsibility: Fail when any route of the purpose map, or any implemented engine, has no reader of the user's own notes - so a new route cannot forget them (docs/RULES.md NOTE-2).
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meshpipeline.application.purpose_map import ROUTES
from meshpipeline.contracts import user_notes as un
from meshpipeline.engines.purposes import PURPOSES
from meshpipeline.engines.registry import engine_names

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
SPEC = "Fixed base plate.\nRefine around the four bolt holes.\nLoad 900 daN on the lug."


def test_every_route_of_the_purpose_map_has_its_readers():
    assert set(un.ROUTE_CONSUMERS) == set(ROUTES), (
        "contracts/user_notes.ROUTE_CONSUMERS must name every route of application/purpose_map - "
        f"missing {sorted(set(ROUTES) - set(un.ROUTE_CONSUMERS))}")
    for route, readers in un.ROUTE_CONSUMERS.items():
        assert set(un.COMMON_CONSUMERS) <= set(readers), route
        assert set(readers) <= set(un.CONSUMERS), route


@pytest.mark.parametrize("engine", engine_names())
def test_every_implemented_engine_has_a_build_step_that_reads_them(engine):
    readers = un.ENGINE_CONSUMERS.get(engine)
    assert readers, (f"engine {engine!r} has no entry in contracts/user_notes.ENGINE_CONSUMERS: "
                     "name the AI step of its build that reads the user's notes")
    assert set(readers) <= set(un.CONSUMERS)


def test_every_purpose_lies_on_a_route_that_reads_them():
    # the purposes are what the routes declare (structural = FEA, the CFD purposes = flow, CHT =
    # electronics): a new purpose with no route would have no readers
    routed = {"structural": "fea", "external_cfd": "flow", "internal_cfd": "flow",
              "conjugate_heat_transfer": "electronics"}
    assert set(PURPOSES) <= set(routed), f"a purpose with no route: {sorted(set(PURPOSES) - set(routed))}"


def _consumers_named_in_code() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "user_notes.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (kw.arg == "consumer" and isinstance(kw.value, ast.Constant)
                            and kw.value.value in un.CONSUMERS):
                        out.setdefault(kw.value.value, []).append(f"{path.relative_to(SRC)}")
    return out


def test_every_declared_consumer_reads_the_notes_somewhere():
    named = _consumers_named_in_code()
    unread = sorted(set(un.CONSUMERS) - set(named))
    assert not unread, (f"declared user-notes consumers that read them nowhere: {unread} - every "
                        "read names its consumer (consumer=\"...\")")


@pytest.mark.parametrize("engine", engine_names())
def test_every_engines_attempt_has_them_in_its_workspace_and_opening(engine, tmp_path):
    from meshpipeline.agents.builder import attempt
    notes = un.clean([un.make(SPEC, source=un.SOURCE_CONSOLE)])
    state = {"user_notes": notes, "request_txt": "r", "engine": engine}
    attempt._context_files(tmp_path, state, "")
    assert SPEC in (tmp_path / un.NOTES_FILE).read_text(encoding="utf-8")
    opening = attempt._opening(tmp_path, state, engine=engine, source_path="")
    assert SPEC in opening[1]["content"] and un.NOTES_FILE in opening[1]["content"]
    # a run without notes: no file, the historical opening
    bare = tmp_path / "bare"
    bare.mkdir()
    attempt._context_files(bare, {"request_txt": "r"}, "")
    assert not (bare / un.NOTES_FILE).exists()
    assert un.NOTES_FILE not in attempt._opening(bare, {}, engine=engine, source_path="")[1]["content"]


@pytest.mark.parametrize("engine", engine_names())
def test_a_model_led_submit_may_say_what_it_did_with_them(engine):
    from meshpipeline.agents.builder.tools import _active_tools
    notes = un.clean([un.make(SPEC, source=un.SOURCE_CHAT, turn=1)])

    def _submit(tools):
        return next((t for t in tools if t["function"]["name"] == "submit_mesh"), None)
    plain, with_notes = _submit(_active_tools(engine)), _submit(_active_tools(engine, user_notes=notes))
    if plain is None:
        return                                         # an engine whose build submits no mesh tool
    assert "notes_remark" not in plain["function"]["parameters"]["properties"]
    assert "notes_remark" in with_notes["function"]["parameters"]["properties"]
    assert "notes_remark" not in with_notes["function"]["parameters"].get("required", [])
