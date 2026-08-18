## Responsibility: Shape every generated revision - the imports and the four identity assignments.
## Boundaries: a template; `##` lines are stripped when it renders, so nothing here reaches a revision file.
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade(): ${upgrades if upgrades else "pass"}
def downgrade(): ${downgrades if downgrades else "pass"}
