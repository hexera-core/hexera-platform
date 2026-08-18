# Responsibility: Verify every geometric tool takes the typed context and never looks geometry up for itself.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
# the geometric tools live in their own families.
TOOLS_PKG = SRC / "agents" / "builder" / "tools"
#: The geometric tools live in two families; the contract spans both.
TOOLS = TOOLS_PKG / "geometry.py"
GEOMETRIC_SOURCES = (TOOLS_PKG / "geometry.py", TOOLS_PKG / "meshing.py",
                     TOOLS_PKG / "__init__.py")
TOOL_CONTEXT = SRC / "agents" / "builder" / "tool_context.py"

#: The tools whose subject IS the geometry. Each must take the typed context, not a bare path.
# dropped the `_tool_` prefix: these are their family's public verbs now.
GEOMETRIC_TOOLS = {
    "geometry_report",
    "measure_scales",
    "configure_mesh",
    "run_mesh",
    "submit_mesh",
}

#: Geometry inputs a tool must not name for ITSELF. The staged surface is exempt by name only
#: through the module's single `STAGED_SURFACE` constant - a derived workspace artefact every
#: engine analyses - so one place decides what that file is called rather than five.
DISCOVERY_LITERALS = {"input.stl", "input.step", "input.stp", "input.vtp", "geom.stl"}


def _module(path: Path) -> ast.Module:
    if path is TOOLS:
        return ast.parse("\n".join(p.read_text() for p in GEOMETRIC_SOURCES),
                         filename="builder-geometric-tools")
    return ast.parse(path.read_text(), filename=str(path))


def _functions(mod: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(mod)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_geometric_tool_takes_the_typed_context_first():
    funcs = _functions(_module(TOOLS))

    missing = sorted(t for t in GEOMETRIC_TOOLS if t not in funcs)
    assert not missing, f"these tools no longer exist under those names: {missing}"

    for name in sorted(GEOMETRIC_TOOLS):
        args = funcs[name].args.args
        assert args, f"{name} takes no arguments at all"
        first = args[0]
        assert first.arg == "ctx", (
            f"{name} takes {first.arg!r} first, not the typed context. A tool that receives a "
            "workspace has to rediscover geometry from files, and a file states no unit.")
        annotation = ast.unparse(first.annotation) if first.annotation else ""
        assert "BuilderToolContext" in annotation, (
            f"{name}'s first parameter is not annotated BuilderToolContext (got {annotation!r})")


def test_the_dispatcher_takes_the_context_rather_than_a_workspace():
    dispatch = _functions(_module(TOOLS))["_dispatch_tool"]
    first = dispatch.args.args[0]
    assert first.arg == "ctx"
    assert "BuilderToolContext" in ast.unparse(first.annotation)


def test_no_geometric_tool_names_a_geometry_file_of_its_own():
    funcs = _functions(_module(TOOLS))
    offenders: list[str] = []

    for name in sorted(GEOMETRIC_TOOLS):
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in DISCOVERY_LITERALS:
                    offenders.append(f"{name} -> {node.value!r} (line {node.lineno})")

    assert not offenders, (
        "a geometric builder tool names a geometry input directly:\n  "
        + "\n  ".join(offenders)
        + "\nUse the module-level STAGED_SURFACE constant, and ctx.require_geometry() for the "
          "physical meaning - a filename carries neither identity nor scale.")


def test_every_geometric_tool_requires_approved_geometry_before_acting():
    funcs = _functions(_module(TOOLS))
    missing = []
    def _requires(fn, depth=0):
        # Follow one hop of delegation inside the module: run_mesh is driven as a prepare/execute
        # pair so its announcement can be authorized on the event loop, and the requirement lives
        # in the half that decides whether the run happens at all.
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "require_geometry" for n in ast.walk(fn)):
            return True
        if depth >= 1:
            return False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in funcs and _requires(funcs[n.func.id], depth + 1):
                return True
        return False

    for name in sorted(GEOMETRIC_TOOLS - {"submit_mesh"}):
        if not _requires(funcs[name]):
            missing.append(name)
    assert not missing, (
        f"these tools act on geometry without requiring it: {missing}. "
        "submit_mesh is exempt: it only checks for a marker the run already produced.")


def test_the_context_derives_geometry_facts_rather_than_storing_them():
    mod = _module(TOOL_CONTEXT)
    cls = next(n for n in ast.walk(mod)
               if isinstance(n, ast.ClassDef) and n.name == "BuilderToolContext")

    stored = {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign)}
    derived = {n.name for n in cls.body
               if isinstance(n, ast.FunctionDef)
               and any(isinstance(d, ast.Name) and d.id == "property" for d in n.decorator_list)}

    for fact in ("source", "interpretation", "prepared", "geometry_path"):
        assert fact in derived, f"{fact} must be a derived property"
        assert fact not in stored, (
            f"{fact} is stored as a field - it can then disagree with `geometry`")

    assert "geometry" in stored, "the one typed geometry must be the stored field"


def test_the_tool_layer_does_not_look_geometry_up_for_itself():
    mod = _module(TOOLS)
    forbidden = ("GeometrySourceRepository", "GeometryInterpretationRepository",
                 "get_object_store", "get_db")
    found: list[str] = []
    for node in ast.walk(mod):
        if isinstance(node, ast.Name) and node.id in forbidden:
            found.append(f"{node.id} (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.append(f"{node.attr} (line {node.lineno})")

    assert not found, (
        "the builder tool layer reaches for its own geometry authority: " + ", ".join(found))


@pytest.mark.parametrize("engine", ["cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"])
def test_every_engine_boundary_keeps_accepting_the_context(engine):
    import inspect

    from meshpipeline.engines.runtime import get_engine

    R = get_engine(engine)
    for method in ("run_cartesian_mesh", "inspect_stl"):
        params = inspect.signature(getattr(R, method)).parameters
        assert "context" in params, f"{engine}.{method} dropped its context parameter"
