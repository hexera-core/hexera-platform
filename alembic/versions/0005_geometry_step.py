# Responsibility: Add the two columns the geometry agent's step keeps on a survey row: its plan and the question only a plan can raise.
# Boundaries: two nullable columns on one existing table; it creates nothing and drops nothing.

# Additive and reversible. Both columns are nullable with no default, so every row that exists stays
# exactly as it is. When this revision shipped the path that writes them was behind a flag that was off
# by default and dead unless the survey was on too, so a deployment that upgraded and turned nothing on
# gained two null columns and no behaviour. That flag is retired and the step runs on every submission
# that has a survey to read; the columns and this migration are unchanged by that.
#
# WHY COLUMNS AND NOT KEYS INSIDE `survey`. `survey` is the measurement package's own handoff, dumped as
# the package dumped it and checked against the package's own validator; the platform does not put its
# own records inside it. `geometry_step` is what this platform did with that handoff.
#
# WHAT GOES WRONG WITHOUT THEM, which is how they were found: the repository writes a named list of
# keys, so a state carrying `geometry_step` and `late` was stored with both silently dropped. The plan
# was made inside the submission turn, written nowhere, and gone by the time the builder read the row,
# so the builder fell back on every job while the step reported success; and the third intake's question
# could not be answered in the next turn, because the row it was raised on no longer had it, which held
# the submission on a question the customer had no way to settle.
#
# Revision ID: 0005_geometry_step
# Revises: 0004_geometry_surveys
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005_geometry_step'
down_revision = '0004_geometry_surveys'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('geometry_surveys',
                  sa.Column('geometry_step', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('geometry_surveys',
                  sa.Column('late', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade():
    op.drop_column('geometry_surveys', 'late')
    op.drop_column('geometry_surveys', 'geometry_step')
