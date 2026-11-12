"""Every Alembic revision id fits alembic_version.version_num, a VARCHAR(32).

A longer id passes every SQLite and unit test, then fails the real Postgres migration job at the very end of the
upgrade ("value too long for type character varying(32)") and stops the deploy (the meshing-standard revisions,
11 Nov 2026). Read the ids from the files, so the check needs no database.
"""
from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
LIMIT = 32  # alembic_version.version_num is VARCHAR(32)


def _ids() -> dict[str, str]:
    out = {}
    for f in sorted(VERSIONS.glob("*.py")):
        m = re.search(r'^revision\s*=\s*["\']([^"\']+)["\']', f.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            out[f.name] = m.group(1)
    return out


def test_there_are_revisions_to_check():
    assert len(_ids()) > 10


def test_every_revision_id_fits_the_version_column():
    long = {name: rid for name, rid in _ids().items() if len(rid) > LIMIT}
    assert not long, f"revision ids longer than {LIMIT} characters: {long}"
