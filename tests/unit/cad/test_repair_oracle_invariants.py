# Responsibility: THE SAFETY ARTEFACT OF CRS v3. Keep big/XL mesher escalation UNREACHABLE from
#                 the repair loop, by pinning what every module this plan creates under
#                 `cad/repair/` is allowed to import and what of `engines/fea_tetwild` it may
#                 touch.
# Boundaries: a static AST read of the source tree plus one patched child-body run. Nothing here
#             meshes, starts a process or reads the environment. It imports no mesher.
#
# WHY AN IMPORT BLOCKLIST IS THE RIGHT SHAPE. The hazard is not that repair decides to escalate -
# it is that a module reaches for a helper which, three calls down, sizes a machine and asks for
# one. Reachability is what has to be impossible, and an import is where reachability begins. So
# this test bans the DOORWAYS rather than auditing the callers.
#
# AN AST READ, NOT A SUBSTRING SEARCH. A docstring that legitimately names `mesh_machine` (this
# file's own comment does) would trip a grep; and a deferred `import` inside a function - which is
# how `cad/repair/` keeps heavy modules out of the parent - is invisible to a module-header scan.
# `ast.walk` sees both, and only real imports.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[3] / "src" / "meshpipeline"
REPAIR = SRC / "cad" / "repair"

#: EVERY MODULE CRS v3 CREATES UNDER `cad/repair/`. Each later task that creates one APPENDS it
#: here; `test_every_v3_module_is_listed` is what makes forgetting fail loudly rather than
#: silently widen the hole. Still to come: `ladder.py` and `investigator_tools.py` (the
#: concurrent ladder lane) - append them when that lane merges.
V3_MODULES = [
    "cad/repair/oracle.py",
    "cad/repair/goal.py",
    "cad/repair/grind.py",
    "cad/repair/tolerant_probe.py",
    "cad/repair/ladder.py",
    "cad/repair/occ_distance.py",
    "cad/repair/investigator_tools.py",
    "cad/repair/investigation_trace.py",
    "cad/repair/investigator.py",
    "cad/repair/tournament.py",
    "cad/repair/mesh_clash.py",
    "cad/repair/occ_distance.py",
    "cad/repair/investigator_budget.py",
    "cad/repair/review_tier.py",
    "cad/repair/memory.py",
    "cad/repair/replay.py",
    "cad/repair/ending.py",
    "cad/repair/gap_split.py",
    "cad/repair/bounded_child.py",
]

#: The doorways to the big machines. A v3 module that imports any of these - at module level or
#: deferred inside a function - could size and request a big/XL VM from inside a repair run.
BANNED = (
    "meshpipeline.contracts.mesh_machine",
    "meshpipeline.contracts.mesh_execution",
    "meshpipeline.adapters",
    "meshpipeline.pipeline",
    "meshpipeline.engines.gmsh",
    "meshpipeline.engines.fea_driver",
    "meshpipeline.engines.fea_ladder",
    "meshpipeline.engines.mesher_stage",
)

#: The ONLY names of `engines/fea_tetwild` a v3 module may touch: the binary's location, one
#: part's run, the CAD tessellation, and the three shares that size them. Everything else in that
#: module is the production rung - `rung_machine`, `run_parts` and `mesh_into` above all, which
#: choose a machine, schedule many parts and write into a gmsh model. `envelope_m` and `MAX_ITS`
#: are read by tolerant_probe's DELIVERY mode (an evaluation reading at the production rung's
#: envelope and iteration count) - two pure values, still no machine, schedule or gmsh model.
FTETWILD_ALLOWED = {"binary", "run_part", "tessellate", "EPS_DIAG_SHARE", "TESSELLATION_SHARE",
                    "SOFT_SHARE", "envelope_m", "MAX_ITS"}

#: `cad/repair/` as it stood BEFORE CRS v3. A file that is neither one of these nor in
#: `V3_MODULES` is a v3 module somebody forgot to list.
PRE_V3_MODULES = frozenset({
    "__init__.py", "brep.py", "caps.py", "catalogue_words.py", "conservative.py", "contracts.py",
    "deviation.py", "directives.py", "disposition.py", "identity.py", "inspect.py",
    "localize.py", "loop.py", "mesh_probe.py", "model_author.py", "observe.py", "operations.py",
    "package.py", "package_review.py", "plan.py", "region_loop.py", "regions.py",
    "rules_author.py", "session.py", "spotlight.py", "surface.py", "triage.py",
})


def _tree(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"), filename=rel)


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every module name this file imports, DEFERRED IMPORTS INCLUDED.

    `from a.b import c` is recorded both as `a.b` and as `a.b.c`, because `c` may itself be a
    module - which is exactly how `from meshpipeline.engines import gmsh` would read.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("rel", V3_MODULES)
def test_no_v3_module_reaches_the_big_machines(rel):
    for name in _imported_modules(_tree(rel)):
        for banned in BANNED:
            assert name != banned and not name.startswith(banned + "."), (rel, name, banned)


@pytest.mark.parametrize("rel", V3_MODULES)
def test_only_the_allowed_ftetwild_names_are_touched(rel):
    """Both ways a name can be reached: `fea_tetwild.<attr>` and `from ...fea_tetwild import x`."""
    tree = _tree(rel)
    touched: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "fea_tetwild"):
            touched.add(node.attr)
        if (isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("engines.fea_tetwild")):
            touched.update(alias.name for alias in node.names)
    assert touched <= FTETWILD_ALLOWED, (rel, sorted(touched - FTETWILD_ALLOWED))


def test_every_v3_module_is_listed():
    """A `cad/repair/` module that is neither pre-v3 nor listed is unguarded. Append it."""
    listed = {Path(rel).name for rel in V3_MODULES}
    present = {p.name for p in REPAIR.glob("*.py")}
    unguarded = present - PRE_V3_MODULES - listed
    assert unguarded == set(), sorted(unguarded)
    # ... and nothing listed has vanished, which would make the guard vacuous.
    assert listed <= present, sorted(listed - present)


def test_no_v3_module_imports_agents_or_pipeline():
    """`cad/` never imports `agents/` or `pipeline/` (the layer rule), restated on the v3 set so a
    v3 module's deferred import is covered too."""
    for rel in V3_MODULES:
        for name in _imported_modules(_tree(rel)):
            assert not name.startswith("meshpipeline.agents"), (rel, name)
            assert not name.startswith("meshpipeline.pipeline"), (rel, name)


def test_no_v3_module_imports_a_mesher_at_module_level():
    """gmsh and fTetWild NEVER in the parent process: neither may be a module-level import.

    `fea_tetwild` is allowed, but only lazily - its module level pulls numpy and the rung's
    machinery, and the parent asks it for `binary()` alone.
    """
    for rel in V3_MODULES:
        tree = _tree(rel)
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            for name in names:
                assert name != "gmsh" and not name.startswith("gmsh."), (rel, name)
                assert "fea_tetwild" not in name, (rel, name)


def test_no_check_maps_to_the_product_mesher():
    """TIER 3 HAS NO IN-LOOP BUDGET. The loop cannot ask for the product mesher at all, and no
    check may be re-pointed at it without this failing."""
    from meshpipeline.cad.repair import oracle

    assert oracle.TIER_3.in_loop is False
    assert oracle.TIER_3.processes == 0
    for check in oracle.CHECKS:
        assert oracle.tier_for(check) is not oracle.TIER_3, check
        assert oracle.tier_for(check).in_loop is True, check


def test_tolerant_probe_never_calls_the_machine_chooser(monkeypatch):
    """The child body runs a tolerant pass with every machine-sizing door nailed shut.

    `mesh_machine.choose`, `fea_tetwild.rung_machine`, `run_parts` and `mesh_into` all raise, so a
    tier-2b pass that reached for a machine, a multi-part schedule or a gmsh model would fail here
    rather than on a worker with a VM quota.
    """
    from meshpipeline.cad.repair import tolerant_probe as tp
    from meshpipeline.contracts import mesh_machine
    from meshpipeline.engines import fea_tetwild

    def nailed(name):
        def shut(*args, **kwargs):
            raise AssertionError(f"tier 2b must never reach {name}")
        return shut

    monkeypatch.setattr(mesh_machine, "choose", nailed("mesh_machine.choose"))
    for name in ("rung_machine", "run_parts", "mesh_into"):
        monkeypatch.setattr(fea_tetwild, name, nailed(f"fea_tetwild.{name}"))

    # A 10 mm box's worth of surface, standing in for the tessellation, and a run that answers.
    monkeypatch.setattr(tp, "_read_faces", lambda path: ("SHAPE", "FMAP", 6))
    monkeypatch.setattr(fea_tetwild, "tessellate",
                        lambda fmap, faces, deflection: ("XYZ", "TRIS", "LABELS"))
    monkeypatch.setattr(fea_tetwild, "run_part",
                        lambda job: {"tets": list(range(1234)), "seconds": 1.0,
                                     "stopped": False})

    answer = tp.child_answer("/tmp/box.brep", 17.3, 30.0)
    assert answer["tets"] == 1234
    assert answer["error"] == ""
