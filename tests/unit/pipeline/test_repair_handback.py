"""THE MESHER HANDS ITS FAILING FACES BACK TO REPAIR, ONCE (plan task 26).

What is pinned here, and why each one is a real risk:

* FLAG-OFF IS BYTE-IDENTICAL ROUTING. The posture is read with `getattr`, so an unset setting must
  read "off" and every refusal must route exactly as it does today.
* ONE HANDBACK PER JOB, EVER. The latch is `repair_handback` on state. Without it a part whose
  re-mesh fails at the same faces hands back again, and again - a loop dressed as an adapter.
* AN ASSEMBLY IS REFUSED, WITH THE REASON. Assembly face names come from the sidecar, not from one
  part's kernel face order, so mapping them to indices would scope the repair to whichever faces
  happened to share a number.
* THE MACHINE IS COPIED, NOT CHOSEN. The hard invariant of the whole plan is that no big/XL VM is
  reachable from inside repair. The handback is the one place a re-mesh is started, so it must not
  reach the chooser at all.
* NO COMMIT ENDS ON THE MESHER'S OWN REFUSAL. An honest "that did not help either" is a result.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from meshpipeline.engines.fea_driver import CAD_DEFECT_GATE, QUALITY_GATE
from meshpipeline.pipeline import graph as G
from meshpipeline.pipeline import repair_handback as HB

# A REAL MESH-2 record, shaped as engines/fea_driver.defect_at_failure returns it and
# pipeline/executor.py puts it on state.
DEFECT = {"faces": ["Face_7", "Face_12"], "confirmed": True,
          "suspects": [{"kind": "self_overlap", "faces": ["Face_7", "Face_12"], "confirmed": True,
                        "body": 1}]}


def mesh2_state(**over) -> dict:
    """The state the executor leaves after a MESH-2 refusal."""
    base = {
        "job_id": "job-mesh2", "engine": "gmsh_fea", "retry_count": 1,
        "executor_success": False,
        "executor_failed_gate": CAD_DEFECT_GATE,
        "executor_failure_cause": "mesh_quality",
        "executor_failure_facts": {"engine": "gmsh", "deterministic": True, "fea": True,
                                   "ladder_exhausted": True, "cad_defect": DEFECT},
        "repair_handback": {},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    """The posture LIVE by default here; the off-path test turns it back off itself."""
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_HANDBACK", "live", raising=False)


# --------------------------------------------------------------------------- the route


def test_handback_off_routes_as_today(monkeypatch):
    """OFF, and UNSET, route exactly where today's code routes. The posture is fail-closed."""
    import meshpipeline.settings.cad_repair as rcfg
    state = mesh2_state()
    for value in ("off", "", "shadow", "LIVE", "true", None):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_HANDBACK", value, raising=False)
        assert HB.posture() == "off", f"{value!r} must read off"
        assert G.route_after_executor(state) != "node_repair_handback"
    # UNSET, the deployment default: the attribute is not there at all.
    monkeypatch.delattr(rcfg, "CAD_REPAIR_MESH_HANDBACK", raising=False)
    assert HB.posture() == "off"
    assert G.route_after_executor(state) == "__end__"


def test_mesh2_refusal_routes_to_handback_once():
    """The first MESH-2 refusal routes to the handback; the second ends as today."""
    assert G.route_after_executor(mesh2_state()) == "node_repair_handback"
    # After the handback the latch is set - whatever the handback concluded.
    after = mesh2_state(repair_handback={"faces": [7, 12], "outcome": "committed",
                                         "committed": True, "machine": {}})
    assert HB.wanted(after) == (False, HB.ALREADY_HANDED_BACK)
    assert G.route_after_executor(after) == "__end__"


def test_assembly_faces_are_refused_with_a_reason():
    """An assembly's sidecar names are not part face indices, and the refusal says so."""
    state = mesh2_state(executor_failure_facts={
        "deterministic": True, "ladder_exhausted": True,
        "cad_defect": {"faces": ["frame_member_1/Face_3", "bracket"], "confirmed": True,
                       "suspects": []}})
    ok, why = HB.wanted(state)
    assert ok is False
    assert why == HB.ASSEMBLY_FACES == "assembly face names are not part faces"
    assert G.route_after_executor(state) != "node_repair_handback"


def test_a_non_cad_defect_refusal_is_untouched():
    """Only MESH-2 hands back. A quality refusal with no `cad_defect` fact is today's path."""
    state = mesh2_state(executor_failed_gate=QUALITY_GATE,
                        executor_failure_facts={"engine": "gmsh", "deterministic": True, "fea": True,
                                                "ladder_exhausted": True})
    assert HB.wanted(state) == (False, HB.NOT_A_CAD_DEFECT)
    assert G.route_after_executor(state) != "node_repair_handback"


def test_a_successful_attempt_never_hands_back():
    """A mesh that PASSED carries no refusal; the route must not read stale facts."""
    state = mesh2_state(executor_success=True, requirement_caveats=[])
    assert G.route_after_executor(state) != "node_repair_handback"


# ------------------------------------------------------------------- the faces and the machine


def test_face_names_map_to_one_based_kernel_indices():
    """`Face_k` is OpenCASCADE's 1-based face order, which is what `focus_faces` takes."""
    focus, why = HB.focus_of(["Face_12", "Face_7", "Face_7"])
    assert focus == (7, 12), "sorted, de-duplicated, and 1-based as named"
    assert why == ""
    assert HB.focus_of([]) == ((), HB.NO_FACES)
    assert HB.focus_of(["Face_0"]) == ((), HB.NO_FACES), "there is no face 0 in a 1-based order"


def test_machine_is_read_off_the_failing_attempts_workspace(tmp_path):
    """The machine is the one the failing attempt ALREADY ran on, read, never decided."""
    from meshpipeline.contracts import mesh_machine as MM
    decision = MM.choose(process_gib=100.0, workspace_gib=1.0, why="fea")
    assert decision["machine"] == "n2-highmem-16", "fixture must pin a real VM decision"
    MM.write(tmp_path, decision)
    assert HB.machine_of({"openfoam_workspace": str(tmp_path)}) == decision
    # No workspace, or no decision file: Cloud Run, as always - recorded as such, not invented.
    assert HB.machine_of({"openfoam_workspace": str(tmp_path / "gone")}) == {}
    assert HB.machine_of({}) == {}


def test_handback_never_calls_the_machine_chooser_itself(monkeypatch, tmp_path):
    """THE PLAN'S HARD INVARIANT. The handback is the one place a re-mesh starts, so it must not
    reach the machine chooser - a chooser call from here is how a repair buys a big VM."""
    from meshpipeline.contracts import mesh_machine as MM

    def boom(*a, **k):
        raise AssertionError("the handback called the machine chooser")

    monkeypatch.setattr(MM, "choose", boom)
    monkeypatch.setattr(MM, "step_up", boom)
    monkeypatch.setattr(MM, "for_cells", boom, raising=False)
    monkeypatch.setattr(MM, "for_time", boom, raising=False)
    monkeypatch.setattr(MM, "off_the_wall", boom, raising=False)

    (tmp_path / MM.MACHINE_FILE).write_text(json.dumps(
        {"where": "gce", "machine": "n2-highmem-16", "memory_gib": 128.0}))

    import asyncio
    _patch_repair(monkeypatch, committed=True, calls=[])
    state = mesh2_state(openfoam_workspace=str(tmp_path))
    out = asyncio.run(HB.node_repair_handback(state))
    assert out["repair_handback"]["machine"] == {"where": "gce", "machine": "n2-highmem-16",
                                                "memory_gib": 128.0}


def test_the_handback_module_imports_no_doorway_to_a_big_machine():
    """An AST check, because a deferred import inside a function is exactly how this would creep
    back in. The handback may READ a decision (`mesh_machine.read`); it may not reach the
    adapters, the FEA driver or the mesh-execution contract that STARTS one."""
    banned = ("meshpipeline.contracts.mesh_execution", "meshpipeline.adapters",
              "meshpipeline.engines.fea_driver", "meshpipeline.engines.fea_ladder",
              "meshpipeline.engines.mesher_stage", "meshpipeline.engines.gmsh")
    tree = ast.parse(Path(HB.__file__).read_text(encoding="utf-8"))
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            seen.add(node.module)
    for name in sorted(seen):
        assert not any(name == b or name.startswith(b + ".") for b in banned), \
            f"pipeline/repair_handback.py imports {name}"
    # `cad/` must not have learned about `pipeline/`: the handback calls INTO cad, never back.
    assert "meshpipeline.cad.repair.conservative" in seen


# --------------------------------------------------------------------------- the node


def _patch_repair(monkeypatch, *, committed, calls, raises=None):
    """Stand in for the scoped repair, recording what it was asked for."""
    class _Report:
        measurements = ()
        operations = ()
        entities = ()

        def to_dict(self):
            return {"committed": committed}

    async def fake(state, *, focus_faces, **kw):
        calls.append({"focus_faces": focus_faces, **kw})
        if raises is not None:
            raise raises
        return {"committed": committed, "outcome": "committed" if committed else "no_commit",
                "report_ref": "repair-report-1",
                "state": {"repair_lineage": {"from": "x"}} if committed else {}}

    monkeypatch.setattr(HB, "_scoped_repair", fake)
    return calls


def test_the_node_scopes_the_repair_to_the_named_faces(monkeypatch):
    import asyncio
    calls = _patch_repair(monkeypatch, committed=True, calls=[])
    out = asyncio.run(HB.node_repair_handback(mesh2_state()))
    assert calls and calls[0]["focus_faces"] == (7, 12), \
        "the repair must be scoped to exactly the faces the mesher named"
    record = out["repair_handback"]
    assert record["faces"] == [7, 12]
    assert record["committed"] is True
    assert record["outcome"] == "committed"
    assert record["report_ref"] == "repair-report-1"


def test_a_committed_handback_routes_to_one_re_mesh(monkeypatch):
    import asyncio
    _patch_repair(monkeypatch, committed=True, calls=[])
    out = asyncio.run(HB.node_repair_handback(mesh2_state()))
    assert G.route_after_handback({**mesh2_state(), **out}) == "node_geometry_admission"
    # the re-mesh starts from a clean executor verdict, or the route would read the OLD failure
    assert out["executor_success"] is False
    assert out["executor_failed_gate"] == ""
    assert out["executor_failure_facts"] == {}
    assert out["geometry_unsuitable_reason"] == ""


def test_no_commit_ends_on_the_original_refusal(monkeypatch):
    """Nothing changed, so re-meshing would ask the same question. The run ends as the mesher
    left it - with the handback's record attached, which is the point of recording a negative."""
    import asyncio
    _patch_repair(monkeypatch, committed=False, calls=[])
    state = mesh2_state()
    out = asyncio.run(HB.node_repair_handback(state))
    assert out["repair_handback"]["committed"] is False
    assert out["repair_handback"]["outcome"] == "no_commit"
    # the refusal is UNTOUCHED: the user's sentence is still the mesher's own
    assert "executor_failure_facts" not in out
    assert "executor_failed_gate" not in out
    merged = {**state, **out}
    assert G.route_after_handback(merged) == "__end__"


def test_a_repair_that_blows_up_is_a_handback_that_did_not_help(monkeypatch):
    """A repair crashing is OUR problem, never the file's: the run ends on the mesher's refusal."""
    import asyncio
    _patch_repair(monkeypatch, committed=False, calls=[], raises=RuntimeError("kernel"))
    out = asyncio.run(HB.node_repair_handback(mesh2_state()))
    assert out["repair_handback"]["committed"] is False
    assert out["repair_handback"]["outcome"] == "failed"
    assert G.route_after_handback({**mesh2_state(), **out}) == "__end__"


def test_the_node_refuses_rather_than_latching_nothing(monkeypatch):
    """The node is only reached through the route, but a node that recorded {} on a refusal would
    un-latch the job - so a refused handback is still a RECORD."""
    import asyncio

    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_HANDBACK", "off", raising=False)
    out = asyncio.run(HB.node_repair_handback(mesh2_state()))
    assert out["repair_handback"]["outcome"] == "declined"
    assert out["repair_handback"]["why"] == HB.NOT_LIVE
    assert out["repair_handback"]["committed"] is False
    assert out["repair_handback"], "a refused handback must still latch the job"


# --------------------------------------------------------------------------- the contract


def test_repair_handback_is_state_only():
    """No caller asks for a handback: the mesher's own refusal is the only thing that can."""
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION, PipelineState
    from meshpipeline.pipeline.data_contract import STATE_FIELDS, dispatch_kwargs

    assert "repair_handback" in PipelineState.__annotations__
    assert "repair_handback" in STATE_FIELDS
    assert "repair_handback" not in set(dispatch_kwargs())
    assert STATE_SCHEMA_VERSION >= 16, "a new state field bumps the checkpoint namespace"


def test_the_graph_wires_the_handback_both_ways():
    """A route that can return a node the graph has no edge for is a runtime crash, not a bug
    report, so the node and both its edges are pinned here."""
    src = Path(G.__file__).read_text(encoding="utf-8")
    assert 'b.add_node("node_repair_handback"' in src
    assert '"node_repair_handback": "node_repair_handback"' in src
    assert 'route_after_handback' in src
    for target in ("node_geometry_admission", "node_deliver_sound", "node_cfd_wrap"):
        assert target in src


def test_the_record_is_json_serialisable(monkeypatch):
    """It rides a LangGraph checkpoint and a job record; a dataclass or an enum in it is a
    serialisation failure in production, after the repair has already been spent."""
    import asyncio
    _patch_repair(monkeypatch, committed=True, calls=[])
    out = asyncio.run(HB.node_repair_handback(mesh2_state()))
    json.dumps(out["repair_handback"])
