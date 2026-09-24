# Responsibility: Add the three columns the survey row had no place for: the finder's decisions, the look queue's answer and the state's own stamp.
# Boundaries: three columns on one existing table; it creates nothing and drops nothing.

# Additive and reversible. All three have a server default, so every row that already exists reads back
# exactly as it does today: `asking` as `{}`, which `geometry_survey.asking_of` already treats as a row
# composed before the finder was wired, and the other two as the empty string.
#
# WHAT GOES WRONG WITHOUT THEM, which is how they were found. `record` writes a NAMED LIST of keys and
# drops the rest in silence. Measured on a real composed survey of `transition_007_fluid`, the state
# carried `asking` and the row kept none of it, and the cost is not cosmetic:
#
#   * `asking.record` is where the question finder puts the machine values for the two questions the whole
#     chain exists to ask. The fluid-side question's options are two SENTENCES for a person to read and
#     the map from a sentence back to a representation lives in that row; the budget trade's two numbers
#     `{cap: 2000000, cells_high: 2046473}` live there too. With the row gone, answering "the fluid flows
#     through the bore; the part is the solid around it" was refused with "names neither reading of this
#     surface, so it settles no representation; the readings are {}", and every one of the trade's three
#     options was refused because its envelope had become None. The answers were ACCEPTED in memory and
#     REFUSED after a round trip through this table, so answering a question achieved nothing.
#   * `asking.put` is the five-question cap and the consequence ranking. Dropped, every question the
#     survey raises is put and the order is the uncertainty list's, not the finder's: measured on the same
#     part, `put` went from `[q_port_roles, q_budget]` to `[q_budget, q_port_roles]`.
#   * `asking.text` is `ask.say`'s own sentence, which is what a customer reads. Dropped, they read
#     `contract.asking`'s fallback rendering instead: "the measurement names the mouth and its size and
#     says..." where the finder wrote "I can see one opening on this part. It is not named...".
#
# `look_queued` is the same failure with a different cost. The look is a queued worker and the stored
# measurement carries no look until that worker writes one, so a survey waiting for eyes and a survey that
# will never have any are the same bytes in the document. The queue's own answer is the only thing that
# tells them apart, `geometry_survey._noted_queue` has always written it onto the state, and it was dropped
# here: `look_state` could return `pending` inside the request that queued the look and never once from a
# stored row. A look that FAILED, a look that has not happened yet and a look that found a clear passage
# are three different things to the builder and two of them had collapsed into one.
#
# `state_schema` is the state dump's own name (`geometry_survey.SURVEY_STATE_SCHEMA`), which was written
# into every state and dropped here. The measurement row keeps the agent's stamps for exactly this reason:
# no stored artefact should be read by code whose stamp differs from the one it was written under, and this
# row could not say which shape it was written under at all.
#
# WHY COLUMNS AND NOT KEYS INSIDE `survey`. `survey` is the measurement package's own handoff, dumped as
# the package dumped it and checked against the package's own validator. None of these three is the
# package's: they are the platform's record of how it composed and queued, and `contract.survey.Uncertainty`
# forbids extra keys on the handoff deliberately.
#
# Revision ID: 0006_geometry_survey_asking
# Revises: 0005_geometry_step
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0006_geometry_survey_asking'
down_revision = '0005_geometry_step'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('geometry_surveys',
                  sa.Column('asking', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                            server_default=sa.text("'{}'::jsonb")))
    op.add_column('geometry_surveys',
                  sa.Column('look_queued', sa.String(length=16), nullable=False, server_default=''))
    op.add_column('geometry_surveys',
                  sa.Column('state_schema', sa.String(length=64), nullable=False, server_default=''))


def downgrade():
    op.drop_column('geometry_surveys', 'state_schema')
    op.drop_column('geometry_surveys', 'look_queued')
    op.drop_column('geometry_surveys', 'asking')
