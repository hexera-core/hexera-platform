# Responsibility: Fail CI when an UNINTERRUPTIBLE OCC call is added to the repair package outside a
#                 bounded child, so the next one is a red test rather than a hung fleet.
# Boundaries: Static. It reads the AST; it runs no kernel and proves nothing about runtime bounds.
"""Three hangs in one night, each found only by a hung VM:

  - the clash gap measurement (fixed in #503),
  - the investigator's distance measurement (#513),
  - lane H's gap_split (fixed via `occ_distance`).

Every one was the same shape: an OCC call that cannot be interrupted, running in the parent, so no
clock written around it could stop it. A deadline checked BETWEEN calls does not bound the call.

This test is the standing guard. It names every call site that exists today; a NEW one fails here
instead of being discovered when a shard stops answering. Bounding a known site means DELETING its
entry, so the list only ever shrinks.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

REPAIR = Path(__file__).resolve().parents[3] / "src" / "meshpipeline" / "cad" / "repair"

#: Kernel entry points that take an unbounded amount of time and cannot be interrupted from
#: Python: no signal is delivered, no timeout applies, and the GIL is held for the duration.
UNINTERRUPTIBLE = (
    "BRepExtrema_DistShapeShape",
    "BRepAlgoAPI_Common", "BRepAlgoAPI_Fuse", "BRepAlgoAPI_Cut", "BRepAlgoAPI_Section",
    "BRepAlgoAPI_Splitter",
    "BOPAlgo_Builder", "BOPAlgo_MakerVolume", "BOPAlgo_Splitter",
    "BRepCheck_Analyzer",
    "GeomAPI_ExtremaCurveCurve", "GeomAPI_ExtremaCurveSurface", "GeomAPI_ExtremaSurfaceSurface",
    "GeomAPI_ProjectPointOnSurf", "GeomAPI_ProjectPointOnCurve",
)

#: Whole families guarded by prefix, so a member nobody listed (`BOPAlgo_ArgumentAnalyzer`, whose
#: `Perform` holds the GIL - see `localize._bounded`) is not missed the way a fixed list missed it.
UNINTERRUPTIBLE_FAMILIES = ("BOPAlgo_", "BRepAlgoAPI_")

#: How a module says "this function IS a child interpreter": the `if __name__ == "__main__"` block
#: that `python -m meshpipeline.cad.repair.<module>` runs. Nothing is declared to this guard - the
#: entry points are READ out of that block, so a lane that writes a new child is covered the day
#: it writes it, and a branch without that child has nothing to reconcile.
MAIN_GUARD = "__main__"


def _entry_points(tree: ast.AST, where: dict[ast.AST, str]) -> set[str]:
    """The module-local functions called from the `__main__` block. Usually one `_child_main`;
    `mesh_probe` has two, since the clash-gap child shares the module with the gmsh probe."""
    local = {node.name for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    roots: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and where.get(node, "<module>") == "<module>"):
            continue
        if MAIN_GUARD not in ast.dump(node.test):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            if name in local:
                roots.add(name)
    return roots


#: #515's dispatcher holds no kernel call itself: `bounded_child.KERNELS` REGISTERS functions that
#: live in other modules and run only inside its child. That registry is read from the source too,
#: so a lane that registers a new kernel needs no edit here - and on a branch without the
#: dispatcher nothing is child-side by that route, which is why the in-parent entries in
#: KNOWN_UNBOUNDED stay true there.
DISPATCHER = "bounded_child.py"
REGISTRY = "KERNELS"


def _registered_kernels() -> dict[str, tuple[str, ...]]:
    """`{module.py: (function, ...)}` read out of the dispatcher's registry literal."""
    path = REPAIR / DISPATCHER
    if not path.exists():
        return {}
    found: dict[str, set[str]] = {}
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        targets = (getattr(node, "targets", None) or
                   ([node.target] if isinstance(node, ast.AnnAssign) else []))
        if not any(isinstance(t, ast.Name) and t.id == REGISTRY for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for value in node.value.values:
            if not isinstance(value, ast.Tuple) or len(value.elts) != 2:
                continue
            module, function = value.elts
            if not isinstance(function, ast.Constant):
                continue
            name = (str(module.value).rsplit(".", 1)[-1] + ".py"
                    if isinstance(module, ast.Constant) else DISPATCHER)
            found.setdefault(name, set()).add(str(function.value))
    return {module: tuple(sorted(names)) for module, names in found.items()}


def _calls_in(tree: ast.AST, where: dict[ast.AST, str]) -> dict[str, set[str]]:
    """Which module-local names each function calls. The call graph, one module wide."""
    local = {node.name for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name in local:
            out.setdefault(where.get(node, "<module>"), set()).add(name)
    return out


def _reachable(roots: set[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(graph.get(name, set()) - seen)
    return seen


def _child_side(module: str, tree: ast.AST, where: dict[ast.AST, str]) -> set[str]:
    """Every function in `module` that runs inside a bounded child: its `__main__` entry points,
    the kernels the dispatcher registers from it, and transitively whatever they call here.

    REACHABILITY, NOT A NAME LIST. `gap_split._child_main` calls `_work`, which calls `partners`,
    which is where the distance call actually is. A guard that allowed only the named entry point
    would flag all three as new in-parent calls - which is exactly what wave 8 saw.
    """
    roots = _entry_points(tree, where) | set(_registered_kernels().get(module, ()))
    return _reachable(roots, _calls_in(tree, where))


#: Call sites that exist today and are NOT yet bounded, each named with why it is tolerated and
#: what would fix it. THIS LIST MAY ONLY SHRINK. Keyed by (module, enclosing function, symbol) so
#: it survives reformatting and line moves. Each entry tolerates exactly ONE call, so a SECOND
#: call in the same function is caught (`_over_allowance`).
KNOWN_UNBOUNDED = {
    # --- rows 1-3 of the audit: a deadline checked only BETWEEN calls -------------------------
    # Up to eight solid pairs.
    ("mesh_probe.py", "_contact_support_faces", "BRepExtrema_DistShapeShape"),
    # One call per deviation sample, inside every tournament rehearsal (the K-fold cost).
    ("deviation.py", "distance", "BRepExtrema_DistShapeShape"),
    # The clash gap. Reported as dead after #503; it is still CALLED on main at mesh_probe.py:752,
    # which is why it is listed rather than deleted.
    ("mesh_probe.py", "_clash_gap", "BRepExtrema_DistShapeShape"),
    # --- rows 4-6: inside fragment_bodies.apply ------------------------------------------------
    # On expiry the OPERATION must DECLINE, which is a different contract from reporting an
    # unmeasured value, so these are their own change.
    ("operations.py", "_meeting_partner", "BRepExtrema_DistShapeShape"),
    ("operations.py", "_common_volume", "BRepAlgoAPI_Common"),
    ("operations.py", "_general_fuse", "BOPAlgo_Builder"),
    # --- found by THIS GUARD, not by the manual audit -----------------------------------------
    # The investigator's thickness tool. Bounded today only by `localize._bounded`'s FORK, which
    # is the biggest unowned process-safety item on the branch: a forked child inherits the
    # parent's OCC state and its locks, so it can hang the same way.
    ("investigator_tools.py", "_thickness_kernel", "BRepExtrema_DistShapeShape"),
    # A whole call family nobody had audited: GeomAPI projection, reached from the spotlight.
    ("spotlight.py", "_normal_of_face", "GeomAPI_ProjectPointOnSurf"),
    # --- BRepCheck_Analyzer: the validity check, uninterruptible and everywhere ----------------
    # Its own follow-up. Every repair path runs it, so bounding it is a larger change than any
    # single distance call.
    ("localize.py", "_status_defects", "BRepCheck_Analyzer"),
    ("localize.py", "localize_scan", "BRepCheck_Analyzer"),
    ("operations.py", "_is_valid", "BRepCheck_Analyzer"),
    ("brep.py", "_is_valid", "BRepCheck_Analyzer"),
}


#: Registered kernels that are STILL called straight from the parent at a named call site, with
#: why. Registering a kernel says it MAY run in a child; these call sites do not, so on these
#: paths the kernel is unbounded. SHRINK-ONLY, like KNOWN_UNBOUNDED.
KNOWN_DIRECT_CALLS = {
    # `fragment_bodies` - row 5 of the audit. #515 registered `_common_volume` so it CAN run in a
    # child, and `investigator_tools` takes that route, but `apply` still calls it here. Bounding
    # it is the row 4-6 change: on expiry the OPERATION must DECLINE, which is a different
    # contract from reporting an unmeasured value, so it is its own PR rather than a swap.
    ("operations.py", "apply", "_common_volume"),
}


def _enclosing(tree: ast.AST) -> dict[ast.AST, str]:
    """Each node's nearest enclosing function name, or "<module>"."""
    where: dict[ast.AST, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                     else name)
            where[child] = inner
            walk(child, inner)

    where[tree] = "<module>"
    walk(tree, "<module>")
    return where


def _is_kernel(name: str) -> bool:
    return name in UNINTERRUPTIBLE or name.startswith(UNINTERRUPTIBLE_FAMILIES)


def _sites_in(module: str, tree: ast.AST, *, in_parent_only: bool = False) -> Counter:
    """How many times each function in one module calls each uninterruptible entry point."""
    where = _enclosing(tree)
    bounded = _child_side(module, tree, where) if in_parent_only else set()
    found: Counter = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        enclosing = where.get(node, "<module>")
        if _is_kernel(name) and enclosing not in bounded:
            found[(module, enclosing, name)] += 1
    return found


def _sites(*, in_parent_only: bool = False) -> Counter:
    """Every call of an uninterruptible kernel entry point in the repair package, COUNTED.

    With `in_parent_only`, the ones that run child-side are left out. The unfiltered set is what
    the shrink-only test compares against: a call that MOVED into a child still exists, and its
    KNOWN_UNBOUNDED entry stays true on a branch where the dispatcher has not landed.
    """
    found: Counter = Counter()
    for path in sorted(REPAIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found.update(_sites_in(path.name, tree, in_parent_only=in_parent_only))
    return found


def _over_allowance(sites: Counter) -> list[tuple[str, str, str]]:
    """Each KNOWN_UNBOUNDED entry allows ONE call; anything else allows none."""
    return sorted(key for key, count in sites.items()
                  if count > (1 if key in KNOWN_UNBOUNDED else 0))


def test_no_new_uninterruptible_kernel_call_is_added():
    """THE GUARD. A new unbounded call fails here, with the name of the function that added it."""
    added = _over_allowance(_sites(in_parent_only=True))

    assert not added, (
        "new uninterruptible OCC call(s) outside a bounded child:\n  "
        + "\n  ".join(f"{m}:{fn} -> {sym}" for m, fn, sym in added)
        + "\n\nRun it in a bounded child (see cad/repair/occ_distance.py: shapes to BREP, a fresh "
          "interpreter in its own session, group-killed at the budget) or, if it genuinely cannot "
          "be bounded yet, add it to KNOWN_UNBOUNDED with the reason and a follow-up.")


def test_the_known_list_does_not_name_calls_that_are_already_gone():
    """THE LIST MAY ONLY SHRINK. An entry for a call that no longer exists is how a guard rots
    into decoration: the next reader cannot tell which entries are real."""
    stale = sorted(KNOWN_UNBOUNDED - set(_sites()))

    assert not stale, (
        "KNOWN_UNBOUNDED names call sites that no longer exist - delete them:\n  "
        + "\n  ".join(f"{m}:{fn} -> {sym}" for m, fn, sym in stale))


def test_no_function_runs_both_in_a_child_and_in_the_parent():
    """THE "ONLY" IN "ONLY REACHABLE FROM A CHILD ENTRY". A helper that a child entry reaches AND
    a parent-side function calls is judged child-side, so its kernel call stops being guarded -
    silently, and in the one case where it can still hang the worker. That ambiguity is a finding
    in itself, so it fails here rather than quietly widening the exemption.
    """
    both: list[str] = []
    # Only the `__main__`-entry route is judged here. A REGISTERED kernel called in the parent is
    # the same fact from the other side, and `test_a_registered_kernel_is_never_called_straight_
    # from_the_parent` names it with the call site - one fact, one failure.
    for path in sorted(REPAIR.glob("*.py")):
        both.extend(_ambiguous(path.name, ast.parse(path.read_text(), filename=str(path))))

    assert not both, "ambiguous dispatch - these are exempted as child-side but run in the parent "\
                     "too:\n  " + "\n  ".join(sorted(both))


def _ambiguous(module: str, tree: ast.AST) -> list[str]:
    """Child-side functions that a parent-side function also calls, where that path can REACH a
    kernel call - directly or through whatever it calls in turn."""
    where = _enclosing(tree)
    graph = _calls_in(tree, where)
    bounded = _reachable(_entry_points(tree, where), graph)
    out: list[str] = []
    for caller, callees in graph.items():
        if caller in bounded or caller == "<module>":
            continue
        for name in sorted(callees & bounded):
            if any(_holds_a_kernel_call(tree, where, fn) for fn in _reachable({name}, graph)):
                out.append(f"{module}:{name} is reached from the child entry AND called by {caller}")
    return out


def _holds_a_kernel_call(tree: ast.AST, where: dict[ast.AST, str], function: str) -> bool:
    """Whether `function` itself calls an uninterruptible entry point. Only those matter here: an
    exemption that covers no kernel call hides nothing."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or where.get(node, "<module>") != function:
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if _is_kernel(name):
            return True
    return False


def test_a_registered_kernel_is_never_called_straight_from_the_parent():
    """REGISTERING A KERNEL IS NOT BOUNDING IT. The dispatcher's registry says a function MAY be
    run in a child; it is the CALL SITE that decides. A direct call in the parent is the
    unbounded call the registry makes it look as though we fixed, and it can be in a different
    module from the one that owns the function, so a per-module reading misses it.

    The dispatcher's own way of invoking one passes it as an argument - `_bounded(fn, ...)` - so
    only a real call is flagged.
    """
    registered = {name for names in _registered_kernels().values() for name in names}
    if not registered:
        pytest.skip(f"{DISPATCHER} is not on this branch, so nothing is registered")

    direct: list[str] = []
    for path in sorted(REPAIR.glob("*.py")):
        if path.name == DISPATCHER:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        where = _enclosing(tree)
        bounded = _child_side(path.name, tree, where)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            enclosing = where.get(node, "<module>")
            if (name in registered and enclosing not in bounded
                    and (path.name, enclosing, name) not in KNOWN_DIRECT_CALLS):
                direct.append(f"{path.name}:{enclosing} calls {name}() directly")

    assert not direct, (
        "a kernel registered with the bounded dispatcher is called straight from the parent, so "
        "on this path it is NOT bounded:\n  " + "\n  ".join(sorted(set(direct)))
        + f"\n\nRoute it through {DISPATCHER} at these call sites, or take it off the registry "
          "so the guard judges it as the in-parent call it is.")


def test_what_a_child_entry_reaches_is_child_side_too():
    """THE INTEGRATION'S LESSON, as a unit. The kernel call is rarely in the entry point itself:
    lane H's child main calls `_work`, which calls `partners`, which holds the distance. A guard
    that allowed only the named entry would flag all three as new in-parent calls, which is
    exactly what happened on wave 8."""
    source = (
        "def _child_main(argv):\n"
        "    return _work(argv)\n"
        "def _work(x):\n"
        "    return partners(x)\n"
        "def partners(x):\n"
        "    return BRepExtrema_DistShapeShape(x, x)\n"
        "def in_the_parent(x):\n"
        "    return BRepExtrema_DistShapeShape(x, x)\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(_child_main(sys.argv[1:]))\n")
    tree = ast.parse(source)

    where = _enclosing(tree)
    reached = _reachable(_entry_points(tree, where), _calls_in(tree, where))

    assert {"_child_main", "_work", "partners"} <= reached
    assert "in_the_parent" not in reached, (
        "only what an entry point REACHES is child-side; everything else is judged in-parent")


def test_a_kernel_registered_by_the_dispatcher_is_child_side_where_it_lives():
    """#515 holds no kernel call of its own: `bounded_child.KERNELS` registers functions in OTHER
    modules - `investigator_tools._thickness_kernel` among them - and runs them in its child. The
    registry is READ, so registering a new kernel needs no edit here."""
    registered = _registered_kernels()
    if not (REPAIR / DISPATCHER).exists():
        pytest.skip(f"{DISPATCHER} is not on this branch, so nothing is child-side by that route")

    assert registered, f"{DISPATCHER} exists but its {REGISTRY} registry was not read"
    assert ("investigator_tools.py", "_thickness_kernel") in {
        (module, name) for module, names in registered.items() for name in names}, (
        "the thickness kernel is the audit's own example of a call bounded only by a fork; if it "
        "is no longer registered, check what bounds it now")


_CHAIN = (
    "def _child_main(argv):\n"
    "    return _work(argv)\n"
    "def _work(x):\n"
    "    return partners(x)\n"
    "def partners(x):\n"
    "    return BRepExtrema_DistShapeShape(x, x)\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(_child_main(sys.argv[1:]))\n")


def test_a_second_call_in_a_known_function_is_caught():
    """A KNOWN_UNBOUNDED entry tolerates the one call it names, not every call that function makes:
    a second kernel call added next to a listed one is a new unbounded call."""
    source = ("def _clash_gap(a, b):\n"
              "    BRepExtrema_DistShapeShape(a, b)\n"
              "    return BRepExtrema_DistShapeShape(b, a)\n")
    sites = _sites_in("mesh_probe.py", ast.parse(source))

    assert _over_allowance(sites) == [("mesh_probe.py", "_clash_gap", "BRepExtrema_DistShapeShape")]


@pytest.mark.parametrize("symbol", ["BOPAlgo_ArgumentAnalyzer", "BOPAlgo_PaveFiller",
                                    "BRepAlgoAPI_Defeaturing"])
def test_the_boolean_families_are_guarded_whole(symbol):
    """`BOPAlgo_*` and `BRepAlgoAPI_*` are guarded as families: `BOPAlgo_ArgumentAnalyzer.Perform`
    holds the GIL like the rest (see `localize._bounded`), and a fixed list is how it was missed."""
    sites = _sites_in("new.py", ast.parse(f"def f(x):\n    return {symbol}()\n"))

    assert _over_allowance(sites) == [("new.py", "f", symbol)]


def test_a_parent_wrapper_around_a_child_side_chain_is_ambiguous():
    """The kernel is rarely in the function the parent calls: `in_the_parent -> _work -> partners`
    reaches it just as surely, so `_work` being exempted as child-side hides a parent-side hang."""
    source = _CHAIN + "def in_the_parent(x):\n    return _work(x)\n"

    found = _ambiguous("chain.py", ast.parse(source))

    assert found == ["chain.py:_work is reached from the child entry AND called by in_the_parent"]
