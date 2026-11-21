# Responsibility: Let an artifact row say it holds the customer's STEP handed back UNCHANGED (a repair
#                 attempt's passthrough), or the mesh-size hints sidecar beside a returned STEP.
# Boundaries: two new labels on the artifacttype enum, nothing else touched.

# WHY TWO NEW LABELS AND NOT `repaired_cad`.
#
# With the meshability pass live, a repair that keeps nothing still returns a STEP: the upload's
# bytes, unchanged. Filing those under `repaired_cad` would tell the download menu and the delivery
# manifest that we repaired a file we did not touch. `returned_cad` is that file, named for what it
# is. `mesh_hints` is the per-face size sidecar (crs-mesh-hints/1) delivered beside a returned or
# repaired STEP - an addition to the STEP, never instead of it.
#
# NEITHER IS REQUIRED. artifact_policy keeps both out of the required classes: most jobs return
# neither, and a job without them is not short a deliverable.
#
# Same shape as revisions 0011 and 0014: ADD VALUE IF NOT EXISTS on the existing type (idempotent,
# one statement for both referencing tables), no autocommit block, and an empty downgrade because
# PostgreSQL cannot drop an enum label and an unused one constrains nothing.

# Revision ID: 0022_returned_cad_hints
# Revises: 0021_meshing_shell_elements
from __future__ import annotations

from alembic import op

revision = "0022_returned_cad_hints"
down_revision = "0021_meshing_shell_elements"
branch_labels = None
depends_on = None

#: Must equal persistence.models.ArtifactType.returned_cad.value, which the unit tier asserts.
_RETURNED_LABEL = "returned_cad"
#: Must equal persistence.models.ArtifactType.mesh_hints.value, which the unit tier asserts.
_HINTS_LABEL = "mesh_hints"


def upgrade() -> None:
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_RETURNED_LABEL}'")
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_HINTS_LABEL}'")


def downgrade() -> None:
    # DELIBERATELY EMPTY; see revision 0011 for the reasoning.
    pass
