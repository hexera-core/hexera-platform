# Responsibility: Verify the repair revisions' upgrade and downgrade bodies actually RUN, not merely read correctly.
# Boundaries: it executes them against a recording double, so it proves the DDL is constructible -
#             never that PostgreSQL accepts it, which is the integration tier's job.
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VERSIONS = REPO / "alembic" / "versions"

# WHY THIS EXISTS. Every other test in this directory reads a revision as TEXT. That catches a
# wrong down_revision or a missing column, and it caught neither of the two ways revision 0012
# could not run: `sa.dialects.postgresql.JSONB()` raises AttributeError, because importing
# sqlalchemy does not bind its dialect submodules, and the expression sits inside `upgrade()`
# where importing the module never evaluates it. Every integration shard failed on the schema
# build before a single test ran. Executing the body against a double is the cheapest thing that
# would have said so on the host, in milliseconds.

_REPAIR_REVISIONS = (
    "0011_repair_report_artifact",
    "0012_cad_repair_jobs",
    "0013_repair_operator_queue",
    "0014_repaired_cad_artifact",
)


class _Recorder:
    """Stands in for `alembic.op`, remembering what the migration asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None
        return _call

    # the few members a migration uses as something other than a plain DDL call
    def f(self, name):                      # noqa: D102 - index-name helper
        self.calls.append(("f", (name,), {}))
        return name

    def get_context(self):
        return self

    def get_bind(self):
        return None

    def autocommit_block(self):
        from contextlib import nullcontext
        self.calls.append(("autocommit_block", (), {}))
        return nullcontext()


def _load(revision: str):
    path = VERSIONS / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(f"_rev_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("revision", _REPAIR_REVISIONS)
def test_the_upgrade_body_runs(revision, monkeypatch):
    module = _load(revision)
    recorder = _Recorder()
    monkeypatch.setattr(module, "op", recorder)
    # An Enum().drop(bind) in a downgrade would otherwise reach a real dialect; the upgrade bodies
    # do not, so nothing else needs standing in.
    module.upgrade()
    assert recorder.calls, f"{revision}.upgrade() emitted no DDL at all"


@pytest.mark.parametrize("revision", _REPAIR_REVISIONS)
def test_the_downgrade_body_runs(revision, monkeypatch):
    module = _load(revision)
    recorder = _Recorder()
    monkeypatch.setattr(module, "op", recorder)
    if revision == "0012_cad_repair_jobs":
        # its downgrade drops the enum type, which needs a bind it cannot have here
        import sqlalchemy as sa
        monkeypatch.setattr(sa, "Enum", lambda *a, **k: type("_E", (), {"drop": lambda s, *a, **k: None})())
    module.downgrade()   # 0011's is deliberately empty, which is allowed


def test_every_jsonb_column_is_built_from_the_bound_dialect():
    # the exact expression that broke: sqlalchemy's dialect submodules are not bound by
    # `import sqlalchemy as sa`, so a JSONB column must come from an explicit import
    for revision in _REPAIR_REVISIONS:
        source = (VERSIONS / f"{revision}.py").read_text()
        assert "sa.dialects." not in source, (
            f"{revision} reaches through sa.dialects, which raises AttributeError at runtime")
        if "JSONB" in source:
            assert "from sqlalchemy.dialects import postgresql" in source, revision


def test_no_revision_commits_early_and_strands_the_schema_anchor():
    # alembic/env.py anchors every unqualified statement with `SET LOCAL search_path`, which
    # reverts when its transaction ends. A revision that opens an autocommit block commits that
    # transaction, so every revision AFTER it runs under the role's ambient search_path - and a
    # role configured `search_path = other, public` then gets its tables built in `other`. This
    # repository had exactly that failure; the ban is cheaper than rediscovering it.
    # Asserted over the AST, not the text: a revision is entitled to EXPLAIN in a comment why it
    # does not do this, and a substring search cannot tell that apart from doing it.
    for revision in _REPAIR_REVISIONS:
        tree = ast.parse((VERSIONS / f"{revision}.py").read_text())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert "autocommit_block" not in called, (
            f"{revision} would strand the search_path anchor for every later revision")


def test_the_repair_revisions_form_one_unbroken_chain():
    chain = {}
    for revision in _REPAIR_REVISIONS:
        module = _load(revision)
        chain[module.revision] = module.down_revision
    assert chain == {
        "0011_repair_report_artifact": "0010_source_upload_hold",
        "0012_cad_repair_jobs": "0011_repair_report_artifact",
        "0013_repair_operator_queue": "0012_cad_repair_jobs",
        "0014_repaired_cad_artifact": "0013_repair_operator_queue",
    }
