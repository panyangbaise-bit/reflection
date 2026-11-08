"""NAV comparison and provenance of estimated opening cost."""

import sqlalchemy as sa
from alembic import op

revision = "9c20a1324c8d"
down_revision = "3fbee3f16914"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("latest_quotes", sa.Column("previous_price", sa.Numeric(28, 12), nullable=True))
    op.add_column(
        "latest_quotes", sa.Column("previous_as_of", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("positions", sa.Column("opening_cost_source", sa.String(64), nullable=True))
    op.add_column(
        "positions", sa.Column("opening_quote_as_of", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("messages", sa.Column("charts", sa.JSON(), server_default="[]", nullable=False))


def downgrade():
    op.drop_column("messages", "charts")
    op.drop_column("positions", "opening_quote_as_of")
    op.drop_column("positions", "opening_cost_source")
    op.drop_column("latest_quotes", "previous_as_of")
    op.drop_column("latest_quotes", "previous_price")
