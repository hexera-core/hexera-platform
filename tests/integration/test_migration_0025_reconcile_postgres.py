# Responsibility: Verify both histories reach one schema at the head: main's path and the path of a database that ran our 0023/0024 before main's 0023 existed.
# Boundaries: real PostgreSQL upgrades through alembic, on the same throwaway database the migration
#             wrapper tests use; it compares schemas, and never touches data.
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from tests.integration.test_migration_wrapper_postgres import (
    _head_in_db,
    _schema_fingerprint,
    _sync_url,
    empty_db,  # noqa: F401 - the fixture
)

from meshpipeline.runtime import migrate

#: every test starts from the wrapper suite's empty database
pytestmark = pytest.mark.usefixtures("empty_db")

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

MAIN_0023 = "0023_repair_bypass_and_viewer"
OURS_0023 = "0023_chat_session_attachments"
OURS_0024 = "0024_meshing_shell_elements_set"
RECONCILE = "0025_repair_bypass_reconcile"


def _config(version_dir: Path | None = None):
    cfg = migrate._alembic_config(configure_logging=False)
    if version_dir is not None:
        cfg.set_main_option("version_locations", str(version_dir))
    return cfg


def _old_chain(tmp_path: Path) -> Path:
    """The revisions as the dev site ran them before main's 0023 landed: everything up to 0022,
    then OUR 0023 straight behind 0022, then our 0024. Built from today's files: our 0023 differs
    from what was applied only in its down_revision, and our 0024 not at all."""
    out = tmp_path / "old_versions"
    out.mkdir()
    for f in VERSIONS.glob("*.py"):
        if f.name.startswith(("0023_", "0024_", "0025_")):
            continue
        shutil.copy(f, out / f.name)
    ours = (VERSIONS / f"{OURS_0023}.py").read_text(encoding="utf-8")
    old, n = re.subn(rf'^down_revision = "{MAIN_0023}"$', 'down_revision = "0022_returned_cad_hints"',
                     ours, flags=re.MULTILINE)
    assert n == 1, "our 0023 no longer sits directly behind main's 0023"
    (out / f"{OURS_0023}.py").write_text(old, encoding="utf-8")
    shutil.copy(VERSIONS / f"{OURS_0024}.py", out / f"{OURS_0024}.py")
    return out


def _organizations_has_bypass_column() -> bool:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            return c.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'organizations' AND column_name = 'repair_auto_accept_allowed'")
            ).first() is not None
    finally:
        eng.dispose()


def _main_path_fingerprint() -> tuple:
    command.upgrade(_config(), MAIN_0023)
    assert _head_in_db() == MAIN_0023
    command.upgrade(_config(), OURS_0024)
    before_reconcile = _schema_fingerprint()
    command.upgrade(_config(), "head")
    assert _head_in_db() == RECONCILE
    after = _schema_fingerprint()
    # ON MAIN'S PATH THE RECONCILING REVISION CHANGES NOTHING
    assert after == before_reconcile
    return after


def test_main_path_reaches_the_head_and_the_reconcile_is_a_no_op():
    _main_path_fingerprint()
    assert _organizations_has_bypass_column()


def test_a_database_that_ran_our_revisions_first_reaches_the_same_schema(tmp_path):
    # THE DEV SITE'S PATH: 0022 -> our 0023 -> our 0024 on the old chain, so main's 0023 never ran
    command.upgrade(_config(_old_chain(tmp_path)), "head")
    assert _head_in_db() == OURS_0024
    assert not _organizations_has_bypass_column(), "the old chain should not have main's column"
    # the NEW chain, from the revision that database is stamped at: only the reconcile runs
    command.upgrade(_config(), "head")
    assert _head_in_db() == RECONCILE
    dev_path = _schema_fingerprint()
    assert _organizations_has_bypass_column()

    # THE SAME DATABASE BUILT ALONG MAIN'S PATH, compared object by object. Taken back to base
    # with the new chain's own downgrades (no schema is dropped by hand), then rebuilt from empty.
    command.downgrade(_config(), "base")
    assert _head_in_db() is None
    main_path = _main_path_fingerprint()
    cols, enums, idx, cons, other = dev_path
    m_cols, m_enums, m_idx, m_cons, m_other = main_path
    assert cols == m_cols, "columns differ between the two paths"
    assert enums == m_enums, "enum labels differ between the two paths"
    assert idx == m_idx, "indexes differ between the two paths"
    assert cons == m_cons, "constraints differ between the two paths"
    assert other == m_other, "sequences or functions differ between the two paths"
