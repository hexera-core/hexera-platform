# Responsibility: Verify every row-locked read of a job refreshes the row, so no lock decides on a read taken before it.
# Boundaries: a static scan of the shipped source; the behaviour is tests/unit/persistence/test_locked_job_reads_are_fresh.py.
from __future__ import annotations

import ast
from pathlib import Path

from tests._scan import scanned

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
ENTITY = "SimulationJob"

# WHY. `SELECT ... FOR UPDATE` locks the row, but SQLAlchemy still hands back the instance already
# in the session's identity map, with the attributes of whichever read put it there. A lock taken
# after an ordinary read of the same job therefore decides on a status the lock never saw: the
# worker's claim read `pending` from before the owner's cancel and wrote `running` over
# `cancelled` (shared dev, 2026-09-29). `execution_options(populate_existing=True)` on the locked
# statement is what makes it read the row it locked.
#
# The scan sees a statement written as one chain (`select(SimulationJob)...with_for_update()`) or
# a `with_for_update=` keyword beside the entity, which is how this codebase writes them. A locked
# statement assembled across several assignments is out of its sight.


def _name(call: ast.Call) -> str:
    return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")


def _spine(call: ast.Call) -> list[ast.Call]:
    # a.b(x).c(y).d() -> [d(), c(y), b(x)]: the calls of one chain, never their arguments
    out: list[ast.Call] = []
    node: ast.AST | None = call
    while isinstance(node, ast.Call):
        out.append(node)
        node = node.func.value if isinstance(node.func, ast.Attribute) else None
    return out


def _names_entity(call: ast.Call) -> bool:
    return any(isinstance(n, ast.Name) and n.id == ENTITY
               for arg in [*call.args, *(k.value for k in call.keywords)] for n in ast.walk(arg))


def _refreshes(call: ast.Call) -> bool:
    return any(k.arg == "populate_existing" and isinstance(k.value, ast.Constant)
               and k.value.value is True for k in call.keywords)


def _locked_job_reads(source: str) -> list[tuple[int, bool]]:
    """(line, refreshes) for every row-locked read of the job entity in `source`."""
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    inner = {id(c.func.value) for c in calls
             if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Call)}
    found = []
    for top in (c for c in calls if id(c) not in inner):
        spine = _spine(top)
        if (any(_name(c) == "with_for_update" for c in spine)
                and any(_names_entity(c) for c in spine)):
            found.append((top.lineno, any(_name(c) == "execution_options" and _refreshes(c)
                                          for c in spine)))
    for call in calls:
        if any(k.arg == "with_for_update" for k in call.keywords) and _names_entity(call):
            found.append((call.lineno, _refreshes(call)))
    return found


def test_every_locked_job_read_refreshes_the_row():
    reads = scanned([(f"{p.relative_to(SRC.parent.parent)}:{line}", refreshes)
                     for p in SRC.rglob("*.py") if "__pycache__" not in p.parts
                     for line, refreshes in _locked_job_reads(p.read_text(encoding="utf-8"))],
                    "row-locked reads of the job", at_least=2)
    stale = [where for where, refreshes in reads if not refreshes]
    assert not stale, (
        "a row-locked job read without populate_existing returns the session's pre-lock copy of "
        "the row, so the lock decides on a stale status:\n  " + "\n  ".join(stale))


def test_the_rule_tells_a_stale_lock_from_a_fresh_one():
    stale_chain = "select(SimulationJob).where(SimulationJob.id == j).with_for_update()\n"
    stale_keyword = "db.get(SimulationJob, j, with_for_update=True)\n"
    fresh_chain = ("select(SimulationJob).where(SimulationJob.id == j).with_for_update()"
                   ".execution_options(populate_existing=True)\n")
    fresh_keyword = "db.get(SimulationJob, j, with_for_update=True, populate_existing=True)\n"
    assert _locked_job_reads(stale_chain) == [(1, False)]
    assert _locked_job_reads(stale_keyword) == [(1, False)]
    assert _locked_job_reads(fresh_chain) == [(1, True)]
    assert _locked_job_reads(fresh_keyword) == [(1, True)]
    # a locked statement passed straight to execute is still one read, reported once
    assert _locked_job_reads(f"await db.execute({fresh_chain.strip()})\n") == [(1, True)]
    # an unlocked job read, and a lock on another table, are not this rule's business
    assert _locked_job_reads("select(SimulationJob).where(SimulationJob.id == j)\n") == []
    assert _locked_job_reads("select(ChatSession).with_for_update()\n") == []
