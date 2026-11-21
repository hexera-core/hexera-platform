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
from pathlib import Path

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

#: Modules whose whole job is to run such a call INSIDE A BOUNDED CHILD. A call here is the point.
#: `bounded_child.py` is #515's fresh-interpreter dispatcher, which replaces `localize._bounded`'s
#: fork for thickness, common volume, render, the heal analysis and the kernel face pairs. It is
#: listed BEFORE it lands so that whichever of #515 and this guard merges second has nothing to
#: reconcile; until the file exists the entry simply matches nothing, and
#: `test_every_allow_listed_module_is_really_a_bounded_child` fails if it ever becomes pointless.
BOUNDED_CHILDREN = {"occ_distance.py", "bounded_child.py"}

#: Call sites that exist today and are NOT yet bounded, each named with why it is tolerated and
#: what would fix it. THIS LIST MAY ONLY SHRINK. Keyed by (module, enclosing function, symbol) so
#: it survives reformatting and line moves, and so a SECOND call in the same function is caught.
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


def _sites() -> set[tuple[str, str, str]]:
    """Every call of an uninterruptible kernel entry point in the repair package."""
    found: set[tuple[str, str, str]] = set()
    for path in sorted(REPAIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        where = _enclosing(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name in UNINTERRUPTIBLE:
                found.add((path.name, where.get(node, "<module>"), name))
    return found


def test_no_new_uninterruptible_kernel_call_is_added():
    """THE GUARD. A new unbounded call fails here, with the name of the function that added it."""
    sites = {s for s in _sites() if s[0] not in BOUNDED_CHILDREN}

    added = sorted(sites - KNOWN_UNBOUNDED)

    assert not added, (
        "new uninterruptible OCC call(s) outside a bounded child:\n  "
        + "\n  ".join(f"{m}:{fn} -> {sym}" for m, fn, sym in added)
        + "\n\nRun it in a bounded child (see cad/repair/occ_distance.py: shapes to BREP, a fresh "
          "interpreter in its own session, group-killed at the budget) or, if it genuinely cannot "
          "be bounded yet, add it to KNOWN_UNBOUNDED with the reason and a follow-up.")


def test_the_known_list_does_not_name_calls_that_are_already_gone():
    """THE LIST MAY ONLY SHRINK. An entry for a call that no longer exists is how a guard rots
    into decoration: the next reader cannot tell which entries are real."""
    stale = sorted(KNOWN_UNBOUNDED - _sites())

    assert not stale, (
        "KNOWN_UNBOUNDED names call sites that no longer exist - delete them:\n  "
        + "\n  ".join(f"{m}:{fn} -> {sym}" for m, fn, sym in stale))


def test_every_allow_listed_module_is_really_a_bounded_child():
    """The allow-list is not a blanket. A module is on it BECAUSE it runs the kernel call inside a
    bounded child, so an entry for a module that exists and contains no such call is meaningless
    and should go - the same rot the shrink-only test guards against, from the other side.

    A module that does NOT YET EXIST is allowed to be listed: that is how this guard and #515 can
    land in either order without a merge conflict.
    """
    for module in sorted(BOUNDED_CHILDREN):
        if not (REPAIR / module).exists():
            continue
        inside = {s for s in _sites() if s[0] == module}
        assert inside, (
            f"{module} is allow-listed as a bounded child but contains no uninterruptible kernel "
            "call - either it is the wrong module or the entry is stale")
