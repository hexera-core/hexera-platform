# Responsibility: Verify that every private helper DEFINED in cfmesh_conjugate/finalize.py is
# actually REACHED by running code, so a comment in another file cannot go on describing one as
# the mechanism after the mechanism has moved.
# Boundaries: source text only. Nothing here meshes, renders, reads a manifest or opens a browser.
#
# THE DEFECT, 2026-11-18. `finalize._floors_had_a_reading` existed, was one line over
# `layers.floors_unjudged`, and had NO CALLER anywhere in src/, ui/, apps/ or tests/ - while two
# comment blocks in `render/mesh_facts` (the `_LINE_METRICS` preamble and the
# `layer_floors_unjudged` entry) named it as the whole of how "this was judged and it met every
# floor" is told from "nobody judged it". The guard that really runs is the
# `len(unjudged) >= len(layers.LAYER_FLOORS)` return inside `_layers_published`, which needs the
# unjudged roster itself and so cannot delegate to a helper that recomputes it.
#
# A DEAD FUNCTION IS NOT A HARMLESS ONE WHEN SOMETHING POINTS AT IT. The next reader follows the
# comment, finds a function whose body looks right, and reasons about a guard that never runs -
# which is how a rule gets "closed" twice and enforced nowhere. PROSE IS NOT A REFERENCE: the
# scan below counts AST name loads, so a mention inside a docstring or a `#` comment does not
# keep a function alive. Against the pre-change tree `_floors_had_a_reading` has four such
# mentions and ZERO loads, and this file fails there on its own assertion.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import finalize as F

#: The module under test, taken from the IMPORT and never from this file's own location, so a run
#: with PYTHONPATH pinned at a snapshot measures the snapshot. The path is printed on failure.
FINALIZE = Path(F.__file__).resolve()
#: The package the helper could be reached from, and the tests tree that may reach in by
#: `F._name`. Both are derived rather than hard-coded, for the same reason.
PACKAGE = FINALIZE.parents[2]                      # .../meshpipeline
TESTS = Path(__file__).resolve().parents[2]        # .../tests


def _private_defs(path: Path) -> list[str]:
    """The module-level `def _name` in `path`. Nested defs are not module-level helpers."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("_")]


def _name_loads(roots: list[Path], wanted: set[str]) -> dict[str, int]:
    """How many times each name in `wanted` is LOADED as a name or an attribute under `roots`.

    A definition is not a load: `ast.FunctionDef.name` is a plain string on the node and never
    appears as an `ast.Name`, so a function that is only ever defined scores 0 here. Neither is a
    docstring or a comment, which is the whole point.
    """
    counts = dict.fromkeys(wanted, 0)
    for root in roots:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            if not any(w in text for w in wanted):      # cheap pre-filter, exactness below
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:                          # not ours to judge
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in counts:
                    counts[node.id] += 1
                elif isinstance(node, ast.Attribute) and node.attr in counts:
                    counts[node.attr] += 1
    return counts


def test_every_private_helper_in_finalize_is_reached_by_running_code():
    """THE RULE, and it is about reachability and not about style.

    MEASURED 2026-11-18 on the post-change tree, scanning the `meshpipeline` package and the
    whole tests tree: nine private module-level helpers, loads 2, 1, 1, 8, 10, 1, 24, 28, 43 -
    every one of them at least once. `_carry_mesh_wide`, `_pieces_short` and `_resolution_short`
    sit at exactly one, which is their single call site inside this module, and that is a caller.
    """
    names = _private_defs(FINALIZE)
    assert names, f"no private helpers were found at all in {FINALIZE}, so nothing is being tested"
    loads = _name_loads([PACKAGE, TESTS], set(names))
    dead = sorted(n for n in names if loads[n] == 0)
    assert not dead, (
        f"{FINALIZE} defines {dead} and nothing anywhere loads the name - a helper with no "
        f"caller that another file describes as the mechanism is a comment pointing at code that "
        f"never runs. Either wire it or remove it. Scanned {PACKAGE} and {TESTS}.")


def test_the_guard_that_tells_an_unjudged_pass_from_a_clean_one_is_in_the_function_that_publishes():
    """WHERE THE MECHANISM IS, pinned as behaviour so the comments have something to be checked
    against. SYNTHETIC. An unreadable layer log publishes the two tally keys and NOTHING else -
    no `layer_short`, whose `[]` a reader is entitled to read as a clean pass, and no
    `layer_iterations`, which is 0 on every unreadable log.
    """
    from meshpipeline.engines.cfmesh_conjugate import layers as LY
    from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID

    raw = LY.read_layer_log("")
    assert raw.get("iterations") == 0, "the premise: an unreadable log still yields an iteration count"
    got = F._layers_published(raw)
    assert set(got) == {"layer_floors_unjudged", "layer_floors_total"}, got
    assert got["layer_floors_total"] == len(LY.LAYER_FLOORS)
    assert len(got["layer_floors_unjudged"]) == len(LY.LAYER_FLOORS), (
        "an unreadable log had no figure for any floor, so every floor is named")
    assert LY.measured(raw, interface_patch=FLUID_TO_SOLID).get("layer_coverage_pct") is None
