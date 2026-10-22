# Responsibility: Verify the rulebook (docs/RULES.md) and the code agree: every rule ID the code cites is in force in the book, every in-force rule is cited, the versions match, and every engine-switch reason has a rule.
# Boundaries: reads source text and the book; runs nothing but the pure rule functions.
from __future__ import annotations

import ast
import re
from pathlib import Path

from meshpipeline.contracts import decision_record as dr

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "meshpipeline"
BOOK = ROOT / "docs" / "RULES.md"

#: A table row of the book: | ID | status | rule |
_ROW = re.compile(r"^\|\s*([A-Z]+-[A-Za-z0-9-]+)\s*\|\s*([^|]+?)\s*\|")
#: A string in the code that is exactly a rule ID of a family the book defines.
_FAMILIES = ("ENG", "SW", "DEL", "VER", "PLAN", "LAD", "TIME")
_ID = re.compile(r"^(?:" + "|".join(_FAMILIES) + r")-[A-Za-z0-9]+(?:-[0-9]+)?$")


def _book() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in BOOK.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m and m.group(1).split("-")[0] in _FAMILIES:
            assert m.group(1) not in rows, f"rule {m.group(1)} is listed twice in docs/RULES.md"
            rows[m.group(1)] = m.group(2).strip().lower()
    return rows


def _in_force() -> set[str]:
    return {rid for rid, status in _book().items() if status == "in force"}


def _cited_in_code() -> dict[str, list[str]]:
    """Every string constant in src/ that is exactly a rule ID, with where it is."""
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _ID.match(node.value):
                out.setdefault(node.value, []).append(f"{path.relative_to(SRC)}:{node.lineno}")
    return out


def test_the_book_version_is_the_code_version():
    m = re.search(r"Rulebook version:\s*\*\*([^*]+)\*\*", BOOK.read_text(encoding="utf-8"))
    assert m, "docs/RULES.md has no 'Rulebook version: **n**' line"
    assert m.group(1).strip() == dr.RULES_VERSION, (
        "docs/RULES.md and contracts/decision_record.RULES_VERSION disagree: raise both together")


def test_every_status_is_in_force_planned_or_retired():
    bad = {rid: s for rid, s in _book().items()
           if not (s == "in force" or s.startswith("planned") or s.startswith("retired"))}
    assert not bad, f"unknown rule status in docs/RULES.md: {bad}"


def test_every_rule_the_code_cites_is_in_force():
    known = _in_force()
    stray = {rid: where for rid, where in _cited_in_code().items() if rid not in known}
    assert not stray, (
        "the code cites rule IDs that are not in force in docs/RULES.md (add them to the book, "
        f"or mark them in force when the PR that builds them lands): {stray}")


def test_every_rule_in_force_is_cited_by_the_code():
    unused = sorted(_in_force() - set(_cited_in_code()))
    assert not unused, (
        f"docs/RULES.md lists rules in force that no code cites: {unused}. A rule in force is a "
        "decision the code makes: cite its ID where it is made, or mark it planned or retired.")


def _texts(node: ast.AST) -> list[str]:
    """The opening text of every string an expression can evaluate to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        return [head.value] if isinstance(head, ast.Constant) and isinstance(head.value, str) else [""]
    if isinstance(node, ast.IfExp):
        return _texts(node.body) + _texts(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [t for v in node.values for t in _texts(v)]
    return [""]                              # a reason computed some other way has no rule


def test_every_engine_switch_reason_has_a_rule():
    """engine_fallback.decide states its reason as text; each one must map to an SW rule, so a new
    or reworded reason fails here instead of reaching a run's record without a rule."""
    path = SRC / "pipeline" / "engine_fallback.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    whys = [t for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Decision"
            for kw in node.keywords if kw.arg == "why" for t in _texts(kw.value)]
    assert len(whys) >= 10, "few Decision(why=...) found: this guard is reading the wrong file"
    missing = [w for w in whys if not dr.switch_rule(w)]
    assert not missing, f"engine_fallback.decide reasons with no SW rule: {missing}"
    stale = [p for p, _ in dr.SWITCH_RULES if not any(w.startswith(p) or p.startswith(w)
                                                      for w in whys if w)]
    assert not stale, f"SW rules whose reason engine_fallback.decide no longer gives: {stale}"
    stale = [prefix for prefix, _ in dr.SWITCH_RULES
             if not any(w.startswith(prefix) or prefix.startswith(w) for w in whys if w)]
    assert not stale, f"SW rules whose reason engine_fallback.decide no longer gives: {stale}"


def test_the_delivery_rules_cover_every_position_of_the_ranking():
    from meshpipeline.application import review_delivery as rd
    c = rd.Candidate(attempt=1, engine="e", workspace="", manifest={})
    assert len(dr.DELIVERY_RULES) == len(rd.rank_key(c)) == len(rd._WHY)
