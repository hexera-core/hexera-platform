# Responsibility: Verify the merged chain keeps our applied ids and that 0025 adds main's 0023 objects only where they are missing.
# Boundaries: runs the revision body against a recording double; real upgrades along both paths are
#             tests/integration/test_migration_0025_reconcile_postgres.py.
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"


def _load(revision: str):
    spec = importlib.util.spec_from_file_location(f"_rev_{revision}", VERSIONS / f"{revision}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_chain_keeps_our_applied_ids_behind_mains_0023():
    chain = {m.revision: m.down_revision for m in map(_load, (
        "0023_repair_bypass_and_viewer", "0023_chat_session_attachments",
        "0024_meshing_shell_elements_set", "0025_repair_bypass_reconcile"))}
    assert chain == {
        "0023_repair_bypass_and_viewer": "0022_returned_cad_hints",
        "0023_chat_session_attachments": "0023_repair_bypass_and_viewer",
        "0024_meshing_shell_elements_set": "0023_chat_session_attachments",
        "0025_repair_bypass_reconcile": "0024_meshing_shell_elements_set",
    }


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Op:
    def __init__(self, column_present: bool) -> None:
        self.calls: list[tuple] = []
        self._present = column_present

    def get_bind(self):
        op = self

        class _Bind:
            def execute(self, *_a, **_k):
                return _Result((1,) if op._present else None)
        return _Bind()

    def add_column(self, table, column):
        self.calls.append(("add_column", table, column.name, column.nullable,
                           str(column.server_default.arg)))

    def execute(self, sql):
        self.calls.append(("execute", sql))


@pytest.mark.parametrize("present", [False, True])
def test_the_column_is_added_only_when_missing_and_the_label_always_idempotently(present, monkeypatch):
    module = _load("0025_repair_bypass_reconcile")
    main = _load("0023_repair_bypass_and_viewer")
    op = _Op(column_present=present)
    monkeypatch.setattr(module, "op", op)
    module.upgrade()
    adds = [c for c in op.calls if c[0] == "add_column"]
    assert adds == ([] if present else
                    [("add_column", "organizations", "repair_auto_accept_allowed", False, "false")])
    # THE SAME STATEMENT main's 0023 runs, which PostgreSQL skips when the label exists
    assert ("execute", f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{main._VIEWER_LABEL}'") in op.calls


def test_the_downgrade_leaves_mains_objects_alone(monkeypatch):
    module = _load("0025_repair_bypass_reconcile")
    op = _Op(column_present=True)
    monkeypatch.setattr(module, "op", op)
    module.downgrade()
    assert op.calls == []
