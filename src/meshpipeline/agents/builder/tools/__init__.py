# Responsibility: Assemble the builder's tool surface and map a tool name to its action.
# Boundaries: composition of the tool families in this package; no tool's behaviour lives here.
from __future__ import annotations

import json
from collections.abc import Callable

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.agents.builder.tool_context import BuilderToolContext
from meshpipeline.agents.builder.tools import geometry, meshing, research, workspace

logger = workspace.logger

#: The staged surface every engine meshes from. Re-exported: several engines and tests import it
#: from this path.
STAGED_SURFACE = workspace.STAGED_SURFACE

# THE REGISTRY. Order is the order the model has always seen; the manifest test compares it
# position by position, because a reordered registry is a changed prompt.
TOOLS: list[dict] = [
    workspace.SCHEMAS[0],     # write_file
    workspace.SCHEMAS[1],     # read_file
    workspace.SCHEMAS[2],     # list_directory
    research.SCHEMAS[0],      # web_search
    geometry.SCHEMAS[0],      # geometry_report
    meshing.SCHEMAS[0],       # submit_mesh
    geometry.SCHEMAS[1],      # measure_scales
    meshing.SCHEMAS[1],       # run_mesh
    research.SCHEMAS[1],      # run_python
]

# What each tool means to the PERSON watching. Tool names are the LLM's vocabulary; the user is
# owed the action, not the API. Each family declares its own words, so a new tool cannot ship
# without one (test-enforced).
ACTIONS: dict[str, str] = {
    **geometry.ACTIONS, **meshing.ACTIONS, **workspace.ACTIONS, **research.ACTIONS,
}


def action_for(tool_name: str) -> str:
    return ACTIONS.get(tool_name, "Working")


# ROUTING AS DATA. Each entry says which family owns the tool and how its arguments are drawn from
# the call. Replacing the former `elif` chain matters beyond tidiness: a family can no longer be
# added to the registry without also being routed, and this module cannot acquire an opinion about
# what an individual tool does.
_ROUTES: dict[str, Callable[[BuilderToolContext, dict], dict]] = {
    "write_file":      lambda ctx, a: workspace.write_file(ctx.workspace, a["path"], a["content"]),
    "read_file":       lambda ctx, a: workspace.read_file(ctx.workspace, a["path"]),
    "list_directory":  lambda ctx, a: workspace.list_directory(ctx.workspace, a.get("path", ".")),
    "web_search":      lambda ctx, a: research.web_search(a["query"], job_id=ctx.job_id),
    "geometry_report": lambda ctx, a: geometry.geometry_report(ctx),
    "measure_scales":  lambda ctx, a: geometry.measure_scales(ctx, a),
    "configure_mesh":  lambda ctx, a: meshing.configure_mesh(ctx, a),
    "run_mesh":        lambda ctx, a: meshing.run_mesh(ctx),
    "run_python":      lambda ctx, a: research.run_python(ctx.workspace, a["code"]),
    "submit_mesh":     lambda ctx, a: meshing.submit_mesh(ctx, a),
}


def _dispatch_tool(ctx: BuilderToolContext, name: str, args: dict) -> str:
    job_id = ctx.job_id
    try:
        route = _ROUTES.get(name)
        result = route(ctx, args) if route is not None else {"error": f"Unknown tool: {name}"}
    except KeyError as exc:
        result = {"error": f"Missing required argument for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a tool crash is MODEL FEEDBACK, never a node death
        # A live run died here: list_directory raised NotADirectoryError and the exception
        # propagated through the loop, failing the whole build on a recoverable mistake.
        logger.warning("Builder: tool '%s' crashed (%s: %s) - returned as an error result "
                       "(job_id=%s)", name, type(exc).__name__, exc, job_id)
        result = {"error": f"{name} failed: {type(exc).__name__}: {exc}"}
    return _serialized(name, job_id, result)


# The one place a tool result becomes a string, so every route - including the mesh run the
# executor drives in two halves - is bounded by the same cap.
def _serialized(name: str, job_id: str, result) -> str:
    out = json.dumps(result)
    # Hard backstop: no single tool result may flood the context window. Even if a tool returns
    # something huge, replace it with a bounded error.
    _cap = rtcfg.MAX_TOOL_OUTPUT_CHARS
    if len(out) > _cap:
        logger.warning("Builder: tool '%s' output %d chars exceeds cap %d - truncated to a notice "
                       "(job_id=%s)", name, len(out), _cap, job_id)
        out = json.dumps({
            "error": f"{name} output was {len(out)} chars - too large to return ({_cap} char cap). "
                     "Return a smaller result.",
            "output_chars": len(out),
        })
    return out


# The execute half of `run_mesh`, after its announcement has been authorized. Same error
# classification and same output cap as any other route.
def dispatch_prepared_mesh(ctx: BuilderToolContext, prepared) -> str:
    job_id = ctx.job_id
    try:
        result = meshing.execute_prepared_mesh_run(ctx, prepared)
    except Exception as exc:  # noqa: BLE001 - a tool crash is MODEL FEEDBACK, never a node death
        logger.warning("Builder: tool 'run_mesh' crashed (%s: %s) - returned as an error result "
                       "(job_id=%s)", type(exc).__name__, exc, job_id)
        result = {"error": f"run_mesh failed: {type(exc).__name__}: {exc}"}
    return _serialized("run_mesh", job_id, result)


def _active_tools(engine: str = "", user_dispute=None) -> list[dict]:
    import copy

    from meshpipeline.contracts.human_flags import flags_of
    from meshpipeline.engines.registry import get_spec
    sp = get_spec(engine)   # unknown/"" resolves to the default engine
    names = set(sp.tool_names)
    tools = [t for t in TOOLS if t.get("function", {}).get("name") in names]
    if "configure_mesh" in names and sp.authoring_tool:
        tools.append(sp.authoring_tool)

    # Only a dispute changes the submit surface; every ordinary build sees the historical roster.
    flags = tuple(flags_of(user_dispute))
    if flags:
        out = []
        for tool in tools:
            if tool.get("function", {}).get("name") != "submit_mesh":
                out.append(tool)
                continue
            sub = copy.deepcopy(tool)
            params = sub["function"]["parameters"]
            params["properties"]["flag_responses"] = meshing.submit_flag_responses_property(flags)
            params["required"] = [*params.get("required", []), "flag_responses"]
            sub["function"]["description"] += (
                " This build answers an engineer's dispute: declare, per flagged region, what you "
                "set out to correct and what you actually changed.")
            out.append(sub)
        tools = out
    return tools


# The names production and the test suite import from `agents.builder.tools`. Re-exported from
# their owning family so this module stays an assembly point rather than a second implementation.
_PROTECTED_PATHS = workspace._PROTECTED_PATHS
_confined = workspace._confined
_restore_protected = workspace._restore_protected
_tool_write_file = workspace.write_file
_tool_read_file = workspace.read_file
_tool_list_directory = workspace.list_directory
_tool_web_search = research.web_search
_tool_run_python = research.run_python
_tool_geometry_report = geometry.geometry_report
_tool_measure_scales = geometry.measure_scales
_tool_configure_mesh = meshing.configure_mesh
_tool_run_mesh = meshing.run_mesh
_tool_submit_mesh = meshing.submit_mesh
_RUN_MESH_FEEDBACK_RESERVE = meshing._RUN_MESH_FEEDBACK_RESERVE
_RUN_MESH_MIN_ATTEMPT = meshing._RUN_MESH_MIN_ATTEMPT
_contract_patches = meshing._contract_patches
_contract_wall_patch = meshing._contract_wall_patch
get_spec_run_files = meshing.get_spec_run_files
input_contract_rejection = meshing.input_contract_rejection

__all__ = ["ACTIONS", "STAGED_SURFACE", "TOOLS", "action_for", "get_spec_run_files",
           "input_contract_rejection"]

