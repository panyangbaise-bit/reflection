"""Persist first-registration password setup gate without blocking existing accounts."""

import sqlalchemy as sa
from alembic import op

revision = "c804_password_onboarding"
down_revision = "c803_catalog_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "password_setup_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.drop_column("users", "password_setup_required")
