# Responsibility: Let an artifact row say it holds a CAD repair inspection report.
# Boundaries: one new label on the artifacttype enum. WHICH jobs get a report is decided by
#             pipeline/repair_inspect.py; the row is written by application/repair_report_delivery.py.

# WHY A NEW ARTIFACT CLASS.
#
# Every mesh-intended job now carries what repair inspection measured about the uploaded CAD
# (STATE_SCHEMA_VERSION 13). That evidence has to outlive the run's workspace for the two readers
# who need it most: an operator deciding whether the file is worth repairing, and the customer
# being told WHY their geometry was refused. Both of those readers exist precisely when the run
# produced no mesh, so the report cannot ride inside the mesh bundle - the job that needs it most
# is the job that never built one.
#
# IT IS EVIDENCE, NOT A DELIVERABLE. application/artifact_policy.py keeps it out of both the
# required classes and the optional-warning list: a job with no report is not short a deliverable,
# and required_ready() is unchanged by this revision. Adding the label therefore cannot alter
# whether any existing or future job counts as delivered.
#
# ADD VALUE, NOT A NEW TYPE. The label is appended to the existing `artifacttype` enum, which
# artifacts.artifact_type and artifact_reconciliations.artifact_type both reference, so one
# statement serves both tables and no row is rewritten.
#
# ADDED BEFORE THE IMAGE THAT WRITES IT, and additive both ways in a rollout: the previous image
# never writes the label and never reads it, and the new image writes it only for runs whose
# inspection produced a report. A rollback leaves the label in place (see downgrade).

# Revision ID: 0011_repair_report_artifact
# Revises: 0010_source_upload_hold
from __future__ import annotations

from alembic import op

revision = "0011_repair_report_artifact"
down_revision = "0010_source_upload_hold"
branch_labels = None
depends_on = None

#: The label this revision adds, spelled once. It must equal
#: persistence.models.ArtifactType.repair_report.value, which the unit tier asserts.
_LABEL = "repair_report"


def upgrade() -> None:
    # IN THE MIGRATION'S OWN TRANSACTION, and deliberately NOT in an autocommit block.
    #
    # PostgreSQL 12 and later accept ALTER TYPE ... ADD VALUE inside a transaction; the remaining
    # restriction is that the new label may not be USED in the same transaction, and nothing here
    # uses it. The older advice to wrap this in `op.get_context().autocommit_block()` is actively
    # harmful in this repository: alembic/env.py anchors every unqualified statement by issuing
    # `SET LOCAL search_path` inside the migration transaction, and SET LOCAL dies with the
    # transaction that set it. An autocommit block commits that transaction, so every revision
    # AFTER this one would run under the role's ambient search_path instead - which, for a role
    # configured `search_path = other, public`, silently creates later tables in the wrong schema.
    # That is exactly what happened: 0012 and 0013 built their tables in `other`, and the
    # completeness guard in runtime/migrate.py refused the result.
    #
    # IF NOT EXISTS keeps a re-run of a partially applied migration from failing on a label that
    # is already there.
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_LABEL}'")


def downgrade() -> None:
    # DELIBERATELY EMPTY. PostgreSQL cannot drop a label from an enum, and the only safe
    # alternative - rebuild the type and rewrite both referencing tables - would have to decide
    # what becomes of rows already carrying it. Leaving the label is harmless: the pre-0011 image
    # neither writes nor reads it, and an unused enum label constrains nothing.
    pass
