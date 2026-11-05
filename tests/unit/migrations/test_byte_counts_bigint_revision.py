# Responsibility: Verify every byte count in the schema can hold a file bigger than 2 GiB.
# Boundaries: reads the models and revision 0018 on the host; that PostgreSQL stores such a size is
#             proven in tests/integration/test_artifact_concurrency_postgres.py.
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from meshpipeline.persistence.models import Base

REPO = Path(__file__).resolve().parents[3]
REVISION = REPO / "alembic" / "versions" / "0018_byte_counts_bigint.py"

# WHY. Job 986ea907 (2026-11-02): a 16.5M-tet FEA assembly built and passed review, then every
# multi-GB file it produced - a 3.6 GB mesh.msh, 3.5-6.2 GB solver decks, the 4.5 GB required
# bundle - was uploaded, refused a row (32-bit INTEGER, ceiling 2,147,483,647), deleted as an
# orphan, and the user was told the mesh "could not be stored for download". These are the sizes
# that tripped it.
INT32_MAX = 2**31 - 1
TRIPPED_SIZES = (3_588_820_000, 3_514_331_954, 4_503_723_800, 4_847_525_420, 6_151_555_156)

#: a column whose NAME says it counts bytes
_BYTE_NAME = re.compile(r"(^|_)(size|bytes)(_|$)|_size$|^size_bytes$")


def _byte_columns():
    out = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if _BYTE_NAME.search(col.name) and isinstance(col.type, sa.Integer):
                out.append((table.name, col.name, col.type))
    return out


def _load_revision():
    spec = importlib.util.spec_from_file_location("_rev_0018", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sizes_that_failed_do_not_fit_32_bits():
    # the failure mode itself: every one of these is past INTEGER's ceiling
    assert all(n > INT32_MAX for n in TRIPPED_SIZES)


def test_the_known_byte_columns_are_found():
    found = {(t, c) for t, c, _ in _byte_columns()}
    assert {("artifacts", "size_bytes"), ("geometry_sources", "size_bytes"),
            ("artifact_reconciliations", "object_size")} <= found


@pytest.mark.parametrize("table,column,type_", _byte_columns(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_byte_count_is_bigint(table, column, type_):
    # A NEW byte-count column declared Integer fails here, before a big file finds it in production.
    assert isinstance(type_, sa.BigInteger), (
        f"{table}.{column} is {type_!r}: a byte count must be BigInteger - a 32-bit column refuses "
        "any file over 2 GiB")


def test_revision_0018_widens_every_byte_column_in_the_models():
    module = _load_revision()
    assert module.down_revision == "0017_chat_session_user_notes"
    model_cols = {(t, c) for t, c, _ in _byte_columns()}
    assert set(module.BYTE_COLUMNS) == model_cols


def test_revision_0018_upgrade_emits_bigint_for_each_column(monkeypatch):
    module = _load_revision()
    calls = []

    class _Op:
        def alter_column(self, table, column, **kw):
            calls.append((table, column, kw))

    monkeypatch.setattr(module, "op", _Op())
    module.upgrade()
    assert {(t, c) for t, c, _ in calls} == set(module.BYTE_COLUMNS)
    assert all(isinstance(kw["type_"], sa.BigInteger) for _, _, kw in calls)

    calls.clear()
    module.downgrade()
    assert {(t, c) for t, c, _ in calls} == set(module.BYTE_COLUMNS)


def test_the_bigint_columns_compile_as_bigint_on_postgresql():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    for table, column in _load_revision().BYTE_COLUMNS:
        ddl = str(CreateTable(tables[table]).compile(dialect=postgresql.dialect()))
        line = next(ln for ln in ddl.splitlines() if ln.strip().startswith(column + " "))
        assert "BIGINT" in line, line
