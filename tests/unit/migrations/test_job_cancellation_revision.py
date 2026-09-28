# Responsibility: Verify the cancellation revision extends the chain, adds exactly the value and the column, and refuses to erase a cancelled job on the way down.
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "alembic" / "versions" / "0009_job_cancellation.py").read_text()


def _constants() -> dict:
    tree = ast.parse(SOURCE)
    return {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant)}


def test_it_extends_the_chain_rather_than_branching_it():
    values = _constants()
    assert values["revision"] == "0009_job_cancellation"
    assert values["down_revision"] == "0008_overage_metering"
    assert values["branch_labels"] is None


def test_upgrade_adds_the_value_and_the_column_and_nothing_else():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    assert re.search(r"ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'cancelled'", up)
    assert 'op.add_column("simulation_jobs"' in up and '"cancel_reason"' in up
    assert "create_table" not in up and "CREATE SCHEMA" not in up
    # inside the migration transaction, so env.py's SET LOCAL search_path still applies to it
    assert "autocommit_block" not in up
    # ...which PostgreSQL only allows from 12 on, so the version is checked, not assumed
    assert "server_version_info" in up and "(12,)" in up


def test_the_value_and_the_column_match_the_model():
    from meshpipeline.persistence.models import JobStatus, SimulationJob

    assert JobStatus.cancelled.value == "cancelled"
    assert SimulationJob.__table__.c.cancel_reason.type.length == 500
    assert "length=500" in SOURCE


def test_downgrade_refuses_while_a_cancelled_job_exists_and_rebuilds_the_type_without_it():
    down = SOURCE[SOURCE.index("def downgrade"):]
    assert "status = 'cancelled'" in down and "raise RuntimeError" in down
    rebuilt = down.split("CREATE TYPE jobstatus AS ENUM")[1]
    assert "'cancelled'" not in rebuilt.split("USING")[0], "the rebuilt type still carries the value"
    assert 'op.drop_column("simulation_jobs", "cancel_reason")' in down
