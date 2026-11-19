"""grind: a failure kind and a part's size -> the mesh cost a mesher would pay. Pure, no gmsh."""

from __future__ import annotations

import ast
import collections
import importlib.util
import json
from pathlib import Path

import pytest

from meshpipeline.cad.repair import grind
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.grind import CHEAP, HEAVY, STANDARD, UNKNOWN
from meshpipeline.cad.repair.localize import EntityDefect

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src" / "meshpipeline"
_AGGREGATE = _ROOT / "devtools/validation/evidence/2026-11-19-corpus-harvest/aggregate.json"
_MANIFEST = _ROOT / "devtools/validation/corpora/mesh-failure-real.json"
_TIERS = {CHEAP, STANDARD, HEAVY, UNKNOWN}

_SPEC = importlib.util.spec_from_file_location(
    "corpus_harvest", _ROOT / "devtools/validation/corpus_harvest.py")
harvest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harvest)

#: The modules allowed to call the predictor. Task 2's goal verdict records it; Task 8 adds the
#: first reader (investigator_budget.py) and extends this set deliberately.
CONSUMERS = {"cad/repair/goal.py", "cad/repair/investigator_budget.py",
             "cad/repair/investigator.py"}

#: The stage a legacy manifest row's family was refused in (the harvest's figures: 99% of each).
_LEGACY_STAGE = {"overlapping_facets": "3d", "edge_through_face": "3d", "no_volume": "3d",
                 "open_loop": "2d", "periodic_surface": "2d"}


def manifest_tiers() -> collections.Counter:
    """The predictor's tier distribution over every entry of the real-failure manifest.

    The Phase 1 baseline (Task 6) reports this figure; a row predates family/stage fields, so its
    family comes from its error text and its stage from the family.
    """
    files = json.loads(_MANIFEST.read_text())["files"]
    out: collections.Counter = collections.Counter()
    for row in files:
        family = harvest.legacy_family(row.get("gmsh_error") or "")
        est = grind.predict(family, _LEGACY_STAGE.get(family, ""),
                            solids=row.get("solids"), faces=row.get("faces"))
        out[est.tier] += 1
    return out


@pytest.mark.parametrize("kind, stage, solids, faces, tier", [
    ("timeout", "", None, None, HEAVY), ("crash", "2d", None, None, HEAVY),
    ("overlapping_facets", "3d", 4, 139, STANDARD), ("edge_through_face", "3d", 1, 173, STANDARD),
    ("open_loop", "2d", 1, 138, CHEAP), ("periodic_surface", "2d", 2, 439, CHEAP),
    ("no_volume", "3d", 1, 21, CHEAP), ("", "", 1, 50, CHEAP),
    ("overlapping_facets", "3d", 73, 3096, HEAVY), ("open_loop", "2d", 1, None, UNKNOWN),
])
def test_predict_tiers(kind, stage, solids, faces, tier):
    assert grind.predict(kind, stage, solids=solids, faces=faces).tier == tier


def test_rules_fire_first_to_last():
    assert grind.predict("probe_error", "", solids=None, faces=None).rule == grind.RULE_NO_ANSWER
    assert grind.predict("timeout", "", solids=1, faces=5000).rule == grind.RULE_NO_ANSWER
    assert grind.predict("other", "3d", solids=1, faces=None).rule == grind.RULE_FACES_UNKNOWN
    assert grind.predict("", "", solids=1, faces=grind.HEAVY_FACES + 1).rule == grind.RULE_MANY_FACES
    assert grind.predict("", "", solids=1, faces=grind.HEAVY_FACES).tier == CHEAP
    assert grind.predict("other", "3d", solids=1, faces=150).rule == grind.RULE_VOLUME_REFUSAL
    assert grind.predict("no_volume", "", solids=1, faces=20).rule == grind.RULE_SURFACE_REFUSAL
    assert grind.predict("", "", solids=1, faces=50).rule == grind.RULE_MESHED
    # a surface-stage family refused in 3D, or a volume family refused in 2D, is an ordinary job
    assert grind.predict("open_loop", "3d", solids=1, faces=50) == grind.CostEstimate(
        STANDARD, grind.RULE_DEFAULT)
    assert grind.predict("overlapping_facets", "2d", solids=1, faces=50).rule == grind.RULE_DEFAULT


@pytest.mark.parametrize("kind, stage, solids, faces", [
    ("timeout", "", None, None), ("", "", 1, 50), ("other", "", None, 10),
    ("overlapping_facets", "3d", 4, 2000), ("open_loop", "2d", 1, None), ("weird", "1d", 0, 0),
    (None, None, None, None), ("", "", "many", "lots"), ("", "", True, True), (3, object(), -1, -1),
])
def test_rule_is_named(kind, stage, solids, faces):
    est = grind.predict(kind, stage, solids=solids, faces=faces)  # type: ignore[arg-type]
    assert est.tier in _TIERS
    assert est.rule and est.rule in grind.RULES


def test_a_non_count_face_figure_is_unknown():
    assert grind.predict("", "", solids=1, faces="lots").tier == UNKNOWN  # type: ignore[arg-type]
    assert grind.predict("", "", solids=1, faces=True).tier == UNKNOWN  # type: ignore[arg-type]


def test_cited_figures_match_the_aggregate():
    families = json.loads(_AGGREGATE.read_text())["all"]["families"]
    assert set(grind.CITED) == set(families)
    for name, fam in families.items():
        cited = grind.CITED[name]
        assert cited["total"] == fam["total"], name
        assert cited["stage"] == fam["stage"], name
        assert cited["solids"] == fam["solids"], name
        assert cited["median_faces"] == fam["median_faces"], name


def test_cited_figures_carry_the_rules_reasons():
    c = grind.CITED
    assert c["timeout"]["total"] + c["crash"]["total"] == 632
    assert sum(f["total"] for f in c.values()) == 9956
    assert c["overlapping_facets"]["stage"]["3d"] == 5840
    assert c["edge_through_face"]["stage"]["3d"] == 2349
    assert c["open_loop"]["stage"]["2d"] == 223 and c["periodic_surface"]["stage"]["2d"] == 390
    assert c["no_volume"]["solids"]["single"] == 162
    # the threshold is calibrated to the family medians: above every one of them by a margin,
    # and not so far above that no real part reaches it (the largest median is periodic's 439).
    assert 2 * c["periodic_surface"]["median_faces"] < grind.HEAVY_FACES
    assert grind.HEAVY_FACES < 3 * c["periodic_surface"]["median_faces"]
    assert 7 * c["overlapping_facets"]["median_faces"] < grind.HEAVY_FACES
    assert grind.HEAVY_FACES < 8 * c["overlapping_facets"]["median_faces"]


def test_every_manifest_entry_gets_a_tier():
    tiers = manifest_tiers()
    assert sum(tiers.values()) == 9956
    assert set(tiers) <= _TIERS


def test_every_unanswered_manifest_entry_is_heavy():
    files = json.loads(_MANIFEST.read_text())["files"]
    unanswered = [r for r in files if harvest.legacy_family(r.get("gmsh_error") or "") == "timeout"]
    assert len(unanswered) == 632
    for row in unanswered:
        assert grind.predict("timeout", "", solids=row.get("solids"),
                             faces=row.get("faces")).tier == HEAVY


def test_record_is_the_verdict_entry():
    assert grind.record("overlapping_facets", "3d", solids=4, faces=139) == {
        "tier": STANDARD, "rule": grind.RULE_VOLUME_REFUSAL}
    assert grind.record("timeout", "", solids=None, faces=None) == {
        "tier": HEAVY, "rule": grind.RULE_NO_ANSWER}


def _defect(code: DefectCode, kind: str, stage: str, severity=DefectSeverity.error) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type="shape", entity_index=0,
                        message="m", measurements={"mesh_kind": kind, "stage": stage})


def test_tier_of_reads_the_most_severe_mesh_defect():
    overlap = _defect(DefectCode.mesh_overlapping_facets, "overlapping_facets", "3d")
    loop = _defect(DefectCode.mesh_open_loop, "open_loop", "2d", DefectSeverity.warning)
    timeout = _defect(DefectCode.mesh_timeout, "timeout", "", DefectSeverity.fatal)
    assert grind.tier_of([loop, overlap], solids=1, faces=100) == STANDARD
    assert grind.tier_of([loop], solids=1, faces=100) == CHEAP
    assert grind.tier_of([overlap, timeout], solids=1, faces=100) == HEAVY


def test_tier_of_with_no_mesh_defect_is_a_meshed_part():
    gap = EntityDefect(code=DefectCode.invalid_brep, severity=DefectSeverity.fatal,
                       entity_type="shape", entity_index=0, message="m")
    assert grind.tier_of([], solids=1, faces=50) == CHEAP
    assert grind.tier_of([gap], solids=1, faces=50) == CHEAP
    assert grind.tier_of([gap], solids=1, faces=None) == UNKNOWN


def test_tier_of_never_raises():
    assert grind.tier_of(None, solids=1, faces=50) in _TIERS  # type: ignore[arg-type]
    assert grind.tier_of([object(), 3], solids=1, faces=50) in _TIERS  # type: ignore[list-item]


def test_the_verdict_records_grind():
    goal_py = _SRC / "cad/repair/goal.py"
    if not goal_py.exists():
        pytest.skip("Task 2's goal.py is not on this branch yet; the recording line lands with it")
    tree = ast.parse(goal_py.read_text())
    keys = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "grind"}
    assert keys == {"grind"}, "the goal verdict must record a 'grind' entry"
    assert "cad/repair/goal.py" in _consumers()


_SURFACE = {"predict", "tier_of", "record"}


def _references_grind(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "meshpipeline.cad.repair.grind":
            if any(a.name in _SURFACE for a in node.names):
                return True
        if (isinstance(node, ast.Attribute) and node.attr in _SURFACE
                and isinstance(node.value, ast.Name) and node.value.id == "grind"):
            return True
    return False


def _consumers() -> set[str]:
    out = set()
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "cad/repair/grind.py":
            continue
        if _references_grind(ast.parse(path.read_text())):
            out.add(rel)
    return out


def _reads_grind_key(expr: ast.AST) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "grind":
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "grind"):
            return True
    return False


def test_nothing_consumes_grind():
    present = {rel for rel in CONSUMERS if (_SRC / rel).exists()}
    assert _consumers() == present
    readers = []
    for path in _SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            tests = []
            if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                tests.append(node.test)
            elif isinstance(node, ast.Compare):
                tests.append(node)
            elif isinstance(node, ast.comprehension):
                tests.extend(node.ifs)
            elif isinstance(node, ast.Match):
                tests.append(node.subject)
            if any(_reads_grind_key(t) for t in tests):
                readers.append(f"{path.relative_to(_SRC)}:{node.lineno if hasattr(node, 'lineno') else '?'}")
    assert readers == [], f"a branch reads the recorded grind estimate: {readers}"
