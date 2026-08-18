# Responsibility: Verify no engine contract imports above its layer or reaches another engine's internals.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests._scan import scanned

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
ENGINES = SRC / "engines"

#: module -> the meshpipeline modules it may NOT import, at any depth or in any form.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    # the judgement vocabulary is a leaf: it knows nothing about engines at all
    "review_types.py": ("meshpipeline.engines.base", "meshpipeline.engines.registry",
                        "meshpipeline.engines.gates", "meshpipeline.api",
                        "meshpipeline.application", "meshpipeline.persistence",
                        "meshpipeline.agents", "meshpipeline.pipeline"),
    # gate execution needs errors, and nothing else from the project
    "gates.py": ("meshpipeline.engines.base", "meshpipeline.engines.registry",
                 "meshpipeline.engines.review_types", "meshpipeline.api",
                 "meshpipeline.application", "meshpipeline.persistence", "meshpipeline.agents"),
    # the declaration hub must never import its own composer
    "base.py": ("meshpipeline.engines.registry", "meshpipeline.api", "meshpipeline.application",
                "meshpipeline.persistence", "meshpipeline.agents"),
}


def _imports(path: Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def context(n) -> str:
        cur, ctx = parents.get(n), []
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ctx.append("deferred")
            elif isinstance(cur, ast.If):
                ctx.append("type_checking" if "TYPE_CHECKING" in ast.unparse(cur.test)
                           else "conditional")
            cur = parents.get(cur)
        return "+".join(sorted(set(ctx))) or "top_level"

    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level:
                # a relative import resolves inside meshpipeline.engines for these modules
                mod = "meshpipeline.engines." + mod if mod else "meshpipeline.engines"
            out.append((mod, n.lineno, context(n)))
        elif isinstance(n, ast.Import):
            out += [(a.name, n.lineno, context(n)) for a in n.names]
    return out


@pytest.mark.parametrize("module", sorted(FORBIDDEN))
def test_a_contract_module_never_imports_above_its_layer(module):
    path = ENGINES / module
    assert path.exists(), f"{module} does not exist - the layer it defines is gone"
    offenders = [f"{mod} at line {ln} ({ctx})"
                 for mod, ln, ctx in _imports(path)
                 for banned in FORBIDDEN[module]
                 if mod == banned or mod.startswith(banned + ".")]
    assert not offenders, f"engines/{module} imports above its layer: {offenders}"


def test_the_declaration_hub_no_longer_depends_on_its_composer():
    mods = {m for m, _l, _c in _imports(ENGINES / "base.py")}
    assert "meshpipeline.engines.registry" not in mods, (
        "engines/base.py imports engines.registry again - the contract hub depends on the module "
        "that composes it, and a deferred import only hides the cycle")


#: Cross-engine internal imports that EXIST TODAY, pinned so no new one can appear unnoticed.
# EMPTY as of. The last two entries were the `criteria` edges: `snappy` and
#: `snappy_multiregion` reached into cfMesh's table for `_FATAL_TOPOLOGY`, `_MAX_NON_ORTHO` and the
#: citation they share. Those three are OpenFOAM's facts rather than cfMesh's - checkMesh validity
# and the standard meshQualityControls bar apply to any mesh a solver receives - so moved
#: them to `engines/openfoam_criteria.py`, which all three bundles now import as peers. The
#: mechanism stays: an edge that reappears here has to be justified, and the set can only shrink.
KNOWN_CROSS_ENGINE_EDGES: set[str] = set()

#: For an edge that cannot be removed yet, the exact SYMBOLS it is permitted to carry.
# EMPTY as of. The one entry it held - multiregion → `snappy.snappy_runner` for
#: `_write_case_skeleton` and `parse_layer_coverage` - is gone: both are snappyHexMesh mechanics
#: rather than snappy policy, and they now live in `engines/snappy_hexmesh.py`, which both engines
#: import as peers. The mechanism stays because it is how a surviving edge is held to exactly the
#: symbols it needs, and because it fails when a listed symbol is no longer used - so a pin cannot
#: outlive its reason.
ALLOWED_CROSS_ENGINE_SYMBOLS: dict[tuple[str, str], set[str]] = {}


def test_a_pinned_cross_engine_edge_carries_only_its_allowed_symbols():
    import ast as _ast

    for (rel, module), allowed in ALLOWED_CROSS_ENGINE_SYMBOLS.items():
        path = SRC / rel
        assert path.exists(), f"{rel} no longer exists - update the pin"
        imported: set[str] = set()
        for n in _ast.walk(_ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, _ast.ImportFrom) and (n.module or "") == module:
                imported |= {a.name for a in n.names}
        extra = sorted(imported - allowed)
        assert not extra, (
            f"{rel} imports {extra} from {module}; only {sorted(allowed)} are still permitted "
            "on this edge, and the set may only shrink")
        gone = sorted(allowed - imported)
        assert not gone, (
            f"{rel} no longer needs {gone} from {module} - narrow "
            "ALLOWED_CROSS_ENGINE_SYMBOLS so the pin keeps shrinking")


def test_no_new_engine_bundle_imports_another_engines_internals():
    bundles = sorted(p for p in ENGINES.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    assert len(bundles) >= 5, f"expected the five engine bundles, found {len(bundles)}"
    names = {p.name for p in bundles}
    found = set()
    for bundle in bundles:
        for py in bundle.rglob("*.py"):
            for mod, _ln, _ctx in _imports(py):
                for other in names - {bundle.name}:
                    if mod.startswith(f"meshpipeline.engines.{other}."):
                        found.add(f"{py.relative_to(SRC).as_posix()} -> {mod}")
    new_edges = sorted(found - KNOWN_CROSS_ENGINE_EDGES)
    assert not new_edges, "NEW cross-engine internal imports: " + "; ".join(new_edges)
    gone = sorted(KNOWN_CROSS_ENGINE_EDGES - found)
    assert not gone, (
        "these cross-engine edges are gone - remove them from KNOWN_CROSS_ENGINE_EDGES so the "
        f"pin keeps shrinking: {gone}")


def test_the_moved_contracts_introduced_no_cross_engine_edge():
    for bundle in sorted(p for p in ENGINES.iterdir() if p.is_dir()):
        for py in bundle.rglob("*.py"):
            for mod, ln, _ctx in _imports(py):
                assert not (mod.startswith("meshpipeline.engines.") and mod.endswith(
                    (".gates", ".review_types")) and mod.count(".") > 3), (
                    f"{py.relative_to(SRC)}:{ln} imports {mod} - a per-engine copy of a shared "
                    "contract")


def test_the_gate_and_judgement_contracts_have_one_canonical_definition_each():
    for symbol in ("GateCtx", "GateSpec", "run_gates", "Criterion", "ReviewAxis"):
        homes = []
        for py in sorted(SRC.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            for n in tree.body:
                if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == symbol:
                    homes.append(py.relative_to(SRC).as_posix())
        assert len(homes) == 1, f"{symbol} is defined in {len(homes)} places: {homes}"


def test_the_moved_symbols_are_not_re_exported_from_their_old_home():
    import meshpipeline.engines.base as base

    for moved in ("GateCtx", "GateSpec", "run_gates", "Criterion", "ReviewAxis"):
        assert not hasattr(base, moved), (
            f"engines.base still exposes {moved} - a compatibility re-export was added")


def test_nothing_imports_the_moved_symbols_from_the_old_path():
    roots = [SRC, SRC.parents[1] / "tests"]
    moved = {"GateCtx", "GateSpec", "run_gates", "Criterion", "ReviewAxis"}
    offenders = []
    for root in roots:
        for py in scanned(sorted(root.rglob("*.py")), "the scanned source root"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("engines.base"):
                    stale = sorted({a.name for a in n.names} & moved)
                    if stale:
                        offenders.append(f"{py.name}:{n.lineno} {stale}")
    assert not offenders, "stale imports from engines.base: " + "; ".join(offenders)


#: The shared OpenFOAM table is narrow BY CONSTRUCTION: exactly the facts a solver cares about
#: without knowing which mesher ran. A broad OpenFOAM layer is deliberately refused.
OPENFOAM_CRITERIA_EXPORTS = {"FATAL_TOPOLOGY", "MAX_NON_ORTHO", "OF_MESH_VALIDITY",
                             "OF_SNAPPY_GUIDE"}


def test_the_shared_openfoam_table_stays_narrow():
    import meshpipeline.engines.openfoam_criteria as oc

    public = {n for n in dir(oc) if not n.startswith("_") and n not in ("annotations", "Criterion")}
    assert public == OPENFOAM_CRITERIA_EXPORTS, (
        f"engines/openfoam_criteria.py now declares {sorted(public)}; it is a narrow table of "
        "engine-neutral OpenFOAM facts, not an OpenFOAM layer")
    assert oc.FATAL_TOPOLOGY.gating is True, "fatal topology stopped gating"
    assert oc.MAX_NON_ORTHO.gating is False, "the non-orthogonality advisory became gating"


def test_the_shared_openfoam_table_imports_no_engine():
    mods = {m for m, _l, _c in _imports(ENGINES / "openfoam_criteria.py")}
    offenders = sorted(m for m in mods
                       if m.startswith("meshpipeline.engines.")
                       and m != "meshpipeline.engines.review_types")
    assert not offenders, f"openfoam_criteria imports {offenders} - it is no longer a leaf"


def test_every_openfoam_engine_judges_fatal_topology_from_the_shared_table():
    import meshpipeline.engines.openfoam_criteria as oc

    seen = 0
    for bundle in ("cfmesh", "snappy", "snappy_multiregion"):
        mod = __import__(f"meshpipeline.engines.{bundle}.criteria", fromlist=["CRITERIA_ROWS"])
        rows = {c.key: c for c in mod.CRITERIA_ROWS}
        assert "fatal" in rows, f"{bundle} no longer declares the fatal-topology criterion"
        assert rows["fatal"] is oc.FATAL_TOPOLOGY, (
            f"{bundle} carries its own copy of the fatal-topology criterion")
        seen += 1
    assert seen == 3, f"expected the three OpenFOAM bundles, checked {seen}"
