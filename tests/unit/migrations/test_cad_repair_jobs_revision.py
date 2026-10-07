# Responsibility: Verify the repair-job revision extends the chain and declares exactly the tables, columns and enum the models do.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PATH = REPO / "alembic" / "versions" / "0012_cad_repair_jobs.py"
SOURCE = PATH.read_text()


def _constants() -> dict:
    tree = ast.parse(SOURCE)
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            try:
                out[n.targets[0].id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    return out


def test_it_extends_the_chain_rather_than_branching_it():
    values = _constants()
    assert values["revision"] == "0012_cad_repair_jobs"
    assert values["down_revision"] == "0011_repair_report_artifact"
    assert values["branch_labels"] is None


def test_the_enum_labels_match_the_model_member_for_member():
    from meshpipeline.persistence.models import RepairJobStatus

    assert tuple(_constants()["_REPAIR_JOB_STATUS"]) == tuple(s.value for s in RepairJobStatus)


#: Columns a LATER revision adds to these tables, and which 0012 therefore must not mention.
#: Listed so that the agreement below stays a real check rather than being loosened to a subset:
#: every model column is either created here or named as arriving later.
_ADDED_AFTER = {
    "cad_repair_jobs": {
        "assigned_operator": "0013_repair_operator_queue",
        "assigned_at": "0013_repair_operator_queue",
    },
}


def test_the_columns_match_the_models():
    from meshpipeline.persistence.models import CadRepairAttempt, CadRepairJob

    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    for model in (CadRepairJob, CadRepairAttempt):
        table = model.__table__
        declared = up[up.index(f'"{table.name}"'):]
        declared = declared[:declared.index("op.create_index")]
        later = _ADDED_AFTER.get(table.name, {})
        for column in table.c:
            if column.name in later:
                # created by a later revision, so it must NOT be here - a column in both places
                # means one of the two revisions is wrong about who owns it
                assert f'"{column.name}"' not in declared, (
                    f"{table.name}.{column.name} is created by {later[column.name]}, "
                    "but revision 0012 also declares it")
                continue
            assert f'"{column.name}"' in declared, (
                f"{table.name}.{column.name} is in the model but in no revision - add it to "
                "0012, or to a later revision and to _ADDED_AFTER")


def test_the_customer_upload_cannot_be_deleted_from_under_a_repair_job():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    for referenced in ("geometry_sources.id", "geometry_interpretations.id"):
        clause = up[up.index(referenced) - 120:up.index(referenced) + 120]
        assert 'ondelete="RESTRICT"' in clause, (
            f"{referenced} must be RESTRICT - a repair job is ABOUT those bytes")
    # the mesh run, by contrast, is a thing that happened to the job: losing it must not take the
    # service record with it
    sim = up[up.index("simulation_jobs.id") - 120:up.index("simulation_jobs.id") + 120]
    assert 'ondelete="SET NULL"' in sim


def test_the_attempt_numbering_is_enforced_by_the_database():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    assert 'UniqueConstraint("repair_job_id", "attempt_no"' in up


def test_the_queue_has_its_own_index_in_the_order_the_queue_reads():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    assert '["status", "service_priority", "created_at"]' in up


def test_the_downgrade_removes_the_enum_as_well_as_the_tables():
    down = SOURCE[SOURCE.index("def downgrade"):]
    assert 'op.drop_table("cad_repair_attempts")' in down
    assert 'op.drop_table("cad_repair_jobs")' in down
    # a left-behind type makes a re-upgrade fail on "type already exists"
    assert 'sa.Enum(name="repairjobstatus").drop(' in down
    assert down.index("cad_repair_attempts") < down.index("cad_repair_jobs"), (
        "the child table must be dropped before its parent")
