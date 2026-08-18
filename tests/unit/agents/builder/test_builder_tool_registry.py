# Responsibility: Verify the registered tool names, order, routes and schemas are stable and scoped to their engine.
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from tests._scan import scanned

from meshpipeline.agents.builder import tools as T
from meshpipeline.agents.builder.tools import geometry, meshing, research, workspace
from meshpipeline.engines.registry import engine_names

PKG = Path(inspect.getsourcefile(T)).parent
FAMILIES = {"workspace": workspace, "geometry": geometry, "meshing": meshing, "research": research}

#: The registry the model has always seen, in order. A change here is a changed prompt.
EXPECTED_ORDER = ["write_file", "read_file", "list_directory", "web_search", "geometry_report",
                  "submit_mesh", "measure_scales", "run_mesh", "run_python"]


def _names(specs):
    return [s["function"]["name"] for s in specs]


# the registry

def test_the_registered_name_set_and_order_are_unchanged():
    assert _names(T.TOOLS) == EXPECTED_ORDER


def test_no_tool_is_registered_twice():
    names = _names(T.TOOLS)
    assert len(names) == len(set(names)), f"duplicate tool registration: {names}"


def test_every_registered_tool_is_routed_and_every_route_is_reachable():
    routed = set(T._ROUTES)
    registered = set(_names(T.TOOLS))
    # configure_mesh is engine-declared rather than base-registered
    assert registered <= routed, f"registered but unrouted: {sorted(registered - routed)}"
    assert routed - registered == {"configure_mesh"}, sorted(routed - registered)


def test_every_tool_has_a_word_for_the_person_watching():
    missing = [n for n in T._ROUTES if n not in T.ACTIONS]
    assert not missing, f"tools with no user-facing action word: {missing}"
    assert T.action_for("nonexistent") == "Working"


def test_each_schema_is_well_formed_and_declares_its_required_arguments():
    for spec in T.TOOLS:
        fn = spec["function"]
        assert spec["type"] == "function"
        assert fn["name"] and fn["description"].strip()
        params = fn.get("parameters", {})
        assert params.get("type") == "object"
        for req in params.get("required", []):
            assert req in params.get("properties", {}), (
                f"{fn['name']}: required argument {req!r} is not declared")


def test_the_schema_and_description_of_every_tool_are_stable():
    digests = {}
    for spec in T.TOOLS:
        fn = spec["function"]
        digests[fn["name"]] = hashlib.sha256(
            json.dumps({"d": fn["description"], "p": fn.get("parameters", {})},
                       sort_keys=True).encode()).hexdigest()[:12]
    assert len(set(digests.values())) == len(digests), "two tools share a schema+description"
    for name in EXPECTED_ORDER:
        assert name in digests


# engine scope

@pytest.mark.parametrize("engine", sorted(engine_names()))
def test_engine_scope_comes_from_the_engine_declaration(engine):
    from meshpipeline.engines.registry import get_spec

    active = _names(T._active_tools(engine))
    declared = set(get_spec(engine).tool_names)
    assert set(active) <= declared, (
        f"{engine} was offered tools it does not declare: {sorted(set(active) - declared)}")


def test_an_engines_authoring_tool_never_reaches_another_engine():
    from meshpipeline.engines.registry import get_spec

    authoring = {}
    for engine in engine_names():
        sp = get_spec(engine)
        if sp.authoring_tool:
            authoring[engine] = json.dumps(sp.authoring_tool, sort_keys=True)
    for engine in engine_names():
        mine = authoring.get(engine)
        offered = [json.dumps(t, sort_keys=True) for t in T._active_tools(engine)]
        foreign = [e for e, blob in authoring.items() if e != engine and blob in offered
                   and blob != mine]
        assert not foreign, f"{engine} was offered {foreign}'s authoring tool"


class _Spec:

    def __init__(self, *, declares_configure_mesh: bool):
        names = {"read_file", "run_mesh"}
        if declares_configure_mesh:
            names.add("configure_mesh")
        self.tool_names = names
        self.authoring_tool = {"type": "function", "function": {
            "name": "configure_mesh", "description": "an engine's own authoring vocabulary",
            "parameters": {"type": "object", "properties": {}, "required": []}}}


@pytest.mark.parametrize("declares,expected", [(True, True), (False, False)])
def test_the_authoring_tool_follows_the_declaration_not_the_engines_possession_of_one(
        monkeypatch, declares, expected):
    import meshpipeline.engines.registry as reg
    monkeypatch.setattr(reg, "get_spec", lambda name: _Spec(declares_configure_mesh=declares))
    offered = "configure_mesh" in _names(T._active_tools("anything"))
    assert offered is expected


def test_no_catalog_engine_receives_an_authoring_tool_it_did_not_declare():
    from meshpipeline.engines.registry import get_spec

    for engine in engine_names():
        if "configure_mesh" not in set(get_spec(engine).tool_names):
            assert "configure_mesh" not in _names(T._active_tools(engine))


def test_the_purpose_never_changes_the_offered_tools():
    for engine in engine_names():
        assert _names(T._active_tools(engine)) == _names(T._active_tools(engine))


# family ownership

def test_every_schema_is_owned_by_exactly_one_family():
    owners: dict[str, list[str]] = {}
    for fam_name, mod in FAMILIES.items():
        for spec in getattr(mod, "SCHEMAS", []):
            owners.setdefault(spec["function"]["name"], []).append(fam_name)
    for name, fams in owners.items():
        assert len(fams) == 1, f"{name} is declared by {fams}"
    assert set(owners) == set(EXPECTED_ORDER)


def test_every_action_word_is_owned_by_exactly_one_family():
    seen: dict[str, list[str]] = {}
    for fam_name, mod in FAMILIES.items():
        for name in getattr(mod, "ACTIONS", {}):
            seen.setdefault(name, []).append(fam_name)
    for name, fams in seen.items():
        assert len(fams) == 1, f"the action word for {name} is declared by {fams}"


def test_no_implementation_survives_in_a_second_place():
    tree = ast.parse((PKG / "__init__.py").read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert defined <= {"action_for", "_dispatch_tool", "_active_tools",
                   # result serialization and the already-announced mesh route are
                   # dispatch concerns, not tool implementations
                   "_serialized", "dispatch_prepared_mesh"}, (
        f"the assembly point defines tool implementations: {sorted(defined)}")


def test_the_facade_holds_no_family_policy():
    src = (PKG / "__init__.py").read_text()
    for policy in ("meshDict", "seccomp", "boundaryLayers", "scan_python_source",
                   "inspect_stl", "_PROTECTED_PATHS = frozenset"):
        assert policy not in src, f"the assembly point carries family policy: {policy}"


def test_there_is_no_central_elif_dispatcher():
    fn = next(n for n in ast.walk(ast.parse((PKG / "__init__.py").read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "_dispatch_tool")
    compares = [n for n in ast.walk(fn)
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                and n.left.id == "name"]
    assert not compares, "a name-comparison dispatcher returned to the assembly point"


def test_no_dumping_ground_module_was_created():
    for banned in ("utils.py", "helpers.py", "common.py", "misc.py", "_shared.py",
                   "tool_helpers.py", "common_tools.py", "misc_tools.py"):
        assert not (PKG / banned).exists(), f"a dumping ground was created: {banned}"


def test_family_dependency_direction_is_one_way():
    ws = (PKG / "workspace.py").read_text()
    for fam in ("geometry", "meshing", "research"):
        assert f"tools.{fam}" not in ws and f"import {fam}" not in ws, (
            f"workspace imports {fam} - the shared primitive depends on its consumer")


def test_no_family_imports_the_application_or_api_layers():
    for fam in scanned(PKG.glob("*.py"), "the Builder tool-family modules"):
        src = fam.read_text()
        for banned in ("meshpipeline.api", "fastapi", "meshpipeline.application.pipeline_run"):
            assert banned not in src, f"{fam.name} imports {banned}"


def test_builder_tools_own_no_approval_or_intake_transaction():
    for fam in scanned(PKG.glob("*.py"), "the Builder tool-family modules"):
        src = fam.read_text()
        for banned in ("intake.approval", "get_for_update", "confirm_pending_approval"):
            assert banned not in src, f"{fam.name} reaches into the approval transaction"


def test_no_family_imports_the_assembly_point():
    for fam in scanned(PKG.glob("*.py"), "the Builder tool-family modules"):
        if fam.name == "__init__.py":
            continue
        src = fam.read_text()
        for cyc in ("from meshpipeline.agents.builder import tools",
                    "from meshpipeline.agents.builder.tools import",
                    "import meshpipeline.agents.builder.tools\n"):
            assert cyc not in src, f"{fam.name} imports the assembly point - that is a cycle"


def test_every_family_module_is_importable_on_its_own():
    import subprocess
    import sys

    for fam in scanned(sorted(f.stem for f in PKG.glob("*.py") if f.name != "__init__.py"), "the Builder tool families"):
        out = subprocess.run(
            [sys.executable, "-c", f"import meshpipeline.agents.builder.tools.{fam}"],
            capture_output=True, text=True)
        assert out.returncode == 0, f"{fam} cannot be imported alone:\n{out.stderr}"


def test_the_package_is_shaped_so_the_wheel_build_will_find_it():
    assert (PKG / "__init__.py").exists(), "the tools package has no __init__.py"
    for d in scanned([PKG, *(x for x in PKG.rglob("*") if x.is_dir() and x.name != "__pycache__")], "the Builder tool package directories"):
        if any(f.suffix == ".py" and f.name != "__init__.py" for f in d.iterdir()):
            assert (d / "__init__.py").exists(), f"{d.name}/ ships .py files but is not a package"


# prompts reference real tools

def test_every_builder_prompt_names_only_registered_tools():
    import importlib
    import re

    registered = set(T._ROUTES)
    for engine in engine_names():
        pack = importlib.import_module(f"meshpipeline.engines.{engine}.pack")
        text = next(v for k, v in vars(pack).items() if k.endswith("_SYSTEM"))
        tools_line = [ln for ln in text.splitlines() if ln.startswith("TOOLS:")]
        for line in tools_line:
            for name in re.findall(r"\b[a-z_]{4,}\b", line.split(":", 1)[1]):
                if name in {"and", "the", "for", "with"}:
                    continue
                assert name in registered or name not in registered and "_" not in name, (
                    f"{engine} prompt names an unregistered tool: {name}")


# the frozen contract
# `builder_tool_manifest.json` is the tool contract as it stood BEFORE the split, captured
# from the 805-line module and checked in unchanged. It is not a snapshot of current behaviour: it
# is the thing current behaviour must still equal. Regenerating it to make a test pass is the one
# edit that defeats the whole file - change it only when the model's contract is deliberately
# changing, and say so in the commit.

BASELINE = json.loads((Path(__file__).parent / "builder_tool_manifest.json").read_text())


def _norm(obj):
    if isinstance(obj, dict):
        return {k: _norm(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_norm(x) for x in obj]
    return obj


def _live_registry() -> list[dict]:
    out = []
    for pos, spec in enumerate(T.TOOLS):
        fn = spec["function"]
        out.append({"position": pos, "name": fn["name"],
                    "description_sha": hashlib.sha256(
                        fn.get("description", "").encode()).hexdigest()[:16],
                    "schema": _norm(fn.get("parameters", {})),
                    "type": spec.get("type")})
    return out


def test_the_registry_still_equals_the_pre_split_contract():
    assert _live_registry() == BASELINE["registry"]


@pytest.mark.parametrize("expected", BASELINE["registry"], ids=lambda e: e["name"])
def test_each_tool_individually_matches_the_pre_split_contract(expected):
    live = {e["name"]: e for e in _live_registry()}
    assert expected["name"] in live, f"{expected['name']} is no longer registered"
    got = live[expected["name"]]
    assert got["position"] == expected["position"], "registration order changed"
    assert got["description_sha"] == expected["description_sha"], "the description changed"
    assert got["schema"] == expected["schema"], "the JSON schema changed"


def test_the_user_facing_action_words_are_unchanged():
    live = {name: {"async": False, "qualname": word} for name, word in T.ACTIONS.items()}
    assert live == BASELINE["actions"]


@pytest.mark.parametrize("engine", sorted(BASELINE["engine_scope"]))
def test_each_engine_sees_exactly_the_tools_it_saw_before_the_split(engine):
    assert _names(T._active_tools(engine)) == BASELINE["engine_scope"][engine]
