# Responsibility: Let an artifact row say it holds the repaired CAD a customer is handed back.
# Boundaries: one new label on the artifacttype enum.

# WHY THE REPAIRED FILE IS A DELIVERABLE AND NOT JUST AN INPUT.
#
# A customer whose geometry we fixed paid for the fixed geometry as much as for the mesh built
# from it. A service that repaired their CAD, meshed it, and handed back only the mesh has
# delivered half the work and kept the more reusable half. So the repaired bytes get their own
# artifact class, downloadable beside the mesh.
#
# STILL NOT REQUIRED. artifact_policy keeps it out of the required classes: most jobs mesh the
# upload as it arrived and have no repaired file to hand back, and a job with none is not short a
# deliverable. Requiredness is decided per outcome by the policy, never by this label existing.
#
# Same shape as revision 0011, and the same reasons: ADD VALUE on the existing type so one
# statement serves both referencing tables, IF NOT EXISTS so a re-run is idempotent, no
# autocommit block so the migration transaction - and with it env.py's search_path anchor -
# survives for every revision after this one, and an empty downgrade because PostgreSQL cannot
# drop an enum label and an unused one constrains nothing.

# Revision ID: 0014_repaired_cad_artifact
# Revises: 0013_repair_operator_queue
from __future__ import annotations

from alembic import op

revision = "0014_repaired_cad_artifact"
down_revision = "0013_repair_operator_queue"
branch_labels = None
depends_on = None

#: Must equal persistence.models.ArtifactType.repaired_cad.value, which the unit tier asserts.
_LABEL = "repaired_cad"


def upgrade() -> None:
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_LABEL}'")


def downgrade() -> None:
    # DELIBERATELY EMPTY; see revision 0011 for the reasoning.
    pass
