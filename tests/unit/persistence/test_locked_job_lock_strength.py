# Responsibility: Verify the job row is locked FOR NO KEY UPDATE: every writer of the row is shut out, a row that only references the job (a capture insert) is not.
from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from meshpipeline.persistence.lease import _locked_job


def test_the_lease_lock_shuts_out_writers_but_not_references():
    sql = str(_locked_job(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR NO KEY UPDATE")
