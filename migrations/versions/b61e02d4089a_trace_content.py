"""Optional administrator-visible agent trace content."""

import sqlalchemy as sa
from alembic import op

revision = "b61e02d4089a"
down_revision = "9c20a1324c8d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_tool_calls", sa.Column("content", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("agent_tool_calls", "content")
