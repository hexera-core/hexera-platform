# Responsibility: Let a source cleanup intent say "not before": the time until which maintenance leaves it alone.
# Boundaries: one nullable column. WHEN an intent is held, and for how long, is decided by
#             api/v1/upload_direct.py; the sweep that honours it is application/maintenance/reconcile.py.

# WHY A HOLD AT ALL.
#
# The cleanup intent is written BEFORE its object exists, and the maintenance sweep reclaims every
# pending intent it can claim: no source row owns the key, so the object is deleted (or, when it is
# not there yet, the intent is closed as "already absent"). For the multipart upload that window is
# the few seconds between the object write and the source transaction.
#
# A DIRECT upload moves the bytes from the browser straight to the object store, so the window is
# the whole transfer - minutes for a 500 MB CAD file on an ordinary connection. A sweep that ran in
# that window would either close the intent before the object arrived (leaving an object nothing
# names, the exact state the intent exists to prevent) or delete the object before the client asked
# to finish the upload. The hold is the upload's own statement of how long its bytes may still be
# arriving; the sweep offers the intent only once it has passed.
#
# NULL is "due now", which is what every existing intent is, so nothing already recorded changes
# meaning and the multipart path - which never sets it - behaves exactly as before. The name and the
# shape are artifact_reconciliations.next_attempt_at's, and the comparison is made by the database
# for the same reason: several sweepers, several clocks.
#
# NULLABLE, AND ADDED BEFORE THE IMAGE THAT WRITES IT. The previous image inserts intents without
# the column and reads none; the new image reads NULL as due. Either order of rollout is safe.

# Revision ID: 0010_source_upload_hold
# Revises: 0009_job_cancellation
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_source_upload_hold"
down_revision = "0009_job_cancellation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_object_cleanups",
                  sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Dropping the hold makes every held intent due at once. That is the pre-0010 behaviour and it
    # loses no record - the intents themselves stay, pending, for the sweep to settle.
    op.drop_column("source_object_cleanups", "next_attempt_at")
