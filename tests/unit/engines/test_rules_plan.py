# Responsibility: Verify AIG-1: a model-led engine's attempt is built by the rules plan whenever the AI builder gives no mesh (down, busy, slow, raised, or ended without submitting), and always with BUILDER_AI=off; the rules plan's tool calls per engine.
# Boundaries: the rules builder's rounds over hand-made conversations, and invoke.run_attempt with the tool loop stubbed. Nothing meshes.
from __future__ import annotations

import asyncio
import json
import types

import pytest

from meshpipeline.agents.builder import invoke
from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import rules_plan as rb


def _conv(*steps):
    """A conversation: (tool name, result dict) per step, as the loop records it."""
    out = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i, (name, result) in enumerate(steps):
        out.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(result)})
    return out


def _calls(r):
    return [(c.name, json.loads(c.arguments)) for c in r.tool_calls]


def test_cfmesh_asks_for_the_layers_the_request_names_then_runs_and_submits():
    b = rb.RulesBuilder("cfmesh", {"request_txt": "internal flow, 5 prism layers"})
    assert _calls(b.next_round(_conv())) == [("configure_mesh", {"n_layers": 5})]
    assert _calls(b.next_round(_conv(("configure_mesh", {"success": True})))) == [("run_mesh", {})]
    assert _calls(b.next_round(_conv(("configure_mesh", {"success": True}),
                                     ("run_mesh", {"success": False})))) == [("submit_mesh", {})]
    done = b.next_round(_conv(("configure_mesh", {}), ("run_mesh", {}), ("submit_mesh", {"success": True})))
    assert not done.tool_calls and "rules plan" in done.assistant_text


def test_cfmesh_with_no_layer_count_leaves_the_engine_its_standard():
    b = rb.RulesBuilder("cfmesh", {"request_txt": "mesh this duct"})
    assert _calls(b.next_round(_conv())) == [("configure_mesh", {})]


def test_vmtk_leaves_staged_ports_to_the_engine_else_seeds_from_the_open_profiles():
    b = rb.RulesBuilder("vmtk", {})
    assert _calls(b.next_round(_conv())) == [("geometry_report", {})]
    staged = _conv(("geometry_report", {"staged_ports": [{"name": "in"}]}))
    assert _calls(b.next_round(staged)) == [("configure_mesh", {})]
    profiles = _conv(("geometry_report", {"open_profiles": [{"index": 0}, {"index": 1}, {"index": 2}]}))
    assert _calls(b.next_round(profiles)) == [("configure_mesh", {"source_ids": [0], "target_ids": [1, 2]})]


def test_gmsh_writes_the_rules_spec_with_no_groups_so_the_engine_binds_the_ports():
    b = rb.RulesBuilder("gmsh", {})
    r = b.next_round(_conv(("geometry_report", {"surfaces": []})))
    (name, args), = _calls(r)
    assert name == "write_file" and args["path"] == "gmsh_spec.json"
    assert json.loads(args["content"])["groups"] == []


def test_multiregion_sends_the_reports_suggested_regions_or_stops():
    b = rb.RulesBuilder("snappy_multiregion", {})
    regions = [{"name": "fluid", "type": "fluid", "solids": [0]}, {"name": "s", "type": "solid", "solids": [1]}]
    r = b.next_round(_conv(("geometry_report", {"suggested_regions": regions})))
    assert _calls(r) == [("configure_mesh", {"regions": regions})]
    stop = b.next_round(_conv(("geometry_report", {})))
    assert not stop.tool_calls and "no regions" in stop.assistant_text


def test_a_refused_configuration_stops_the_plan_and_says_why():
    b = rb.RulesBuilder("cfmesh", {})
    r = b.next_round(_conv(("configure_mesh", {"success": False, "error": "bad"})))
    assert not r.tool_calls and "refused" in r.assistant_text


def test_a_production_grade_mesh_already_built_is_submitted():
    b = rb.RulesBuilder("cfmesh", {})
    r = b.next_round(_conv(("configure_mesh", {"success": True, "mesh_already_production_grade": True})))
    assert _calls(r) == [("submit_mesh", {})]


# ------------------------------------------------------------------ when the rules plan builds ----------
def _attempt(tmp_path, engine="cfmesh"):
    return types.SimpleNamespace(engine=engine, workspace=tmp_path, messages=[{"role": "user", "content": "x"}],
                                 tool_calls=[], mode="initial", max_rounds=5, geometry=None, generation=0,
                                 source_path=None)


def _run(tmp_path, monkeypatch, ai, *, engine="cfmesh", builder_ai=True):
    seen = []

    async def _loop(messages, workspace, **kw):
        pc = kw.get("provider_call")
        seen.append("rules" if pc is not None else "ai")
        if pc is not None:
            return "Submitted by the rules plan.", list(messages)
        return await ai(kw)

    import meshpipeline.agents.builder.loop as builder_loop
    monkeypatch.setattr(builder_loop, "_run_tool_loop", _loop)
    monkeypatch.setattr(invoke, "get_spec", lambda *_a, **_k: types.SimpleNamespace(build_driver=None),
                        raising=False)
    monkeypatch.setattr(rb, "BUILDER_AI", builder_ai)
    state = {"job_id": "aig1", "retry_count": 0, "request_txt": "5 layers"}
    out = asyncio.run(invoke.run_attempt(_attempt(tmp_path, engine), state, job_id="aig1", publish=None,
                                         timeout_s=60))
    return out, seen


async def _down(kw):
    raise RuntimeError("[API_FAILURE] <<API_FAILURE:builder_server_error>>")


async def _slow(kw):
    raise TimeoutError()


async def _raises(kw):
    raise ValueError("bad answer")


async def _quiet(kw):
    return "", []


async def _wandered(kw):
    kw["tool_calls_out"].append({"tool": "run_mesh"})
    return "", []


@pytest.mark.parametrize("ai", [_down, _slow, _raises, _quiet, _wandered],
                         ids=["down", "slow", "raised", "no-mesher", "no-submit"])
def test_an_ai_that_gives_no_mesh_hands_the_attempt_to_the_rules_plan(tmp_path, monkeypatch, ai):
    dr._CITED.pop("aig1", None)
    out, seen = _run(tmp_path, monkeypatch, ai)
    assert seen == ["ai", "rules"]
    assert out.api_failure == "" and not out.timed_out and out.final_text.startswith("Submitted")
    rows = [e for e in dr.cited_for("aig1") if e["stage"] == "builder"]
    assert rows and rows[0]["rule"] == "AIG-1" and rows[0]["value"] == "rules plan"


def test_an_ai_that_submits_keeps_its_mesh(tmp_path, monkeypatch):
    async def _ok(kw):
        return "submitted", []
    out, seen = _run(tmp_path, monkeypatch, _ok)
    assert seen == ["ai"] and out.final_text == "submitted"


def test_builder_ai_off_builds_by_the_rules_plan_and_asks_no_model(tmp_path, monkeypatch):
    out, seen = _run(tmp_path, monkeypatch, _down, builder_ai=False)
    assert seen == ["rules"] and out.api_failure == ""
